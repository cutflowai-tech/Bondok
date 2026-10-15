"""Acceptance isolation (operator, ON THE SERVER as the store owner): test items never reach Instagram.

    python3 -m waset_runtime.isolation set --code-prefix ACC --name-prefix "Acceptance " [--id ITEM ...] --reason "..."
    python3 -m waset_runtime.isolation show
    python3 -m waset_runtime.isolation release      # test cleanup: release the reservations of isolated items only
    python3 -m waset_runtime.isolation clear
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from waset_ops import Command, Ops

from .service import read_env


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('action', choices=['set', 'show', 'release', 'clear'])
    ap.add_argument('--code-prefix', action='append', default=[])
    ap.add_argument('--name-prefix', action='append', default=[])
    ap.add_argument('--id', action='append', default=[])
    ap.add_argument('--reason')
    ap.add_argument('--env', default=None)
    a = ap.parse_args(argv)
    env = read_env(a.env or os.environ.get('WASET_RUNTIME_ENV', '/opt/waset-bondok/runtime.env'))
    ops = Ops(env['PIPELINE_DB'])
    actor = 'service:acceptance'
    stamp = f'{int(time.time())}'
    if a.action == 'show':
        with ops.store.read() as c:
            iso = ops.isolation(c)
            hits = [dict(r) for r in c.execute('SELECT item_id, code, name FROM ops_items').fetchall()
                    if ops.send_isolated(c, r['item_id'])]
        print(json.dumps({'isolation': iso, 'isolated_items': hits}, ensure_ascii=False, indent=1))
        return 0
    if a.action == 'release':
        r = ops.submit(Command(f'isolation-release:{stamp}', 'release_isolated', actor, actor, None, {}))
    else:
        args = {} if a.action == 'clear' else {'item_ids': a.id, 'code_prefixes': a.code_prefix,
                                                 'name_prefixes': a.name_prefix, 'reason': a.reason}
        r = ops.submit(Command(f'isolation-{a.action}:{stamp}', 'set_isolation', actor, actor, None, args))
    print(json.dumps(r, ensure_ascii=False, indent=1, default=str))
    return 0 if r.get('state') == 'completed' else 1


if __name__ == '__main__':
    sys.exit(main())
