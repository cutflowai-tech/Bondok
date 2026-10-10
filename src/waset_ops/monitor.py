"""Schedule supervisor: read-only inspection + bounded repairs through the handler.

* ``inspect`` never writes (opens a read connection only).
* ``repair`` submits each finding as an idempotent command; repeated cycles
  with unchanged findings produce no new repairs and no new notifications.
* Protected: in-flight/unknown/published/paused/skipped items; owner-pinned
  slots are reported, never moved automatically. Variety is a preference only.
"""
from __future__ import annotations

import shutil

from . import rules
from .core import NOTICE_SECONDS, Command, Rejected
from .db import audit, dumps, loads

STALE_HEARTBEAT = {'wf2': 300, 'wf1': 1800, 'wf3': 4200}


class MonitorMixin:
    def inspect(self) -> dict:
        now, now_dt = self.now(), self.now_dt()
        findings = []
        with self.store.read() as c:
            rows = c.execute('SELECT r.*, i.name, i.content_rev AS item_rev FROM ops_reservations r '
                             'JOIN ops_items i USING(item_id)').fetchall()
            for r in rows:
                it = self.item(c, r['item_id'])
                slot = rules.instant(r['slot'])
                att = self.active_attempt(c, r['item_id'])
                if att or it['publication'] != 'not_started':
                    continue
                near = slot <= now_dt + rules.NEAR_DUE
                why = self.eligible(c, it)
                if self.deadline(c, r) < now_dt:
                    # Closed with its actual cause by repair_missed, which also sends the one truthful notice:
                    # an owner time gets a question, an automatic slot its new time (R5 A1, LOW-09).
                    findings.append(self._f('missed', r, 'missed',
                                            f"{it['name']} missed its slot {rules.display(slot)}.", notify=False))
                elif why and why != 'a publication attempt is active':
                    findings.append(self._f('ineligible', r, 'release',
                                            f"{it['name']} holds {rules.display(slot)} but is not eligible ({why}); "
                                            'the slot is released.'))
                elif self.verification_problem(c, it):
                    findings.append(self._f('verification', r, 'release',
                                            f"{it['name']}: {self.verification_problem(c, it)}; slot released and file "
                                            'rechecked.'))
                elif r['content_rev'] != it['content_rev'] or r['payload_fp'] != self.payload_fp(c, it):
                    findings.append(self._f('revision', r, 'reauthorize',
                                            f"{it['name']}: content changed; reservation re-validated for the new "
                                            'revision.', notify=False))
                elif not rules.on_grid(r['format'], slot) and not near and not r['owner_pinned']:
                    # Owner times may be off-grid (contract §5); an automatic slot off the grid is moved.
                    findings.append(self._f('off_grid', r, 'release',
                                            f"{it['name']} is reserved at {rules.display(slot)}, outside the usual "
                                            'schedule; it will be moved to a valid slot.'))
            for a in c.execute("SELECT * FROM ops_attempts WHERE stage='outcome_unknown'").fetchall():
                findings.append({'fingerprint': f"unknown:{a['id']}", 'item_id': a['item_id'], 'kind': 'outcome_unknown',
                                 'action': 'notify', 'notify': False,  # already notified when it became unknown
                                 'detail': f"Publication outcome unknown for item {a['item_id']}."})
            for r in c.execute("SELECT item_id, name FROM ops_items WHERE publication='outcome_unknown' AND item_id NOT IN "
                               "(SELECT item_id FROM ops_attempts WHERE stage='outcome_unknown')").fetchall():
                # Imported while the board showed Publishing: no attempt row, still needs the owner's answer.
                findings.append({'fingerprint': f"unknown-item:{r['item_id']}", 'item_id': r['item_id'],
                                 'kind': 'outcome_unknown', 'action': 'notify', 'notify': False,
                                 'detail': f"Publication outcome unknown for {r['name']} ({r['item_id']})."})
            for o in c.execute("SELECT * FROM ops_outbox WHERE state='escalated'").fetchall():
                findings.append({'fingerprint': f"outbox:{o['id']}", 'item_id': o['item_id'], 'kind': 'sync_escalated',
                                 'action': 'notify', 'notify': False, 'detail': o['last_error'] or ''})
            for name, limit in STALE_HEARTBEAT.items():
                hb = c.execute('SELECT at FROM ops_heartbeat WHERE name=?', (name,)).fetchone()
                if hb and now - hb['at'] > limit:
                    findings.append({'fingerprint': f"heartbeat:{name}", 'item_id': None, 'kind': 'heartbeat',
                                     'action': 'notify', 'notify': True,
                                     'detail': f"{name} has not reported for {int((now - hb['at']) / 60)} minutes."})
            free = shutil.disk_usage(self.store.path.resolve().parent).free
            if free < rules.MEDIA_MIN_FREE_BYTES:
                # One alert while preparation is blocked (production 2026-10-10: only per-item board notes).
                waiting = c.execute('SELECT COUNT(*) FROM ops_items WHERE infra_issue IS NOT NULL').fetchone()[0]
                findings.append({'fingerprint': 'low_disk', 'item_id': None, 'kind': 'low_disk', 'action': 'notify',
                                 'notify': True,
                                 'detail': f'Media preparation is paused: the server has {free / 1e9:.1f} GB of free '
                                           f'disk space and needs {rules.MEDIA_MIN_FREE_BYTES / 1e9:.0f} GB. {waiting} '
                                           'item(s) are waiting. Free disk space; preparation resumes automatically. '
                                           'Do not delete the prepared media folder: it holds the scheduled videos.'})
        return {'findings': findings, 'checked_at': self.iso_ts(now), 'read_only': True}

    @staticmethod
    def _f(kind, r, action, detail, notify=True):
        return {'fingerprint': f"{kind}:{r['item_id']}:{r['slot']}:{r['content_rev']}", 'item_id': r['item_id'],
                'kind': kind, 'action': action, 'detail': detail, 'notify': notify, 'slot': r['slot']}

    def repair(self, run_id=None, fence=None) -> dict:
        report = self.inspect()
        applied, new = [], 0
        live = set()
        for f in report['findings']:
            live.add(f['fingerprint'])
            with self.store.tx() as c:
                if run_id:
                    self.run_check(c, 'wf3', run_id, fence)
                is_new = self.finding(c, f['fingerprint'], f['item_id'], f['kind'], f['detail'], notify=f['notify'])
            new += int(is_new)
            if f['action'] in ('release', 'reauthorize', 'missed'):
                op = {'release': 'repair_release', 'reauthorize': 'repair_reauthorize',
                      'missed': 'repair_missed'}[f['action']]
                r = self.submit(Command('wf3:' + f['fingerprint'], op, 'service:wf3', 'service:wf3', f['item_id'],
                                        {'kind': f['kind'], 'detail': f['detail'], 'slot': f.get('slot')}))
                if not r.get('duplicate'):
                    applied.append({'item_id': f['item_id'], 'kind': f['kind'], 'state': r['state']})
        applied += self.reconcile_schedule(run_id)
        self.sweep_proposals()
        with self.store.tx() as c:
            for r in c.execute('SELECT fingerprint FROM ops_findings WHERE resolved IS NULL').fetchall():
                # Findings raised by WF1 (missing on the board) are resolved there, not by this inspection
                # (audit MS11: resolving them here re-notified the same deleted item after every WF3 run).
                if r['fingerprint'] not in live and not r['fingerprint'].startswith('missing_on_board:'):
                    self.resolve_finding(c, r['fingerprint'])
            # Expired 24 h notices are only removed by a projection; nothing else re-projects an unchanged item
            # (audit MS10: stale Action text stayed on the board).
            for r in c.execute("SELECT item_id, observed FROM ops_items WHERE observed LIKE '%\"_notice\"%'").fetchall():
                n = (loads(r['observed'], {}) or {}).get('_notice')
                if n and self.now() - n.get('at', 0) >= NOTICE_SECONDS:
                    obs = loads(r['observed'], {})
                    obs.pop('_notice', None)
                    c.execute('UPDATE ops_items SET observed=? WHERE item_id=?', (dumps(obs), r['item_id']))
                    self.project(c, r['item_id'])
            c.execute('INSERT OR REPLACE INTO ops_heartbeat VALUES(?,?,?)', ('wf3', self.now(), None))
        return {'findings': len(report['findings']), 'new_findings': new, 'repairs': applied}

    def op_repair_release(self, c, cmd: Command):
        it = self.item(c, cmd.item_id)
        res = self.reservation(c, it['item_id'])
        if not res:
            return {'noop': True}
        if cmd.args.get('slot') and res['slot'] != cmd.args['slot']:
            return {'noop': True, 'note': 'reservation changed since inspection'}      # audit I6
        if res['owner_pinned']:
            raise Rejected('Owner-pinned slots are never moved automatically; reported only', 'owner_pinned')
        if self.active_attempt(c, it['item_id']) or it['publication'] != 'not_started':
            raise Rejected('Protected publication state; repair skipped', 'protected')
        if rules.instant(res['slot']) <= self.now_dt() + rules.NEAR_DUE and \
                rules.instant(res['slot']) + rules.LATE_WINDOW >= self.now_dt():
            raise Rejected('Near-due slot is never moved automatically', 'near_due')
        self.release(c, it, 'supervisor: ' + cmd.args.get('kind', ''), keep_request=False)
        out = self.evaluate(c, it['item_id'], cmd.actor)
        self.project(c, it['item_id'])         # the board must not keep showing the released slot (audit S9)
        return {'released': res['slot'], **out}

    def op_repair_reauthorize(self, c, cmd: Command):
        return self.reauthorize(c, cmd.item_id, cmd.actor, 'supervisor')

    def sweep_proposals(self) -> int:
        """Interaction lifecycle (R5 LOW-17): expired proposals reach a terminal state and their holds end; a caption
        approval whose draft is no longer pending becomes stale. The content decisions themselves are kept."""
        now, n = self.now(), 0
        with self.store.tx() as c:
            for p in c.execute("SELECT * FROM ops_proposals WHERE state='pending'").fetchall():
                state = None
                if p['kind'] in ('approve_caption', 'approve_captions'):
                    if p['kind'] == 'approve_caption' and not c.execute(
                            "SELECT 1 FROM ops_caption_drafts WHERE proposal_id=? AND state='pending_approval'",
                            (p['id'],)).fetchone():
                        state = 'stale'
                elif p['expires'] < now:
                    state = 'expired'
                if not state:
                    continue
                c.execute('UPDATE ops_proposals SET state=?, result=?, updated=? WHERE id=?',
                          (state, 'closed by the supervisor', now, p['id']))
                for b in loads(p['bindings'], []):
                    it = self.item(c, b['item_id'], required=False)
                    if it and (loads(it['hold'], {}) or {}).get('kind') in ('format_change', 'resume'):
                        self.update_item(c, it['item_id'], 'service:wf3', 'proposal closed', hold=None)
                n += 1
        return n

    # ------------------------------------------------------------------ missed slots and reconciliation
    def missed_cause(self, c, item_id, slot) -> str:
        rows = c.execute('SELECT stage, evidence FROM ops_attempts WHERE item_id=? AND slot=? ORDER BY updated DESC',
                         (str(item_id), slot)).fetchall()
        if not rows:
            return 'no publication attempt was made at that time'
        ev = loads(rows[0]['evidence'], {}) or {}
        why = ev.get('abandoned') or ev.get('refused') or ev.get('error') or ev.get('why') or rows[0]['stage']
        return f'{len(rows)} publication attempt(s) did not complete: {str(why)[:200]}'

    def op_repair_missed(self, c, cmd: Command):
        """A reservation whose publication window ended without publication (R5 A1). An owner time is closed with
        one question (never left 'Scheduled' in the past); an automatic slot is replaced by the next valid one."""
        it = self.item(c, cmd.item_id)
        res = self.reservation(c, it['item_id'])
        if not res or (cmd.args.get('slot') and res['slot'] != cmd.args['slot']):
            return {'noop': True, 'note': 'reservation changed since inspection'}
        if self.active_attempt(c, it['item_id']) or it['publication'] != 'not_started':
            raise Rejected('Protected publication state; repair skipped', 'protected')
        if self.deadline(c, res) >= self.now_dt():
            return {'noop': True, 'note': 'publication window still open'}
        cause = self.missed_cause(c, it['item_id'], res['slot'])
        self.release(c, it, 'missed slot', keep_request=False)
        audit(c, it['item_id'], 'missed_slot', cmd.actor, {'slot': res['slot'], 'cause': cause,
                                                           'pinned': res['owner_pinned']})
        if res['owner_pinned'] and it['owner_state'] == 'active' and not loads(it['hold'], None):
            c.execute("UPDATE ops_items SET requested_at=?, requested_by='owner' WHERE item_id=?",
                      (res['slot'], it['item_id']))
            out = self._missed_request(c, self.item(c, it['item_id']), cmd.actor, cause)
            self.project(c, it['item_id'])
            return {'missed': res['slot'], 'question': True, **out}
        out = self.evaluate(c, it['item_id'], cmd.actor)
        nxt = out.get('scheduled')
        self.notify(c, f"missed-auto:{it['item_id']}:{res['slot']}",
                    f"{it['name']} ({it['item_id']}) missed its slot {rules.display(rules.instant(res['slot']))} "
                    f'({cause}). ' + (f'It is now scheduled for {rules.display(rules.instant(nxt))}.' if nxt else
                                      'It is not scheduled yet: ' + str(out.get('waiting') or out.get('reason') or
                                                                       out.get('readiness'))), it['item_id'])
        self.project(c, it['item_id'])
        return {'missed': res['slot'], **out}

    def op_repair_schedule(self, c, cmd: Command):
        it = self.item(c, cmd.item_id)
        if self.reservation(c, it['item_id']) or it['publication'] != 'not_started' or it['owner_state'] != 'active':
            return {'noop': True}
        return self.try_schedule(c, it['item_id'], cmd.actor)

    def reconcile_schedule(self, run_id=None, limit=50) -> list:
        """Every supervisor cycle: try to schedule every eligible unreserved item and close owner times that
        passed while the item was still preparing (R5 A17, L5). Bounded; deterministic order (requested time
        first, then longest waiting)."""
        now = rules.iso(self.now_dt())
        with self.store.read() as c:
            rows = c.execute("SELECT item_id FROM ops_items WHERE publication='not_started' AND owner_state='active' "
                             'AND item_id NOT IN (SELECT item_id FROM ops_reservations) AND '
                             "(readiness='ready' OR (requested_at IS NOT NULL AND requested_at<=? AND "
                             "COALESCE(requested_by,'owner')!='legacy-board')) "
                             "ORDER BY COALESCE(requested_at,'9999'), COALESCE(waiting_since,created) LIMIT ?",
                             (now, limit)).fetchall()
        tag = run_id or f't{int(self.now() // 60)}'
        out = []
        for r in rows:
            res = self.submit(Command(f"{tag}:schedule:{r['item_id']}", 'repair_schedule', 'service:wf3',
                                      'service:wf3', r['item_id'], {}))
            if res.get('scheduled') or res.get('missed'):
                out.append({'item_id': r['item_id'], 'kind': 'schedule', 'state': res['state']})
        return out

    # ------------------------------------------------------------------ reads (Bondok)
    def item_status(self, item_id) -> dict:
        with self.store.read() as c:
            it = self.item(c, item_id)
            res = self.reservation(c, item_id) if it['publication'] == 'not_started' else None
            att = c.execute('SELECT id,stage,updated,media_id,permalink FROM ops_attempts WHERE item_id=? '
                            'ORDER BY created DESC LIMIT 1', (str(item_id),)).fetchone()
            pend = c.execute("SELECT id,kind,summary,expires FROM ops_proposals WHERE state='pending' AND bindings LIKE ?",
                             (f'%"{item_id}"%',)).fetchall()
            return {
                'item_id': it['item_id'], 'name': it['name'], 'code': it['code'], 'format': it['format'],
                'version': it['version'], 'content_rev': it['content_rev'],
                'readiness': it['readiness'], 'reason': it['block_reason'], 'owner_state': it['owner_state'],
                'hold': loads(it['hold'], None), 'publication': it['publication'],
                'caption_state': it['caption_state'], 'topaz_confirmed_for_selected': bool(
                    it['asset_key'] and it['topaz_asset'] == it['asset_key']),
                'selected_file': it['file_name'], 'infra_issue': it['infra_issue'],
                'reservation': {'slot': res['slot'], 'cairo': rules.display(rules.instant(res['slot'])),
                                'pinned': bool(res['owner_pinned'])} if res else None,
                'requested_at': it['requested_at'],
                'last_attempt': dict(att) if att else None,
                'pending_proposals': [dict(p) for p in pend],
                'as_of': self.iso_ts(self.now()),
            }

    def schedule(self, days=14) -> list[dict]:
        horizon = rules.iso(self.now_dt() + __import__('datetime').timedelta(days=days))
        with self.store.read() as c:
            # Pending work only: published items keep their reservation row as history (R5 LOW-08).
            rows = c.execute('SELECT r.slot, r.format, r.owner_pinned, i.item_id, i.name, i.code FROM ops_reservations r '
                             "JOIN ops_items i USING(item_id) WHERE r.slot<=? AND r.slot>=? AND i.publication='not_started' "
                             'ORDER BY r.slot', (horizon, rules.iso(self.now_dt() - rules.LATE_WINDOW))).fetchall()
            return [{**dict(r), 'cairo': rules.display(rules.instant(r['slot']))} for r in rows]

    def health(self) -> dict:
        """Read-only health (no health-row writes, unlike legacy /v1/health)."""
        now = self.now()
        with self.store.read() as c:
            hb = {r['name']: int(now - r['at']) for r in c.execute('SELECT * FROM ops_heartbeat')}
            q = lambda sql: c.execute(sql).fetchone()[0]
            return {'schema': self.store.schema_version(), 'heartbeat_age_seconds': hb,
                    'outcome_unknown': q("SELECT COUNT(*) FROM ops_items WHERE publication='outcome_unknown'"),
                    'outbox_pending': q("SELECT COUNT(*) FROM ops_outbox WHERE state IN ('pending','failed','in_flight')"),
                    'outbox_escalated': q("SELECT COUNT(*) FROM ops_outbox WHERE state='escalated'"),
                    'reservations': q('SELECT COUNT(*) FROM ops_reservations'),
                    'open_findings': q('SELECT COUNT(*) FROM ops_findings WHERE resolved IS NULL'),
                    'as_of': self.iso_ts(now)}

    def command_status(self, command_id) -> dict | None:
        with self.store.read() as c:
            r = c.execute('SELECT id,op,item_id,state,result,updated FROM ops_commands WHERE id=?',
                          (command_id,)).fetchone()
            return {**dict(r), 'result': loads(r['result'])} if r else None
