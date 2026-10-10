"""Media preflight/preparation, reused from the deployed helper (2026-10-09).

Jobs run detached (two FFmpeg slots per host) and write results to the legacy
``jobs``/``media`` tables, which stay the immutable verification store.
Changes from the deployed helper:
* the transfer cap is named honestly (TRANSFER_MAX_BYTES), the business limit
  stays 300,000,000 bytes;
* an explicit recheck re-measures the existing prepared file (ffprobe + sha256)
  instead of returning a cached result; its verdict is recognised by the recheck
  token, so a recheck always terminates with one result (R5 A9);
* Topaz is evidence bound to the asset by the handler; never a filename;
* a Story source of 60-65 s is trimmed at the end to 59.9 s while encoding
  (the Dropbox original is only downloaded, never changed);
* every job result carries a failure class (R5 A10): ``defect`` (the file itself
  cannot be used: permanent, waits for a new file revision), ``verdict`` (measured
  and outside policy), ``transient`` (finite retry budget, then ``exhausted``) and
  ``capacity`` (local disk; no budget consumed);
* a prepared file is found through a portable cache key resolved against the
  runtime's data root, not only the absolute path of the runtime that wrote it (R5 A4);
* the encoder normalises frame rate and audio to Instagram's published limits and
  the output is checked against them (R5-LOW-15);
* files of killed jobs are reaped once their worker is proven dead (R5 M16).
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import ipaddress
import json
import math
import os
import re
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
SOURCE_MAX_BYTES = 5_000_000_000      # largest source the worker downloads
PROBE_TIMEOUT = 60
ENCODE_TIMEOUT = 1200                 # per FFmpeg pass
MAX_TRANSIENT_ATTEMPTS = 6            # per source version + policy (job key); an explicit recheck starts a new budget
CAPACITY_RETRY_SECONDS = 1800
ORPHAN_GRACE_SECONDS = 900            # job files younger than this are never reaped

# Instagram Graph API publishing requirements, read 2026-10-10 from Meta's "IG User Media" reference
# (Reel specifications, Story video specifications, `caption` parameter). Only documented limits are enforced.
#   Reels and Stories: AAC audio, 48 kHz maximum, 1 or 2 channels; H.264/HEVC; 23-60 FPS; at most 1920 columns;
#   video bitrate VBR 25 Mbps maximum; 3 seconds minimum. Reels: 15 minutes maximum, aspect 0.01:1-10:1,
#   300 MB maximum. Stories: 60 seconds maximum, aspect 0.1:1-10:1, 100 MB maximum ("100MB": read as
#   100,000,000 bytes, the stricter reading).
# Documented but NOT enforced here (not verified against our outputs, see docs): "no edit lists"; closed GOP and
# progressive scan are libx264 defaults for the encoder settings used below.
IG_MIN_SECONDS = 3.0
IG_REEL_MAX_SECONDS = 900.0
IG_MAX_COLUMNS = 1920
IG_FPS_RANGE = (23.0, 60.0)
IG_AUDIO_MAX_RATE = 48000
IG_AUDIO_MAX_CHANNELS = 2
IG_STORY_MAX_BYTES = 100_000_000
IG_MAX_BITRATE = 25_000_000
IG_ASPECT = {'Post': (0.01, 10.0), 'Story': (0.1, 10.0)}


class MediaDefect(ValueError):
    """The file itself cannot be used; the same bytes always give the same answer (permanent until a new revision)."""


class CapacityError(ValueError):
    """Not enough local disk for the job: not the item's fault, no retry budget is consumed."""


def _now() -> float:
    return time.time()


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


def cache_key(mid: str) -> str:
    """Portable identity of a prepared file: relative to the data root of whichever runtime reads it (R5 A4)."""
    return 'media/' + mid + '.mp4'


def local_file(row, roots=None) -> Path | None:
    """The prepared file of a `media` row as this runtime sees it, or None.

    `media.path` is the absolute path in the runtime that wrote it (the n8n container). Bondok sees the same data
    volume under another root (bind mount, ProtectHome), so the path alone is not an identity. Tried in order: the
    portable cache key under each data root, the stored path, and the stored file name under <root>/media (rows
    written before cache keys existed)."""
    if not row:
        return None
    row = dict(row)
    roots = [Path(r) for r in (roots or [ROOT])]
    info = loads(row.get('metadata'), {}) or {}
    key = info.get('cacheKey')
    candidates = []
    if isinstance(key, str) and key and not key.startswith('/') and '..' not in Path(key).parts:
        candidates += [r / key for r in roots]
    stored = Path(row['path']) if row.get('path') else None
    if stored is not None:
        candidates.append(stored)
        if stored.parent.name == 'media':
            candidates += [r / 'media' / stored.name for r in roots]
    for c in candidates:
        try:
            if c.is_file():
                return c
        except OSError:            # e.g. a path inside a directory this runtime may not read
            continue
    return None


def dropbox_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while block := stream.read(4 * 1024 * 1024):
            digest.update(hashlib.sha256(block).digest())
    return digest.hexdigest()


def sha256_file(path):
    d = hashlib.sha256()
    with Path(path).open('rb') as s:
        while block := s.read(1024 * 1024):
            d.update(block)
    return d.hexdigest()


def run(args, timeout=ENCODE_TIMEOUT):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise TimeoutError(f'{Path(args[0]).name} timed out after {timeout:g} s') from None
    if r.returncode:
        raise ValueError('Media tool failed: ' + r.stderr[-700:])
    return r.stdout


def _rate(text):
    try:
        num, _, den = str(text or '').partition('/')
        v = float(num) / float(den or 1)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def probe(path):
    try:
        out = run(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)], PROBE_TIMEOUT)
    except ValueError as e:
        # ffprobe ran and refused the (hash-verified, complete) bytes: the file itself is unreadable.
        raise MediaDefect('The file cannot be read as a video (damaged or incomplete export, or an unsupported '
                          'format): ' + str(e).replace('Media tool failed: ', '').strip()[-300:]) from None
    d = json.loads(out)
    streams = d.get('streams') or []
    v = next((s for s in streams if s.get('codec_type') == 'video' and
              not (s.get('disposition') or {}).get('attached_pic')), None)
    if not v:
        raise MediaDefect('No video stream in the file (audio only or not a video)')
    try:
        duration = float((d.get('format') or {}).get('duration') or 0)
        width, height = int(v['width']), int(v['height'])
    except (KeyError, TypeError, ValueError):
        raise MediaDefect('The file has no readable duration or picture size (raw stream or damaged container)') \
            from None
    if not math.isfinite(duration) or duration <= 0:
        raise MediaDefect('The file has no readable duration (raw stream or damaged container)')
    if duration > 3600:
        raise MediaDefect('The video is longer than one hour; not supported')
    a = next((s for s in streams if s.get('codec_type') == 'audio'), None)
    return {'width': width, 'height': height, 'duration': duration,
            'bytes': Path(path).stat().st_size, 'codec': v.get('codec_name'),
            'fps': _rate(v.get('avg_frame_rate')) or _rate(v.get('r_frame_rate')),
            'audioCodec': a.get('codec_name') if a else None,
            'audioRate': int(a['sample_rate']) if a and str(a.get('sample_rate') or '').isdigit() else None,
            'audioChannels': int(a['channels']) if a and a.get('channels') else None}


def platform_failure(info: dict, fmt: str) -> str | None:
    """Instagram publishing requirements for a prepared file (see IG_* above); None when it conforms. Frame rate
    and audio are checked when measured (rows prepared before those fields existed keep their earlier verdict)."""
    try:
        w, h = float(info['width']), float(info['height'])
        d, size = float(info['duration']), float(info['bytes'])
    except (KeyError, TypeError, ValueError):
        return 'Video measurements are missing or invalid'
    if not all(math.isfinite(x) and x > 0 for x in (w, h, d, size)):
        return 'Video measurements are missing or invalid'
    if d < IG_MIN_SECONDS:
        return f'Video is {d:g} seconds; Instagram requires at least 3 seconds (3-second minimum)'
    if fmt == 'Post' and d > IG_REEL_MAX_SECONDS:
        return f'Video is {d:g} seconds; Instagram Reels are limited to 15 minutes (15-minute maximum)'
    if w > IG_MAX_COLUMNS:
        return (f'Video is {w:g} pixels wide; Instagram accepts at most 1920 columns. With the required 1080-pixel '
                'short edge only formats from 9:16 to 16:9 fit; supply a narrower edit')
    lo, hi = IG_ASPECT.get(fmt, (0.1, 10.0))
    if not lo <= w / h <= hi:
        return f'Aspect ratio {w:g}x{h:g} is outside what Instagram accepts for a {fmt}'
    if fmt == 'Story' and size >= IG_STORY_MAX_BYTES:
        return f'Story file is {size / 1e6:.1f} MB; Instagram accepts Story videos up to 100 MB'
    if size * 8 / d > IG_MAX_BITRATE:
        return f'Video bitrate {size * 8 / d / 1e6:.1f} Mbps is above Instagram\'s 25 Mbps maximum'
    fps = info.get('fps')
    if fps is not None and not IG_FPS_RANGE[0] - 0.01 <= float(fps) <= IG_FPS_RANGE[1] + 0.01:
        return f'Frame rate {float(fps):g} fps is outside Instagram\'s 23-60 FPS range'
    if info.get('audioCodec') is not None:
        if info['audioCodec'] != 'aac':
            return f"Audio codec {info['audioCodec']} is not AAC"
        if info.get('audioRate') and int(info['audioRate']) > IG_AUDIO_MAX_RATE:
            return f"Audio sample rate {int(info['audioRate'])} Hz is above Instagram's 48 kHz maximum"
        if info.get('audioChannels') and int(info['audioChannels']) not in (1, 2):
            return f"Audio has {int(info['audioChannels'])} channels; Instagram accepts 1 or 2 channels"
    return None


def _download(source, original, content_hash):
    if shutil.disk_usage(ROOT).free < rules.MEDIA_MIN_FREE_BYTES:
        raise CapacityError('Insufficient free space for safe media preparation')
    opener = build_opener(SafeRedirect())
    total = 0
    with opener.open(Request(source, headers={'User-Agent': 'WasetMediaWorker/3'}), timeout=60) as r, \
            original.open('wb') as f:
        while block := r.read(1024 * 1024):
            if shutil.disk_usage(ROOT).free < rules.MEDIA_RESERVE_BYTES:
                raise CapacityError('Disk reserve reached; media job stopped safely')
            total += len(block)
            if total > SOURCE_MAX_BYTES:
                raise MediaDefect('Source file is larger than the 5 GB preparation limit; supply a smaller export')
            f.write(block)
    if dropbox_hash(original) != content_hash:
        raise ValueError('Dropbox file changed after selection; resolve the latest revision again')


def _job_result(job_id):
    with store().read() as c:
        r = c.execute('SELECT result FROM jobs WHERE id=?', (job_id,)).fetchone()
    return loads(r['result']) if r else None


def _record(job_id, result):
    with store().tx() as c:
        c.execute('INSERT OR REPLACE INTO jobs VALUES(?,?)', (job_id, dumps(result)))


def _job_alive(job_id) -> bool:
    """True while a worker holds the job's lock (the detached job keeps it for its whole lifetime)."""
    lock = ROOT / (job_id + '.lock')
    if not lock.exists():
        return False
    with lock.open('a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
    return False


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


def _token(b):
    """Identity of the explicit recheck this request belongs to (the handler's request time), else None."""
    return b.get('recheckRequestedAt') if b.get('recheck') else None


def _failure(b, cls, reason, attempts):
    """Typed failure result. Transient failures have a finite budget per job key: the last one is `exhausted`."""
    out = {'ready': False, 'failureClass': cls, 'reason': str(reason)[:600], 'attempts': attempts,
           'assetKey': b.get('assetKey'), 'contentHash': b.get('contentHash'), 'format': b.get('format')}
    if cls == 'transient' and attempts >= MAX_TRANSIENT_ATTEMPTS:
        out.update(failureClass='exhausted', reason=f'Preparation stopped retrying after {attempts} failed attempts '
                   f'(last: {str(reason)[:400]})')
    elif cls == 'transient':
        out.update(retryable=True, retryAt=_now() + min(7200, 300 * 2 ** max(0, attempts - 1)))
    elif cls == 'capacity':
        out.update(retryable=True, retryAt=_now() + CAPACITY_RETRY_SECONDS)
    return out


def _settle(job_id, prior, b):
    """A 'running' marker is the live job (pending) or, once its lock is free, a killed job: one failed attempt
    within the transient budget, never a verdict (R5 M16)."""
    if not prior or not prior.get('running'):
        return prior
    if _job_alive(job_id):
        return {'ready': False, 'pending': True, 'running': True, 'reason': 'The same check is already running'}
    res = _failure(b, 'transient', (prior.get('kind') or 'media') + ' job stopped before finishing (worker killed or '
                   'restarted)', int(prior.get('attempts') or 1))
    if res.get('retryable'):
        res['retryAt'] = _now()                        # the kill already cost the wait
    res.update(checkedAt=_now(), recheckOf=prior.get('recheckOf'), killed=True)
    _record(job_id, res)
    return res


def _reuse(prior, recheck):
    """Immutable evidence may be reused; explicit rechecks never reuse it. A temporary failure is retried once its
    backoff has passed; the job itself turns the last allowed failure into `exhausted` (R5 A10)."""
    if not prior or recheck:
        return None
    if prior.get('retryable') and _now() >= prior.get('retryAt', 0):
        return None
    return prior


def preflight(b):
    """Story duration measured on the actual selected file before encoding."""
    if b.get('format') != 'Story':
        return {'ready': True, 'skipped': True, 'assetKey': b.get('assetKey'), 'contentHash': b.get('contentHash')}
    job_id = 'preflight-' + media_key(b)
    prior = _settle(job_id, _job_result(job_id), b)
    if prior and prior.get('pending'):
        return prior
    token = _token(b)
    if token is not None:
        if prior and prior.get('recheckOf') == token:
            return prior                      # this recheck's result: the recheck terminates (R5 A9)
        return _launch('preflight', job_id, b, 1)
    reuse = _reuse(prior, False)
    if reuse:
        return reuse
    return _launch('preflight', job_id, b, (prior or {}).get('attempts', 0) + 1)


def prepare(b):
    mid = media_key(b)
    token = _token(b)
    with store().read() as c:
        cached = c.execute('SELECT * FROM media WHERE id=?', (mid,)).fetchone()
    cached = dict(cached) if cached else None
    prior = _settle(mid, _job_result(mid), b)
    if prior and prior.get('pending'):
        return prior
    path = local_file(cached) if cached else None
    if cached and path is None:
        # The prepared file is gone (disk cleanup, restore): its old "ready" job result must not be reused
        # (audit MP3). Prepare it again from the source.
        with store().tx() as c:
            c.execute('DELETE FROM media WHERE id=?', (mid,))
        return _launch('prepare', mid, b, 1)
    if cached:
        info = loads(cached['metadata'], {})
        if token is not None and info.get('recheckOf') != token:
            if prior and prior.get('recheckOf') == token and not prior.get('ready'):
                return prior                   # the recheck's verdict (audit MP1, R5 A9)
            return _launch('reverify', mid, b, 1)
        bad = rules.media_failure(info, b['format'])
        if bad:
            return {'ready': False, 'reason': bad, 'failureClass': 'verdict', 'assetKey': b['assetKey'],
                    'format': b['format'], 'recheckOf': token}
        return {'ready': True, 'mediaId': mid, 'filePath': str(path), **info}
    if prior and prior.get('ready'):
        prior = None        # a success whose media row/file is gone is not evidence any more (audit MP3)
    if token is not None:
        if prior and prior.get('recheckOf') == token:
            return prior
        return _launch('prepare', mid, b, 1)
    reuse = _reuse(prior, False)
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
        bad = rules.story_source_failure(info['duration'])
        if bad is None and info['duration'] < IG_MIN_SECONDS:
            bad = (f"Story is {info['duration']:g} seconds; Instagram requires at least 3 seconds (3-second minimum). "
                   'Supply a longer edit')
        return {'ready': bad is None, 'duration': info['duration'], 'reason': bad, 'replaceRequired': bad is not None,
                'failureClass': 'verdict' if bad else None, 'trimTo': rules.story_trim_target(info['duration']),
                'assetKey': b['assetKey'], 'contentHash': b['contentHash'], 'checkedAt': _now()}
    finally:
        original.unlink(missing_ok=True)


def _source_policy(info, fmt):
    """Reasons a measured SOURCE cannot become a conforming prepared file (no encoding is attempted)."""
    w, h, d = info['width'], info['height'], info['duration']
    if min(w, h) < rules.MIN_SHORT_EDGE:
        return 'Source short edge is below 1080 pixels'
    if d < IG_MIN_SECONDS:
        return f'Source is {d:g} seconds; Instagram requires at least 3 seconds (3-second minimum)'
    if fmt == 'Post' and d > IG_REEL_MAX_SECONDS:
        return f'Source is {d:g} seconds; Instagram Reels are limited to 15 minutes (15-minute maximum)'
    out_w = rules.MIN_SHORT_EDGE if h >= w else 2 * round(w * rules.MIN_SHORT_EDGE / h / 2)
    if out_w > IG_MAX_COLUMNS:
        return (f'Source is {w}x{h}: at the required 1080-pixel short edge it would be {out_w} pixels wide, above '
                "Instagram's 1920-column maximum. Supply an edit between 9:16 and 16:9")
    return None


def job_prepare(b):
    mid = media_key(b)
    folder = ROOT / 'media'
    folder.mkdir(parents=True, exist_ok=True)
    original, output = folder / (mid + '.input'), folder / (mid + '.mp4')
    base = {'assetKey': b['assetKey'], 'format': b['format'], 'failureClass': 'verdict'}
    ok = False
    try:
        _download(raw_url(b['sourceUrl']), original, b['contentHash'])
        info = probe(original)
        trim = None
        if b['format'] == 'Story':
            bad = rules.story_source_failure(info['duration'])
            if bad:
                return {**base, 'ready': False, 'reason': bad}
            trim = rules.story_trim_target(info['duration'])
        bad = _source_policy(info, b['format'])
        if bad:
            return {**base, 'ready': False, 'reason': bad}
        bitrate = min(12_000_000, int(TARGET_BYTES * 8 / (trim or info['duration'])) - 160_000)
        if bitrate < 500_000:
            return {**base, 'ready': False, 'reason': 'Duration cannot fit the current transfer path at usable quality'}
        # 1080 short edge (owner policy); frame rate brought into Instagram's 23-60 FPS range only when outside it.
        vf = "scale=w='if(gte(ih,iw),1080,-2)':h='if(gte(ih,iw),-2,1080)'"
        fps = info.get('fps')
        if fps is not None and not IG_FPS_RANGE[0] <= fps <= IG_FPS_RANGE[1]:
            vf += ',fps=' + ('60' if fps > IG_FPS_RANGE[1] else '30')
        audio = ['-c:a', 'aac', '-b:a', '128k']
        if info.get('audioRate') and info['audioRate'] > IG_AUDIO_MAX_RATE:
            audio += ['-ar', str(IG_AUDIO_MAX_RATE)]
        if info.get('audioChannels') and info['audioChannels'] > IG_AUDIO_MAX_CHANNELS:
            audio += ['-ac', '2']
        # Automatic Story trim: keep the start, cut the end (output option, same for both passes).
        cut = ['-t', f'{trim:.3f}'] if trim else []
        common = ['ffmpeg', '-nostdin', '-y', '-v', 'error', '-i', str(original), '-map', '0:v:0', '-threads', '2',
                  '-vf', vf, '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-b:v', str(bitrate),
                  '-passlogfile', str(folder / mid)] + cut
        run(common + ['-pass', '1', '-an', '-f', 'null', '/dev/null'], ENCODE_TIMEOUT)
        run(common + ['-pass', '2', '-map', '0:a:0?'] + audio + ['-movflags', '+faststart', str(output)],
            ENCODE_TIMEOUT)
        result = probe(output)
        result.update(assetKey=b['assetKey'], fileId=b['fileId'], revision=b['revision'],
                      contentHash=b['contentHash'], format=b['format'], qaPolicy=rules.QA_POLICY,
                      orientation='vertical' if result['height'] > result['width'] else 'horizontal',
                      sha256=sha256_file(output), dropboxHash=dropbox_hash(output), cacheKey=cache_key(mid),
                      checkedAt=_now())
        if trim:
            result.update(trimmedFrom=info['duration'], trimmedTo=trim)
            if not trim - 0.5 <= result['duration'] < rules.STORY_MAX_SECONDS:
                return {**base, 'ready': False, 'reason': f"Automatic Story trim produced {result['duration']:g} "
                        'seconds instead of about 59.9; supply an edit under 60 seconds'}
        bad = rules.media_failure(result, b['format']) or platform_failure(result, b['format'])
        if bad:
            return {**base, 'ready': False, 'reason': bad}
        with store().tx() as c:
            c.execute('INSERT OR REPLACE INTO media VALUES(?,?,?,?,?)',
                      (mid, str(b['itemId']), str(output), raw_url(b['sourceUrl']), dumps(result)))
        ok = True
        return {'ready': True, 'mediaId': mid, 'filePath': str(output), **result}
    finally:
        original.unlink(missing_ok=True)
        if not ok:
            output.unlink(missing_ok=True)     # no partial output left behind (audit MP6)
        for f in folder.glob(mid + '-*'):
            f.unlink(missing_ok=True)


def job_reverify(b):
    """Explicit recheck of an existing prepared file: fresh measurements."""
    mid = media_key(b)
    with store().read() as c:
        row = c.execute('SELECT * FROM media WHERE id=?', (mid,)).fetchone()
    path = local_file(row) if row else None
    if not row or path is None:
        return {'ready': False, 'retryable': True, 'failureClass': 'transient', 'attempts': 0, 'retryAt': 0,
                'reason': 'Prepared file missing; it will be prepared again', 'assetKey': b['assetKey'],
                'format': b['format']}
    info = loads(row['metadata'], {})
    digest = sha256_file(path)
    if digest != info.get('sha256'):
        # Our own copy is damaged: discard it and prepare again from the source; never an editor issue.
        with store().tx() as c:
            c.execute('DELETE FROM media WHERE id=?', (mid,))
        path.unlink(missing_ok=True)
        return {'ready': False, 'retryable': True, 'failureClass': 'transient', 'attempts': 0, 'retryAt': 0,
                'reverify': True, 'reason': 'Prepared file changed on disk; preparing it again',
                'assetKey': b['assetKey'], 'format': b['format']}
    fresh = probe(path)
    info.update({k: fresh[k] for k in ('width', 'height', 'bytes', 'duration', 'fps', 'audioCodec', 'audioRate',
                                       'audioChannels')}, checkedAt=_now(), recheckOf=_token(b))
    with store().tx() as c:
        c.execute('UPDATE media SET metadata=? WHERE id=?', (dumps(info), mid))
    bad = rules.media_failure(info, b['format']) or platform_failure(info, b['format'])
    if bad:
        return {'ready': False, 'reason': bad, 'failureClass': 'verdict', 'assetKey': b['assetKey'],
                'format': b['format'], 'reverify': True}
    return {'ready': True, 'mediaId': mid, 'filePath': str(path), **info}


def run_job(payload):
    kind, b = payload['kind'], payload['body']
    attempt = int(payload.get('attempt', 1))
    token = _token(b)
    try:                                        # marker: a job that dies leaves evidence of its attempt (R5 M16)
        _record(payload['mid'], {'running': True, 'kind': kind, 'attempts': attempt, 'startedAt': _now(),
                                 'recheckOf': token})
    except Exception:  # noqa: BLE001 - the job still runs; its result write below reports any storage problem
        pass
    try:
        result = {'preflight': job_preflight, 'prepare': job_prepare, 'reverify': job_reverify}[kind](b)
    except MediaDefect as error:            # the file itself: permanent until a new revision (R5 A10)
        result = _failure(b, 'defect', str(error), attempt)
    except CapacityError as error:          # local disk: retried, no budget consumed
        result = _failure(b, 'capacity', kind + ' postponed: ' + str(error), attempt - 1)
    except Exception as error:  # noqa: BLE001 - transport/tool failure: finite budget, then escalation
        result = _failure(b, 'transient', kind + ' failed (temporary): ' + str(error)[:500], attempt)
    result.setdefault('checkedAt', _now())
    result['recheckOf'] = token
    with store().tx() as c:
        c.execute('INSERT OR REPLACE INTO jobs VALUES(?,?)', (payload['mid'], dumps(result)))
    for fd in payload.get('fds', []):
        try:
            os.close(fd)
        except OSError:
            pass


_JOB_FILE = re.compile(r'^([0-9a-f]{64})(\.input|\.mp4|-\d+\.log(?:\.mbtree)?(?:\.temp)?)$')


def reap_orphans(grace=ORPHAN_GRACE_SECONDS) -> int:
    """Remove temporary files of media jobs whose worker is proven dead (its lock is free): the downloaded source,
    FFmpeg pass logs and a partial output (R5 M16). Never touches a file of a running job, a file younger than the
    grace period, or anything a `media` row refers to (verified outputs, which reservations and publication
    attempts reference); lock files are left in place (removing a lock another process may open is unsafe)."""
    with store().read() as c:
        referenced = {r['id'] for r in c.execute('SELECT id FROM media')}
        try:
            referenced |= {r['verification_id'] for r in c.execute(
                'SELECT verification_id FROM ops_items WHERE verification_id IS NOT NULL')}
            referenced |= {loads(r['payload'], {}).get('verification_id') for r in c.execute(
                "SELECT payload FROM ops_attempts WHERE stage IN ('claimed','container_created','committed',"
                "'outcome_unknown')")}
        except Exception:  # noqa: BLE001 - legacy store without ops tables: media rows are the references
            pass
    removed, now = 0, time.time()
    for folder, prefix in (('media', ''), ('preflight', 'preflight-')):
        d = ROOT / folder
        if not d.is_dir():
            continue
        for f in d.iterdir():
            m = _JOB_FILE.match(f.name)
            if not m or not f.is_file() or m[1] in referenced:
                continue
            try:
                if now - f.stat().st_mtime < grace or _job_alive(prefix + m[1]):
                    continue
            except OSError:
                continue
            f.unlink(missing_ok=True)
            removed += 1
    return removed


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
    # Prepared files of superseded versions (file replaced, format changed) were never removed and filled the
    # disk until every download failed the free-space guard (audit MP6). Removed after 7 days unless the
    # current item version, its verification or any unfinished publication attempt still refers to them.
    superseded = 0
    old = time.time() - 7 * 86400
    with store().tx() as c:
        live = {loads(a['payload'], {}).get('verification_id') for a in c.execute(
            "SELECT payload FROM ops_attempts WHERE stage IN ('claimed','container_created','committed','outcome_unknown')")}
        for m in c.execute('SELECT m.id, m.path, m.metadata, i.asset_key, i.format, i.verification_id FROM media m '
                           'LEFT JOIN ops_items i ON i.item_id=m.item').fetchall():
            info = loads(m['metadata'], {}) or {}
            current = info.get('assetKey') == m['asset_key'] and info.get('format') == m['format']
            f = Path(m['path'])
            if current or m['id'] == m['verification_id'] or m['id'] in live or f.parent != ROOT / 'media':
                continue
            if f.exists() and f.stat().st_mtime > old:
                continue
            f.unlink(missing_ok=True)
            c.execute('DELETE FROM media WHERE id=?', (m['id'],))
            superseded += 1
    orphans = reap_orphans()
    return {'removedPublishedFiles': removed, 'removedSupersededFiles': superseded, 'removedOrphanFiles': orphans,
            'freeBytes': shutil.disk_usage(ROOT).free}
