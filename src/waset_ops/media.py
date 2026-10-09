"""Media preflight/preparation, reused from the deployed helper (2026-10-09).

Jobs run detached (two FFmpeg slots per host) and write results to the legacy
``jobs``/``media`` tables, which stay the immutable verification store.
Changes from the deployed helper:
* the transfer cap is named honestly (TRANSFER_MAX_BYTES), the business limit
  stays 300,000,000 bytes;
* an explicit recheck re-measures the existing prepared file (ffprobe + sha256)
  instead of returning a cached result;
* Topaz is evidence bound to the asset by the handler; never a filename.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import ipaddress
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import rules
from .db import Store, dumps, loads

ROOT = Path(os.environ.get('WASET_SOCIAL_DATA_DIR', str(Path.home() / '.n8n-files/waset-social')))
CAPACITY = 2
TARGET_BYTES = 140_000_000            # DEFAULT encode target under TRANSFER_MAX_BYTES


def store() -> Store:
    return Store(ROOT / 'state.sqlite')


def safe_download_url(url):
    p = urlparse(url)
    host = p.hostname or ''
    if p.scheme != 'https' or p.username or p.password or p.port not in (None, 443):
        raise ValueError('Only HTTPS source links are allowed')
    if not any(host == d or host.endswith('.' + d) for d in ('dropbox.com', 'dropboxusercontent.com')):
        raise ValueError('Source must be a Dropbox file URL')
    if '/scl/fo/' in p.path:
        raise ValueError('Source URL is a folder, not a video')
    for result in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM):
        if not ipaddress.ip_address(result[4][0]).is_global:
            raise ValueError('Private source addresses are not allowed')
    return url


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        safe_download_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def raw_url(url):
    p = urlparse(safe_download_url(url))
    q = dict(parse_qsl(p.query))
    q.pop('dl', None)
    q['raw'] = '1'
    return urlunparse(p._replace(query=urlencode(q)))


def media_key(b) -> str:
    if not all(b.get(k) for k in ('fileId', 'revision', 'contentHash', 'assetKey')):
        raise ValueError('Exact Dropbox file ID, revision and content hash are required')
    identity = '|'.join(str(b.get(k, '')) for k in ('itemId', 'fileId', 'revision', 'contentHash', 'format', 'assetKey'))
    return hashlib.sha256((identity + '|qa' + str(rules.QA_POLICY)).encode()).hexdigest()


def dropbox_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(4 * 1024 * 1024):
            digest.update(hashlib.sha256(block).digest())
    return digest.hexdigest()


def sha256_file(path):
    d = hashlib.sha256()
    with Path(path).open('rb') as s:
        while block := s.read(1024 * 1024):
            d.update(block)
    return d.hexdigest()


def run(args, timeout=1200):
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise ValueError('Media tool failed: ' + r.stderr[-700:])
    return r.stdout


def probe(path):
    d = json.loads(run(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)], 60))
    v = next((s for s in d['streams'] if s['codec_type'] == 'video'), None)
    if not v:
        raise ValueError('No video stream')
    duration = float(d['format'].get('duration') or 0)
    if not math.isfinite(duration) or duration <= 0 or duration > 3600:
        raise ValueError('Invalid or unsupported duration')
    return {'width': int(v['width']), 'height': int(v['height']), 'duration': duration,
            'bytes': Path(path).stat().st_size, 'codec': v.get('codec_name')}


def _download(source, original, content_hash):
    if shutil.disk_usage(ROOT).free < 6_000_000_000:
        raise ValueError('Insufficient free space for safe media preparation')
    opener = build_opener(SafeRedirect())
    total = 0
    with opener.open(Request(source, headers={'User-Agent': 'WasetMediaWorker/3'}), timeout=60) as r, \
            original.open('wb') as f:
        while block := r.read(1024 * 1024):
            if shutil.disk_usage(ROOT).free < 2_000_000_000:
                raise ValueError('Disk reserve reached; media job stopped safely')
            total += len(block)
            if total > 5_000_000_000:
                raise ValueError('Source exceeds 5 GB')
            f.write(block)
    if dropbox_hash(original) != content_hash:
        raise ValueError('Dropbox file changed after selection; resolve the latest revision again')


def _job_result(job_id):
    with store().read() as c:
        r = c.execute('SELECT result FROM jobs WHERE id=?', (job_id,)).fetchone()
    return loads(r['result']) if r else None


def _launch(kind, job_id, body, attempt):
    """Start a detached job holding an item lock and one capacity slot."""
    ROOT.mkdir(parents=True, exist_ok=True)
    lock = (ROOT / (job_id + '.lock')).open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return {'ready': False, 'pending': True, 'running': True, 'reason': 'The same check is already running'}
    capacity = None
    for slot in range(CAPACITY):
        handle = (ROOT / ('media-capacity-' + str(slot) + '.lock')).open('a')
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            capacity = handle
            break
        except BlockingIOError:
            handle.close()
    if capacity is None:
        lock.close()
        return {'ready': False, 'pending': True, 'reason': 'Waiting for a free media worker slot'}
    payload = {'path': '/internal/job', 'kind': kind, 'body': body, 'mid': job_id,
               'fds': [lock.fileno(), capacity.fileno()], 'attempt': attempt}
    arg = base64.b64encode(json.dumps(payload).encode()).decode()
    helper = os.environ.get('WASET_HELPER', str(ROOT / 'helper.py'))
    subprocess.Popen([sys.executable, helper, arg], start_new_session=True,
                     pass_fds=(lock.fileno(), capacity.fileno()), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    lock.close()
    capacity.close()
    return {'ready': False, 'pending': True, 'reason': kind + ' job started; result on the next cycle'}


def _reuse(prior, recheck):
    """Immutable evidence may be reused; explicit rechecks never reuse it."""
    if not prior or recheck:
        return None
    if prior.get('retryable') and prior.get('attempts', 0) < 3 and time.time() >= prior.get('retryAt', 0):
        return None
    return prior


def preflight(b):
    """Story duration measured on the actual selected file before encoding."""
    if b.get('format') != 'Story':
        return {'ready': True, 'skipped': True, 'assetKey': b.get('assetKey'), 'contentHash': b.get('contentHash')}
    job_id = 'preflight-' + media_key(b)
    prior = _job_result(job_id)
    if b.get('recheck') and prior and prior.get('checkedAt', 0) >= b.get('recheckRequestedAt', 0) > 0:
        return prior
    reuse = _reuse(prior, b.get('recheck'))
    if reuse:
        return reuse
    return _launch('preflight', job_id, b, (prior or {}).get('attempts', 0) + 1)


def prepare(b):
    mid = media_key(b)
    with store().read() as c:
        cached = c.execute('SELECT * FROM media WHERE id=?', (mid,)).fetchone()
    prior = _job_result(mid)
    if cached and Path(cached['path']).exists():
        info = loads(cached['metadata'], {})
        if b.get('recheck') and info.get('checkedAt', 0) < b.get('recheckRequestedAt', 0):
            return _launch('reverify', mid, b, 1)
        bad = rules.media_failure(info, b['format'])
        if bad:
            return {'ready': False, 'reason': bad, 'assetKey': b['assetKey'], 'format': b['format']}
        return {'ready': True, 'mediaId': mid, 'filePath': cached['path'], **info}
    reuse = _reuse(prior, b.get('recheck'))
    if reuse:
        return reuse
    return _launch('prepare', mid, b, (prior or {}).get('attempts', 0) + 1)


def job_preflight(b):
    folder = ROOT / 'preflight'
    folder.mkdir(parents=True, exist_ok=True)
    original = folder / (media_key(b) + '.input')
    try:
        _download(raw_url(b['sourceUrl']), original, b['contentHash'])
        info = probe(original)
        bad = rules.story_duration_failure(info['duration'])
        return {'ready': bad is None, 'duration': info['duration'], 'reason': bad, 'replaceRequired': bad is not None,
                'assetKey': b['assetKey'], 'contentHash': b['contentHash'], 'checkedAt': time.time()}
    finally:
        original.unlink(missing_ok=True)


def job_prepare(b):
    mid = media_key(b)
    folder = ROOT / 'media'
    folder.mkdir(parents=True, exist_ok=True)
    original, output = folder / (mid + '.input'), folder / (mid + '.mp4')
    base = {'assetKey': b['assetKey'], 'format': b['format']}
    try:
        _download(raw_url(b['sourceUrl']), original, b['contentHash'])
        info = probe(original)
        if b['format'] == 'Story':
            bad = rules.story_duration_failure(info['duration'])
            if bad:
                return {**base, 'ready': False, 'reason': bad}
        if min(info['width'], info['height']) < rules.MIN_SHORT_EDGE:
            return {**base, 'ready': False, 'reason': 'Source short edge is below 1080 pixels'}
        scale = "scale=w='if(gte(ih,iw),1080,-2)':h='if(gte(ih,iw),-2,1080)'"
        bitrate = min(12_000_000, int(TARGET_BYTES * 8 / info['duration']) - 160_000)
        if bitrate < 500_000:
            return {**base, 'ready': False, 'reason': 'Duration cannot fit the current transfer path at usable quality'}
        common = ['ffmpeg', '-nostdin', '-y', '-v', 'error', '-i', str(original), '-map', '0:v:0', '-threads', '2',
                  '-vf', scale, '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-b:v', str(bitrate),
                  '-passlogfile', str(folder / mid)]
        run(common + ['-pass', '1', '-an', '-f', 'null', '/dev/null'])
        run(common + ['-pass', '2', '-map', '0:a:0?', '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart',
                      str(output)])
        result = probe(output)
        result.update(assetKey=b['assetKey'], fileId=b['fileId'], revision=b['revision'],
                      contentHash=b['contentHash'], format=b['format'], qaPolicy=rules.QA_POLICY,
                      orientation='vertical' if result['height'] > result['width'] else 'horizontal',
                      sha256=sha256_file(output), checkedAt=time.time())
        bad = rules.media_failure(result, b['format'])
        if bad:
            output.unlink(missing_ok=True)
            return {**base, 'ready': False, 'reason': bad}
        with store().tx() as c:
            c.execute('INSERT OR REPLACE INTO media VALUES(?,?,?,?,?)',
                      (mid, str(b['itemId']), str(output), raw_url(b['sourceUrl']), dumps(result)))
        return {'ready': True, 'mediaId': mid, 'filePath': str(output), **result}
    finally:
        original.unlink(missing_ok=True)
        for f in folder.glob(mid + '-*'):
            f.unlink(missing_ok=True)


def job_reverify(b):
    """Explicit recheck of an existing prepared file: fresh measurements."""
    mid = media_key(b)
    with store().read() as c:
        row = c.execute('SELECT * FROM media WHERE id=?', (mid,)).fetchone()
    if not row or not Path(row['path']).exists():
        return {'ready': False, 'retryable': True, 'reason': 'Prepared file missing; it will be prepared again'}
    info = loads(row['metadata'], {})
    fresh = probe(row['path'])
    digest = sha256_file(row['path'])
    if digest != info.get('sha256'):
        return {'ready': False, 'reason': 'Prepared file content changed on disk; it must be prepared again',
                'assetKey': b['assetKey'], 'format': b['format']}
    info.update(width=fresh['width'], height=fresh['height'], bytes=fresh['bytes'], duration=fresh['duration'],
                checkedAt=time.time())
    with store().tx() as c:
        c.execute('UPDATE media SET metadata=? WHERE id=?', (dumps(info), mid))
    bad = rules.media_failure(info, b['format'])
    if bad:
        return {'ready': False, 'reason': bad, 'assetKey': b['assetKey'], 'format': b['format']}
    return {'ready': True, 'mediaId': mid, 'filePath': row['path'], **info}


def run_job(payload):
    kind, b = payload['kind'], payload['body']
    try:
        result = {'preflight': job_preflight, 'prepare': job_prepare, 'reverify': job_reverify}[kind](b)
    except Exception as error:  # infrastructure failure: retryable, never a content verdict
        attempt = payload.get('attempt', 1)
        result = {'ready': False, 'retryable': True, 'attempts': attempt, 'retryAt': time.time() + min(3600, 60 * 2 ** attempt),
                  'reason': kind + ' failed (temporary): ' + str(error)[:600], 'assetKey': b.get('assetKey'),
                  'contentHash': b.get('contentHash'), 'format': b.get('format')}
    with store().tx() as c:
        c.execute('INSERT OR REPLACE INTO jobs VALUES(?,?)', (payload['mid'], dumps(result)))
    for fd in payload.get('fds', []):
        try:
            os.close(fd)
        except OSError:
            pass


def maintenance(retain_days=30) -> dict:
    """Delete local prepared files only for confirmed publications older than
    the retention period. Receipts and records are retained indefinitely."""
    removed = 0
    cutoff = time.time() - retain_days * 86400
    with store().read() as c:
        rows = c.execute("SELECT m.path FROM media m JOIN ops_attempts a ON a.item_id=m.item "
                         "WHERE a.stage='published' AND a.updated<?", (cutoff,)).fetchall()
    for r in rows:
        f = Path(r['path'])
        if f.parent == ROOT / 'media' and f.exists():
            f.unlink()
            removed += 1
    return {'removedPublishedFiles': removed, 'freeBytes': shutil.disk_usage(ROOT).free}
