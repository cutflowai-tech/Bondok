"""Simulated Monday board + WF2 display sync: a Python mirror of the generated JS (compare-before-write
guard, Monday read-back shapes). Test utility (from the Round 5 board-sync review); synthetic data only."""
import copy
import hashlib
import json

import support  # noqa: F401,E402  (puts src/ on the path)
from waset_ops import board as B  # noqa: E402

INV = {v: k for k, v in B.COL.items()}


def hid(x):
    return hashlib.sha256(str(x).encode()).hexdigest()[:8]


def cell_after_write(colid, mv, now_iso='2026-10-10T10:00:00Z'):
    """How Monday stores a change_multiple_column_values value (shape read back by the API)."""
    key = INV.get(colid)
    kind = B.KIND.get(key, 'text')
    if kind == 'text' or isinstance(mv, str):
        s = mv if isinstance(mv, str) else ''
        if s == '':
            return None
        return {'id': colid, 'text': s, 'value': json.dumps(s)}
    if not mv:      # {} clears
        return None
    if kind == 'status':
        return {'id': colid, 'text': mv['label'], 'value': json.dumps({'index': 1, 'post_id': None, 'changed_at': now_iso})}
    if kind == 'long':
        t = mv.get('text') or ''
        if t == '':
            return {'id': colid, 'text': '', 'value': None}
        return {'id': colid, 'text': t, 'value': json.dumps({'text': t, 'changed_at': now_iso})}
    if kind == 'link':
        return {'id': colid, 'text': f"{mv.get('text')} - {mv['url']}",
                'value': json.dumps({'url': mv['url'], 'text': mv.get('text'), 'changed_at': now_iso})}
    if kind == 'datetime':
        return {'id': colid, 'text': f"{mv['date']} {mv.get('time', '')[:5]}",
                'value': json.dumps({'date': mv['date'], 'time': mv.get('time'), 'changed_at': now_iso})}
    if kind == 'date':
        return {'id': colid, 'text': mv['date'], 'value': json.dumps({'date': mv['date'], 'icon': None, 'changed_at': now_iso})}
    if kind == 'hour':
        h, m = mv['hour'], mv['minute']
        ampm = 'AM' if h < 12 else 'PM'
        h12 = h % 12 or 12
        return {'id': colid, 'text': f'{h12:02d}:{m:02d} {ampm}', 'value': json.dumps({'hour': h, 'minute': m, 'changed_at': now_iso})}
    raise ValueError((colid, mv))


def js_norm(c, kind):
    if not c:
        return None
    if kind == 'long':
        try:
            v = json.loads(c.get('value') or 'null')
            if v and isinstance(v.get('text'), str):
                return v['text'].strip() or None
        except Exception:  # noqa: BLE001
            pass
    return str(c.get('text') or '').strip() or None


class Board:
    def __init__(self, items):
        self.items = {str(i['id']): copy.deepcopy(i) for i in items}
        self.log = []          # (item, key, old_norm, new_norm)
        self.moves = []

    def snapshot(self):
        return [copy.deepcopy(i) for i in self.items.values()]

    def cell(self, iid, colid):
        return next((c for c in self.items[iid]['column_values'] if c['id'] == colid), None)

    def set_raw(self, iid, colid, newcell):
        it = self.items[iid]
        it['column_values'] = [c for c in it['column_values'] if c['id'] != colid]
        if newcell is not None:
            it['column_values'].append(newcell)

    def apply(self, payload):
        iid = str(payload['item_id'])
        if iid not in self.items:
            return 'item not found'
        for colid, mv in (payload.get('columns') or {}).items():
            key = INV.get(colid)
            old = B.norm(self.items[iid], key) if key else None
            self.set_raw(iid, colid, cell_after_write(colid, mv))
            new = B.norm(self.items[iid], key) if key else None
            if old != new:
                self.log.append((iid, key, old, new))
        g = payload.get('group')
        if g:
            cur = (self.items[iid].get('group') or {}).get('id')
            if cur != g:
                self.moves.append((iid, cur, g))
            self.items[iid]['group'] = {'id': g}
        return None

    def guard_conflict(self, payload):
        guard = payload.get('guard') or {}
        changed = []
        iid = str(payload['item_id'])
        if iid not in self.items:            # WF2: 'item not found on the board' is a read error, acked as failed
            return ['item not found']
        for colid, g in guard.items():
            cur = js_norm(self.cell(iid, colid), g.get('kind'))
            was = g.get('was', None)
            ok = was if isinstance(was, list) else [was]
            ok = [None if v is None else v for v in ok]
            if cur not in ok and cur != g.get('new'):
                changed.append(colid)
        return changed


def wf2_sync(ops, board, worker='wf2-sim', limit=10, runs=1, clock=None, step=60):
    """WF2 display sync: take, guard-check against the board, apply, ack."""
    stats = {'taken': 0, 'applied': 0, 'conflicts': 0}
    for _ in range(runs):
        jobs = ops.outbox_take(['monday', 'source_monday'], worker, limit)
        for j in jobs:
            stats['taken'] += 1
            if j['kind'] == 'source_monday':
                ops.outbox_ack(j['id'], worker, True)
                continue
            p = j['payload']
            ch = board.guard_conflict(p) if p.get('guard') else []
            if ch:
                stats['conflicts'] += 1
                ops.outbox_ack(j['id'], worker, False, 'conflict: changed on the board by a person (' + ','.join(ch) + ')')
                continue
            err = board.apply(p)
            ops.outbox_ack(j['id'], worker, err is None, err)
            stats['applied'] += 1
        if clock is not None:
            clock.advance(step)
    return stats
