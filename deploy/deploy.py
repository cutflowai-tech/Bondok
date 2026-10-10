"""Guarded deployment of a built release: helper bundle + Bondok code, and inactive workflow import.

Run ON THE SERVER as root, only with the owner's authorization for the specific change set. Nothing here
hard-codes a server address, container name, project id, credential id or path: every environment value comes
from a private JSON config that is never committed (template: deploy/deploy.example.json).

  deploy.py snapshot-live --config C                  live hashes of the managed code (review it -> baseline)
  deploy.py backup        --config C                  complete backup + restore verification
  deploy.py verify-backup --config C --backup DIR     restore verification of an existing backup
  deploy.py code          --config C --release DIR [--expect-live BASELINE]
  deploy.py workflows     --config C --release DIR    import INACTIVE + compare with the artifact (never activates)
  deploy.py status        --config C                  fence, drain state, recorded release
  deploy.py unfence       --config C --reason TEXT    remove a fence left by an interrupted deployment

`code` runs these gates in order; any failure before the switch removes the fence it set and exits non-zero:
  1. lock (no concurrent deployment) and no fence already up (another or an interrupted deployment);
  2. release verified against its manifest (every hash; code deployable);
  3. live code equals the recorded current release (state_dir/current.json), or, for the first deployment, a
     reviewed baseline (--expect-live, produced by snapshot-live). Unknown drift fails the gate;
  4. complete backup (helper + waset_ops as found, Bondok code incl. .env, bondok.sqlite and state.sqlite via
     the SQLite backup API, unit files, permissions/ACLs, live workflows if n8n is configured) and its restore
     test (hashes, integrity_check, import of the backed-up package);
  5. staging of the complete release beside the live code (helper: <data>/releases/<id>/, immutable; Bondok:
     <code dir>.next-<id>, runtime files such as .env and venv hard-linked from live) and, still before any
     switch: staged hashes, import tests (helper in its own runtime; Bondok with its venv) and a helper health
     run on a SQLite copy of the live database;
  6. fence: <data>/deploy-fence.json (the helper defers new runs, empties queues, refuses claims; the run and
     attempts already in flight may finish), Bondok stopped, then wait until the drain probe reports no live
     run lease, attempt lease, in-flight outbox job or media job. Timeout = FAIL CLOSED: fence removed, Bondok
     restarted, exit 3, nothing switched;
  7. switch: the helper is ONE atomic rename (helper.py becomes a relative symlink to releases/<id>/helper.py;
     every helper process started afterwards runs the new release completely); Bondok is two directory
     renames while it is stopped. Cross-service interval (printed with timestamps): from the helper rename to
     the Bondok start, Bondok is down and the n8n writers are fenced, so no two code versions run unfenced;
  8. live hashes after the switch, then health JSON fields (helper: ok, schema, release id; Bondok: configured
     check). Any failure: automatic rollback to the previous code, verified by hash. The databases are never
     restored (newer publication evidence and owner decisions are kept); if the database is newer than the
     old code supports, or the rollback cannot be verified, the writers stay fenced and Bondok stays stopped.

Exit codes: 0 done; 2 refused, nothing changed; 3 drain timeout, nothing switched, fence removed;
4 failed after the switch, previous code restored and verified, fence removed; 5 contained: the writers stay
fenced and Bondok stopped; operator action required (printed).
What this tool cannot guarantee: see the "Limits" section printed by `status` and docs/WORKFLOW_CHANGES.md.
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

FENCE_FILE = 'deploy-fence.json'
HELPER_TOP = ('helper.py', 'RELEASE.json', 'drain_probe.py')
BONDOK_TOP = re.compile(r'[A-Za-z0-9_]+\.py|policy\.txt|RELEASE\.json')
REDACTED = '<REDACTED>'
SQLITE_BACKUP = ('import sqlite3,sys\n'
                 's=sqlite3.connect(sys.argv[1],timeout=60)\n'
                 'd=sqlite3.connect(sys.argv[2])\n'
                 's.backup(d)\nd.close()\ns.close()\n')
HELPER_IMPORT = ('import json,sys\nsys.path.insert(0,sys.argv[1])\n'
                 'import helper,waset_ops\nfrom waset_ops.db import SCHEMA_VERSION\n'
                 'print(json.dumps({"version":waset_ops.__version__,"schema":SCHEMA_VERSION,'
                 '"release":helper.release_info(),"file":helper.__file__}))\n')
LIMITS = """Limits (not guaranteed by this tool):
* A helper process that started before the switch finishes on the old code (the drain waits for every lease,
  not for processes outside the store, e.g. a Code node that has not yet called the helper).
* The first deployment from a helper that does not know the fence relies on the drain only (the old helper
  ignores deploy-fence.json): deactivate WF1/WF2/WF3 first, or accept that a run starting during the wait
  extends it (the timeout still fails closed).
* Workflow activation, n8n's own execution queue and Monday/Instagram side effects are outside its control.
* A host crash between the two Bondok renames leaves <code dir>.prev-* and no <code dir>: restore by renaming
  (recorded in the journal)."""


class Refused(Exception):
    """A gate failed before anything changed (exit 2)."""


class DrainTimeout(Exception):
    pass


def say(*parts):
    print(*parts)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def utc(ts=None) -> str:
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def stamp() -> str:
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def write_private(path: Path, text: str, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('.' + path.name + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, 'w') as f:
        f.write(text)
    os.replace(tmp, path)


def package_files(base: Path, rel_prefix: str, top: list[str]) -> dict[str, Path]:
    """Managed code files under base: the given top-level names plus waset_ops/**/*.py (no caches)."""
    out = {}
    for name in top:
        p = base / name
        if p.is_file():
            out[f'{rel_prefix}/{name}'] = p
    pkg = base / 'waset_ops'
    if pkg.is_dir():
        for p in sorted(pkg.rglob('*')):
            if p.is_file() and '__pycache__' not in p.parts and not p.name.endswith('.pyc'):
                out[f'{rel_prefix}/waset_ops/{p.relative_to(pkg).as_posix()}'] = p
    return out


class Deployer:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.state_dir = Path(cfg['state_dir'])
        self.backup_root = Path(cfg['backup_root'])
        h = cfg['helper']
        self.data = Path(h['data_dir'])
        self.runner = list(h.get('runner') or ['python3'])
        self.runner_data = h.get('runner_data_dir') or str(self.data)
        self.runner_env = h.get('runner_env') or {}
        b = cfg.get('bondok') or None
        self.bondok = b
        self.code_dir = Path(b['code_dir']) if b else None
        self.host_python = cfg.get('host_python') or sys.executable
        self.lock_fd = None
        self.fence_ours = False

    # ------------------------------------------------------------------ plumbing
    def journal(self, step, **detail):
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(self.state_dir / 'journal.jsonl', 'a') as f:
            f.write(json.dumps({'at': utc(), 'step': step, **detail}, default=str) + '\n')
        os.chmod(self.state_dir / 'journal.jsonl', 0o600)

    def run(self, argv, *, env=None, timeout=900, check=False):
        e = {**os.environ, **(env or {})}
        p = subprocess.run([str(a) for a in argv], capture_output=True, text=True, env=e, timeout=timeout)
        if check and p.returncode:
            raise Refused(f'command failed ({p.returncode}): {argv[0]} … {p.stderr.strip()[-400:]}')
        return p

    def runner_path(self, host_path: Path) -> str:
        rel = Path(host_path).resolve().relative_to(self.data.resolve())
        return self.runner_data.rstrip('/') + '/' + rel.as_posix()

    def lock(self):
        lf = Path(self.cfg['lock_file'])
        lf.parent.mkdir(parents=True, exist_ok=True)
        self.lock_fd = open(lf, 'a')
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock_fd.close()
            self.lock_fd = None
            raise Refused(f'another deployment holds {lf}') from None
        self.journal('locked', pid=os.getpid())

    def unlock(self):
        if self.lock_fd is not None:
            fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            self.lock_fd.close()
            self.lock_fd = None

    # ------------------------------------------------------------------ live inventory
    def helper_layout(self) -> dict:
        hp = self.data / 'helper.py'
        if hp.is_symlink():
            target = os.readlink(hp)
            return {'kind': 'symlink', 'target': target, 'dir': str((self.data / target).resolve().parent)}
        if hp.is_file():
            st = hp.stat()
            return {'kind': 'file', 'target': None, 'dir': str(self.data), 'mode': st.st_mode & 0o7777,
                    'uid': st.st_uid, 'gid': st.st_gid}
        return {'kind': 'missing', 'target': None, 'dir': str(self.data)}

    def live_files(self) -> dict[str, Path]:
        lay = self.helper_layout()
        out = package_files(Path(lay['dir']), 'helper', list(HELPER_TOP))
        if lay['kind'] == 'file':
            out['helper/helper.py'] = self.data / 'helper.py'
        if self.code_dir and self.code_dir.is_dir():
            top = sorted(p.name for p in self.code_dir.iterdir() if p.is_file() and BONDOK_TOP.fullmatch(p.name))
            out.update(package_files(self.code_dir, 'bondok', top))
        return out

    def live_hashes(self) -> dict[str, str]:
        return {k: sha256(p) for k, p in sorted(self.live_files().items())}

    def expected_live(self, baseline: str | None) -> tuple[dict, str]:
        cur = self.state_dir / 'current.json'
        if cur.exists():
            return json.loads(cur.read_text())['files'], f"recorded release {json.loads(cur.read_text())['release']}"
        if baseline:
            return json.loads(Path(baseline).read_text())['files'], f'reviewed baseline {baseline}'
        raise Refused('no recorded current release on this host: run snapshot-live, review it (unexplained '
                      'differences are drift) and pass it as the reviewed baseline with --expect-live')

    def drift_gate(self, baseline):
        expected, source = self.expected_live(baseline)
        live = self.live_hashes()
        diff = sorted(k for k in set(expected) | set(live) if expected.get(k) != live.get(k))
        if diff:
            raise Refused(f'live code drift against the {source}: ' + ', '.join(diff[:20]) +
                          (' …' if len(diff) > 20 else '') + ' (unknown drift fails the gate; investigate first)')
        self.journal('drift_ok', source=source, files=len(live))
        return live

    # ------------------------------------------------------------------ release
    def load_release(self, rel_dir: Path, part='code') -> dict:
        m = json.loads((rel_dir / 'MANIFEST.json').read_text())
        bad = []
        for k, v in m['files'].items():
            p = rel_dir / k
            if not p.is_file() or sha256(p) != v:
                bad.append(k)
        extra = [p.relative_to(rel_dir).as_posix() for p in rel_dir.rglob('*')
                 if p.is_file() and p.name != 'MANIFEST.json' and p.relative_to(rel_dir).as_posix() not in m['files']]
        if bad or extra:
            raise Refused('release does not match its manifest: ' + ', '.join(sorted(bad + extra)[:20]))
        if not m.get('deployable', {}).get(part):
            raise Refused(f'release is not deployable for {part}: ' + '; '.join(m.get('blockers') or []))
        rel = json.loads((rel_dir / 'helper' / 'RELEASE.json').read_text())
        if rel.get('release') != m['release'] or rel.get('schema_version') != m['schema_version']:
            raise Refused('RELEASE.json does not match the manifest')
        return m

    # ------------------------------------------------------------------ backup
    def sqlite_backup(self, src: Path, dst: Path):
        """WAL-consistent copy through the SQLite backup API, run as the database owner (sqlite_runner) so no
        sidecar (-wal/-shm) is ever created with another owner (the 2026-10-09 ACL outage)."""
        side = {s: (Path(str(src) + s).stat().st_uid if Path(str(src) + s).exists() else None) for s in ('-wal', '-shm')}
        owner = src.stat().st_uid
        tmpdir = Path(tempfile.mkdtemp(prefix='.sqlite-', dir=dst.parent))
        try:
            if os.geteuid() == 0 and self.cfg.get('sqlite_runner'):
                os.chown(tmpdir, owner, src.stat().st_gid)
            out = tmpdir / dst.name
            p = self.run([*(self.cfg.get('sqlite_runner') or []), self.host_python, '-I', '-c', SQLITE_BACKUP,
                          str(src), str(out)], timeout=1800)
            if p.returncode or not out.exists():
                raise Refused(f'SQLite backup of {src} failed: {p.stderr.strip()[-300:]}')
            os.replace(out, dst)
            os.chmod(dst, 0o600)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        for s, before in side.items():
            q = Path(str(src) + s)
            if before is None and q.exists() and q.stat().st_uid != owner:
                raise Refused(f'the backup created {q.name} owned by uid {q.stat().st_uid} (database owner {owner}); '
                              'fix the sqlite_runner before deploying')

    def permissions(self) -> dict:
        out = {}
        paths = [self.data, self.data / 'helper.py', self.data / 'state.sqlite',
                 self.data / 'state.sqlite-wal', self.data / 'state.sqlite-shm', self.data / 'inbox']
        if self.code_dir:
            paths += [self.code_dir, self.code_dir / '.env']
        for p in paths:
            if os.path.lexists(p):
                st = os.lstat(p)
                out[str(p)] = {'mode': oct(st.st_mode & 0o7777), 'uid': st.st_uid, 'gid': st.st_gid,
                               'link': os.readlink(p) if p.is_symlink() else None}
        acl = self.cfg.get('acl_command')
        if acl:
            p = self.run([*acl, *[str(x) for x in paths if os.path.lexists(x)]])
            out['_acl'] = p.stdout
        return out

    def backup(self, label='manual') -> Path:
        self.backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        bdir, n = self.backup_root / f'{stamp()}-{label}', 1
        while True:                     # never reuse (or overwrite) a backup taken in the same second
            try:
                bdir.mkdir(mode=0o700)
                break
            except FileExistsError:
                bdir, n = self.backup_root / f'{stamp()}-{label}-{n}', n + 1
        os.chmod(bdir, 0o700)
        lay = self.helper_layout()
        files = self.live_files()
        identity = {}
        for key, src in files.items():
            if key.startswith('helper/'):
                dst = bdir / key
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        identity['helper'] = {'layout': lay['kind'], **self.code_identity(bdir / 'helper')}
        write_private(bdir / 'helper_layout.json', json.dumps(lay, indent=1))
        self.sqlite_backup(self.data / 'state.sqlite', bdir / 'state.sqlite')
        if self.bondok:
            with tarfile.open(bdir / 'bondok-code.tar.gz', 'w:gz') as t:
                t.add(self.code_dir, arcname='.', filter=lambda ti: None if (
                    '__pycache__' in ti.name or ti.name in ('./venv', './.venv') or
                    ti.name.startswith(('./venv/', './.venv/'))) else ti)
            os.chmod(bdir / 'bondok-code.tar.gz', 0o600)
            if self.bondok.get('db') and Path(self.bondok['db']).exists():
                self.sqlite_backup(Path(self.bondok['db']), bdir / 'bondok.sqlite')
            for u in self.bondok.get('unit_files') or []:
                (bdir / 'units').mkdir(exist_ok=True)
                shutil.copy2(u, bdir / 'units' / Path(u).name)
            identity['bondok'] = {'release': json.loads((self.code_dir / 'RELEASE.json').read_text()).get('release')
                                  if (self.code_dir / 'RELEASE.json').exists() else None}
        write_private(bdir / 'permissions.json', json.dumps(self.permissions(), indent=1))
        n8n = self.cfg.get('n8n')
        if n8n and n8n.get('export') and n8n.get('workflow_ids'):
            for wid in n8n['workflow_ids']:
                self.export_workflow(wid, bdir / 'workflows' / f'{wid}.json')
        manifest = {'created_at': utc(), 'label': label, 'identity': identity,
                    'sources': {'data_dir': str(self.data), 'code_dir': str(self.code_dir) if self.code_dir else None},
                    'live_hashes': {k: sha256(p) for k, p in files.items()},
                    'files': {p.relative_to(bdir).as_posix(): sha256(p) for p in sorted(bdir.rglob('*'))
                              if p.is_file()}}
        write_private(bdir / 'MANIFEST.json', json.dumps(manifest, indent=1))
        result = self.verify_backup(bdir)
        if not result['ok']:
            raise Refused(f'backup {bdir} failed its restore test: {json.dumps(result)[:600]}')
        self.journal('backup_verified', backup=str(bdir))
        return bdir

    @staticmethod
    def code_identity(base: Path) -> dict:
        """What code this is, read from the files themselves (never from a file name such as helper.v1.py)."""
        def grab(name, pattern):
            p = base / 'waset_ops' / name
            m = re.search(pattern, p.read_text(), re.M) if p.exists() else None
            return m.group(1) if m else None
        schema = grab('db.py', r'^SCHEMA_VERSION = (\d+)')
        rel = base / 'RELEASE.json'
        return {'schema_version': int(schema) if schema else None,
                'version': grab('__init__.py', r"__version__ = '([^']+)'"),
                'release': json.loads(rel.read_text()).get('release') if rel.exists() else None,
                'helper_sha256': sha256(base / 'helper.py') if (base / 'helper.py').exists() else None}

    def verify_backup(self, bdir: Path) -> dict:
        m = json.loads((bdir / 'MANIFEST.json').read_text())
        checks, ok = {}, True
        bad = [k for k, v in m['files'].items() if not (bdir / k).is_file() or sha256(bdir / k) != v]
        checks['hashes'] = {'ok': not bad, 'mismatch': bad}
        ok &= not bad
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            for name in ('state.sqlite', 'bondok.sqlite'):
                if (bdir / name).exists():
                    shutil.copy2(bdir / name, d / name)
                    try:
                        with sqlite3.connect(d / name) as c:
                            integrity = c.execute('PRAGMA integrity_check').fetchone()[0]
                            tables = c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
                    except sqlite3.Error as e:
                        integrity, tables = f'error: {e}', 0
                    checks[name] = {'integrity': integrity, 'tables': tables}
                    ok &= integrity == 'ok'
            if (bdir / 'helper' / 'waset_ops').is_dir():
                shutil.copytree(bdir / 'helper', d / 'helper')
                p = self.run([self.host_python, '-I', '-c', 'import sys;sys.path.insert(0,sys.argv[1]);'
                              'import waset_ops;print(waset_ops.__version__)', str(d / 'helper')])
                checks['helper_import'] = {'ok': p.returncode == 0, 'version': p.stdout.strip(),
                                           'error': p.stderr.strip()[-300:]}
                ok &= p.returncode == 0
            if (bdir / 'bondok-code.tar.gz').exists():
                try:
                    with tarfile.open(bdir / 'bondok-code.tar.gz') as t:
                        members = [x.name[2:] if x.name.startswith('./') else x.name for x in t.getmembers()]
                        t.extractall(d / 'bondok', filter='data') if hasattr(tarfile, 'data_filter') else \
                            t.extractall(d / 'bondok')
                    checks['bondok-code.tar.gz'] = {'ok': True, 'members': sorted(members)}
                except (tarfile.TarError, OSError) as e:
                    checks['bondok-code.tar.gz'] = {'ok': False, 'error': str(e)}
                    ok = False
        result = {'ok': bool(ok), 'checked_at': utc(), 'checks': checks}
        write_private(bdir / 'RESTORE_TEST.json', json.dumps(result, indent=1))
        return result

    # ------------------------------------------------------------------ fence and drain
    def fence_path(self) -> Path:
        return self.data / FENCE_FILE

    def set_fence(self, release):
        if self.fence_path().exists():
            raise Refused('a deployment fence is already up (another deployment, or an interrupted one): inspect '
                          'the journal, then `deploy.py unfence --reason …`')
        text = json.dumps({'release': release, 'since': time.time(), 'by': 'deploy.py', 'pid': os.getpid()})
        tmp = self.data / ('.' + FENCE_FILE + '.tmp')
        tmp.write_text(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, self.fence_path())
        self.fence_ours = True
        self.journal('fenced', release=release)

    def remove_fence(self, why):
        self.fence_path().unlink(missing_ok=True)
        self.fence_ours = False
        self.journal('unfenced', why=why)

    def drain_probe(self, probe: Path) -> dict:
        p = self.run([*self.runner, self.runner_path(probe), self.runner_data], env=self.runner_env, timeout=120)
        try:
            return json.loads(p.stdout)
        except ValueError:
            return {'drained': False, 'error': (p.stderr or p.stdout).strip()[-300:]}

    def wait_drained(self, probe: Path) -> dict:
        d = self.cfg.get('drain') or {}
        deadline = time.time() + float(d.get('timeout_seconds', 900))
        while True:
            st = self.drain_probe(probe)
            if st.get('drained'):
                self.journal('drained', status=st)
                return st
            if time.time() >= deadline:
                raise DrainTimeout(st)
            time.sleep(float(d.get('poll_seconds', 10)))

    def service(self, which):
        for cmd in (self.bondok or {}).get(which) or []:
            p = self.run(cmd, timeout=180)
            self.journal(f'bondok_{which}', rc=p.returncode)
            if p.returncode:
                return False
        return True

    # ------------------------------------------------------------------ staging
    def _own(self, root: Path, owner):
        if owner and os.geteuid() == 0:
            for p in [root, *root.rglob('*')]:
                os.lchown(p, int(owner[0]), int(owner[1]))

    def stage_helper(self, rel: Path, manifest: dict) -> Path:
        dest = self.data / 'releases' / manifest['release']
        if dest.exists():
            if self.dir_matches(dest, rel / 'helper', manifest, 'helper/'):
                return dest
            raise Refused(f'{dest} exists with different content (releases are immutable)')
        tmp = self.data / 'releases' / ('.staging-' + manifest['release'])
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(rel / 'helper', tmp)
        for p in tmp.rglob('*'):
            os.chmod(p, 0o755 if p.is_dir() else 0o644)
        os.chmod(tmp, 0o755)
        self._own(tmp, (self.cfg['helper'].get('owner')))
        os.replace(tmp, dest)
        self.created_helper_stage = dest
        return dest

    def dir_matches(self, base: Path, src_part: Path, manifest: dict, prefix: str) -> bool:
        want = {k[len(prefix):]: v for k, v in manifest['files'].items() if k.startswith(prefix)}
        have = {p.relative_to(base).as_posix(): p for p in base.rglob('*')
                if p.is_file() and '__pycache__' not in p.parts}
        return set(have) == set(want) and all(sha256(have[k]) == v for k, v in want.items())

    def stage_bondok(self, rel: Path, manifest: dict) -> Path:
        live, nxt = self.code_dir, self.code_dir.with_name(f"{self.code_dir.name}.next-{manifest['release']}")
        shutil.rmtree(nxt, ignore_errors=True)
        managed = set(k[len('bondok/'):] for k in self.live_files() if k.startswith('bondok/'))

        def ignore(d, names):
            rel_d = Path(d).relative_to(live)
            skip = {n for n in names if n == '__pycache__'}
            if rel_d == Path('.'):
                skip |= {n for n in names if n in managed or n == 'waset_ops'}
            return skip

        def link(src, dst):
            try:
                os.link(src, dst)
            except OSError:
                shutil.copy2(src, dst)
        shutil.copytree(live, nxt, symlinks=True, ignore=ignore, copy_function=link)
        if os.geteuid() == 0:
            for d in [nxt, *[p for p in nxt.rglob('*') if p.is_dir() and not p.is_symlink()]]:
                src = live / d.relative_to(nxt)
                if src.exists():
                    st = src.stat()
                    os.chown(d, st.st_uid, st.st_gid)
        for k in manifest['files']:
            if k.startswith('bondok/'):
                dst = nxt / k[len('bondok/'):]
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(rel / k, dst)
                os.chmod(dst, 0o644)
        self._own_release_files(nxt, manifest)
        return nxt

    def _own_release_files(self, nxt, manifest):
        owner = (self.bondok or {}).get('owner')
        if owner and os.geteuid() == 0:
            for k in manifest['files']:
                if k.startswith('bondok/'):
                    os.lchown(nxt / k[len('bondok/'):], int(owner[0]), int(owner[1]))

    def verify_staged(self, staged_helper, staged_bondok, manifest, rel):
        problems = []
        if not self.dir_matches(staged_helper, rel / 'helper', manifest, 'helper/'):
            problems.append(f'staged helper {staged_helper} does not match the manifest')
        if staged_bondok:
            want = {k: v for k, v in manifest['files'].items() if k.startswith('bondok/')}
            got = package_files(staged_bondok, 'bondok', sorted(p.name for p in staged_bondok.iterdir()
                                                                 if p.is_file() and BONDOK_TOP.fullmatch(p.name)))
            if set(got) != set(want) or any(sha256(got[k]) != v for k, v in want.items()):
                problems.append(f'staged Bondok {staged_bondok} does not match the manifest '
                                f'(extra: {sorted(set(got) - set(want))[:5]}, missing: {sorted(set(want) - set(got))[:5]})')
        if problems:
            raise Refused('; '.join(problems))

    def preflight(self, staged_helper, staged_bondok, manifest):
        p = self.run([*self.runner, '-I', '-c', HELPER_IMPORT, self.runner_path(staged_helper)], env=self.runner_env)
        try:
            info = json.loads(p.stdout)
        except ValueError:
            raise Refused(f'staged helper import test failed in its runtime: {p.stderr.strip()[-400:]}') from None
        if (info.get('release') or {}).get('release') != manifest['release'] or info['schema'] != manifest['schema_version']:
            raise Refused(f'staged helper reports {info}, expected release {manifest["release"]}')
        with tempfile.TemporaryDirectory() as d:          # health of the new code on a copy of today's data
            self.sqlite_backup(self.data / 'state.sqlite', Path(d) / 'state.sqlite')
            arg = base64.b64encode(json.dumps({'path': '/v2/health', 'body': {}}).encode()).decode()
            p = self.run([self.host_python, str(staged_helper / 'helper.py'), arg], env={'WASET_SOCIAL_DATA_DIR': d})
            h = self.parse_health(p)
            if not (h.get('ok') is True and h.get('schema') == manifest['schema_version']):
                raise Refused(f'staged helper health on a database copy failed: {str(h)[:400]}')
        if staged_bondok:
            mods = (self.bondok.get('import_modules') or ['waset_ops', 'store', 'bridge', 'monday_read', 'agent', 'app'])
            p = self.run([self.bondok.get('python') or self.host_python, '-I', '-c',
                          'import sys;sys.path.insert(0,sys.argv[1]);import ' + ','.join(mods), str(staged_bondok)])
            if p.returncode:
                raise Refused(f'staged Bondok import test failed: {p.stderr.strip()[-400:]}')
        self.journal('preflight_ok', helper=info)

    @staticmethod
    def parse_health(p) -> dict:
        try:
            out = json.loads(p.stdout)
            return out if isinstance(out, dict) else {'ok': False, 'error': 'not an object'}
        except ValueError:
            return {'ok': False, 'error': (p.stderr or p.stdout).strip()[-300:], 'exit': p.returncode}

    # ------------------------------------------------------------------ switch / verify / rollback
    def switch(self, ctx):
        tmp = self.data / '.helper.py.next'
        tmp.unlink(missing_ok=True)
        os.symlink(f"releases/{ctx['release']}/helper.py", tmp)
        os.replace(tmp, self.data / 'helper.py')
        ctx['t_helper'] = time.time()
        self.journal('switched_helper', target=f"releases/{ctx['release']}/helper.py")
        if ctx.get('staged_bondok'):
            prev = self.code_dir.with_name(f'{self.code_dir.name}.prev-{stamp()}')
            n = 1
            while prev.exists():        # an earlier deployment in the same second keeps its own copy
                prev = self.code_dir.with_name(f'{self.code_dir.name}.prev-{stamp()}-{n}')
                n += 1
            os.rename(self.code_dir, prev)
            ctx['bondok_prev'] = prev   # only once moved: a rollback must never swap in someone else's copy
            self.journal('bondok_moved_aside', to=str(prev))
            os.rename(ctx['staged_bondok'], self.code_dir)
            ctx['t_bondok'] = time.time()
            self.journal('switched_bondok', prev=str(prev))
        self.journal('switched')

    def helper_health(self) -> dict:
        arg = base64.b64encode(json.dumps({'path': '/v2/health', 'body': {}}).encode()).decode()
        return self.parse_health(self.run([*self.runner, self.runner_data.rstrip('/') + '/helper.py', arg],
                                          env=self.runner_env, timeout=120))

    def bondok_health(self) -> tuple[bool, str]:
        hc = (self.bondok or {}).get('health')
        if not hc:
            return True, 'not configured'
        p = self.run(hc['command'], timeout=120)
        out = p.stdout.strip()
        return (p.returncode == 0 or hc.get('ignore_exit')) and out == hc.get('expect_stdout', out), out[-200:]

    def verify_after_switch(self, manifest):
        want = {k: v for k, v in manifest['files'].items() if k.startswith(('helper/', 'bondok/'))}
        if not self.bondok:
            want = {k: v for k, v in want.items() if k.startswith('helper/')}
        live = self.live_hashes()
        diff = sorted(k for k in set(want) | set(live) if want.get(k) != live.get(k))
        if diff:
            raise RuntimeError('live code after the switch does not match the release: ' + ', '.join(diff[:10]))
        self.journal('verified', files=len(live))

    def check_health(self, manifest):
        h = self.helper_health()
        problems = []
        if h.get('ok') is not True:
            problems.append(f"helper health not ok: {str(h.get('error') or h)[:200]}")
        if h.get('schema') != manifest['schema_version']:
            problems.append(f"helper reports schema {h.get('schema')}, release has {manifest['schema_version']}")
        if ((h.get('helper') or {}).get('release') or {}).get('release') != manifest['release']:
            problems.append('helper does not report the deployed release id')
        if not isinstance(h.get('heartbeat_age_seconds'), dict):
            problems.append('helper health lacks heartbeat_age_seconds')
        if self.bondok:
            if not self.service('start'):
                problems.append('Bondok did not start')
            ok, text = self.bondok_health()
            if not ok:
                problems.append(f'Bondok health check failed: {text!r}')
        if problems:
            raise RuntimeError('health check failed: ' + '; '.join(problems))
        self.journal('healthy', helper={k: h.get(k) for k in ('schema', 'outcome_unknown', 'outbox_escalated')})

    def rollback(self, ctx, why) -> int:
        self.journal('rollback_start', why=str(why))
        if not self.fence_path().exists():
            self.set_fence(ctx['release'] + ':rollback')
        self.service('stop')
        prev = ctx['previous_layout']
        tmp = self.data / '.helper.py.rollback'
        tmp.unlink(missing_ok=True)
        if prev['kind'] == 'symlink':
            os.symlink(prev['target'], tmp)
        else:
            shutil.copyfile(ctx['backup'] / 'helper' / 'helper.py', tmp)
            os.chmod(tmp, prev.get('mode', 0o644))
            if os.geteuid() == 0 and prev.get('uid') is not None:
                os.chown(tmp, prev['uid'], prev['gid'])
        os.replace(tmp, self.data / 'helper.py')
        if ctx.get('bondok_prev') and Path(ctx['bondok_prev']).exists():
            failed = self.code_dir.with_name(f'{self.code_dir.name}.failed-{stamp()}')
            if self.code_dir.exists():
                os.rename(self.code_dir, failed)
            os.rename(ctx['bondok_prev'], self.code_dir)
        self.journal('code_restored')
        live = self.live_hashes()
        mismatch = sorted(k for k in set(live) | set(ctx['previous_hashes'])
                          if live.get(k) != ctx['previous_hashes'].get(k))
        probe = self.drain_probe(ctx['staged_helper'] / 'drain_probe.py')
        db_schema, old_schema = probe.get('schema'), ctx['previous_schema']
        contained = []
        if mismatch:
            contained.append('restored code does not match the previous hashes: ' + ', '.join(mismatch[:10]))
        if db_schema is None or (old_schema is not None and db_schema > old_schema):
            contained.append(f'the database schema ({db_schema}) is newer than the previous code supports '
                             f'({old_schema}); it is not restored over newer evidence')
        if not contained:
            h = self.helper_health()
            if h.get('ok') is not True:
                contained.append(f'the previous helper is not healthy: {str(h)[:200]}')
        if contained:
            for cmd in self.cfg.get('contain_commands') or []:
                self.run(cmd)
            self.journal('contained', reasons=contained)
            say('CONTAINED: ' + ' | '.join(contained))
            say('Writers stay fenced (deploy-fence.json) and Bondok stays stopped. A helper older than this '
                'tool ignores the fence: deactivate WF1/WF2/WF3 now if the previous code is live. Then recover '
                'from the journal and the backup at ' + str(ctx['backup']))
            return 5
        if self.bondok:
            self.service('start')
            ok, text = self.bondok_health()
            if not ok:
                say(f'WARNING: Bondok health after rollback: {text!r} (previous code restored)')
        self.remove_fence('rolled back')
        self.journal('rolled_back', why=str(why))
        say(f'ROLLED BACK: {why}. Previous code restored and verified; databases kept as they are.')
        return 4

    # ------------------------------------------------------------------ commands
    def deploy_code(self, rel_dir: Path, baseline=None) -> int:
        self.lock()
        if self.fence_path().exists():
            raise Refused('a deployment fence is already up (another deployment, or an interrupted one): inspect '
                          'the journal, then `deploy.py unfence --reason …`')
        manifest = self.load_release(rel_dir, 'code')
        rid = manifest['release']
        previous = self.drift_gate(baseline)
        bdir = self.backup(f'pre-{rid}')
        layout = json.loads((bdir / 'helper_layout.json').read_text())
        ctx = {'release': rid, 'backup': bdir, 'previous_layout': layout, 'previous_hashes': previous,
               'previous_schema': json.loads((bdir / 'MANIFEST.json').read_text())['identity']['helper']['schema_version']}
        self.created_helper_stage = None
        staged_bondok = None
        try:
            ctx['staged_helper'] = self.stage_helper(rel_dir, manifest)
            if self.bondok:
                staged_bondok = self.stage_bondok(rel_dir, manifest)
            ctx['staged_bondok'] = staged_bondok
            self.verify_staged(ctx['staged_helper'], staged_bondok, manifest, rel_dir)
            self.preflight(ctx['staged_helper'], staged_bondok, manifest)
        except BaseException:
            if staged_bondok:
                shutil.rmtree(staged_bondok, ignore_errors=True)
            if self.created_helper_stage:
                shutil.rmtree(self.created_helper_stage, ignore_errors=True)
            raise
        self.journal('staged', helper=str(ctx['staged_helper']), bondok=str(staged_bondok))
        self.set_fence(rid)
        t_fence = time.time()
        stopped = self.service('stop')
        try:
            if not stopped:
                raise DrainTimeout({'error': 'Bondok did not stop'})
            self.wait_drained(ctx['staged_helper'] / 'drain_probe.py')
            if self.live_hashes() != previous:
                raise DrainTimeout({'error': 'live code changed while waiting (another writer of the code)'})
            self.verify_staged(ctx['staged_helper'], staged_bondok, manifest, rel_dir)
        except DrainTimeout as e:
            self.remove_fence('drain did not complete; nothing switched')
            if self.bondok:
                self.service('start')
            if staged_bondok:
                shutil.rmtree(staged_bondok, ignore_errors=True)
            self.journal('drain_timeout', status=e.args[0] if e.args else None)
            say('FAILED CLOSED: writers did not drain in time; nothing was switched, the fence is removed and '
                f'Bondok restarted. Still busy: {json.dumps((e.args[0] if e.args else {}), default=str)[:600]}')
            return 3
        except Refused:
            self.remove_fence('staged release changed while waiting; nothing switched')
            if self.bondok:
                self.service('start')
            raise
        try:
            self.switch(ctx)
            self.verify_after_switch(manifest)
            self.check_health(manifest)
        except Exception as e:  # noqa: BLE001 - every failure after the switch goes to the verified rollback
            return self.rollback(ctx, e)
        live = self.live_hashes()
        write_private(self.state_dir / 'current.json', json.dumps({
            'release': rid, 'commit': manifest['commit'], 'schema_version': manifest['schema_version'],
            'deployed_at': utc(), 'files': live, 'backup': str(bdir)}, indent=1))
        self.remove_fence('deployed')
        t_end = time.time()
        say(f'DEPLOYED release {rid} (commit {manifest["commit"][:12]}). Backup: {bdir}')
        say(f"Cross-service interval: fence {utc(t_fence)} -> helper switch {utc(ctx['t_helper'])}"
            + (f" -> Bondok switch {utc(ctx['t_bondok'])}" if ctx.get('t_bondok') else '')
            + f' -> fence removed {utc(t_end)} ({t_end - t_fence:.1f} s fenced). During this interval the n8n '
              'writers were fenced and Bondok was stopped; no two code versions ran unfenced.')
        say('Workflows are NOT changed by this command (deploy.py workflows; activation is a separate step).')
        return 0

    # ------------------------------------------------------------------ workflows (inactive import)
    def export_workflow(self, wid, dest: Path) -> dict:
        n8n = self.cfg['n8n']
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fdir = Path(n8n['file_dir'])
        fdir.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = fdir / f'export-{wid}.json'
        rpath = n8n.get('runner_file_dir', str(fdir)).rstrip('/') + '/' + tmp.name
        p = self.run([a.replace('{id}', wid).replace('{file}', rpath) for a in n8n['export']], timeout=300)
        if p.returncode or not tmp.exists():
            raise Refused(f'export of workflow {wid} failed: {p.stderr.strip()[-300:]}')
        data = json.loads(tmp.read_text())
        data = data[0] if isinstance(data, list) else data
        write_private(dest, json.dumps(data, ensure_ascii=False, indent=1))
        tmp.unlink(missing_ok=True)
        return data

    def import_workflow(self, wf: dict):
        n8n = self.cfg['n8n']
        fdir = Path(n8n['file_dir'])
        tmp = fdir / f"import-{wf['id']}.json"
        write_private(tmp, json.dumps(wf, ensure_ascii=False))
        rpath = n8n.get('runner_file_dir', str(fdir)).rstrip('/') + '/' + tmp.name
        p = self.run([a.replace('{file}', rpath).replace('{project}', str(n8n.get('project') or ''))
                      for a in n8n['import']], timeout=300)
        tmp.unlink(missing_ok=True)
        if p.returncode:
            raise RuntimeError(f"import of {wf['id']} failed: {p.stderr.strip()[-300:]}")

    @staticmethod
    def fingerprint(w: dict) -> dict:
        nodes = {n['name']: {k: n.get(k) for k in ('type', 'typeVersion', 'parameters', 'credentials', 'onError',
                                                     'disabled', 'executeOnce', 'retryOnFail')}
                 for n in w.get('nodes', [])}
        return {'nodes': nodes, 'connections': w.get('connections'), 'settings': w.get('settings')}

    def compare(self, want: dict, got: dict) -> list[str]:
        a, b = self.fingerprint(want), self.fingerprint(got)
        diffs = []
        for name in sorted(set(a['nodes']) | set(b['nodes'])):
            if a['nodes'].get(name) != b['nodes'].get(name):
                diffs.append(f'node {name!r}')
        if a['connections'] != b['connections']:
            diffs.append('connections')
        for k, v in (a['settings'] or {}).items():
            if (b['settings'] or {}).get(k) != v:
                diffs.append(f'setting {k}')
        return diffs

    def deploy_workflows(self, rel_dir: Path) -> int:
        manifest = self.load_release(rel_dir, 'code')          # hashes; workflow deployability decided here
        n8n = self.cfg.get('n8n') or {}
        creds = n8n.get('credentials') or {}
        wfs, unresolved = [], []
        for k in sorted(manifest['files']):
            if k.startswith('workflows/'):
                w = json.loads((rel_dir / k).read_text())
                for n in w['nodes']:
                    for t, c in (n.get('credentials') or {}).items():
                        if c.get('id') in (None, '', REDACTED):
                            cid = creds.get(f"{t}:{c.get('name')}")
                            if cid:
                                c['id'] = str(cid)
                            else:
                                unresolved.append(f"{w['id']}/{n['name']} {t}:{c.get('name')}")
                w['active'] = False
                wfs.append(w)
        if unresolved:
            raise Refused(f'{len(unresolved)} credential reference(s) are still {REDACTED} (map them in the '
                          'private config n8n.credentials): ' + '; '.join(unresolved[:8]))
        self.lock()
        record = self.state_dir / 'workflows.json'
        recorded = json.loads(record.read_text()) if record.exists() else {}
        bdir = self.backup_root / f'{stamp()}-workflows'
        live = {}
        for w in wfs:
            got = self.export_workflow(w['id'], bdir / f"{w['id']}.json")
            expected = recorded.get(w['id'], {}).get('versionId') or (w.get('meta') or {}).get('built_from')
            if got.get('versionId') != expected:
                raise Refused(f"live workflow drift: {w['id']} is at version {got.get('versionId')}, expected "
                              f'{expected} (someone changed it since the last recorded import)')
            live[w['id']] = got
        self.journal('workflows_backed_up', backup=str(bdir))
        failures, done = [], {}
        for w in wfs:
            before = live[w['id']]
            self.import_workflow(w)
            got = self.export_workflow(w['id'], bdir / f"{w['id']}.imported.json")
            diffs = self.compare(w, got)
            if (got.get('active'), got.get('activeVersionId')) != (before.get('active'), before.get('activeVersionId')):
                diffs.append('activation state changed by the import')
            if diffs:
                failures.append(f"{w['id']}: imported version differs from the tested artifact ({', '.join(diffs[:6])})")
                try:
                    self.import_workflow(before)               # put the previous content back as the latest draft
                except RuntimeError as e:
                    failures.append(str(e))
            else:
                done[w['id']] = {'versionId': got.get('versionId'), 'release': manifest['release'], 'imported_at': utc()}
        if done:
            write_private(record, json.dumps({**recorded, **done}, indent=1))
        self.journal('workflows_imported', ok=sorted(done), failures=failures)
        for f in failures:
            say('FAILED:', f)
        say(f'Imported (inactive): {sorted(done) or "none"}. Active versions unchanged: activation is a separate, '
            'authorized step (and should follow the code deployment).')
        return 0 if not failures else 4


def load_config(path) -> dict:
    cfg = json.loads(Path(path).read_text())
    for k in ('state_dir', 'backup_root', 'lock_file', 'helper'):
        if k not in cfg:
            raise Refused(f'config lacks {k}')
    st = os.stat(path)
    if st.st_mode & 0o077 and os.geteuid() == 0:
        raise Refused(f'{path} must not be readable by others (chmod 600)')
    return cfg


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Guarded Waset deployment (see module docstring).')
    ap.add_argument('command', choices=['snapshot-live', 'backup', 'verify-backup', 'code', 'workflows', 'status',
                                        'unfence'])
    ap.add_argument('--config', required=True)
    ap.add_argument('--release')
    ap.add_argument('--expect-live')
    ap.add_argument('--backup')
    ap.add_argument('--reason')
    a = ap.parse_args(argv)
    dep = None
    try:
        dep = Deployer(load_config(a.config))
        if a.command == 'snapshot-live':
            say(json.dumps({'taken_at': utc(), 'layout': dep.helper_layout(), 'files': dep.live_hashes()}, indent=1))
            return 0
        if a.command == 'backup':
            dep.lock()
            say(str(dep.backup('manual')))
            return 0
        if a.command == 'verify-backup':
            r = dep.verify_backup(Path(a.backup))
            say(json.dumps(r, indent=1))
            return 0 if r['ok'] else 1
        if a.command == 'status':
            cur = dep.state_dir / 'current.json'
            say(json.dumps({'fence': json.loads(dep.fence_path().read_text()) if dep.fence_path().exists() else None,
                            'current': json.loads(cur.read_text()) if cur.exists() else None,
                            'layout': dep.helper_layout()}, indent=1, default=str))
            say(LIMITS)
            return 0
        if a.command == 'unfence':
            if not a.reason:
                raise Refused('--reason is required (it is journaled)')
            dep.lock()
            dep.remove_fence('operator: ' + a.reason)
            say('fence removed')
            return 0
        if not a.release:
            raise Refused('--release is required')
        if a.command == 'code':
            return dep.deploy_code(Path(a.release), a.expect_live)
        return dep.deploy_workflows(Path(a.release))
    except Refused as e:
        say(f'REFUSED: {e}')
        return 2
    finally:
        if dep is not None:
            dep.unlock()


if __name__ == '__main__':
    sys.exit(main())
