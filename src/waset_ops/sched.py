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

    # ------------------------------------------------------------------ cross-item duplicate guard (R5 B1)
    def content_identities(self, c, it) -> set[str]:
        """Publication identities of an item's content: account + format + source bytes (Dropbox content hash)
        and account + format + delivered video sha256. Story and Post of one video are different publications."""
        fmt, out = it.get('format'), set()
        if fmt not in rules.FORMATS:
            return out
        if it.get('content_hash'):
            out.add(f"{rules.ACCOUNT}|{fmt}|src:{it['content_hash']}")
        m = self.media_row(c, it.get('verification_id'))
        sha = (loads(m['metadata'], {}) or {}).get('sha256') if m else None
        if sha:
            out.add(f'{rules.ACCOUNT}|{fmt}|{sha}')
        return out

    def duplicate_of(self, c, it) -> dict | None:
        """Another item that already uses this content: published, owner-reported, in flight, or reserved earlier.
        Includes the local publication journal (legacy `assets`), so a deleted or archived original still counts."""
        ids = self.content_identities(c, it)
        if not ids:
            return None
        marks = ','.join('?' * len(ids))
        for j in c.execute(f'SELECT DISTINCT item FROM assets WHERE identity IN ({marks}) AND item!=?',
                           (*ids, it['item_id'])).fetchall():
            # A journal row blocks while its item is (or may be) published: v1-only items, legacy Posted,
            # owner-reported, committed/unknown/published attempts. A definitively failed attempt does not.
            o = self.item(c, j['item'], required=False)
            if o is None or o['legacy_posted'] or o['publication'] in ('published', 'outcome_unknown', 'in_progress') \
                    or c.execute("SELECT 1 FROM ops_attempts WHERE item_id=? AND stage IN "
                                 "('committed','outcome_unknown','published')", (j['item'],)).fetchone():
                return {'item_id': j['item'], 'name': o['name'] if o else None,
                        'state': (o['publication'].replace('_', ' ') if o else 'published (earlier system)')}
        mine = self.reservation(c, it['item_id'])
        for o in c.execute("SELECT * FROM ops_items WHERE item_id!=? AND format=? AND content_hash IS NOT NULL",
                           (it['item_id'], it['format'])).fetchall():
            o = dict(o)
            if not (self.content_identities(c, o) & ids):
                continue
            if o['publication'] in ('published', 'outcome_unknown', 'in_progress') or self.active_attempt(c, o['item_id']):
                return {'item_id': o['item_id'], 'name': o['name'], 'state': o['publication'].replace('_', ' ')}
            res = self.reservation(c, o['item_id'])
            if res and o['owner_state'] == 'active' and (not mine or (res['slot'], o['item_id']) < (mine['slot'], it['item_id'])):
                return {'item_id': o['item_id'], 'name': o['name'], 'state': 'scheduled ' + rules.display(rules.instant(res['slot']))}
        return None

    @staticmethod
    def duplicate_reason(dup) -> str:
        who = f"{dup.get('name') or 'item'} ({dup['item_id']})"
        return (f'Same video as {who}, which is {dup["state"]}; publishing the same video twice is blocked. '
                'Select a different file for one of them or skip one.')

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
        dup = self.duplicate_of(c, it)
        if dup:
            return self.duplicate_reason(dup)
        return None

    # ------------------------------------------------------------------ slots, owner requests and windows
    def reserved_slots(self, c, fmt, exclude=None) -> set[str]:
        return {r['slot'] for r in c.execute('SELECT slot,item_id FROM ops_reservations WHERE account=? AND format=?',
                                             (rules.ACCOUNT, fmt)) if r['item_id'] != exclude}

    @staticmethod
    def owner_request(it) -> bool:
        """An owner/board time request (legacy preferences restored from earlier systems are not)."""
        return bool(it.get('requested_at')) and it.get('requested_by') != 'legacy-board'

    def requested_slots(self, c, fmt, exclude=None) -> set[str]:
        """Future owner-requested times of active items still preparing: protected from automatic allocation
        (R5 A5). Existing reservations still follow option B."""
        now = rules.iso(self.now_dt())
        return {r['requested_at'] for r in c.execute(
            "SELECT item_id, requested_at FROM ops_items WHERE format=? AND requested_at IS NOT NULL AND "
            "COALESCE(requested_by,'owner')!='legacy-board' AND owner_state='active' AND publication='not_started' "
            'AND item_id NOT IN (SELECT item_id FROM ops_reservations)', (fmt,))
            if r['item_id'] != exclude and r['requested_at'] > now}

    def taken(self, c, fmt, exclude=None) -> set[str]:
        """Slots automatic allocation must not use."""
        return self.reserved_slots(c, fmt, exclude) | self.requested_slots(c, fmt, exclude)

    def next_reserved_after(self, c, fmt, slot, exclude=None):
        r = c.execute("SELECT MIN(r.slot) FROM ops_reservations r JOIN ops_items i USING(item_id) WHERE r.account=? "
                      "AND r.format=? AND r.slot>? AND r.item_id!=? AND i.publication='not_started'",
                      (rules.ACCOUNT, fmt, slot, str(exclude))).fetchone()
        return r[0] if r else None

    def deadline(self, c, res):
        """Latest publication moment for a reservation (R5 M29)."""
        return rules.late_deadline(res['slot'], self.next_reserved_after(c, res['format'], res['slot'], res['item_id']))

    def window(self, it) -> dict | None:
        return (loads(it.get('observed'), {}) or {}).get('_window') or None

    def _store_window(self, c, item_id, window, actor):
        obs = loads(self.item(c, item_id)['observed'], {}) or {}
        if window:
            obs['_window'] = {k: v for k, v in window.items() if k in ('not_before', 'on_date') and v}
            obs['_window'].update(by=actor, at=self.now())
        else:
            obs.pop('_window', None)
        c.execute('UPDATE ops_items SET observed=? WHERE item_id=?', (dumps(obs), str(item_id)))

    @staticmethod
    def parse_window(args) -> dict | None:
        w = {}
        if args.get('not_before'):
            try:
                w['not_before'] = rules.iso(rules.instant(args['not_before']))
            except (ValueError, TypeError, rules.RuleError):
                raise Rejected('The "not before" time could not be read', 'invalid_window')
        if args.get('on_date'):
            d = str(args['on_date']).strip()
            try:
                __import__('datetime').date.fromisoformat(d)
            except ValueError:
                raise Rejected('The day could not be read (expected YYYY-MM-DD, Cairo)', 'invalid_window')
            w['on_date'] = d
        return w or None

    @staticmethod
    def window_text(w) -> str:
        parts = []
        if w and w.get('on_date'):
            parts.append('on ' + w['on_date'])
        if w and w.get('not_before'):
            parts.append('not before ' + rules.display(rules.instant(w['not_before'])))
        return ' and '.join(parts)

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

    # ------------------------------------------------------------------ allocation
    def try_schedule(self, c, item_id, actor) -> dict:
        """Reserve the owner's requested time, else an automatic free grid slot inside the owner's window.
        Never displaces anyone (option B)."""
        it = self.item(c, item_id)
        res = self.reservation(c, item_id)
        if res:
            if res['content_rev'] != it['content_rev']:
                return self.reauthorize(c, item_id, actor, 'content revision changed')
            return {'scheduled': res['slot']}
        if self.owner_request(it) and it['requested_at'] <= rules.iso(self.now_dt()) and \
                it['publication'] == 'not_started' and it['owner_state'] == 'active' and \
                (loads(it['hold'], {}) or {}).get('kind') in (None, *self.TIME_HOLDS):
            return self._missed_request(c, it, actor, 'it was not ready to publish at that time')
        why = self.eligible(c, it)
        if why:
            return {'scheduled': None, 'waiting': why}
        now = self.now_dt()
        if it['requested_at']:
            legacy = not self.owner_request(it)
            try:
                at = (rules.validate_requested_slot if legacy else rules.validate_owner_time)(
                    now, it['format'], it['requested_at'])
            except rules.RuleError as e:
                if legacy:
                    audit(c, item_id, 'legacy_request_dropped', actor, {'at': it['requested_at'], 'why': str(e)})
                    self.update_item(c, item_id, actor, 'legacy request invalid', requested_at=None, requested_by=None)
                    return self.try_schedule(c, item_id, actor)
                return self._missed_request(c, it, actor, str(e))
            if rules.iso(at) in self.reserved_slots(c, it['format'], item_id):
                if legacy:
                    audit(c, item_id, 'legacy_request_dropped', actor, {'at': it['requested_at'], 'why': 'occupied'})
                    self.update_item(c, item_id, actor, 'legacy request occupied', requested_at=None, requested_by=None)
                    return self.try_schedule(c, item_id, actor)
                return self._requested_slot_occupied(c, it, at, actor)
            slot = self._reserve(c, it, at, 'legacy' if legacy else 'owner', not legacy, actor)
            return {'scheduled': slot, 'origin': 'requested'}
        win = self.window(it)
        key = rules.rotation_key(it['format'], it['code'], it['name'], it['variety'])
        at = rules.choose_slot(now, it['format'], key, self.occupied(c, it['format'], item_id),
                               self.taken(c, it['format'], item_id), window=win)
        if at is None:
            where = (' ' + self.window_text(win)) if win else ' in the next 84 days'
            self.notify(c, f"no-slot:{item_id}:{self.window_text(win)}", f"No free {it['format']} slot{where} for "
                        f"{it['name']} ({item_id}). Tell Bondok another day or time.", item_id)
            return {'scheduled': None, 'waiting': 'no free slot' + where}
        return {'scheduled': self._reserve(c, it, at, 'auto', False, actor), 'origin': 'auto'}

    def _missed_request(self, c, it, actor, why) -> dict:
        """An owner time that passed (or can no longer be honoured) is closed with its cause and one question;
        it never stays 'Scheduled' in the past or silently becomes another time (R5 A1, A5)."""
        past = it['requested_at']
        obs = loads(it['observed'], {}) or {}
        obs['_missed'] = (obs.get('_missed') or [])[-4:] + [{'at': past, 'why': why[:200], 'closed': self.now()}]
        c.execute('UPDATE ops_items SET observed=? WHERE item_id=?', (dumps(obs), it['item_id']))
        when = rules.display(rules.instant(past))
        reason = (f'The requested time {when} passed without publication ({why}). Choose a new time, or tell Bondok '
                  'to publish it at the next free time.')
        self.update_item(c, it['item_id'], actor, 'requested time missed', requested_at=None, requested_by=None,
                         hold=dumps({'kind': 'missed_time', 'time': past, 'origin': 'system', 'party': 'owner',
                                     'recover': 'a new time, "publish it", or resume', 'reason': reason}))
        self.notify(c, f"missed:{it['item_id']}:{past}", f"{it['name']} ({it['item_id']}): {reason}", it['item_id'])
        return {'scheduled': None, 'waiting': 'requested time passed', 'missed': past}

    def _requested_slot_occupied(self, c, it, at, actor) -> dict:
        """The owner's requested time is held by another reservation that existed first: one concrete proposal
        (option B), offered once; nothing moves before approval."""
        other = c.execute('SELECT * FROM ops_reservations WHERE account=? AND format=? AND slot=?',
                          (rules.ACCOUNT, it['format'], rules.iso(at))).fetchone()
        open_p = c.execute("SELECT id FROM ops_proposals WHERE kind='swap_slot' AND state='pending' AND "
                           "json_extract(payload,'$.item_id')=? AND json_extract(payload,'$.slot')=?",
                           (it['item_id'], rules.iso(at))).fetchone()
        if other and not open_p:
            try:
                self._propose_swap(c, Command('sched:' + it['item_id'], 'propose', actor, 'service', it['item_id']),
                                   it, dict(other), at)
            except Rejected as e:
                self.notify(c, f"req-taken:{it['item_id']}:{rules.iso(at)}",
                            f"{it['name']} ({it['item_id']}) is ready, but its requested time {rules.display(at)} is "
                            f'reserved by another item ({e.reason}). Tell Bondok which time to use.', it['item_id'])
        return {'scheduled': None, 'waiting': 'requested time is reserved by another item; proposal sent'}

    def reauthorize(self, c, item_id, actor, why) -> dict:
        """Explicitly re-bind an existing reservation to the current content revision, or release it. An obsolete
        payload never stays authorized; a released item is scheduled again (R5 A17)."""
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
        if reason is None and res['format'] == it['format'] and self.deadline(c, res) > self.now_dt():
            c.execute('UPDATE ops_reservations SET content_rev=?, payload_fp=?, updated=? WHERE item_id=?',
                      (it['content_rev'], self.payload_fp(c, it), self.now(), item_id))
            audit(c, item_id, 'reservation_reauthorized', actor, {'slot': res['slot'], 'why': why,
                                                                  'rev': it['content_rev']})
            self.project(c, item_id)
            return {'scheduled': res['slot'], 'reauthorized': True}
        self.release(c, it, 'not reauthorized: ' + (reason or 'slot passed'))
        self.project(c, item_id)
        if reason is None:
            return {'released': True, **self.try_schedule(c, item_id, actor)}
        return {'scheduled': None, 'released': True, 'waiting': reason}

    # ------------------------------------------------------------------ owner/board requests
    def set_notice(self, c, item_id, text):
        it = self.item(c, item_id)
        obs = loads(it['observed'], {}) or {}
        obs['_notice'] = {'text': text, 'at': self.now()}
        c.execute('UPDATE ops_items SET observed=? WHERE item_id=?', (dumps(obs), str(item_id)))

    TIME_HOLDS = ('unscheduled', 'incomplete_time', 'missed_time')

    def _clear_time_hold(self, c, it, actor):
        if (loads(it['hold'], {}) or {}).get('kind') in self.TIME_HOLDS:
            return self.update_item(c, it['item_id'], actor, 'time hold answered', hold=None)
        return it

    def op_request_reschedule(self, c, cmd: Command):
        it = self.item(c, cmd.item_id)
        self._guard_mutable(c, it, allow_paused=True)     # a paused item keeps the owner's time as its request
        res = self.reservation(c, it['item_id'])
        att = self.active_attempt(c, it['item_id'])
        at_raw = cmd.args.get('at')
        if at_raw is None and cmd.args.get('incomplete_date'):
            day = cmd.args['incomplete_date']
            self.release(c, it, 'incomplete requested time', keep_request=False)
            self.update_item(c, it['item_id'], cmd.actor, 'incomplete time', requested_at=None, requested_by=None,
                             hold=dumps({'kind': 'incomplete_time', 'date': day, 'origin': 'owner', 'party': 'owner',
                                         'recover': 'a complete time, a cleared Publish at, or a time in Slack',
                                         'reason': f'Publish at has the date {day} but no time. Add the time '
                                                   '(Cairo); it will not be published until then.'}))
            self.notify(c, f"incomplete-time:{it['item_id']}:{day}", f"{it['name']} ({it['item_id']}): Publish at "
                        f"has the date {day} but no time, so it is not scheduled. Add the time on the board or tell "
                        'Bondok the time.', it['item_id'])
            return {'incomplete': True, 'date': day}
        if at_raw is None:
            # Clearing the time cancels the owner's request; it is not recreated automatically (R5 A16).
            self.release(c, it, 'unscheduled by request', keep_request=False)
            self._store_window(c, it['item_id'], None, cmd.actor)
            self.update_item(c, it['item_id'], cmd.actor, 'unscheduled', requested_at=None, requested_by=None,
                             hold=dumps({'kind': 'unscheduled', 'origin': 'owner', 'party': 'owner',
                                         'recover': 'a new time, "publish it", or resume',
                                         'reason': 'Publish time was cleared; set a new time or tell Bondok to '
                                                   'publish it'}))
            self.notify(c, f"unscheduled:{it['item_id']}:{it['version']}", f"{it['name']} ({it['item_id']}) is not "
                        'scheduled because its Publish at was cleared. Set a new time on the board, or tell Bondok to '
                        'publish it.', it['item_id'])
            return {'unscheduled': True,
                    **({'publication': 'stopped_before_commit'} if att else {})}
        owner = cmd.actor_kind in (OWNER, MONDAY)
        try:
            at = (rules.validate_owner_time if owner else rules.validate_requested_slot)(self.now_dt(), it['format'],
                                                                                      at_raw)
        except (rules.RuleError, ValueError) as e:
            alts = rules.alternatives(self.now_dt(), it['format'], self.taken(c, it['format'], it['item_id']))
            msg = f"{e}. Free times on the usual schedule: " + ', '.join(rules.display(a) for a in alts)
            raise Rejected('Requested time rejected: ' + msg, 'invalid_slot', alternatives=[rules.iso(a) for a in alts])
        slot = rules.iso(at)
        if res and res['slot'] == slot:
            self._clear_time_hold(c, it, cmd.actor)
            return {'scheduled': slot, 'unchanged': True}
        it = self._clear_time_hold(c, it, cmd.actor)    # a new time answers "when should it be published?"
        self._store_window(c, it['item_id'], None, cmd.actor)   # an exact time supersedes an earlier window
        other = c.execute('SELECT * FROM ops_reservations WHERE account=? AND format=? AND slot=?',
                          (rules.ACCOUNT, it['format'], slot)).fetchone()
        why = self.eligible(c, it)
        if why == 'a publication attempt is active':
            why = None                                   # pre-commit: refused at commit once the reservation moves
        if other and other['item_id'] != it['item_id']:
            if why is None:
                return self._propose_swap(c, cmd, it, dict(other), at)
            self.release(c, it, 'owner chose another time', keep_request=False)
            self.update_item(c, it['item_id'], cmd.actor, 'requested time', requested_at=slot,
                             requested_by='owner' if cmd.actor_kind == OWNER else 'board')
            return {'_state': 'accepted', 'requested': slot,
                    'message': f'Time recorded as requested ({rules.display(at)}); another item holds it, so when this '
                               'item is ready you will get one proposal to move that item. Not yet reserved: ' + why}
        if why is not None:
            self.release(c, it, 'owner chose another time', keep_request=False)
            self.update_item(c, it['item_id'], cmd.actor, 'requested time', requested_at=slot,
                             requested_by='owner' if cmd.actor_kind == OWNER else 'board')
            return {'_state': 'accepted', 'requested': slot,
                    'message': f'Time recorded as requested ({rules.display(at)}); not yet reserved: ' + why}
        if res:
            c.execute('DELETE FROM ops_reservations WHERE item_id=?', (it['item_id'],))
        self._reserve(c, self.item(c, it['item_id']), at, 'owner', True, cmd.actor)
        out = {'scheduled': slot, 'display': rules.display(at)}
        if att:
            out['message'] = ('The publisher had already started on the previous time; that attempt stops before '
                              'publishing because the time changed.')
        return out

    def op_set_window(self, c, cmd: Command):
        """Owner constraint for an item's publication day/earliest time (e.g. "not before tomorrow"). An existing
        reservation outside the window is released and the item is scheduled inside it."""
        it = self.item(c, cmd.item_id)
        self._guard_mutable(c, it, allow_paused=True)
        win = self.parse_window(cmd.args)
        self._store_window(c, it['item_id'], win, cmd.actor)
        res = self.reservation(c, it['item_id'])
        if it['requested_at'] and win and not rules.in_window(rules.instant(it['requested_at']), win):
            self.update_item(c, it['item_id'], cmd.actor, 'request outside window', requested_at=None, requested_by=None)
        if res and win and not rules.in_window(rules.instant(res['slot']), win):
            self.release(c, self.item(c, it['item_id']), 'outside the owner window', keep_request=False)
        return {'window': win, **self.try_schedule(c, it['item_id'], cmd.actor)}

    def _propose_swap(self, c, cmd, it, other, at):
        """Option B: one concrete proposal naming every affected item and its resulting time (R5 M5). If the
        requester already holds a slot this is a true swap; otherwise the other item moves to the earliest free
        slot on the usual schedule. Pins are stated and kept; nothing moves before approval."""
        o = self.item(c, other['item_id'])
        now = self.now_dt()
        if rules.instant(other['slot']) <= now + rules.NEAR_DUE:
            raise Rejected('The requested slot is about to publish another item; choose another time', 'near_due')
        cur = self.reservation(c, it['item_id'])
        if cur and rules.instant(cur['slot']) > now + rules.NEAR_DUE:
            new_for_other, swap = rules.instant(cur['slot']), True
        else:
            exclude = self.taken(c, it['format']) | {rules.iso(at)}
            new_for_other = next((g for g in rules.grid(now, it['format'], rules.NEAR_DUE)
                                  if rules.iso(g) not in exclude), None)
            swap = False
            if new_for_other is None:
                raise Rejected('The requested slot is occupied and no free slot exists to move the other item', 'no_slot')
        pinned = bool(other['owner_pinned'])
        payload = {'item_id': it['item_id'], 'slot': rules.iso(at), 'other_id': o['item_id'],
                   'other_slot': rules.iso(new_for_other), 'swap': swap, 'other_pinned': pinned,
                   'requester_old': cur['slot'] if cur else None, 'other_old': other['slot']}
        summary = (f"Move {it['name']} ({it['item_id']}) "
                   f"{'from ' + rules.display(rules.instant(cur['slot'])) + ' ' if cur else ''}to {rules.display(at)}. "
                   f"That time is reserved by {o['name']} ({o['item_id']}); "
                   + (f"the two times are swapped, so it moves to {rules.display(new_for_other)}. "
                      if swap else f"it would move to {rules.display(new_for_other)} (earliest free slot). ")
                   + ('Its owner pin is kept. ' if pinned else 'It stays an automatic slot. ')
                   + 'Nothing changes until approved.')
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
        why = self.eligible(c, it)
        if why and why != 'a publication attempt is active':
            raise Rejected(f"{it['name']} is no longer eligible ({why}); proposal not executed", 'stale')
        mine, theirs = self.reservation(c, it['item_id']), self.reservation(c, o['item_id'])
        if (mine['slot'] if mine else None) != payload.get('requester_old', mine['slot'] if mine else None) or \
                (theirs['slot'] if theirs else None) not in (payload.get('other_old', payload['slot']), None):
            raise Rejected('A reservation changed after the proposal; it was not executed', 'stale')
        for r in (mine, theirs):
            if r and rules.instant(r['slot']) <= now + rules.NEAR_DUE:
                raise Rejected('A current slot is within 10 minutes of publication; proposal not executed', 'near_due')
        for slot in (payload['slot'], payload['other_slot']):
            try:
                rules.validate_owner_time(now, it['format'], slot)
            except rules.RuleError as e:
                raise Rejected(f'A proposed time is no longer valid ({e}); proposal not executed', 'stale')
        keep = {r['slot'] for r in (mine, theirs) if r}
        taken = self.reserved_slots(c, it['format']) - keep
        if payload['other_slot'] in taken or payload['slot'] in taken:
            raise Rejected('A proposed slot was taken in the meantime; proposal not executed', 'stale')
        c.execute('DELETE FROM ops_reservations WHERE item_id IN (?,?)', (it['item_id'], o['item_id']))
        moved = [{'item_id': it['item_id'], 'slot': payload['slot']}]
        if theirs and not self.eligible(c, o):
            self._reserve(c, self.item(c, o['item_id']), rules.instant(payload['other_slot']),
                          theirs['origin'] if payload.get('other_pinned') else 'auto', payload.get('other_pinned'),
                          cmd.actor)
            moved.append({'item_id': o['item_id'], 'slot': payload['other_slot']})
        self._reserve(c, self.item(c, it['item_id']), rules.instant(payload['slot']), 'owner', True, cmd.actor)
        return {'moved': moved}

    def op_request_publish(self, c, cmd: Command):
        """Owner asks to get an item out: the earliest free slot on the usual schedule (inside any day/not-before
        constraint given with the instruction) through the normal publisher. Never a direct publish."""
        it = self.item(c, cmd.item_id)
        self._guard_mutable(c, it, allow_paused=True)
        win = self.parse_window(cmd.args)
        res = self.reservation(c, it['item_id'])
        if res and self.deadline(c, res) <= self.now_dt():
            self.release(c, it, 'missed slot replaced by an owner publish request', keep_request=False)
            res = None
        if res and (not win or rules.in_window(rules.instant(res['slot']), win)):
            return {'scheduled': res['slot'], 'display': rules.display(rules.instant(res['slot'])),
                    'message': 'Already scheduled; the publisher will post it at that time'}
        it = self._clear_time_hold(c, it, cmd.actor)    # "publish it" answers an open time question (R5 A16)
        if win or res:
            self._store_window(c, it['item_id'], win, cmd.actor)
        if res:
            self.release(c, it, 'outside the owner window', keep_request=False)
        if it['requested_at']:
            self.update_item(c, it['item_id'], cmd.actor, 'publish request replaces earlier time', requested_at=None,
                             requested_by=None)
        it = self.item(c, it['item_id'])
        why = self.eligible(c, it)
        if why and it['owner_state'] == 'active' and not loads(it['hold'], None) and it['readiness'] != 'ready':
            return {'_state': 'accepted', 'message': f'It will be scheduled as soon as it is ready ({why}).',
                    **({'window': win} if win else {})}
        if why:
            raise Rejected('Cannot schedule yet: ' + why, 'not_eligible')
        for at in rules.grid(self.now_dt(), it['format'], rules.NEAR_DUE, window=win):
            if rules.iso(at) not in self.taken(c, it['format'], it['item_id']):
                self._reserve(c, it, at, 'owner', False, cmd.actor)
                return {'scheduled': rules.iso(at), 'display': rules.display(at)}
        raise Rejected('No free slot ' + (self.window_text(win) if win else 'in the scheduling horizon'), 'no_slot')
