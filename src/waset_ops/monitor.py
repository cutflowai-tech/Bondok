"""Schedule supervisor: read-only inspection + bounded repairs through the handler.

* ``inspect`` never writes (opens a read connection only).
* ``repair`` submits each finding as an idempotent command; repeated cycles
  with unchanged findings produce no new repairs and no new notifications.
* Protected: in-flight/unknown/published/paused/skipped items; owner-pinned
  slots are reported, never moved automatically. Variety is a preference only.
"""
from __future__ import annotations

from . import rules
from .core import NOTICE_SECONDS, Command, Rejected
from .db import dumps, loads

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
                if slot + rules.LATE_WINDOW < now_dt:
                    findings.append(self._f('missed', r, 'release',
                                            f"{it['name']} missed its slot {rules.display(slot)} without a publication "
                                            'attempt; it will be rescheduled to the next free slot.'))
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
                elif not rules.on_grid(r['format'], slot) and not near:
                    action = 'notify' if r['owner_pinned'] else 'release'
                    findings.append(self._f('off_grid', r, action,
                                            f"{it['name']} is reserved at {rules.display(slot)}, outside the agreed "
                                            + ('grid; it was chosen by the owner, so it is only reported.'
                                               if r['owner_pinned'] else 'grid; it will be moved to a valid slot.')))
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
            if f['action'] in ('release', 'reauthorize'):
                op = 'repair_release' if f['action'] == 'release' else 'repair_reauthorize'
                r = self.submit(Command('wf3:' + f['fingerprint'], op, 'service:wf3', 'service:wf3', f['item_id'],
                                        {'kind': f['kind'], 'detail': f['detail'], 'slot': f.get('slot')}))
                if not r.get('duplicate'):
                    applied.append({'item_id': f['item_id'], 'kind': f['kind'], 'state': r['state']})
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

    # ------------------------------------------------------------------ reads (Bondok)
    def item_status(self, item_id) -> dict:
        with self.store.read() as c:
            it = self.item(c, item_id)
            res = self.reservation(c, item_id)
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
            rows = c.execute('SELECT r.slot, r.format, r.owner_pinned, i.item_id, i.name, i.code FROM ops_reservations r '
                             'JOIN ops_items i USING(item_id) WHERE r.slot<=? ORDER BY r.slot', (horizon,)).fetchall()
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
