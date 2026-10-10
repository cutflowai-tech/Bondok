"""Caption drafts: generation (WF1 model call) and approval are separate.

A draft is stored by its input hash, so retries never regenerate it. Drafts
never reach the board or a payload until the owner approves the exact text.
"""
from __future__ import annotations

import hashlib

from . import rules
from .core import Command, Rejected
from .db import dumps, loads

CAPTION_POLICY = 1


def caption_input_hash(item_id, title, brief) -> str:
    return hashlib.sha256(dumps({'item': str(item_id), 'title': title or '', 'brief': brief or '',
                                 'policy': CAPTION_POLICY}).encode()).hexdigest()


class CaptionMixin:
    def caption_needed(self, item_id, title, brief) -> dict:
        h = caption_input_hash(item_id, title, brief)
        with self.store.read() as c:
            it = self.item(c, item_id)
            if it['format'] != 'Post' or it['caption_state'] in ('approved', 'pending_approval', 'legacy_unapproved'):
                return {'needed': False, 'input_hash': h, 'reason': 'caption_state=' + str(it['caption_state'])}
            if not (brief or '').strip():
                return {'needed': False, 'input_hash': h, 'reason': 'no client brief'}
            prior = c.execute('SELECT state FROM ops_caption_drafts WHERE input_hash=?', (h,)).fetchone()
            if prior:
                return {'needed': False, 'input_hash': h, 'reason': 'draft already ' + prior['state']}
        return {'needed': True, 'input_hash': h}

    def op_caption_draft(self, c, cmd: Command):
        a = cmd.args
        it = self.item(c, cmd.item_id)
        prior = c.execute('SELECT * FROM ops_caption_drafts WHERE input_hash=?', (a['input_hash'],)).fetchone()
        if prior:
            return {'draft_state': prior['state'], 'duplicate_draft': True}
        text = (a.get('text') or '').strip()
        now = self.now()
        if a.get('error') or not text:
            c.execute('INSERT INTO ops_caption_drafts VALUES(?,?,?,?,?,?,?,?,?)',
                      (a['input_hash'], it['item_id'], None, a.get('model'), 'invalid',
                       (a.get('error') or 'empty model output')[:300], None, now, now))
            self.notify(c, f"draft-failed:{a['input_hash'][:12]}", f"Caption draft for {it['name']} ({it['item_id']}) "
                        'could not be generated. Media preparation continues; write the caption on the board or ask '
                        'Bondok for a new draft.', it['item_id'])
            return {'draft_state': 'invalid'}
        bad = rules.validate_draft_caption(text)
        if bad:
            c.execute('INSERT INTO ops_caption_drafts VALUES(?,?,?,?,?,?,?,?,?)',
                      (a['input_hash'], it['item_id'], text, a.get('model'), 'invalid', bad, None, now, now))
            self.notify(c, f"draft-invalid:{a['input_hash'][:12]}", f"Caption draft for {it['name']} "
                        f"({it['item_id']}) failed validation ({bad}); it was not used.", it['item_id'])
            return {'draft_state': 'invalid', 'reason': bad}
        c.execute("UPDATE ops_caption_drafts SET state='superseded', updated=? WHERE item_id=? AND state='pending_approval'",
                  (now, it['item_id']))
        p = self.create_proposal(c, 'approve_caption', [it['item_id']],
                                 {'item_id': it['item_id'], 'input_hash': a['input_hash'], 'text': text,
                                  'base_caption': it['caption'], 'base_state': it['caption_state']},
                                 f"Caption draft for {it['name']} ({it['item_id']}):\n\n{text}", cmd.actor)
        c.execute('INSERT INTO ops_caption_drafts VALUES(?,?,?,?,?,?,?,?,?)',
                  (a['input_hash'], it['item_id'], text, a.get('model'), 'pending_approval', None,
                   p['proposal_id'], now, now))
        if it['caption_state'] != 'approved':
            # An approved caption stays approved (and scheduled) while an alternative draft waits (audit S9).
            self.update_item(c, it['item_id'], cmd.actor, 'caption draft pending', caption_state='pending_approval')
        return {'draft_state': 'pending_approval', **p}

    def execute_approve_caption(self, c, payload, cmd, pid):
        d = c.execute('SELECT * FROM ops_caption_drafts WHERE input_hash=?', (payload['input_hash'],)).fetchone()
        it = self.item(c, payload['item_id'])
        if not d or d['state'] != 'pending_approval' or d['text'] != payload['text']:
            raise Rejected('Draft was superseded; nothing approved', 'stale')
        if 'base_caption' in payload:
            changed = it['caption'] != payload['base_caption'] or \
                it['caption_state'] not in ('pending_approval', payload.get('base_state'))
        else:
            changed = it['caption_state'] != 'pending_approval'
        if changed or it['format'] != 'Post':
            # The caption or format changed since the draft (board edit, Bondok, format change).
            raise Rejected('The caption or format changed after this draft; nothing approved', 'stale')
        self._guard_mutable(c, it, allow_paused=True)
        c.execute("UPDATE ops_caption_drafts SET state='approved', updated=? WHERE input_hash=?",
                  (self.now(), payload['input_hash']))
        c.execute('UPDATE ops_items SET content_rev=content_rev+1 WHERE item_id=?', (it['item_id'],))
        it = self.update_item(c, it['item_id'], cmd.actor, 'caption draft approved', caption=payload['text'],
                              caption_state='approved', caption_origin='approved_draft')
        self.write_human(c, it, {'caption': payload['text']})
        return {'caption_approved': True, **self.reauthorize(c, it['item_id'], cmd.actor, 'caption approved')}

    def op_approve_caption_draft(self, c, cmd: Command):
        """Owner approves the item's current caption draft directly (no proposal id to copy). The draft does not
        expire with its interaction; it is bound to its exact text and the caption it was drafted against (R5 A8)."""
        it = self.item(c, cmd.item_id)
        d = c.execute("SELECT * FROM ops_caption_drafts WHERE item_id=? AND state='pending_approval' "
                      'ORDER BY created DESC LIMIT 1', (it['item_id'],)).fetchone()
        if not d:
            raise Rejected('There is no caption draft waiting for approval for this item', 'no_draft')
        shown = cmd.args.get('text')
        if shown is not None and ' '.join(str(shown).split()) != ' '.join((d['text'] or '').split()):
            raise Rejected('The draft changed since it was shown; nothing was approved', 'stale')
        p = c.execute('SELECT * FROM ops_proposals WHERE id=?', (d['proposal_id'],)).fetchone() if d['proposal_id'] else None
        payload = loads(p['payload'], {}) if p else {'item_id': it['item_id'], 'input_hash': d['input_hash'],
                                                     'text': d['text']}
        result = self.execute_approve_caption(c, payload, cmd, d['proposal_id'])
        if p and p['state'] in ('pending', 'expired', 'stale'):
            c.execute("UPDATE ops_proposals SET state='executed', decided_by=?, result=?, updated=? WHERE id=?",
                      (cmd.actor, dumps(result), self.now(), p['id']))
        return {'approved_draft': d['input_hash'][:12], **result}

    def reject_approve_caption(self, c, payload, cmd):
        c.execute("UPDATE ops_caption_drafts SET state='rejected', updated=? WHERE input_hash=?",
                  (self.now(), payload['input_hash']))
        it = self.item(c, payload['item_id'])
        if it['caption_state'] == 'pending_approval':
            # Back to what the item had before the draft: board text stays unapproved text, not "missing".
            self.update_item(c, it['item_id'], cmd.actor, 'caption draft rejected',
                             caption_state=payload.get('base_state') if payload.get('base_state') not in
                             (None, 'pending_approval') else ('legacy_unapproved' if it['caption'] else 'missing'))
