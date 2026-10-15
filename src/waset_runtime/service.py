"""Bondok runtime service: the durable timer that replaces n8n's schedule triggers (agent migration P4-01).

    python3 -m waset_runtime.service [--env /opt/waset-bondok/runtime.env] [--once prepare|publish|supervise]
                                     [--dry-run]

* Cadence as in n8n: publish/display sync every minute, preparation every 10 minutes, supervision at :05 and :35.
  A cycle that is due while the previous one of the same kind still runs is skipped (n8n misfirePolicy 'skip'); a
  missed cycle after a restart runs at once (the handler's leases and windows decide what is still valid).
* Scheduled work runs only while the store says the Bondok runtime owns it (``Ops.runtime_owner()``, switched by
  the owner command ``set_runtime_owner``). Otherwise the service stays idle and says so in its heartbeat.
* No model is called by any cycle: authorized, recorded work continues when the chat or the model is down (A24).
* ``--dry-run`` never writes to Monday or Dropbox and never calls Instagram: reads are real, intended writes are
  logged (shadow comparison, P5-05). Use it against a copy of the store.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from waset_ops import Ops  # noqa: E402

LOG = logging.getLogger('waset_runtime')
CADENCE = {'sync': 60, 'publish': 60, 'prepare': 600, 'supervise': None}   # supervise: minutes 5 and 35
REQUIRED = ('PIPELINE_DB', 'MONDAY_API_TOKEN', 'INSTAGRAM_ACCOUNT_ID', 'INSTAGRAM_TOKEN')


def read_env(path) -> dict:
    out = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def build(env: dict, dry_run=False):
    from .captions import CaptionWriter
    from .cycles import Runner
    from .dropbox import Dropbox
    from .instagram import Instagram
    from .monday import Monday
    missing = [k for k in REQUIRED if not env.get(k)]
    if missing:
        raise SystemExit('missing configuration: ' + ', '.join(missing))
    db = env['PIPELINE_DB']
    if dry_run:
        db = shadow_copy(db)      # never the live store: a consistent copy (sqlite backup API)
        from waset_ops import media
        media.preflight = media.prepare = lambda b: {'ready': False, 'pending': True, 'reason': 'dry run'}
    from waset_ops import media
    os.environ['WASET_SOCIAL_DATA_DIR'] = str(Path(db).parent)
    media.ROOT = Path(db).parent                         # prepared files and job records live beside the store
    os.environ.setdefault('WASET_HELPER', str(HERE / 'helper.py'))   # detached media jobs run through the helper
    ops = Ops(db)
    ops.store.migrate()
    if not dry_run:
        detect_restore(ops, Path(db).parent / 'store-generation.json')
    monday = Monday(env['MONDAY_API_TOKEN'], boards=lambda: ops.policy_value('allowed_boards'))
    dropbox = Dropbox(token=env.get('DROPBOX_TOKEN'), refresh_token=env.get('DROPBOX_REFRESH_TOKEN'),
                      app_key=env.get('DROPBOX_APP_KEY'), app_secret=env.get('DROPBOX_APP_SECRET'))
    token = env['INSTAGRAM_TOKEN']
    if env.get('INSTAGRAM_TOKEN_KIND') == 'instagram_login':      # 60-day token: refreshed automatically
        from .instagram import TokenStore
        token = TokenStore(Path(env.get('INSTAGRAM_TOKEN_FILE') or Path(db).parent / 'instagram-token.json'), token)
    ig = Instagram(token, env['INSTAGRAM_ACCOUNT_ID'])
    captions = CaptionWriter(env['OPENROUTER_API_KEY']) if env.get('OPENROUTER_API_KEY') else None
    if dry_run:
        from .dry import dry
        monday, dropbox, ig, captions = dry(monday), dry(dropbox), dry(ig), None
    return ops, Runner(ops, monday, dropbox, ig, captions, name='bondok')


def latest_command(ops) -> float:
    with ops.store.read() as c:
        return float(c.execute('SELECT COALESCE(MAX(created), 0) FROM ops_commands').fetchone()[0] or 0)


def detect_restore(ops, marker: Path):
    """The store is older than what this host already saw (a backup was restored): open the restore gap by itself,
    so publishing waits for the Instagram check instead of relying on a manual --mark-restored (audit H4). The marker
    lives beside the store but is not part of it, so a restored store file does not bring an older marker."""
    try:
        seen = json.loads(marker.read_text()).get('last_command_at', 0)
    except (OSError, ValueError):
        return None
    store_last = latest_command(ops)
    with ops.store.read() as c:
        open_gap = ops.restore_gap(c)
    if store_last + 60 < float(seen) and not open_gap:
        LOG.warning('store_restored detected store_last=%s seen=%s', store_last, seen)
        return ops.mark_restored(store_last, actor='service:runtime')
    return None


def write_generation(ops, marker: Path):
    try:
        tmp = marker.with_suffix('.tmp')
        tmp.write_text(json.dumps({'last_command_at': latest_command(ops), 'at': time.time()}))
        os.replace(tmp, marker)
    except OSError as e:
        LOG.warning('generation_marker_failed detail=%s', str(e)[:120])


def shadow_copy(db) -> str:
    import sqlite3
    import tempfile
    d = Path(tempfile.mkdtemp(prefix='waset-shadow-'))
    src = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    dst = sqlite3.connect(d / 'state.sqlite')
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    LOG.info('dry run on a copy of the store: %s', d / 'state.sqlite')
    return str(d / 'state.sqlite')


class Scheduler:
    def __init__(self, ops, runner, *, clock=time.time, dry_run=False, data_dir=None):
        from concurrent.futures import ThreadPoolExecutor
        self.ops, self.runner, self.clock, self.dry_run = ops, runner, clock, dry_run
        self.data_dir = Path(data_dir) if data_dir else Path(str(ops.store.path)).parent
        self.last = {k: 0.0 for k in CADENCE}
        self.running = {k: False for k in CADENCE}
        self.stop = threading.Event()
        self.threads = []
        self.pool = ThreadPoolExecutor(max_workers=getattr(runner, 'publishers', 3), thread_name_prefix='publish')

    def fenced(self) -> bool:
        """A deployment is in progress (deploy.py writes deploy-fence.json beside the store): start nothing new."""
        return (self.data_dir / 'deploy-fence.json').exists()

    def due(self, kind, now) -> bool:
        if self.running[kind]:
            return False
        every = CADENCE[kind]
        if every:
            return now - self.last[kind] >= every
        # supervise at minutes 5 and 35 (n8n cron 0 5,35 * * * *), or at once when the last run is older than 30 min
        minute = int(now // 60) % 60
        return (minute in (5, 35) and now - self.last[kind] >= 120) or now - self.last[kind] >= 1800 + 120

    def tick(self) -> list[str]:
        """Start every due cycle; returns the kinds started."""
        owner = self.ops.runtime_owner()
        now = self.clock()
        self.threads = [t for t in self.threads if t.is_alive()]
        if owner != 'bondok' and not self.dry_run:
            self.ops.heartbeat('runtime', {'idle': True, 'owner': owner})
            return []
        if self.fenced():
            self.ops.heartbeat('runtime', {'idle': True, 'owner': owner, 'deploy_fence': True})
            return []
        started = []
        for kind in CADENCE:
            if self.due(kind, now):
                self.last[kind] = now
                self.running[kind] = True
                t = threading.Thread(target=self._run, args=(kind,), daemon=True, name='runtime-' + kind)
                t.start()
                self.threads.append(t)
                started.append(kind)
        self.ops.heartbeat('runtime', {'owner': owner, 'dry_run': self.dry_run,
                                       'running': [k for k, v in self.running.items() if v]})
        if not self.dry_run and self.data_dir is not None:
            write_generation(self.ops, self.data_dir / 'store-generation.json')
        return started

    def _run(self, kind):
        try:
            if kind == 'publish':
                out = self.runner.publish_cycle(pool=self.pool, sync=False)
            else:
                out = getattr(self.runner, kind + '_cycle')()
            LOG.info('cycle kind=%s result=%s', kind, json.dumps(_summary(out), ensure_ascii=False)[:400])
        except Exception as e:  # noqa: BLE001 - a failed cycle is recorded by the handler (run_fail) and retried
            LOG.error('cycle_failed kind=%s type=%s detail=%s', kind, type(e).__name__, str(e)[:200])
        finally:
            self.running[kind] = False

    def run_forever(self, interval=5):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 - e.g. the store is briefly unreadable
                LOG.error('tick_failed type=%s', type(e).__name__)
            self.stop.wait(interval)
        self.shutdown()

    def shutdown(self, timeout=900):
        """Let running cycles and publications finish (a publication cut mid-way becomes 'outcome unknown')."""
        deadline = time.monotonic() + timeout
        for t in list(self.threads):
            t.join(max(0.0, deadline - time.monotonic()))
        self.pool.shutdown(wait=True)


def _summary(out):
    if not isinstance(out, dict):
        return out
    return {k: (len(v) if isinstance(v, list) else v) for k, v in out.items() if k != 'maintenance'}


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('--env', default=os.environ.get('WASET_RUNTIME_ENV', '/opt/waset-bondok/runtime.env'))
    p.add_argument('--once', choices=sorted(CADENCE))
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--mark-restored', metavar='BACKUP_TIME',
                   help='after restoring a backup: its time (ISO, with zone); publishing pauses until the gap since '
                        'then is checked on Instagram')
    p.add_argument('--switch-owner', choices=['bondok', 'n8n'],
                   help='cutover/rollback step, run only with the owner\'s authorization: which runner executes the '
                        'scheduled work')
    p.add_argument('--authorized-by', help='reference of the owner authorization for --switch-owner')
    p.add_argument('--legacy-rollback', action='store_true',
                   help='only with --switch-owner n8n after the n8n workflows, credential and data were restored')
    a = p.parse_args(argv)
    if a.switch_owner:
        from waset_ops import OWNER, Command
        if not a.authorized_by:
            raise SystemExit('--switch-owner needs --authorized-by (the owner\'s authorization reference)')
        ops = Ops(read_env(a.env)['PIPELINE_DB'])
        print(json.dumps(ops.submit(Command(f'switch-owner:{a.switch_owner}:{int(time.time())}', 'set_runtime_owner',
                                            'operator', OWNER, None, {'owner': a.switch_owner,
                                                                      'legacy_rollback': bool(a.legacy_rollback)},
                                            auth={'authorized_by': a.authorized_by}))))
        return
    if a.mark_restored:
        from waset_ops import rules
        ops = Ops(read_env(a.env)['PIPELINE_DB'])
        print(json.dumps(ops.mark_restored(rules.instant(a.mark_restored).timestamp(), actor='operator:restore')))
        return
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    ops, runner = build(read_env(a.env), dry_run=a.dry_run)
    if a.once:
        if ops.runtime_owner() != 'bondok' and not a.dry_run:
            raise SystemExit('The Bondok runtime does not own scheduled work (runtime_owner is '
                             f'{ops.runtime_owner()}); use --dry-run or switch the owner first.')
        print(json.dumps(_summary(getattr(runner, a.once + '_cycle')()), ensure_ascii=False, default=str))
        return
    s = Scheduler(ops, runner, dry_run=a.dry_run)
    signal.signal(signal.SIGTERM, lambda *_: s.stop.set())
    LOG.info('runtime ready owner=%s dry_run=%s', ops.runtime_owner(), a.dry_run)
    s.run_forever()


if __name__ == '__main__':
    main()
