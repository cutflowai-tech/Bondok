"""Assemble an immutable release from the committed tree, by explicit allowlist, with a SHA-256 manifest.

    python3 deploy/build_release.py [--repo DIR] [--out DIR] [--creds PRIVATE_CREDENTIAL_MAP.json]
                                    [--site PRIVATE_SITE.json]

Content comes from ``git archive HEAD`` (committed files only), so a local .env, database or scratch file can
never ship (R5 LOW-19), and only these paths are taken:

  helper/     helper.py, waset_ops/*.py, drain_probe.py, RELEASE.json -> <data dir>/releases/<id>/ (deploy.py)
  bondok/     *.py, policy.txt, waset_ops/*.py, RELEASE.json         -> Bondok code dir (runtime files kept)
  workflows/  *.v2.json (must equal a fresh workflows/build.py run of the same commit)

Refuses (exit 1): uncommitted changes to tracked files (the release would not be its commit), stale
workflows/dist, a secret-looking string in any packaged file.

Workflow credential ids stay ``<REDACTED>`` (public exports) unless ``--creds`` maps "<type>:<name>" to an id
(a private file; the output then holds private ids and is written 0700). While any stays unresolved the manifest
marks the workflows not deployable and deploy.py refuses to import them.
Deployment-specific ids (the Instagram user id) are ``<NAME>`` placeholders in the public tree (R5 LOW-18);
``--site`` maps NAME to its value (a private file) and every packaged file gets the value. While any placeholder
stays unresolved the manifest marks code and workflows not deployable.
Output: <out>/<release id>/ (default <repo>/release/, gitignored). The last line printed is that directory.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ALLOW = [  # (pattern in the repository, destination prefixes)
    (re.compile(r'src/helper\.py'), ['helper/helper.py']),
    (re.compile(r'src/waset_ops/([a-z_]+\.py)'), ['helper/waset_ops/{0}', 'bondok/waset_ops/{0}']),
    (re.compile(r'deploy/drain_probe\.py'), ['helper/drain_probe.py']),
    (re.compile(r'bondok/([a-z_]+\.py)'), ['bondok/{0}']),
    (re.compile(r'bondok/policy\.txt'), ['bondok/policy.txt']),
    (re.compile(r'workflows/dist/([A-Za-z0-9_]+\.v2\.json)'), ['workflows/{0}']),
]
SECRET = re.compile(rb'xox[abposr]-[A-Za-z0-9-]{10,}|xapp-[0-9]-[A-Za-z0-9-]{10,}|sk-or-v1-[A-Za-z0-9]{16,}|'
                    rb'sk-[A-Za-z0-9]{32,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|eyJhbGciOi[A-Za-z0-9_-]{20,}\.')
REDACTED = '<REDACTED>'
PLACEHOLDERS = ('IG_ACCOUNT',)


class BuildError(Exception):
    pass


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(repo, *args, binary=False):
    p = subprocess.run(['git', *args], cwd=repo, capture_output=True, check=True)
    return p.stdout if binary else p.stdout.decode()


def committed_tree(repo) -> dict[str, bytes]:
    data = git(repo, 'archive', '--format=tar', 'HEAD', binary=True)
    out = {}
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        for m in t.getmembers():
            if m.isfile():
                out[m.name] = t.extractfile(m).read()
    return out


def check_exports_fresh(tree: dict[str, bytes]):
    """workflows/dist must be exactly what this commit's workflows/build.py generates (never hand-edited)."""
    with tempfile.TemporaryDirectory() as d:
        for name, data in tree.items():
            if name.startswith(('workflows/', 'src/')):
                p = Path(d) / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
        dist = Path(d) / 'workflows' / 'dist'
        for p in dist.glob('*.json'):
            p.unlink()
        subprocess.run([sys.executable, '-I', str(Path(d) / 'workflows' / 'build.py')], check=True,
                       capture_output=True, cwd=d)
        fresh = {f'workflows/dist/{p.name}': p.read_bytes() for p in dist.glob('*.json')}
    committed = {k: v for k, v in tree.items() if k.startswith('workflows/dist/') and k.endswith('.json')}
    if fresh != committed:
        stale = sorted(set(fresh) ^ set(committed) | {k for k in fresh if committed.get(k) != fresh[k]})
        raise BuildError('workflows/dist is stale (run python3 workflows/build.py and commit): ' + ', '.join(stale))


def resolve_credentials(wf: dict, creds: dict) -> list[dict]:
    unresolved = []
    for n in wf.get('nodes', []):
        for t, c in (n.get('credentials') or {}).items():
            if c.get('id') in (None, '', REDACTED):
                cid = creds.get(f"{t}:{c.get('name')}")
                if cid:
                    c['id'] = str(cid)
                else:
                    unresolved.append({'workflow': wf.get('id'), 'node': n['name'], 'type': t, 'name': c.get('name')})
    return unresolved


def build(repo: Path, out_root: Path, creds_file: Path | None = None, site_file: Path | None = None) -> Path:
    if git(repo, 'status', '--porcelain', '--untracked-files=no').strip():
        raise BuildError('the repository has uncommitted changes to tracked files; commit them first '
                         '(a release is always exactly one commit)')
    commit = git(repo, 'rev-parse', 'HEAD').strip()
    rid = commit[:12]
    tree = committed_tree(repo)
    check_exports_fresh(tree)
    files: dict[str, bytes] = {}
    for name, data in sorted(tree.items()):
        for pat, dests in ALLOW:
            m = pat.fullmatch(name)
            if m:
                for d in dests:
                    files[d.format(*m.groups())] = data
    db = tree['src/waset_ops/db.py'].decode()
    schema = int(re.search(r'^SCHEMA_VERSION = (\d+)', db, re.M).group(1))
    version = re.search(r"__version__ = '([^']+)'", tree['src/waset_ops/__init__.py'].decode()).group(1)
    identity = {'release': rid, 'commit': commit, 'schema_version': schema, 'version': version}
    rel = (json.dumps(identity, indent=1, sort_keys=True) + '\n').encode()
    files['helper/RELEASE.json'] = rel
    files['bondok/RELEASE.json'] = rel
    creds = json.loads(Path(creds_file).read_text()) if creds_file else {}
    unresolved = []
    for name in [n for n in files if n.startswith('workflows/')]:
        wf = json.loads(files[name])
        unresolved += resolve_credentials(wf, creds)
        if creds:
            files[name] = (json.dumps(wf, ensure_ascii=False, indent=1) + '\n').encode()
    site = json.loads(Path(site_file).read_text()) if site_file else {}
    missing = set()
    for name in list(files):
        for key in PLACEHOLDERS:
            token = f'<{key}>'.encode()
            if token in files[name]:
                value = str(site.get(key) or '')
                if not re.fullmatch(r'[0-9A-Za-z_-]{1,64}', value):
                    missing.add(key)
                    continue
                files[name] = files[name].replace(token, value.encode())
    for name, data in files.items():
        if SECRET.search(data) or '.env' in name:
            raise BuildError(f'{name}: secret-looking content or a .env file; refusing to package')
    blockers = []
    if unresolved:
        blockers.append(f'{len(unresolved)} workflow credential reference(s) are {REDACTED}; deploy.py must resolve '
                        'them from its private config before import')
    if missing:
        blockers.append('deployment placeholder(s) ' + ', '.join(sorted(missing)) + ' have no value; build with '
                        '--site PRIVATE_SITE.json')
    out = out_root / rid
    if out.exists():
        raise BuildError(f'{out} already exists (releases are immutable; remove it or build into another --out)')
    out.mkdir(parents=True, mode=0o700 if creds or site else 0o755)
    for name, data in files.items():
        p = out / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    manifest = {**identity, 'built_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                'files': {k: sha(v) for k, v in sorted(files.items())},
                'deployable': {'code': not missing, 'workflows': not unresolved and not missing},
                'unresolved_placeholders': sorted(missing),
                'unresolved_credentials': unresolved, 'credentials_resolved': bool(creds) and not unresolved,
                'blockers': blockers}
    (out / 'MANIFEST.json').write_text(json.dumps(manifest, indent=1) + '\n')
    return out


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--repo', default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument('--out')
    ap.add_argument('--creds', help='private JSON {"<credential type>:<name>": "<id>"}; never committed')
    ap.add_argument('--site', help='private JSON {"IG_ACCOUNT": "<id>"}; never committed')
    a = ap.parse_args(argv)
    repo = Path(a.repo).resolve()
    try:
        out = build(repo, Path(a.out) if a.out else repo / 'release', Path(a.creds) if a.creds else None,
                    Path(a.site) if a.site else None)
    except (BuildError, subprocess.CalledProcessError) as e:
        print('REFUSED:', e, file=sys.stderr)
        return 1
    m = json.loads((out / 'MANIFEST.json').read_text())
    print(json.dumps({k: m[k] for k in ('release', 'commit', 'schema_version', 'deployable', 'blockers')}, indent=1))
    print(out)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
