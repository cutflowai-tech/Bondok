"""Attribution correction run (owner review 2026-10-15), ON THE SERVER as the store owner.

    python3 -m waset_runtime.corrections plan  <out.json>
    python3 -m waset_runtime.corrections apply <plan.json> --authorized-by "<owner authorization reference>"

plan: reads the store, finds the candidates (waset_ops.corrections), reads Monday's activity log of the time
columns for them (read-only) and keeps only the edits that are not Bondok's own acknowledged writes, then writes
the plan with every item's state before. Nothing is changed.
apply: one idempotent command per correction; a second run of the same plan changes nothing (duplicates), and an
item that changed since the plan is refused (stale) instead of being overwritten. Prints before/after.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from waset_ops import Ops
from waset_ops.corrections import TIME_COLUMNS

from .monday import Monday
from .service import read_env


def plan(ops: Ops, monday) -> dict:
    first = ops.correction_plan(None)
    cand = {e['item_id']: e for e in first['unresolved']}
    edits = {}
    if cand:
        since = min(float(e['owner_instruction']['at']) for e in cand.values()) - 60
        events = monday.activity(sorted(cand), TIME_COLUMNS, since)
        for iid, e in cand.items():
            ours = ops.our_board_writes(iid, TIME_COLUMNS, e['owner_instruction']['at'])
            edits[iid] = [ev for ev in events if ev['item_id'] == iid and float(ev['at']) > float(
                e['owner_instruction']['at']) and not any(-180 <= float(ev['at']) - t <= 30 for t in ours)]
    out = ops.correction_plan(edits)
    out['activity_checked'] = sorted(cand)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('action', choices=['plan', 'apply'])
    ap.add_argument('path')
    ap.add_argument('--authorized-by')
    ap.add_argument('--env', default=None)
    a = ap.parse_args(argv)
    env = read_env(a.env or os.environ.get('WASET_RUNTIME_ENV', '/opt/waset-bondok/runtime.env'))
    ops = Ops(env['PIPELINE_DB'])
    if a.action == 'plan':
        p = plan(ops, Monday(env['MONDAY_API_TOKEN']))
        with open(a.path, 'w') as f:
            json.dump(p, f, indent=1, ensure_ascii=False, default=str)
        print(json.dumps({k: len(v) for k, v in p.items() if isinstance(v, list)}))
        return 0
    if not a.authorized_by:
        sys.exit('--authorized-by is required')
    with open(a.path) as f:
        p = json.load(f)
    print(json.dumps(ops.apply_corrections(p, a.authorized_by), indent=1, ensure_ascii=False, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
