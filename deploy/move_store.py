"""One-time move of Bondok's operational store off the n8n data volume (run ON THE SERVER as root).

    python3 move_store.py --units DIR --tombstone retired_helper.py [--apply]

Before: /var/lib/docker/volumes/waset_social_data/_data (mounted into the n8n container) bind-mounted into both
Bondok services as /var/lib/bondok/pipeline. After: /var/lib/bondok/pipeline is a real Bondok-owned directory on the
host; the services lose the bind mount; the old volume keeps only a tombstone helper.py that refuses every call.

Steps (each checked; without --apply only the checks and the plan run):
  1. no reserved, unpublished slot within 20 minutes; no publication attempt between claim and result;
  2. stop waset-runtime and bondok; checkpoint the WAL; consistent backup copy to the backup root;
  3. rename every entry of the old directory into the new one (same filesystem: atomic, no copy of media);
  4. ownership bondok:bondok, mode 0770; tombstone helper.py into the old directory;
  5. install the unit files without BindPaths, daemon-reload, start bondok and waset-runtime;
  6. the store opens at the new path with the same row counts and runtime owner.
Rollback: stop both services, move the entries back, restore the previous unit files (kept in the backup), start.
"""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

OLD = Path('/var/lib/docker/volumes/waset_social_data/_data')
NEW = Path('/var/lib/bondok/pipeline')
UNITS = ('bondok.service', 'waset-runtime.service')
BACKUPS = Path('/root/bondok-deploy/backups')
TABLES = ('ops_items', 'ops_reservations', 'ops_attempts', 'ops_commands', 'ops_outbox', 'ops_audit', 'ops_policies')


def counts(db):
    c = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    try:
        out = {t: c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in TABLES}
        out['runtime_owner'] = (c.execute("SELECT value FROM ops_meta WHERE key='runtime_owner'").fetchone() or [None])[0]
        out['integrity'] = c.execute('PRAGMA integrity_check').fetchone()[0]
        return out
    finally:
        c.close()


def preflight(db):
    c = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    try:
        now = datetime.now(timezone.utc)
        soon = [r[0] for r in c.execute(
            "SELECT r.slot FROM ops_reservations r JOIN ops_items i USING(item_id) WHERE i.publication='not_started'")
            if now - timedelta(minutes=5) < datetime.fromisoformat(r[0].replace('Z', '+00:00')) < now + timedelta(minutes=20)]
        live = c.execute("SELECT COUNT(*) FROM ops_attempts WHERE stage NOT IN ('published','failed','abandoned',"
                         "'outcome_unknown','owner_unpublished')").fetchone()[0]
    finally:
        c.close()
    problems = []
    if soon:
        problems.append(f'reserved slot within 20 minutes: {soon}')
    if live:
        problems.append(f'{live} publication attempt(s) in progress')
    return problems


def run(*cmd):
    print('+', ' '.join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--units', required=True, help='directory with the new bondok.service and waset-runtime.service')
    ap.add_argument('--tombstone', required=True)
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()
    if os.geteuid() != 0:
        sys.exit('run as root')
    if NEW.exists() and any(NEW.iterdir()):
        sys.exit(f'{NEW} already has content; nothing moved')
    db = OLD / 'state.sqlite'
    before = counts(db)
    problems = preflight(db)
    print(json.dumps({'before': before, 'problems': problems, 'entries': sorted(p.name for p in OLD.iterdir())[:20]},
                     indent=1))
    for u in UNITS:
        text = (Path(a.units) / u).read_text()
        if 'BindPaths' in text or 'waset_social_data' in text:
            sys.exit(f'{u} still binds the old volume')
    if problems or before['integrity'] != 'ok':
        sys.exit('preflight failed')
    if not a.apply:
        print('dry run: nothing changed')
        return
    ts = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    keep = BACKUPS / f'store-move-{ts}'
    keep.mkdir(parents=True)
    run('systemctl', 'stop', 'waset-runtime')
    run('systemctl', 'stop', 'bondok')
    for u in UNITS:
        shutil.copy2(f'/etc/systemd/system/{u}', keep / u)
    c = sqlite3.connect(db)
    c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    out = sqlite3.connect(keep / 'state.sqlite')
    c.backup(out)
    out.close()
    c.close()
    NEW.mkdir(mode=0o770, exist_ok=True)
    moved = []
    for p in sorted(OLD.iterdir()):
        os.rename(p, NEW / p.name)
        moved.append(p.name)
    (keep / 'moved.json').write_text(json.dumps(moved))
    run('chown', '-R', 'bondok:bondok', str(NEW))
    os.chmod(NEW, 0o770)
    shutil.copy2(a.tombstone, OLD / 'helper.py')
    os.chown(OLD / 'helper.py', 0, 0)
    os.chmod(OLD / 'helper.py', 0o644)
    after = counts(NEW / 'state.sqlite')
    if {k: v for k, v in after.items()} != before:
        print(json.dumps({'after': after}), flush=True)
        sys.exit('row counts differ after the move: services left stopped; roll back from ' + str(keep))
    for u in UNITS:
        shutil.copy2(Path(a.units) / u, f'/etc/systemd/system/{u}')
    run('systemctl', 'daemon-reload')
    run('systemctl', 'start', 'bondok')
    run('systemctl', 'start', 'waset-runtime')
    print(json.dumps({'after': after, 'backup': str(keep), 'moved': len(moved)}, indent=1))


if __name__ == '__main__':
    main()
