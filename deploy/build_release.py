"""Assemble a release directory with a SHA-256 manifest.

    python3 deploy/build_release.py [--creds <live n8n snapshot dir>]

Output: release/<git-sha>/ (gitignored)
  helper/       -> /home/node/.n8n-files/waset-social/  (helper.py + waset_ops/)
  bondok/       -> /opt/waset-bondok/                   (app + waset_ops/; .env untouched)
  workflows/    -> imported into n8n with the same workflow IDs
  MANIFEST.json -> hashes used for read-back verification after cutover
Credential IDs are filled in only when --creds is given (from a local, gitignored
snapshot of the live workflows); secrets are never involved.
"""
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def main(argv):
    rev = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(['git', 'status', '--porcelain'], cwd=ROOT, capture_output=True, text=True).stdout.strip())
    out = ROOT / 'release' / (rev + ('-dirty' if dirty else ''))
    if out.exists():
        shutil.rmtree(out)
    ignore = shutil.ignore_patterns('__pycache__', '*.pyc')
    shutil.copytree(ROOT / 'src' / 'waset_ops', out / 'helper' / 'waset_ops', ignore=ignore)
    shutil.copy(ROOT / 'src' / 'helper.py', out / 'helper' / 'helper.py')
    shutil.copytree(ROOT / 'bondok', out / 'bondok', ignore=ignore)
    shutil.copytree(ROOT / 'src' / 'waset_ops', out / 'bondok' / 'waset_ops', ignore=ignore)
    subprocess.run([sys.executable, str(ROOT / 'workflows' / 'build.py')], check=True, capture_output=True)
    shutil.copytree(ROOT / 'workflows' / 'dist', out / 'workflows')
    if '--creds' in argv:
        live = Path(argv[argv.index('--creds') + 1])
        for wf in (out / 'workflows').glob('*.json'):
            w = json.loads(wf.read_text())
            src = json.loads((live / (w['id'] + '.json')).read_text())
            ids = {}
            for n in src['nodes']:
                for t, c in (n.get('credentials') or {}).items():
                    ids[(t, c['name'])] = c['id']
            for n in w['nodes']:
                for t, c in (n.get('credentials') or {}).items():
                    c['id'] = ids[(t, c['name'])]          # KeyError = credential missing live: stop
            wf.write_text(json.dumps(w, ensure_ascii=False, indent=1))
    files = sorted(p for p in out.rglob('*') if p.is_file())
    manifest = {'git': rev, 'dirty': dirty, 'files': {str(p.relative_to(out)): sha(p) for p in files}}
    (out / 'MANIFEST.json').write_text(json.dumps(manifest, indent=1))
    print(out)
    print(json.dumps({k: v for k, v in manifest['files'].items() if k.endswith(('helper.py', 'app.py', '.v2.json'))}, indent=1))


if __name__ == '__main__':
    main(sys.argv[1:])
