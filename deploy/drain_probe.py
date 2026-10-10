"""Read-only drain probe used by deploy/deploy.py before it switches code (stdlib only).

    python3 drain_probe.py <data dir>

Ships inside the helper bundle (releases/<id>/drain_probe.py) so it runs in the helper's own namespace (same
user, same view of state.sqlite) and does not depend on the version of the live helper: it never imports
waset_ops and never migrates. The connection is query-only.

Prints one JSON object. ``drained`` is true when no writer is working: no run lease (WF1/WF3) is live, no
publication attempt holds a live lease (pre-commit or committed), no outbox job is in flight and no detached
media job holds a capacity lock. Attempts whose lease already expired are listed as ``unresolved_attempts``:
their worker is gone; they are reconciled by the code that runs next and are never treated as canceled.
"""
import glob
import json
import os
import sqlite3
import sys
import time

try:
    import fcntl
except ImportError:          # pragma: no cover - not a Linux/macOS host
    fcntl = None


def media_jobs(data_dir):
    held = 0
    for path in sorted(glob.glob(os.path.join(data_dir, 'media-capacity-*.lock'))):
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        except BlockingIOError:
            held += 1
        finally:
            os.close(fd)
    return held


def probe(data_dir):
    now = time.time()
    db = os.path.join(data_dir, 'state.sqlite')
    out = {'as_of': now, 'fenced': os.path.exists(os.path.join(data_dir, 'deploy-fence.json'))}
    if not os.path.isfile(db):
        return {**out, 'drained': False, 'error': 'state.sqlite not found'}
    c = sqlite3.connect(db, timeout=30)
    try:
        c.execute('PRAGMA query_only=1')
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        runs, attempts, outbox, schema, legacy = [], [], 0, 0, []
        if 'ops_meta' in tables:
            row = c.execute("SELECT value FROM ops_meta WHERE key='schema_version'").fetchone()
            schema = int(row[0]) if row else 0
        if 'ops_runs' in tables:
            runs = [{'kind': k, 'run_id': r, 'lease_until': u} for k, r, u in
                    c.execute('SELECT kind, run_id, lease_until FROM ops_runs WHERE lease_until > ?', (now,))]
        if 'ops_attempts' in tables:
            attempts = [{'id': i, 'item_id': it, 'stage': s, 'lease_until': u} for i, it, s, u in c.execute(
                "SELECT id, item_id, stage, lease_until FROM ops_attempts "
                "WHERE stage IN ('claimed','container_created','committed')")]
        if 'ops_outbox' in tables:
            outbox = c.execute("SELECT COUNT(*) FROM ops_outbox WHERE state='in_flight' AND lease_until > ?",
                               (now,)).fetchone()[0]
        if 'publications' in tables:          # v1 publisher stages (informational: v1 is not a live writer)
            legacy = [{'item': i, 'stage': s} for i, s in c.execute(
                "SELECT item, stage FROM publications WHERE stage IN ('claimed','container_created','publish_requested')")]
    finally:
        c.close()
    live = [a for a in attempts if (a['lease_until'] or 0) > now]
    jobs = media_jobs(data_dir) if fcntl else 0
    return {**out, 'schema': schema,
            'drained': not runs and not live and not outbox and not jobs,
            'blocking': {'runs': runs, 'attempts': live, 'outbox_in_flight': outbox, 'media_jobs': jobs},
            'unresolved_attempts': [a for a in attempts if (a['lease_until'] or 0) <= now],
            'legacy_in_flight': legacy}


if __name__ == '__main__':
    print(json.dumps(probe(sys.argv[1])))
