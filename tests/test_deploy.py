"""Release packaging and the guarded deployment tool (R5 LOW-19, R5 §6, directive §17 / Gate 6).

Everything runs against a temporary fake server layout: a data directory laid out like the live n8n volume
(helper.py + waset_ops/ + state.sqlite), a Bondok code directory with a canary .env and a venv, a Bondok database,
a fake service manager and a fake n8n CLI. No network, no production paths, no real credentials.
"""
import base64
import fcntl
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from support import ROOT

HELPER_SRC = ROOT / 'src' / 'helper.py'
CANARY = 'CANARY-SLACK-TOKEN-must-never-ship'


def load_deploy():
    spec = importlib.util.spec_from_file_location('waset_deploy', ROOT / 'deploy' / 'deploy.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def git(repo, *args):
    return subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def make_repo(dest: Path) -> Path:
    """A committed copy of this working tree (the release is built from commits, never from loose files)."""
    for d in ('src', 'bondok', 'deploy', 'workflows'):
        shutil.copytree(ROOT / d, dest / d, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.env',
                                                                           'deploy_*.sh', 'import_*.sh'))
    (dest / '.gitignore').write_text((ROOT / '.gitignore').read_text())
    git(dest, 'init', '-q')
    git(dest, 'config', 'user.email', 'release@test.invalid')
    git(dest, 'config', 'user.name', 'release test')
    git(dest, 'add', '-A')
    git(dest, 'commit', '-q', '-m', 'release under test')
    return dest


def build_release(repo: Path, out: Path, *extra):
    p = subprocess.run([sys.executable, str(repo / 'deploy' / 'build_release.py'), '--repo', str(repo),
                        '--out', str(out), *extra], capture_output=True, text=True, timeout=300)
    return p


class ReleaseFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp_cls = tempfile.TemporaryDirectory()
        base = Path(cls.tmp_cls.name)
        cls.repo = make_repo(base / 'repo')
        # Untracked, gitignored files that must never ship.
        (cls.repo / 'bondok' / '.env').write_text(f'SLACK_BOT_TOKEN={CANARY}\n')
        (cls.repo / 'bondok' / 'scratch.sqlite').write_bytes(b'SQLite format 3\x00' + CANARY.encode())
        cls.site = base / 'private-site.json'
        cls.site.write_text(json.dumps({'IG_ACCOUNT': '17800000000000001'}))
        p = build_release(cls.repo, base / 'release', '--site', str(cls.site))
        if p.returncode:
            raise AssertionError(p.stdout + p.stderr)
        cls.release = Path(p.stdout.strip().splitlines()[-1])
        cls.manifest = json.loads((cls.release / 'MANIFEST.json').read_text())

    @classmethod
    def tearDownClass(cls):
        cls.tmp_cls.cleanup()


class R5_LOW19_ReleasePackaging(ReleaseFixture):
    def test_canary_env_and_local_files_never_enter_the_release(self):
        names = [p.relative_to(self.release).as_posix() for p in self.release.rglob('*') if p.is_file()]
        self.assertFalse([n for n in names if '.env' in n or n.endswith('.sqlite')], names)
        for p in self.release.rglob('*'):
            if p.is_file():
                self.assertNotIn(CANARY.encode(), p.read_bytes(), p)
        self.assertEqual(set(self.manifest['files']), set(names) - {'MANIFEST.json'})

    def test_allowlist_only(self):
        tops = {n.split('/')[0] for n in self.manifest['files']}
        self.assertEqual(tops, {'helper', 'bondok', 'workflows'})
        for n in self.manifest['files']:
            self.assertRegex(n, r'^(helper/(helper\.py|drain_probe\.py|RELEASE\.json|waset_ops/[a-z_]+\.py)|'
                                r'bondok/([a-z_]+\.py|policy\.txt|RELEASE\.json|waset_ops/[a-z_]+\.py)|'
                                r'workflows/[A-Za-z0-9_]+\.v2\.json)$')
        rel = json.loads((self.release / 'helper' / 'RELEASE.json').read_text())
        self.assertEqual(rel['release'], self.manifest['release'])
        self.assertEqual(rel['schema_version'], self.manifest['schema_version'])

    def test_deployment_ids_are_placeholders_filled_only_in_the_release(self):      # R5 LOW-18
        self.assertIn("ACCOUNT = '<IG_ACCOUNT>'", (self.repo / 'src' / 'waset_ops' / 'rules.py').read_text())
        for p in self.release.rglob('*'):
            if p.is_file():
                self.assertNotIn(b'<IG_ACCOUNT>', p.read_bytes(), p)
        self.assertIn('17800000000000001', (self.release / 'helper' / 'waset_ops' / 'rules.py').read_text())
        self.assertIn('/v26.0/17800000000000001/media', next(self.release.glob('workflows/pUIshuf16zIYoYRz*')).read_text())
        p = build_release(self.repo, Path(self.tmp_cls.name) / 'release-nosite')
        self.assertEqual(p.returncode, 0, p.stderr)
        m = json.loads((Path(p.stdout.strip().splitlines()[-1]) / 'MANIFEST.json').read_text())
        self.assertFalse(m['deployable']['code'])
        self.assertFalse(m['deployable']['workflows'])
        self.assertEqual(m['unresolved_placeholders'], ['IG_ACCOUNT'])

    def test_redacted_credentials_are_not_deployable(self):
        self.assertTrue(self.manifest['unresolved_credentials'])
        self.assertFalse(self.manifest['deployable']['workflows'])
        self.assertTrue(self.manifest['deployable']['code'])
        self.assertTrue(any('credential' in b for b in self.manifest['blockers']))

    def test_uncommitted_changes_or_stale_exports_refuse_to_build(self):
        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(Path(d) / 'repo')
            (repo / 'src' / 'helper.py').write_text((repo / 'src' / 'helper.py').read_text() + '\n# local edit\n')
            p = build_release(repo, Path(d) / 'out1')
            self.assertNotEqual(p.returncode, 0)
            self.assertIn('uncommitted', p.stdout + p.stderr)
            git(repo, 'checkout', '--', 'src/helper.py')
            dist = next((repo / 'workflows' / 'dist').glob('*.json'))
            dist.write_text(dist.read_text().replace('"active": false', '"active": false ', 1))
            git(repo, 'commit', '-qam', 'hand edit of a generated export')
            p = build_release(repo, Path(d) / 'out2')
            self.assertNotEqual(p.returncode, 0)
            self.assertIn('stale', p.stdout + p.stderr)


# ----------------------------------------------------------------------------------- fake server layout
SVC = r"""
import pathlib, sys
state = pathlib.Path(sys.argv[1]); cmd = sys.argv[2]
if cmd == 'stop': state.write_text('inactive')
elif cmd == 'start': state.write_text('active')
elif cmd == 'is-active': print(state.read_text().strip() if state.exists() else 'inactive')
"""

FAKE_N8N = r"""
import json, pathlib, sys, time
store = pathlib.Path(sys.argv[1]); store.mkdir(exist_ok=True); op = sys.argv[2]
args = dict(a.split('=', 1) for a in sys.argv[3:] if '=' in a)
if op == 'export':
    src = store / (args['--id'] + '.json')
    if not src.exists(): sys.exit('not found')
    pathlib.Path(args['--output']).write_text(src.read_text())
elif op == 'import':
    w = json.loads(pathlib.Path(args['--input']).read_text())
    old = store / (w['id'] + '.json')
    prev = json.loads(old.read_text()) if old.exists() else {}
    w['active'] = prev.get('active', False)                 # importing never changes activation (n8n 2.x drafts)
    w['activeVersionId'] = prev.get('activeVersionId')
    w['versionId'] = 'imported-%d' % (len(list(store.glob('*.json'))) + int(time.time() * 1000))
    if (store / 'TAMPER').exists():
        w['nodes'][0].pop('onError', None); w['nodes'][0]['parameters'] = {'tampered': True}
    (store / 'IMPORTED').write_text('yes')
    old.write_text(json.dumps(w))
"""


class Layout:
    def __init__(self, base: Path, release: Path):
        self.base, self.release = base, release
        self.data = base / 'n8n-volume'
        self.code = base / 'opt' / 'waset-bondok'
        self.bondok_db = base / 'var' / 'bondok' / 'bondok.sqlite'
        self.unit = base / 'etc' / 'bondok.service'
        self.service = base / 'service.state'
        self.store = base / 'n8n-store'
        self.cfg_path = base / 'private' / 'deploy.json'
        for d in (self.data, self.code, self.bondok_db.parent, self.unit.parent, self.cfg_path.parent):
            d.mkdir(parents=True, exist_ok=True)
        (base / 'svc.py').write_text(SVC)
        (base / 'fake_n8n.py').write_text(FAKE_N8N)

    def install_live(self):
        """Today's production layout: a regular helper.py beside waset_ops/ in the data directory."""
        shutil.copy(HELPER_SRC, self.data / 'helper.py')
        with (self.data / 'helper.py').open('a') as f:
            f.write('\n# live release before this deployment\n')
        shutil.copytree(ROOT / 'src' / 'waset_ops', self.data / 'waset_ops',
                        ignore=shutil.ignore_patterns('__pycache__'))
        sys.path.insert(0, str(ROOT / 'src'))
        from waset_ops import Ops
        o = Ops(self.data / 'state.sqlite')
        o.store.migrate()
        for f in (ROOT / 'bondok').glob('*.py'):
            shutil.copy(f, self.code / f.name)
        shutil.copy(ROOT / 'bondok' / 'policy.txt', self.code / 'policy.txt')
        shutil.copytree(ROOT / 'src' / 'waset_ops', self.code / 'waset_ops', ignore=shutil.ignore_patterns('__pycache__'))
        (self.code / '.env').write_text(f'SLACK_BOT_TOKEN={CANARY}\n')
        os.chmod(self.code / '.env', 0o600)
        (self.code / 'venv' / 'lib').mkdir(parents=True)
        (self.code / 'venv' / 'lib' / 'site.txt').write_text('runtime dependency')
        with sqlite3.connect(self.bondok_db) as c:
            c.execute('CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT)')
            c.execute("INSERT INTO kv VALUES('errors_offset','42')")
        self.unit.write_text('[Service]\nExecStart=/bin/true\n')
        self.service.write_text('active')
        return self

    def config(self, **over):
        svc = [sys.executable, str(self.base / 'svc.py'), str(self.service)]
        n8n = [sys.executable, str(self.base / 'fake_n8n.py'), str(self.store)]
        cfg = {
            'state_dir': str(self.base / 'deploy-state'), 'backup_root': str(self.base / 'backups'),
            'lock_file': str(self.base / 'deploy.lock'),
            'helper': {'data_dir': str(self.data), 'runner': [sys.executable], 'runner_data_dir': str(self.data),
                       'runner_env': {'WASET_SOCIAL_DATA_DIR': str(self.data)}},
            'host_python': sys.executable,
            'bondok': {'code_dir': str(self.code), 'python': sys.executable, 'db': str(self.bondok_db),
                       'unit_files': [str(self.unit)], 'stop': [svc + ['stop']], 'start': [svc + ['start']],
                       'health': {'command': svc + ['is-active'], 'expect_stdout': 'active'}},
            'drain': {'timeout_seconds': 3, 'poll_seconds': 0.2},
            'n8n': {'export': n8n + ['export', '--id={id}', '--output={file}'],
                    'import': n8n + ['import', '--input={file}'],
                    'file_dir': str(self.base / 'n8n-files'), 'runner_file_dir': str(self.base / 'n8n-files'),
                    'credentials': {}},
        }
        for k, v in over.items():
            cfg[k] = v
        self.cfg_path.write_text(json.dumps(cfg))
        return self.cfg_path

    # ------------------------------------------------------------------ observations
    def helper_call(self, path, body=None):
        arg = base64.b64encode(json.dumps({'path': path, 'body': body or {}}).encode()).decode()
        p = subprocess.run([sys.executable, str(self.data / 'helper.py'), arg], capture_output=True, text=True,
                           env={**os.environ, 'WASET_SOCIAL_DATA_DIR': str(self.data)}, timeout=60)
        return json.loads(p.stdout)

    def code_hashes(self):
        import hashlib
        out = {}
        for root in (self.data, self.code):
            for p in sorted(root.rglob('*')):
                if p.is_file() and '__pycache__' not in p.parts and p.suffix in ('.py', '.txt', '.json') \
                        and 'releases' not in p.relative_to(root).parts[:1] and p.name != 'deploy-fence.json':
                    out[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
        out['helper.py is a link'] = (self.data / 'helper.py').is_symlink()
        return out


class DeployCase(ReleaseFixture):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lay = Layout(Path(self.tmp.name), self.release).install_live()
        self.cfg = self.lay.config()
        self.deploy = load_deploy()
        baseline = self.run_tool('snapshot-live')
        self.assertEqual(baseline[0], 0, baseline)
        self.baseline = Path(self.tmp.name) / 'private' / 'reviewed-baseline.json'
        self.baseline.write_text(baseline[1])

    def tearDown(self):
        self.tmp.cleanup()

    def run_tool(self, *args, expect_live=False):
        argv = [args[0], '--config', str(self.cfg), *args[1:]]
        if expect_live:
            argv += ['--expect-live', str(self.baseline)]
        out = []
        with mock.patch('builtins.print', lambda *a, **k: out.append(' '.join(str(x) for x in a))):
            code = self.deploy.main(argv)
        return code, '\n'.join(out)

    def deploy_code(self):
        return self.run_tool('code', '--release', str(self.release), expect_live=True)

    def journal(self):
        p = Path(self.tmp.name) / 'deploy-state' / 'journal.jsonl'
        return [json.loads(x)['step'] for x in p.read_text().splitlines()] if p.exists() else []

    def hold_wf1_lease(self, seconds=3600):
        with sqlite3.connect(self.lay.data / 'state.sqlite') as c:
            c.execute("INSERT OR REPLACE INTO ops_runs(kind,run_id,fence,lease_until,started,heartbeat) "
                      "VALUES('wf1','wf1-busy',1,?,?,?)", (time.time() + seconds, time.time() - 5, time.time()))


class R5_Deploy_SuccessfulSwitch(DeployCase):
    def test_complete_deploy_switches_both_services_and_records_the_release(self):
        code, out = self.deploy_code()
        self.assertEqual(code, 0, out)
        rid = self.manifest['release']
        self.assertTrue((self.lay.data / 'helper.py').is_symlink())
        self.assertEqual(os.readlink(self.lay.data / 'helper.py'), f'releases/{rid}/helper.py')
        health = self.lay.helper_call('/v2/health')
        self.assertTrue(health['ok'])
        self.assertEqual(health['helper']['release']['release'], rid)
        self.assertIsNone(health['deploy_fence'])                         # writers released at the end
        self.assertEqual((self.lay.code / '.env').read_text(), f'SLACK_BOT_TOKEN={CANARY}\n')   # kept, not shipped
        self.assertEqual((self.lay.code / 'venv' / 'lib' / 'site.txt').read_text(), 'runtime dependency')
        self.assertEqual(json.loads((self.lay.code / 'RELEASE.json').read_text())['release'], rid)
        self.assertEqual(self.lay.service.read_text(), 'active')
        cur = json.loads((Path(self.tmp.name) / 'deploy-state' / 'current.json').read_text())
        self.assertEqual(cur['release'], rid)
        self.assertIn('interval', out)                                    # the cross-service interval is stated
        steps = self.journal()
        for s in ('locked', 'backup_verified', 'staged', 'fenced', 'drained', 'switched', 'verified', 'unfenced'):
            self.assertIn(s, steps)
        self.assertLess(steps.index('backup_verified'), steps.index('fenced'))
        self.assertLess(steps.index('drained'), steps.index('switched'))
        # A second deployment of the same release is checked against the recorded current release (no baseline).
        code, out = self.run_tool('code', '--release', str(self.release))
        self.assertEqual(code, 0, out)


class R5_Deploy_Backup(DeployCase):
    def test_backup_is_complete_version_correct_and_restorable(self):
        with sqlite3.connect(self.lay.data / 'state.sqlite') as c:
            c.execute('PRAGMA journal_mode=WAL')
        writer = sqlite3.connect(self.lay.data / 'state.sqlite', isolation_level=None)
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute("INSERT INTO ops_heartbeat VALUES('wal-only-row', 1, NULL)")   # only in the WAL so far
        try:
            code, out = self.run_tool('backup')
        finally:
            writer.close()
        self.assertEqual(code, 0, out)
        bdir = Path(out.strip().splitlines()[-1])
        m = json.loads((bdir / 'MANIFEST.json').read_text())
        files = set(m['files'])
        self.assertIn('helper/helper.py', files)                          # the real name, not helper.v1.py
        self.assertFalse([f for f in files if 'v1' in f])
        self.assertTrue({'helper/waset_ops/core.py', 'state.sqlite', 'bondok.sqlite', 'bondok-code.tar.gz',
                         'units/bondok.service', 'helper_layout.json', 'permissions.json'} <= files, files)
        self.assertEqual(m['identity']['helper']['schema_version'], 1)
        self.assertEqual(stat_mode(bdir), 0o700)
        with sqlite3.connect(bdir / 'state.sqlite') as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM ops_heartbeat WHERE name='wal-only-row'").fetchone()[0], 1)
        rt = json.loads((bdir / 'RESTORE_TEST.json').read_text())
        self.assertTrue(rt['ok'], rt)
        self.assertEqual(rt['checks']['state.sqlite']['integrity'], 'ok')
        self.assertTrue(rt['checks']['helper_import']['ok'])
        self.assertIn('.env', rt['checks']['bondok-code.tar.gz']['members'])          # restorable, stays on host
        (bdir / 'helper' / 'waset_ops' / 'rules.py').write_text('# damaged\n')       # a damaged backup fails
        code, out = self.run_tool('verify-backup', '--backup', str(bdir))
        self.assertNotEqual(code, 0)


def stat_mode(p):
    return os.stat(p).st_mode & 0o777


class R5_Deploy_Gates(DeployCase):
    def test_fence_wait_timeout_fails_closed_without_switch(self):
        self.hold_wf1_lease()
        before = self.lay.code_hashes()
        code, out = self.deploy_code()
        self.assertEqual(code, 3, out)
        self.assertEqual(self.lay.code_hashes(), before)                  # nothing switched
        self.assertFalse((self.lay.data / 'deploy-fence.json').exists())  # fence removed
        self.assertEqual(self.lay.service.read_text(), 'active')          # Bondok restarted as it was
        self.assertIn('drain_timeout', self.journal())
        self.assertNotIn('switched', self.journal())

    def test_helper_honours_the_fence_while_draining(self):
        self.hold_wf1_lease(seconds=60)
        (self.lay.data / 'deploy-fence.json').write_text(json.dumps({'release': 'x', 'since': time.time()}))
        r = self.lay.helper_call('/v2/run/start', {'kind': 'wf3', 'runId': 'wf3-new'})
        self.assertEqual((r['ok'], r['acquired'], r.get('maintenance')), (True, False, True))
        self.assertEqual(self.lay.helper_call('/v2/publish/due', {'worker': 'w'})['work'], [])
        r = self.lay.helper_call('/v2/publish/claim', {'itemId': '1', 'worker': 'w'})
        self.assertEqual((r['ok'], r['kind']), (False, 'maintenance'))
        r = self.lay.helper_call('/v2/board/missing', {'ids': ['1'], 'run': {'kind': 'wf1', 'id': 'wf1-busy', 'fence': 1}})
        self.assertTrue(r['ok'], r)                                       # the run in flight may finish
        r = self.lay.helper_call('/v2/run/finish', {'kind': 'wf1', 'runId': 'wf1-busy', 'fence': 1})
        self.assertTrue(r['released'])
        probe = subprocess.run([sys.executable, str(ROOT / 'deploy' / 'drain_probe.py'), str(self.lay.data)],
                               capture_output=True, text=True, check=True)
        self.assertTrue(json.loads(probe.stdout)['drained'])

    def test_lock_held_by_another_deployment_refuses(self):
        cfg = json.loads(self.cfg.read_text())
        with open(cfg['lock_file'], 'a') as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, out = self.deploy_code()
        self.assertEqual(code, 2, out)
        self.assertFalse((self.lay.data / 'deploy-fence.json').exists())

    def test_leftover_fence_refuses(self):
        (self.lay.data / 'deploy-fence.json').write_text('{"release":"other","since":1}')
        code, out = self.deploy_code()
        self.assertEqual(code, 2, out)
        self.assertTrue((self.lay.data / 'deploy-fence.json').exists())   # not ours to remove

    def test_unknown_live_drift_refuses(self):
        with (self.lay.data / 'waset_ops' / 'rules.py').open('a') as f:
            f.write('\n# manual production edit\n')
        before = self.lay.code_hashes()
        code, out = self.deploy_code()
        self.assertEqual(code, 2, out)
        self.assertIn('drift', out)
        self.assertEqual(self.lay.code_hashes(), before)

    def test_first_deploy_needs_a_reviewed_baseline(self):
        code, out = self.run_tool('code', '--release', str(self.release))
        self.assertEqual(code, 2, out)
        self.assertIn('baseline', out)

    def test_pre_switch_hash_failure_refuses_before_any_change(self):
        with tempfile.TemporaryDirectory() as d:
            tampered = Path(d) / 'release'
            shutil.copytree(self.release, tampered)
            with (tampered / 'helper' / 'waset_ops' / 'publish.py').open('a') as f:
                f.write('\n# changed after the build\n')
            before = self.lay.code_hashes()
            code, out = self.run_tool('code', '--release', str(tampered), expect_live=True)
        self.assertEqual(code, 2, out)
        self.assertIn('publish.py', out)
        self.assertEqual(self.lay.code_hashes(), before)
        self.assertFalse((self.lay.data / 'deploy-fence.json').exists())

    def test_release_for_another_instagram_account_refuses_before_any_change(self):      # R5 LOW-18
        with sqlite3.connect(self.lay.data / 'state.sqlite') as c:
            c.execute("INSERT INTO ops_items(item_id, name, format) VALUES('9', 'x', 'Story')")
            c.execute("INSERT INTO ops_reservations(item_id, account, format, slot, content_rev, payload_fp, origin) "
                      "VALUES('9', '17899999999999999', 'Story', '2026-10-11T11:00:00Z', 1, 'fp', 'auto')")
        before = self.lay.code_hashes()
        code, out = self.run_tool('code', '--release', str(self.release), expect_live=True)
        self.assertEqual(code, 2, out)
        self.assertIn('Instagram account', out)
        self.assertEqual(self.lay.code_hashes(), before)
        self.assertFalse((self.lay.data / 'deploy-fence.json').exists())

    def test_staged_copy_mismatch_refuses_before_the_switch(self):
        real = self.deploy.Deployer.stage_helper

        def corrupt(dep, *a, **k):
            staged = real(dep, *a, **k)
            os.chmod(staged / 'waset_ops' / 'items.py', 0o644)
            with (staged / 'waset_ops' / 'items.py').open('a') as f:
                f.write('\n# corrupted while staging\n')
            return staged
        before = self.lay.code_hashes()
        with mock.patch.object(self.deploy.Deployer, 'stage_helper', corrupt):
            code, out = self.deploy_code()
        self.assertEqual(code, 2, out)
        self.assertEqual(self.lay.code_hashes(), before)
        self.assertNotIn('fenced', self.journal())


class R5_Deploy_Rollback(DeployCase):
    def switch_then(self, damage):
        real = self.deploy.Deployer.switch

        def switch(dep, *a, **k):
            r = real(dep, *a, **k)
            damage(dep)
            return r
        return mock.patch.object(self.deploy.Deployer, 'switch', switch)

    def test_post_switch_hash_mismatch_rolls_back_and_keeps_newer_receipts(self):
        before = self.lay.code_hashes()

        def damage(dep):
            rid = self.manifest['release']
            f = self.lay.data / 'releases' / rid / 'waset_ops' / 'sched.py'
            os.chmod(f, 0o644)
            with f.open('a') as fh:
                fh.write('\n# changed under the switch\n')
            with sqlite3.connect(self.lay.data / 'state.sqlite') as c:          # evidence newer than the backup
                c.execute("INSERT INTO ops_attempts(id,item_id,content_rev,payload_fp,payload,slot,fence,stage,"
                          "media_id,created,updated) VALUES('att-new','42',1,'fp','{}','2026-10-10T19:00:00Z',1,"
                          "'published','17900',1,1)")
        with self.switch_then(damage):
            code, out = self.deploy_code()
        self.assertEqual(code, 4, out)
        self.assertEqual(self.lay.code_hashes(), before)                  # previous code restored exactly
        self.assertFalse((self.lay.data / 'helper.py').is_symlink())
        with sqlite3.connect(self.lay.data / 'state.sqlite') as c:        # database never restored over it
            self.assertEqual(c.execute("SELECT stage FROM ops_attempts WHERE id='att-new'").fetchone()[0], 'published')
        self.assertFalse((self.lay.data / 'deploy-fence.json').exists())
        self.assertEqual(self.lay.service.read_text(), 'active')
        self.assertIn('rolled_back', self.journal())
        self.assertTrue(self.lay.helper_call('/v2/health')['ok'])

    def test_failed_health_rolls_back(self):
        cfg = json.loads(self.cfg.read_text())
        cfg['bondok']['health'] = {'command': [sys.executable, '-c', 'print("failed")'], 'expect_stdout': 'active'}
        self.cfg.write_text(json.dumps(cfg))
        before = self.lay.code_hashes()
        code, out = self.deploy_code()
        self.assertEqual(code, 4, out)
        self.assertEqual(self.lay.code_hashes(), before)
        self.assertIn('health', out)

    def test_helper_health_fields_are_checked_not_just_the_exit_code(self):
        real = self.deploy.Deployer.helper_health

        def lying(dep, *a, **k):
            h = real(dep, *a, **k)
            h['schema'] = 999                                             # exit 0, ok:true, wrong schema
            return h
        before = self.lay.code_hashes()
        with mock.patch.object(self.deploy.Deployer, 'helper_health', lying):
            code, out = self.deploy_code()
        self.assertEqual(code, 4, out)
        self.assertEqual(self.lay.code_hashes(), before)

    def test_database_newer_than_the_old_code_is_contained_not_revived(self):
        def damage(dep):
            with sqlite3.connect(self.lay.data / 'state.sqlite') as c:
                c.execute("UPDATE ops_meta SET value='7' WHERE key='schema_version'")
            f = self.lay.data / 'releases' / self.manifest['release'] / 'helper.py'
            os.chmod(f, 0o644)
            with f.open('a') as fh:
                fh.write('\n# broken\n')
        with self.switch_then(damage):
            code, out = self.deploy_code()
        self.assertEqual(code, 5, out)
        self.assertTrue((self.lay.data / 'deploy-fence.json').exists())    # writers stay fenced
        self.assertEqual(self.lay.service.read_text(), 'inactive')        # old Bondok not revived
        self.assertIn('contained', out.lower())


class R5_Deploy_Workflows(DeployCase):
    def setUp(self):
        super().setUp()
        self.lay.store.mkdir()
        for p in (self.release / 'workflows').glob('*.json'):
            w = json.loads(p.read_text())
            live = {**w, 'versionId': w['meta']['built_from'], 'active': True, 'activeVersionId': w['meta']['built_from']}
            (self.lay.store / (w['id'] + '.json')).write_text(json.dumps(live))
        self.creds = {}
        for p in (self.release / 'workflows').glob('*.json'):
            for n in json.loads(p.read_text())['nodes']:
                for t, c in (n.get('credentials') or {}).items():
                    self.creds[f"{t}:{c['name']}"] = 'cred-' + t

    def with_creds(self, creds):
        cfg = json.loads(self.cfg.read_text())
        cfg['n8n']['credentials'] = creds
        self.cfg.write_text(json.dumps(cfg))

    def test_unresolved_redacted_credential_blocks_the_import(self):
        code, out = self.run_tool('workflows', '--release', str(self.release))
        self.assertEqual(code, 2, out)
        self.assertIn('<REDACTED>', out)
        self.assertFalse((self.lay.store / 'IMPORTED').exists())

    def test_import_is_inactive_compared_and_never_activates(self):
        self.with_creds(self.creds)
        code, out = self.run_tool('workflows', '--release', str(self.release))
        self.assertEqual(code, 0, out)
        for p in (self.release / 'workflows').glob('*.json'):
            w = json.loads(p.read_text())
            got = json.loads((self.lay.store / (w['id'] + '.json')).read_text())
            self.assertEqual(got['activeVersionId'], w['meta']['built_from'])   # still the old active version
            for n in got['nodes']:
                for c in (n.get('credentials') or {}).values():
                    self.assertNotEqual(c['id'], '<REDACTED>')
        self.assertIn('activation is a separate', out)

    def test_import_that_does_not_match_the_artifact_is_reported_and_restored(self):
        self.with_creds(self.creds)
        (self.lay.store / 'TAMPER').write_text('x')
        code, out = self.run_tool('workflows', '--release', str(self.release))
        self.assertNotEqual(code, 0, out)
        self.assertIn('differs', out)

    def test_live_workflow_drift_refuses(self):
        self.with_creds(self.creds)
        p = next(self.lay.store.glob('*.json'))
        w = json.loads(p.read_text())
        w['versionId'] = 'someone-else-edited'
        p.write_text(json.dumps(w))
        code, out = self.run_tool('workflows', '--release', str(self.release))
        self.assertEqual(code, 2, out)
        self.assertIn('drift', out)
        self.assertFalse((self.lay.store / 'IMPORTED').exists())


if __name__ == '__main__':
    unittest.main()
