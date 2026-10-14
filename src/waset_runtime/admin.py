"""Applies the owner's board and file administration queued by the handler (outbox kinds monday_admin and
dropbox_admin; waset_ops/admin.py). Runs in the Bondok service, so it works before and after the n8n cutover (n8n
never takes these kinds)."""
from __future__ import annotations

from .dropbox import DropboxError
from .http import HttpError
from .monday import MondayError

from waset_ops.rules import BOARD

KINDS = ['monday_admin', 'dropbox_admin']


def run_admin_jobs(ops, monday, dropbox, worker='bondok-admin', limit=5) -> list[dict]:
    out = []
    for j in ops.outbox_take(KINDS, worker, limit, lease=300):
        p = j['payload']
        act = p.get('action')
        try:
            if j['kind'] == 'monday_admin':
                if monday is None:
                    raise MondayError('Monday is not configured for Bondok', answered=True)
                if act == 'create':
                    try:
                        iid = monday.create_item(p['name'], p.get('columns') or {}, p.get('group'))
                    except MondayError as e:
                        if e.answered:
                            raise
                        # No answer: it may exist. Never created twice; the owner is told to check.
                        with ops.store.tx() as c:
                            ops.notify(c, f"admin-uncertain:{j['id']}", f"⚠️ Creating \"{p['name']}\" on the board got "
                                       'no answer from Monday; it may or may not exist. Check the board before asking '
                                       'again.')
                        ops.outbox_ack(j['id'], worker, True, None, {'uncertain': True})
                        out.append({'job': j['id'], 'uncertain': True})
                        continue
                    result = {'item_id': iid}
                elif act in ('archive', 'delete') and monday.board_of(p['item_id']) != BOARD:
                    raise ValueError('the item is not on the For Social Media board; nothing was changed')
                elif act == 'archive':
                    monday.archive_item(p['item_id'])
                    result = {'archived': p['item_id']}
                elif act == 'delete':
                    monday.delete_item(p['item_id'])
                    result = {'deleted': p['item_id']}
                else:
                    raise ValueError('unknown board action ' + str(act))
            else:
                if dropbox is None:
                    raise DropboxError('Dropbox is not configured for Bondok', kind='config')
                meta = dropbox.metadata(p['path'])
                if meta.get('.tag') != 'file':
                    raise ValueError(f"{p['path']} is not a file; only files are changed")
                if act in ('rename', 'move'):
                    m = dropbox.move(p['path'], p['to'])
                    result = {'path': ((m or {}).get('metadata') or {}).get('path_display') or p['to']}
                elif act == 'delete':
                    dropbox.delete(p['path'])
                    result = {'deleted': p['path']}
                else:
                    raise ValueError('unknown file action ' + str(act))
            ops.outbox_ack(j['id'], worker, True, None, result)
            with ops.store.tx() as c:
                ops.notify(c, f"admin-done:{j['id']}", '✅ ' + _done_text(j['kind'], act, p, result), j.get('item_id'))
            out.append({'job': j['id'], **result})
        except Exception as e:  # noqa: BLE001 - one job's failure is recorded and never stops the others
            permanent = isinstance(e, ValueError) or getattr(e, 'kind', None) == 'config' or (
                isinstance(e, MondayError) and e.answered and not e.status)
            ops.outbox_ack(j['id'], worker, False, str(e)[:400])
            if permanent:
                with ops.store.tx() as c:
                    ops.notify(c, f"admin-failed:{j['id']}", f'⛔ Not done ({act}): {str(e)[:300]}', j.get('item_id'))
            out.append({'job': j['id'], 'error': str(e)[:200]})
    return out


def _done_text(kind, act, p, result):
    if kind == 'monday_admin':
        return {'create': f"Created \"{p.get('name')}\" on the board (item {result.get('item_id')}).",
                'archive': f"Archived item {p.get('item_id')} on the board.",
                'delete': f"Deleted item {p.get('item_id')} from the board."}[act]
    return {'rename': f"Renamed {p['path']} → {result.get('path')}.", 'move': f"Moved {p['path']} → {result.get('path')}.",
            'delete': f"Moved {p['path']} to Dropbox's deleted files."}[act]
