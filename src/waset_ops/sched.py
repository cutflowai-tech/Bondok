"""Scheduling: one implementation for WF1, WF3, Bondok and board edits."""
from __future__ import annotations

import sqlite3

from . import rules
from .core import MONDAY, OWNER, Command, Rejected, fingerprint
from .db import audit, dumps, loads


class SchedMixin:
    # ------------------------------------------------------------------ payload binding
    def payload(self, c, it) -> dict:
        """Exactly what WF2 will send to Instagram for this content revision."""
        m = self.media_row(c, it['verification_id'])
        info = loads(m['metadata'], {}) if m else {}
        body = {'account': rules.ACCOUNT, 'format': it['format'],
                'media_type': 'REELS' if it['format'] == 'Post' else 'STORIES',
                'video_url': info.get('url'), 'video_sha256': info.get('sha256'),
                'verification_id': it['verification_id'], 'content_rev': it['content_rev']}
        if it['format'] == 'Post':
            body.update(caption=it['caption'], share_to_feed=True)
        return body

    def payload_fp(self, c, it) -> str:
        return fingerprint(self.payload(c, it))

    def eligible(self, c, it) -> str | None:
        """Reason the item cannot hold a reservation, or None."""
        if it['publication'] != 'not_started':
            return 'publication state is ' + it['publication']
        if it['owner_state'] != 'active':
            return 'item is ' + it['owner_state']
        if loads(it['hold'], None):
            return (loads(it['hold'], {}) or {}).get('reason') or 'waiting for owner decision'
        if it['readiness'] != 'ready':
            return it['block_reason'] or 'media is not verified yet'
        if it['format'] == 'Post' and it['caption_state'] != 'approved':
            return 'caption is not approved'
        if self.active_attempt(c, it['item_id']):
            return 'a publication attempt is active'
        return None

    def taken(self, c, fmt, exclude=None) -> set[str]:
        return {r['slot'] for r in c.execute('SELECT slot,item_id FROM ops_reservations WHERE account=? AND format=?',
                                             (rules.ACCOUNT, fmt)) if r['item_id'] != exclude}

    def occupied(self, c, fmt, exclude=None):
        out = []
        for r in c.execute('SELECT r.slot, i.* FROM ops_reservations r JOIN ops_items i USING(item_id) '
                           'WHERE r.account=? AND r.format=?', (rules.ACCOUNT, fmt)):
            if r['item_id'] == exclude:
                continue
            try:
                key = rules.rotation_key(r['format'], r['code'], r['name'], r['variety'])
            except rules.RuleError:
                key = None
            out.append((rules.instant(r['slot']), key))
        return out

    def _reserve(self, c, it, at, origin, pinned, actor):
        slot = rules.iso(at)
        try:
            c.execute('INSERT INTO ops_reservations(item_id,account,format,slot,content_rev,payload_fp,origin,'
                      'owner_pinned,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (it['item_id'], rules.ACCOUNT, it['format'], slot, it['content_rev'], self.payload_fp(c, it),
                       origin, 1 if pinned else 0, self.now(), self.now()))
        except sqlite3.IntegrityError:
            raise Rejected('That slot was just taken by another item', 'slot_taken')
        audit(c, it['item_id'], 'reserved', actor, {'slot': slot, 'origin': origin, 'rev': it['content_rev']})
        self.update_item(c, it['item_id'], actor, 'reserved', requested_at=None, requested_by=None)
        return slot

    def try_schedule(self, c, item_id, actor) -> dict:
        """Automatic allocation to a free valid slot. Never displaces anyone."""
        it = self.item(c, item_id)
        res = self.reservation(c, item_id)
        if res:
            if res['content_rev'] != it['content_rev']:
                return self.reauthorize(c, item_id, actor, 'content revision changed')
            return {'scheduled': res['slot']}
        why = self.eligible(c, it)
        if why:
            return {'scheduled': None, 'waiting': why}
        now = self.now_dt()
        if it['requested_at']:
            legacy = it['requested_by'] == 'legacy-board'   # set by earlier schedulers: a preference, not owner intent
            try:
                at = rules.validate_requested_slot(now, it['format'], it['requested_at'])
            except rules.RuleError as e:
                if legacy:
                    audit(c, item_id, 'legacy_request_dropped', actor, {'at': it['requested_at'], 'why': str(e)})
                    self.update_item(c, item_id, actor, 'legacy request invalid', requested_at=None, requested_by=None)
                    return self.try_schedule(c, item_id, actor)
                self.notify(c, f"req-invalid:{item_id}:{it['requested_at']}",
                            f"Requested time {rules.display(rules.instant(it['requested_at']))} for {it['name']} "
                            f"({item_id}) is no longer valid ({e}); it will be scheduled automatically.", item_id)
                self.update_item(c, item_id, actor, 'requested slot invalid', requested_at=None, requested_by=None)
                it = self.item(c, item_id)
            else:
                if rules.iso(at) in self.taken(c, it['format'], item_id) and legacy:
                    audit(c, item_id, 'legacy_request_dropped', actor, {'at': it['requested_at'], 'why': 'occupied'})
                    self.update_item(c, item_id, actor, 'legacy request occupied', requested_at=None, requested_by=None)
                    return self.try_schedule(c, item_id, actor)
                if rules.iso(at) in self.taken(c, it['format'], item_id):
                    self.notify(c, f"req-taken:{item_id}:{rules.iso(at)}",
                                f"{it['name']} ({item_id}) is ready, but its requested time {rules.display(at)} is "
                                'taken by another item. Tell Bondok which time to use.', item_id)
                    return {'scheduled': None, 'waiting': 'requested slot is occupied'}
                slot = self._reserve(c, it, at, 'legacy' if legacy else 'owner', not legacy, actor)
                return {'scheduled': slot, 'origin': 'requested'}
        key = rules.rotation_key(it['format'], it['code'], it['name'], it['variety'])
        at = rules.choose_slot(now, it['format'], key, self.occupied(c, it['format'], item_id),
                               self.taken(c, it['format'], item_id))
        if at is None:
            self.notify(c, f"no-slot:{item_id}", f"No free {it['format']} slot in the next 84 days for "
                        f"{it['name']} ({item_id}).", item_id)
            return {'scheduled': None, 'waiting': 'no free slot in horizon'}
        return {'scheduled': self._reserve(c, it, at, 'auto', False, actor), 'origin': 'auto'}

    def reauthorize(self, c, item_id, actor, why) -> dict:
        """Explicitly re-bind an existing reservation to the current content
        revision, or release it. An obsolete payload never stays authorized."""
        it = self.item(c, item_id)
        res = self.reservation(c, item_id)
        if not res:
            return self.try_schedule(c, item_id, actor)
        if res['content_rev'] == it['content_rev'] and res['payload_fp'] == self.payload_fp(c, it):
            return {'scheduled': res['slot']}
        reason = self.eligible(c, it)
        att = self.active_attempt(c, item_id)
        if reason == 'a publication attempt is active' and att and att['stage'] in ('claimed', 'container_created'):
            reason = None   # the pre-commit attempt is refused at commit (payload mismatch) and re-claimed
        if reason is None and res['format'] == it['format'] and \
                rules.instant(res['slot']) + rules.LATE_WINDOW > self.now_dt():
            c.execute('UPDATE ops_reservations SET content_rev=?, payload_fp=?, updated=? WHERE item_id=?',
                      (it['content_rev'], self.payload_fp(c, it), self.now(), item_id))
            audit(c, item_id, 'reservation_reauthorized', actor, {'slot': res['slot'], 'why': why,
                                                                  'rev': it['content_rev']})
            self.project(c, item_id)
            return {'scheduled': res['slot'], 'reauthorized': True}
        self.release(c, it, 'not reauthorized: ' + (reason or 'slot passed'))
        self.project(c, item_id)
        return {'scheduled': None, 'released': True, 'waiting': reason}

    # ------------------------------------------------------------------ owner/board requests
    def set_notice(self, c, item_id, text):
        it = self.item(c, item_id)
        obs = loads(it['observed'], {}) or {}
        obs['_notice'] = {'text': text, 'at': self.now()}
        c.execute('UPDATE ops_items SET observed=? WHERE item_id=?', (dumps(obs), str(item_id)))

    def op_request_reschedule(self, c, cmd: Command):
        it = self.item(c, cmd.item_id)
        self._guard_mutable(c, it)
        res = self.reservation(c, it['item_id'])
        if res and rules.instant(res['slot']) <= self.now_dt() + rules.NEAR_DUE:
            raise Rejected('The current slot is within 10 minutes of publication; it cannot be moved now', 'near_due')
        at_raw = cmd.args.get('at')
        if at_raw is None and cmd.args.get('incomplete_date'):
            day = cmd.args['incomplete_date']
            self.release(c, it, 'incomplete requested time', keep_request=False)
            self.update_item(c, it['item_id'], cmd.actor, 'incomplete time', requested_at=None, requested_by=None,
                             hold=dumps({'kind': 'incomplete_time', 'date': day, 'origin': 'owner',
                                         'recover': 'a complete time, a cleared Publish at, or a time in Slack',
                                         'reason': f'Publish at has the date {day} but no time. Add the time '
                                                   '(Cairo); it will not be published until then.'}))
            self.notify(c, f"incomplete-time:{it['item_id']}:{day}", f"{it['name']} ({it['item_id']}): Publish at "
                        f"has the date {day} but no time, so it is not scheduled. Add the time on the board or tell "
                        'Bondok the time.', it['item_id'])
            return {'incomplete': True, 'date': day}
        if at_raw is None:
            self.release(c, it, 'unscheduled by request', keep_request=False)
            self.update_item(c, it['item_id'], cmd.actor, 'unscheduled', requested_at=None, requested_by=None,
                             hold=dumps({'kind': 'unscheduled', 'reason': 'Publish time was cleared; tell Bondok '
                                         'when to schedule it'}))
            return {'unscheduled': True}
        try:
            at = rules.validate_requested_slot(self.now_dt(), it['format'], at_raw)
        except (rules.RuleError, ValueError) as e:
            alts = rules.alternatives(self.now_dt(), it['format'], self.taken(c, it['format'], it['item_id']))
            msg = f"{e}. Valid free alternatives: " + ', '.join(rules.display(a) for a in alts)
            raise Rejected('Requested time rejected: ' + msg, 'invalid_slot', alternatives=[rules.iso(a) for a in alts])
        slot = rules.iso(at)
        if res and res['slot'] == slot:
            return {'scheduled': slot, 'unchanged': True}
        if (loads(it['hold'], {}) or {}).get('kind') in ('unscheduled', 'incomplete_time'):
            # A new time answers "tell Bondok when to schedule it" (audit S1/S4).
            it = self.update_item(c, it['item_id'], cmd.actor, 'unscheduled hold cleared', hold=None)
        other = c.execute('SELECT * FROM ops_reservations WHERE account=? AND format=? AND slot=?',
                          (rules.ACCOUNT, it['format'], slot)).fetchone()
        why = self.eligible(c, it)
        if other and other['item_id'] != it['item_id']:
            if why is not None:
                # Moving a scheduled item for one that cannot publish would give its slot away (audit S6).
                alts = rules.alternatives(self.now_dt(), it['format'], self.taken(c, it['format'], it['item_id']))
                raise Rejected(f'That time is reserved by another item and this item is not ready ({why}). Free '
                               'alternatives: ' + ', '.join(rules.display(a) for a in alts), 'slot_taken',
                               alternatives=[rules.iso(a) for a in alts])
            return self._propose_swap(c, cmd, it, dict(other), at)
        if why is not None and not res:
            self.update_item(c, it['item_id'], cmd.actor, 'requested time', requested_at=slot,
                             requested_by='owner' if cmd.actor_kind == OWNER else 'board')
            return {'_state': 'accepted', 'requested': slot,
                    'message': 'Time recorded as requested (not yet reserved): ' + why}
        if res:
            c.execute('DELETE FROM ops_reservations WHERE item_id=?', (it['item_id'],))
        self._reserve(c, self.item(c, it['item_id']), at, 'owner', True, cmd.actor)
        return {'scheduled': slot, 'display': rules.display(at)}

    def _propose_swap(self, c, cmd, it, other, at):
        o = self.item(c, other['item_id'])
        exclude = self.taken(c, it['format']) | {rules.iso(at)}
        new_for_other = None
        for cand in rules.grid(self.now_dt(), it['format'], rules.NEAR_DUE):
            if rules.iso(cand) not in exclude:
                new_for_other = cand
                break
        if new_for_other is None:
            raise Rejected('The requested slot is occupied and no free slot exists to move the other item', 'no_slot')
        if rules.instant(other['slot']) <= self.now_dt() + rules.NEAR_DUE:
            raise Rejected('The requested slot is about to publish another item; choose another time', 'near_due')
        payload = {'item_id': it['item_id'], 'slot': rules.iso(at), 'other_id': o['item_id'],
                   'other_slot': rules.iso(new_for_other)}
        cur = self.reservation(c, it['item_id'])
        summary = (f"Move {it['name']} ({it['item_id']}) "
                   f"{'from ' + rules.display(rules.instant(cur['slot'])) + ' ' if cur else ''}to {rules.display(at)}. "
                   f"That time is reserved by {o['name']} ({o['item_id']}); it would move to "
                   f"{rules.display(new_for_other)}. Nothing changes until approved.")
        p = self.create_proposal(c, 'swap_slot', [it['item_id'], o['item_id']], payload, summary, cmd.actor,
                                 cmd.auth.get('thread'))
        if cmd.actor_kind == MONDAY:
            self.set_notice(c, it['item_id'], 'Requested time is occupied; approval requested in Slack (' + p['proposal_id'] + ')')
            self.project(c, it['item_id'], force_keys=('publish_at', 'post_date', 'post_time', 'action'))
        return {'_state': 'awaiting_approval', **p, 'affected': [
            {'item_id': it['item_id'], 'current': cur['slot'] if cur else None, 'proposed': rules.iso(at)},
            {'item_id': o['item_id'], 'current': other['slot'], 'proposed': rules.iso(new_for_other)}]}

    def execute_swap_slot(self, c, payload, cmd, pid):
        now = self.now_dt()
        it, o = self.item(c, payload['item_id']), self.item(c, payload['other_id'])
        for x in (it, o):
            why = self.eligible(c, x)
            if why:
                raise Rejected(f"{x['name']} is no longer eligible ({why}); proposal not executed", 'stale')
        for slot in (payload['slot'], payload['other_slot']):
            try:
                rules.validate_requested_slot(now, it['format'], slot)
            except rules.RuleError as e:
                raise Rejected(f'A proposed time is no longer valid ({e}); proposal not executed', 'stale')
        for r in (self.reservation(c, it['item_id']), self.reservation(c, o['item_id'])):
            if r and rules.instant(r['slot']) <= now + rules.NEAR_DUE:
                raise Rejected('A current slot is within 10 minutes of publication; proposal not executed', 'near_due')
        taken = self.taken(c, it['format'], None) - {r['slot'] for r in (self.reservation(c, it['item_id']),
                                                                       self.reservation(c, o['item_id'])) if r}
        if payload['other_slot'] in taken or payload['slot'] in taken:
            raise Rejected('A proposed slot was taken in the meantime; proposal not executed', 'stale')
        c.execute('DELETE FROM ops_reservations WHERE item_id IN (?,?)', (it['item_id'], o['item_id']))
        self._reserve(c, self.item(c, o['item_id']), rules.instant(payload['other_slot']), 'owner', True, cmd.actor)
        self._reserve(c, self.item(c, it['item_id']), rules.instant(payload['slot']), 'owner', True, cmd.actor)
        return {'moved': [{'item_id': it['item_id'], 'slot': payload['slot']},
                          {'item_id': o['item_id'], 'slot': payload['other_slot']}]}

    def op_request_publish(self, c, cmd: Command):
        """Owner asks to get an item out: earliest valid free slot through the
        normal publisher. Never a direct publish and never off-grid."""
        it = self.item(c, cmd.item_id)
        res = self.reservation(c, it['item_id'])
        if res:
            return {'scheduled': res['slot'], 'display': rules.display(rules.instant(res['slot'])),
                    'message': 'Already scheduled; the publisher will post it at that time'}
        why = self.eligible(c, it)
        if why:
            raise Rejected('Cannot schedule yet: ' + why, 'not_eligible')
        for at in rules.grid(self.now_dt(), it['format'], rules.NEAR_DUE):
            if rules.iso(at) not in self.taken(c, it['format']):
                self._reserve(c, it, at, 'owner', True, cmd.actor)
                return {'scheduled': rules.iso(at), 'display': rules.display(at)}
        raise Rejected('No free slot in the scheduling horizon', 'no_slot')
