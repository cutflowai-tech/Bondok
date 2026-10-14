"""Owner administration of the board and the files (agent migration P2-04/P2-05): create or remove social items,
edit owner data columns, rename/move/delete Dropbox files. The handler records the decision and queues the external
change in the outbox (kinds ``monday_admin`` and ``dropbox_admin``); the runtime applies it and acknowledges, exactly
like display writes. A removal never strands a publication: it is refused while an attempt is past the point of
sending, and a removed item is skipped (reservation released) before the board changes.
"""
from __future__ import annotations

from . import board, rules
from .core import MONDAY, Command, Rejected
from .db import audit, dumps, next_counter

FIELDS = ('notes', 'code', 'variety', 'collab')
REMOVED = 'Removed from the board by the owner'


class AdminMixin:
    def op_board_create(self, c, cmd: Command):
        a = cmd.args
        fmt = a.get('format')
        if fmt not in rules.FORMATS:
            raise Rejected('Format must be Post or Story', 'invalid')
        name = str(a.get('name') or '').strip()
        if not name:
            raise Rejected('A new item needs a name', 'invalid')
        code = str(a.get('code') or '').strip()
        cols = {board.COL['format']: {'label': fmt}, board.COL['status']: {'label': board.LABELS['checking']},
                board.COL['topaz']: {'label': 'Not yet'}}
        if code:
            try:
                cols[board.COL['style']] = rules.style(code)
            except rules.RuleError as e:
                raise Rejected(str(e), 'invalid_code')
            cols[board.COL['code']] = code
        if a.get('dropbox_url'):
            cols[board.COL['dropbox']] = {'url': a['dropbox_url'], 'text': 'Source'}
        if a.get('caption') and fmt == 'Post':
            cols[board.COL['caption']] = {'text': a['caption']}
        n = next_counter(c, 'admin:create')
        key = f'admin-create:{n}'
        self.enqueue(c, 'monday_admin', key, {'action': 'create', 'name': name, 'group': board.GROUPS[fmt],
                                              'columns': cols, 'command': cmd.id})
        audit(c, None, 'board_create_requested', cmd.actor, {'name': name, 'format': fmt, 'key': key})
        return {'_state': 'accepted', 'job': key,
                'message': f'{fmt} "{name}" will be created on the board within a minute; it is checked and prepared '
                           'like every item once it appears.'}

    def op_board_remove(self, c, cmd: Command):
        it = self.item(c, cmd.item_id)
        mode = 'archive'                 # recoverable unless the owner said permanently (or changed the rule)
        if cmd.args.get('mode') == 'delete' and (cmd.args.get('permanent') or
                                                 not self.policy(c)['delete_means_archive']):
            mode = 'delete'
        att = self.active_attempt(c, it['item_id'])
        if att and att['stage'] in ('committed', 'outcome_unknown'):
            raise Rejected('A publication of this item may already be in progress or its outcome is not known yet; '
                           'it is removed once that is settled', 'in_progress')
        if att:
            c.execute("UPDATE ops_attempts SET stage='abandoned', evidence=?, updated=? WHERE id=?",
                      (dumps({'abandoned': 'item removed by the owner'}), self.now(), att['id']))
        self.release(c, it, 'removed by the owner', keep_request=False)
        if it['publication'] != 'published':
            self.update_item(c, it['item_id'], cmd.actor, 'removed by the owner', owner_state='skipped',
                             owner_state_reason=REMOVED)
        self.enqueue(c, 'monday_admin', f"admin-remove:{it['item_id']}:{mode}:{it['version']}",
                     {'action': mode, 'item_id': it['item_id'], 'command': cmd.id}, it['item_id'])
        audit(c, it['item_id'], 'board_remove_requested', cmd.actor, {'mode': mode})
        return {'_state': 'accepted', 'mode': mode,
                'message': f"{it['name']} will be {'archived (recoverable from the board archive)' if mode == 'archive' else 'deleted from the board'}"
                           '; it will not be published.'}

    def op_board_set_field(self, c, cmd: Command):
        """Owner data columns (Notes, Code, Story variety, IG Colab) changed from Slack: stored, applied by the same
        command as a board edit, and written to the board."""
        field = cmd.args.get('field')
        if field not in FIELDS:
            raise Rejected('Bondok can set: ' + ', '.join(FIELDS) + ' (caption, format, time and file have their own '
                           'tools)', 'invalid')
        it = self.item(c, cmd.item_id)
        value = str(cmd.args.get('value') or '').strip()
        sub = Command(cmd.id + ':' + field, 'set_' + field, cmd.actor, cmd.actor_kind, it['item_id'], {'value': value},
                      auth=cmd.auth)
        self.authorize(c, sub)
        out = getattr(self, 'op_set_' + field)(c, sub) or {}
        if cmd.actor_kind != MONDAY:
            self.write_human(c, self.item(c, it['item_id']), {field: value or None})
        return {**out, 'field': field, 'value': value}

    def op_file_action(self, c, cmd: Command):
        a = cmd.args
        action = a.get('action')
        if action not in ('rename', 'move', 'delete'):
            raise Rejected('File actions: rename, move, delete', 'invalid')
        path = str(a.get('path') or '').strip()
        if not path.startswith('/'):
            raise Rejected('A Dropbox path (starting with /) is required', 'invalid')
        to = str(a.get('to') or '').strip()
        if action in ('rename', 'move') and not to:
            raise Rejected('Where should it go? A new name or folder is required', 'invalid')
        if action == 'rename' and '/' not in to:
            to = path.rsplit('/', 1)[0] + '/' + to
        if cmd.item_id:
            it = self.item(c, cmd.item_id)
            att = self.active_attempt(c, it['item_id'])
            if att and action in ('move', 'delete'):
                raise Rejected('This item has a publication attempt in progress; change its files after it ends',
                               'in_progress')
        key = f'admin-file:{next_counter(c, "admin:file")}'
        self.enqueue(c, 'dropbox_admin', key, {'action': action, 'path': path, 'to': to or None,
                                               'command': cmd.id}, cmd.item_id)
        audit(c, cmd.item_id, 'file_action_requested', cmd.actor, {'action': action, 'path': path, 'to': to})
        what = {'rename': f'renamed to {to.rsplit("/", 1)[-1]}', 'move': f'moved to {to}',
                'delete': 'moved to Dropbox\'s deleted files (recoverable there)'}[action]
        return {'_state': 'accepted', 'job': key, 'message': f'{path} will be {what}.'}
