"""Evidence-based correction of decisions the automation attributed to the owner (owner review 2026-10-15).

Cause (fixed in items.py/sched.py the same day): board reads of the legacy Post Date / Post Time columns, and in
the 2026-10-13 15:31 cycle a re-observation of their stale values, were converted into owner-kind time decisions
(cleared time -> "unscheduled" hold stored as the owner's; old dates -> "requested time"; reservations
origin=owner). Those reads overwrote time instructions the owner had given (2026-10-10/11, via the authorized
operator session) and were shown to the owner as the owner's own actions.

The plan is computed from the store plus Monday's activity log (supplied by the runtime): an item is corrected only
when its latest explicit owner time instruction was overwritten by board-derived decisions, the owner gave no
later instruction, and the activity log shows no edit of the time columns after the instruction that is not one
of Bondok's own writes. Everything else is listed as kept or unresolved. Each correction is one idempotent command
(``attribution_correction``) that checks the state it was planned against, restores the owner's instruction under
the current policies (a passed time -> the missed-time policy, never an immediate post; an occupied time -> kept as
the request with the conflict shown, nobody displaced) and records an audit row linked to the original commands.
Original records are never rewritten.
"""
from __future__ import annotations

from . import rules
from .core import MONDAY, OWNER, Command, Rejected
from .db import audit, dumps, loads

CORRECTION = 'attribution-2026-10-15'
TIME_COLUMNS = ('date4', 'hour_mm7xy9cf', 'date_mm7y8s9t')


class CorrectionsMixin:
    def _owner_time(self, c, item_id):
        r = c.execute("SELECT id, created, result FROM ops_commands WHERE item_id=? AND actor_kind=? AND "
                      "op='request_reschedule' AND state IN ('completed','accepted') ORDER BY created DESC LIMIT 1",
                      (item_id, OWNER)).fetchone()
        if not r:
            return None
        res = loads(r['result'], {}) or {}
        at = res.get('requested') or res.get('scheduled') or res.get('slot')
        return {'command': r['id'], 'at': r['created'], 'time': at} if at else None

    def _state(self, c, it) -> dict:
        res = self.reservation(c, it['item_id'])
        return {'hold': loads(it['hold'], None), 'requested_at': it['requested_at'],
                'requested_by': it['requested_by'], 'owner_state': it['owner_state'],
                'reservation': {'slot': res['slot'], 'origin': res['origin'], 'pinned': res['owner_pinned']}
                if res else None}

    def correction_plan(self, board_edits: dict | None) -> dict:
        """``board_edits``: item -> list of activity-log events on the time columns that are not Bondok's own writes
        (None for an item means the log could not be read). Returns {'corrections', 'kept', 'unresolved'}."""
        out = {'corrections': [], 'kept': [], 'unresolved': []}
        with self.store.read() as c:
            items = c.execute("SELECT * FROM ops_items WHERE publication='not_started'").fetchall()
            for it in map(dict, items):
                iid = it['item_id']
                own = self._owner_time(c, iid)
                state = self._state(c, it)
                if own:
                    later = [r['id'] for r in c.execute(
                        "SELECT id FROM ops_commands WHERE item_id=? AND actor_kind=? AND op='request_reschedule' AND "
                        "created>? ORDER BY created", (iid, MONDAY, own['at']))]
                    later_owner = c.execute("SELECT 1 FROM ops_commands WHERE item_id=? AND actor_kind=? AND created>? "
                                            "AND op IN ('request_reschedule','request_publish','set_window','pause',"
                                            "'skip','resume') LIMIT 1", (iid, OWNER, own['at'])).fetchone()
                    done = c.execute("SELECT 1 FROM ops_audit WHERE item_id=? AND kind='attribution_correction' AND "
                                     "json_extract(detail,'$.owner_instruction.command')=?",
                                     (iid, own['command'])).fetchone()
                    current = bool(done) or (state['requested_at'] == own['time'] and
                                             state['requested_by'] == 'owner') or \
                        ((state['reservation'] or {}).get('slot') == own['time'])
                    if later and not later_owner and not current and it['owner_state'] == 'active':
                        edits = (board_edits or {}).get(iid, None) if board_edits is not None else None
                        entry = {'item_id': iid, 'name': it['name'], 'kind': 'restore_owner_time',
                                 'owner_instruction': own, 'overwritten_by': later[-12:],
                                 'overwrites': len(later), 'before': state}
                        after = [e for e in (edits or []) if float(e['at']) > float(own['at'])]
                        if edits is None:
                            out['unresolved'].append({**entry, 'missing': 'Monday activity log of the time columns'})
                        elif after:
                            out['kept'].append({**entry, 'why': 'the board time columns were edited after the '
                                                                 'instruction by someone other than Bondok',
                                                'edits': after[:5]})
                        else:
                            out['corrections'].append(entry)
                        continue
                res = state['reservation']
                if res and res['origin'] == 'owner' and not (own and own['time'] == res['slot']):
                    out['corrections'].append({'item_id': iid, 'name': it['name'], 'kind': 'relabel_reservation',
                                               'before': state, 'origin': 'board',
                                               'why': 'the reserved time came from a board value, not an owner '
                                                      'instruction (the slot itself is kept)'})
                hold = state['hold'] or {}
                if hold.get('origin') == 'owner' and not hold.get('set_by'):
                    prov = self.hold_provenance(c, it) or {}
                    if prov.get('source') != 'owner':
                        out['corrections'].append({'item_id': iid, 'name': it['name'], 'kind': 'relabel_hold',
                                                   'before': state, 'source': prov.get('source'),
                                                   'set_by': prov.get('set_by'), 'set_at': prov.get('set_at')})
        return out

    def op_attribution_correction(self, c, cmd: Command):
        e = cmd.args
        it = self.item(c, cmd.item_id)
        before = self._state(c, it)
        if before != e.get('before'):
            raise Rejected('The item changed since this correction was planned; plan again', 'stale',
                           current=before)
        kind = e['kind']
        if kind == 'relabel_reservation':
            c.execute('UPDATE ops_reservations SET origin=?, updated=? WHERE item_id=?',
                      (e['origin'], self.now(), it['item_id']))
        elif kind == 'relabel_hold':
            hold = loads(it['hold'], {}) or {}
            hold.update({'origin': e['source'] or 'unknown', 'set_by': e.get('set_by'), 'corrected': CORRECTION})
            self.update_item(c, it['item_id'], cmd.actor, 'hold source corrected', hold=dumps(hold))
        elif kind == 'restore_owner_time':
            own = e['owner_instruction']
            self.release(c, it, 'correction: the reservation came from a board read', keep_request=False)
            self.update_item(c, it['item_id'], cmd.actor, 'owner instruction restored', hold=None,
                             requested_at=own['time'], requested_by='owner')
            it = self.item(c, it['item_id'])
            at = rules.instant(own['time'])
            if at <= self.now_dt() + rules.NEAR_DUE:
                # The instruction's time passed while it was overwritten: kept as the historical request and closed by
                # the missed-time policy (one question), never published now.
                self._missed_request(c, it, cmd.actor, 'it had been overwritten by a board read and was restored '
                                                        'after its time')
            else:
                self.try_schedule(c, it['item_id'], cmd.actor)
        else:
            raise Rejected('Unknown correction', 'invalid')
        after = self._state(c, self.item(c, it['item_id']))
        audit(c, it['item_id'], 'attribution_correction', cmd.actor,
              {'correction': CORRECTION, 'kind': kind, 'before': before, 'after': after,
               'owner_instruction': e.get('owner_instruction'), 'overwritten_by': e.get('overwritten_by'),
               'authorized_by': (cmd.auth or {}).get('authorized_by')})
        self.project(c, it['item_id'])
        return {'kind': kind, 'before': before, 'after': after}

    def apply_corrections(self, plan: dict, authorized_by: str) -> list[dict]:
        out = []
        for e in plan.get('corrections') or []:
            r = self.submit(Command(f"{CORRECTION}:{e['kind']}:{e['item_id']}", 'attribution_correction',
                                    'service:correction', 'service:correction', e['item_id'], e,
                                    auth={'authorized_by': authorized_by}))
            out.append({'item_id': e['item_id'], 'kind': e['kind'], 'state': r.get('state'),
                        'duplicate': bool(r.get('duplicate')), 'after': r.get('after'), 'reason': r.get('reason')})
        return out
