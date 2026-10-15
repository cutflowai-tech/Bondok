"""Waset local n8n helper. No HTTP service. Invoked only by disarmed n8n drafts.
FFmpeg, transactional slots and publication receipts persist in n8n storage.
Dropbox/Instagram/Monday credentials stay in their existing n8n credential store.
"""
import fcntl
import base64
import sys
from contextlib import closing
import hashlib
import hmac
import ipaddress
import math
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse, parse_qsl
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

ROOT = Path(os.environ.get('WASET_SOCIAL_DATA_DIR', str(Path.home()/'.n8n-files/waset-social')))
TZ = ZoneInfo('Africa/Cairo')
MAX_BYTES = 300_000_000
REELS = {5: (21, 0), 0: (21, 0), 2: (22, 45), 3: (21, 0)}
STORIES = [(11, 0), (14, 0), (18, 0), (21, 0), (22, 0)]


QA_POLICY = 3
LEASE_SECONDS = 180


def media_failure(info, fmt, topazed):
    """Fail closed for all final-file checks, including cached media."""
    if topazed is not True:
        return 'Topaz version confirmation is required'
    if fmt not in ('Post', 'Story'):
        return 'A valid Post or Story format is required'
    try:
        width, height = float(info['width']), float(info['height'])
        size, duration = float(info['bytes']), float(info['duration'])
        if not all(math.isfinite(x) and x > 0 for x in (width,height,size,duration)):
            return 'Video measurements are missing or invalid'
    except (ValueError, TypeError, KeyError):
        return 'Video measurements are missing or invalid'
    if min(width, height) < 1080:
        return 'Video short edge must be at least 1080 pixels'
    if size >= MAX_BYTES:
        return 'Final video must be strictly under 300 MB'
    if fmt == 'Story' and duration >= 60:
        return 'Story must be strictly under 60 seconds; send a shorter edit'
    return None


def verified_media(info, fmt):
    if info.get('format') != fmt:
        return 'Media format changed; verify the selected file again'
    if info.get('qaPolicy') != QA_POLICY:
        return 'Media requires verification under the current quality rules'
    return media_failure(info, fmt, info.get('topazed'))


def media_key(b, source):
    if not all(b.get(k) for k in ('fileId','revision','contentHash','assetKey')):
        raise ValueError('Exact Dropbox file ID, revision and content hash are required')
    identity = '|'.join(str(b.get(k,'')) for k in ('itemId','fileId','revision','contentHash','format','assetKey'))
    return hashlib.sha256((identity+'|qa'+str(QA_POLICY)).encode()).hexdigest()


def dropbox_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(4*1024*1024):
            digest.update(hashlib.sha256(block).digest())
    return digest.hexdigest()


def style(code):
    m = re.match(r'^\s*([a-z]{2,3})\s*[#_\- ]?\s*\d+', str(code), re.I)
    if not m:
        raise ValueError('Code must begin with 2–3 letters followed by a number')
    return m[1].upper()


def instant(value):
    d = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if d.tzinfo is None:
        raise ValueError('Timestamp must contain a timezone')
    return d.astimezone(timezone.utc)


def slots(now, fmt, horizon=84):
    local = now.astimezone(TZ)
    for offset in range(horizon):
        day = local.date() + timedelta(days=offset)
        times = STORIES if fmt == 'Story' else ([REELS[day.weekday()]] if day.weekday() in REELS else [])
        for hour, minute in times:
            d = datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)
            if d > now + timedelta(minutes=5):
                yield d.astimezone(timezone.utc)


def next_slot(now, fmt, code, occupied, weekday_map=None, rotation=None):
    prefix = style(code)
    key = rotation or prefix
    events = sorted(((instant(e['at']), e.get('rotation') or e.get('style')) for e in occupied if e.get('format') == fmt), key=lambda e:e[0])
    used = {d for d, _ in events}
    allowed = (weekday_map or {}).get(prefix)
    fallback = None
    for at in slots(now, fmt):
        if at in used: continue
        if fmt == 'Post' and allowed and at.astimezone(TZ).isoweekday() not in allowed: continue
        if fallback is None: fallback = at
        # Diversity is a bounded preference; scarce styles cannot starve the queue.
        if at-fallback > timedelta(days=7 if fmt=='Post' else 1): break
        before = [s for d,s in events if d < at]
        after = [s for d,s in events if d > at]
        if (before and before[-1]==key) or (after and after[0]==key): continue
        return at
    return fallback


def db():
    ROOT.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(ROOT / 'state.sqlite', timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    c.executescript('''
      CREATE TABLE IF NOT EXISTS locks (name TEXT PRIMARY KEY, owner TEXT, until REAL);
      CREATE TABLE IF NOT EXISTS reservations (item TEXT PRIMARY KEY, format TEXT,
        style TEXT, at TEXT, committed INTEGER DEFAULT 0, UNIQUE(format,at));
      CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, result TEXT);
      CREATE TABLE IF NOT EXISTS media (id TEXT PRIMARY KEY, item TEXT, path TEXT,
        source TEXT, metadata TEXT);
      CREATE TABLE IF NOT EXISTS publications (item TEXT PRIMARY KEY, owner TEXT,
        stage TEXT, data TEXT, updated REAL);
    ''')
    c.execute('CREATE TABLE IF NOT EXISTS assets (identity TEXT PRIMARY KEY, item TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS health (name TEXT PRIMARY KEY, updated REAL, data TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS item_state (item TEXT PRIMARY KEY, format TEXT, asset TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY, item TEXT, kind TEXT, detail TEXT, created REAL)')
    return c


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
            'bytes': path.stat().st_size, 'codec': v.get('codec_name')}


def prepare_media(b):
    if b.get('topazed') is not True:
        return {'ready':False,'needsEditor':True,'reason':'Topaz version confirmation is required'}
    if b.get('format') not in ('Post','Story'):
        return {'ready':False,'needsReview':True,'reason':'Post or Story format is required'}
    p = urlparse(safe_download_url(b['sourceUrl']))
    q = dict(parse_qsl(p.query)); q.pop('dl', None); q.pop('raw', None); q['raw'] = '1'
    source = urlunparse(p._replace(query=urlencode(q)))
    # Source revision/path comes from Dropbox metadata; no arbitrary cache reuse.
    mid = media_key(b, source)
    with closing(db()) as c:
        old = c.execute('SELECT * FROM media WHERE id=?', (mid,)).fetchone()
    if old and Path(old['path']).exists():
        reason = verified_media(json.loads(old['metadata']), b['format'])
        if reason: return {'ready':False,'needsEditor':True,'reason':reason}
        return {'ready': True, 'mediaId': mid, 'filePath': old['path'], **json.loads(old['metadata'])}
    if not b.get('topazed'):
        return {'ready': False, 'needsEditor': True, 'reason': 'Topaz version required'}
    folder = ROOT / 'media'
    folder.mkdir(parents=True, exist_ok=True)
    original, output = folder / (mid + '.input'), folder / (mid + '.mp4')
    try:
        if shutil.disk_usage(ROOT).free < 6_000_000_000: raise ValueError('Insufficient free space for safe media preparation')
        opener = build_opener(SafeRedirect())
        total = 0
        with opener.open(Request(source, headers={'User-Agent': 'WasetMediaWorker/2'}), timeout=60) as r, original.open('wb') as f:
            while block := r.read(1024*1024):
                if shutil.disk_usage(ROOT).free < 2_000_000_000: raise ValueError('Disk reserve reached; media job stopped safely')
                total += len(block)
                if total > 5_000_000_000:
                    raise ValueError('Source exceeds 5 GB')
                f.write(block)
        if dropbox_hash(original) != b['contentHash']:
            raise ValueError('Dropbox file changed after selection; resolve the latest revision again')
        info = probe(original)
        if b['format']=='Story' and info['duration'] >= 60:
            return {'ready':False,'needsEditor':True,'reason':'Story must be strictly under 60 seconds; send a shorter edit'}
        if min(info['width'], info['height']) < 1080:
            return {'ready': False, 'needsEditor': True, 'reason': 'Source short edge is below 1080p'}
        # H.264/AAC MP4; cap derivative below Dropbox's single-upload limit,
        # which also satisfies the requested under-300-MB limit.
        scale = "scale=w='if(gte(ih,iw),1080,-2)':h='if(gte(ih,iw),-2,1080)'"
        bitrate = min(12_000_000, int(140_000_000*8/info['duration']) - 160_000)
        if bitrate < 500_000:
            return {'ready': False, 'needsEditor': True, 'reason': 'Duration cannot fit under 300 MB at usable quality'}
        common = ['ffmpeg', '-nostdin', '-y', '-v', 'error', '-i', str(original), '-map', '0:v:0',
                  '-threads', '2', '-vf', scale, '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-b:v', str(bitrate),
                  '-passlogfile', str(folder / mid)]
        run(common + ['-pass', '1', '-an', '-f', 'null', '/dev/null'])
        run(common + ['-pass', '2', '-map', '0:a:0?', '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', str(output)])
        result = probe(output)
        reason = media_failure(result,b['format'],b['topazed'])
        if reason:
            output.unlink(missing_ok=True)
            return {'ready':False,'needsEditor':True,'reason':reason}
        result.update(topazed=True,topazVerification='editor_confirmation_bound_to_asset',assetKey=b['assetKey'],fileId=b['fileId'],revision=b['revision'],contentHash=b['contentHash'],format=b['format'],qaPolicy=QA_POLICY)
        if min(result['width'], result['height']) < 1080 or result['bytes'] >= 145_000_000:
            output.unlink(missing_ok=True)
            raise ValueError('Output failed 1080p / under 300 MB checks')
        result['orientation'] = 'vertical' if result['height'] > result['width'] else 'horizontal'
        digest = hashlib.sha256()
        with output.open('rb') as stream:
            while block := stream.read(1024*1024):
                digest.update(block)
        result['sha256'] = digest.hexdigest()
        with closing(db()) as c:
            c.execute('INSERT OR REPLACE INTO media VALUES(?,?,?,?,?)', (mid, str(b['itemId']), str(output), source, json.dumps(result)))
        return {'ready': True, 'mediaId': mid, 'filePath': str(output), **result}
    finally:
        original.unlink(missing_ok=True)
        for f in folder.glob(mid + '-*'):
            f.unlink(missing_ok=True)


def duration_result(duration):
    if not isinstance(duration, (int,float)) or not math.isfinite(duration) or duration <= 0:
        return {'ready':False,'needsReview':True,'reason':'تعذر التأكد من مدة الستوري؛ الجدولة متوقفة حتى التحقق من الملف.'}
    if duration >= 60:
        return {'ready':False,'needsReview':True,'replaceRequired':True,'duration':duration,
                'reason':f'مدة الستوري {duration:g} ثانية؛ لازم تكون أقل من 60 ثانية. استبدل الفيديو بنسخة أقصر؛ لم يتم حجز موعد أو بدء توباز أو التصغير.'}
    return {'ready':True,'duration':duration}


def preflight_media(b):
    source = safe_download_url(b['sourceUrl'])
    p = urlparse(source)
    q = dict(parse_qsl(p.query)); q.pop('dl',None); q.pop('raw',None); q['raw']='1'
    source = urlunparse(p._replace(query=urlencode(q)))
    mid = media_key(b, source)
    folder = ROOT / 'preflight'
    folder.mkdir(parents=True, exist_ok=True)
    original, output = folder / (mid + '.input'), folder / (mid + '.mp4')
    try:
        if shutil.disk_usage(ROOT).free < 6_000_000_000: raise ValueError('Insufficient free space for safe media preparation')
        opener = build_opener(SafeRedirect())
        total = 0
        with opener.open(Request(source, headers={'User-Agent': 'WasetMediaWorker/2'}), timeout=60) as r, original.open('wb') as f:
            while block := r.read(1024*1024):
                if shutil.disk_usage(ROOT).free < 2_000_000_000: raise ValueError('Disk reserve reached; media job stopped safely')
                total += len(block)
                if total > 5_000_000_000:
                    raise ValueError('Source exceeds 5 GB')
                f.write(block)
        if dropbox_hash(original) != b['contentHash']:
            raise ValueError('Dropbox file changed after selection; resolve the latest revision again')
        info = probe(original)
        return {**duration_result(info['duration']), 'assetKey':b['assetKey'], 'contentHash':b['contentHash']}
    finally:
        original.unlink(missing_ok=True)


def preflight(b):
    if b.get('format') not in ('Post','Story'):
        return {'ready':False,'needsReview':True,'reason':'Post or Story format is required'}
    if b['format']=='Post': return {'ready':True,'skipped':True}
    source = safe_download_url(b['sourceUrl'])
    q = dict(parse_qsl(urlparse(source).query)); q.pop('dl', None); q.pop('raw', None); q['raw'] = '1'
    source = urlunparse(urlparse(source)._replace(query=urlencode(q)))
    mid = 'preflight-' + media_key(b, source)
    with closing(db()) as c:
        result = c.execute('SELECT result FROM jobs WHERE id=?', (mid,)).fetchone()
    if result:
        prior = json.loads(result['result'])
        if not prior.get('retryable') or prior.get('attempts',0)>=3 or time.time()<prior.get('retryAt',0):
            return prior
    ROOT.mkdir(parents=True, exist_ok=True)
    lock = (ROOT / (mid + '.lock')).open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return {'ready': False, 'pending': True, 'reason': 'جارٍ فحص مدة الستوري قبل التجهيز والجدولة'}
    # Only two transcoding processes across all n8n executions on this host.
    capacity = None
    for slot in range(2):
        handle = (ROOT / ('media-capacity-' + str(slot) + '.lock')).open('a')
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB); capacity = handle; break
        except BlockingIOError:
            handle.close()
    if capacity is None:
        lock.close()
        return {'ready': False, 'pending': True, 'reason': 'في انتظار فحص مدة الستوري؛ لم يتم حجز موعد'}
    payload = {'path':'/internal/preflight-job','body':b,'mid':mid,'fds':[lock.fileno(),capacity.fileno()],'attempt':(json.loads(result['result']).get('attempts',0) if result else 0)+1}
    arg = base64.b64encode(json.dumps(payload).encode()).decode()
    subprocess.Popen([sys.executable, __file__, arg], start_new_session=True,
                     pass_fds=(lock.fileno(),capacity.fileno()), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    lock.close(); capacity.close()
    return {'ready': False, 'pending': True, 'reason': 'جارٍ التحقق من مدة الستوري؛ النتيجة في دورة التحقق التالية قبل أي تجهيز أو جدولة'}


def prepare(b):
    if b.get('topazed') is not True:
        return {'ready':False,'needsEditor':True,'reason':'Topaz version confirmation is required'}
    if b.get('format') not in ('Post','Story'):
        return {'ready':False,'needsReview':True,'reason':'Post or Story format is required'}
    source = safe_download_url(b['sourceUrl'])
    q = dict(parse_qsl(urlparse(source).query)); q.pop('dl', None); q.pop('raw', None); q['raw'] = '1'
    source = urlunparse(urlparse(source)._replace(query=urlencode(q)))
    mid = media_key(b, source)
    with closing(db()) as c:
        cached = c.execute('SELECT * FROM media WHERE id=?', (mid,)).fetchone()
        result = c.execute('SELECT result FROM jobs WHERE id=?', (mid,)).fetchone()
    if cached and Path(cached['path']).exists():
        reason = verified_media(json.loads(cached['metadata']), b['format'])
        if reason: return {'ready':False,'needsEditor':True,'reason':reason}
        return {'ready': True, 'mediaId': mid, 'filePath': cached['path'], **json.loads(cached['metadata'])}
    if not b.get('topazed'):
        return {'ready': False, 'needsEditor': True, 'reason': 'Topaz version required'}
    if result:
        prior = json.loads(result['result'])
        if not prior.get('retryable') or prior.get('attempts',0)>=3 or time.time()<prior.get('retryAt',0):
            return prior
    ROOT.mkdir(parents=True, exist_ok=True)
    lock = (ROOT / (mid + '.lock')).open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return {'ready': False, 'pending': True, 'reason': 'FFmpeg preparation is in progress inside n8n'}
    # Only two transcoding processes across all n8n executions on this host.
    capacity = None
    for slot in range(2):
        handle = (ROOT / ('media-capacity-' + str(slot) + '.lock')).open('a')
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB); capacity = handle; break
        except BlockingIOError:
            handle.close()
    if capacity is None:
        lock.close()
        return {'ready': False, 'pending': True, 'reason': 'Waiting for an available FFmpeg slot inside n8n'}
    payload = {'path':'/internal/media-job','body':b,'mid':mid,'fds':[lock.fileno(),capacity.fileno()],'attempt':(json.loads(result['result']).get('attempts',0) if result else 0)+1}
    arg = base64.b64encode(json.dumps(payload).encode()).decode()
    subprocess.Popen([sys.executable, __file__, arg], start_new_session=True,
                     pass_fds=(lock.fileno(),capacity.fileno()), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    lock.close(); capacity.close()
    return {'ready': False, 'pending': True, 'reason': 'FFmpeg job started inside n8n; checked on the next cycle'}


def monitor_plan(c, items):
    """Only propose bounded, reversible changes. Fresh board read is required before apply."""
    now=datetime.now(timezone.utc)
    jobs=[json.loads(r['result']) for r in c.execute('SELECT result FROM jobs')]
    def measured(i):
        return next((j for j in jobs if j.get('assetKey')==i.get('asset') and j.get('replaceRequired')),None)
    def verified(i):
        r=c.execute('SELECT * FROM media WHERE id=? AND item=?',(i.get('media',''),str(i['id']))).fetchone()
        if not r:return False
        info=json.loads(r['metadata'])
        return bool(i.get('topazed') and i.get('processedFormat')==i['format'] and info.get('assetKey')==i.get('asset') and not verified_media(info,i['format']) and Path(r['path']).exists() and info.get('url')==i.get('url'))
    def rotation_key(i):
        try:return i.get('rotation') or style(i.get('code',''))
        except ValueError:return None
    active=[i for i in items if i['status']=='Scheduled']
    active.sort(key=lambda i:(i.get('at') or '',str(i['id'])))
    plans=[];seen=set();previous={}
    for i in active:
        if c.execute('SELECT 1 FROM publications WHERE item=?',(str(i['id']),)).fetchone():continue
        reason=None;kind='block';stamp=None;status='Needs Review'
        try:stamp=instant(i['at']) if i.get('at') else None
        except (ValueError,TypeError):pass
        duration=measured(i) if i['format']=='Story' else None
        good=verified(i)
        if duration:
            reason=duration['reason'];status='ستوري طويل'
        elif not good:
            reason=i.get('issue') or 'الملف غير معتمد للنشر؛ يجب إكمال فحص المدة وتوباز والدقة والحجم أولًا'
        elif not rotation_key(i):reason='كود المشروع غير صالح لتحديد الستايل'
        elif not stamp:reason='موعد النشر غير مكتمل؛ اختر موعدًا صالحًا'
        elif stamp<now-timedelta(hours=2):reason='فات موعد النشر؛ يلزم اختيار موعد جديد دون إعادة نشر تلقائية'
        elif stamp<=now+timedelta(minutes=10):continue
        else:
            local=stamp.astimezone(TZ);hm=(local.hour,local.minute)
            slot_ok=(hm in STORIES if i['format']=='Story' else REELS.get(local.weekday())==hm) and local.second==0
            slot=(i['format'],stamp)
            key=rotation_key(i)
            alt=any(x['format']==i['format'] and rotation_key(x) and rotation_key(x)!=key and verified(x) for x in active)
            if not slot_ok:reason='الموعد خارج جدول النشر المتفق عليه';kind='reschedule'
            elif slot in seen:reason='تعارض مع عنصر آخر في نفس موعد ونوع النشر';kind='reschedule'
            elif previous.get(i['format'])==key and alt:reason='تتابع نفس الستايل مع وجود بديل معتمد';kind='reschedule'
            else:seen.add(slot);previous[i['format']]=key
            if not reason:
                r=c.execute('SELECT * FROM reservations WHERE item=?',(str(i['id']),)).fetchone()
                if not r or not r['committed'] or r['format']!=i['format'] or instant(r['at'])!=stamp:
                    reason='الموعد المكتوب غير مطابق للحجز المعتمد';kind='reschedule'
        if reason:
            plans.append({'item':i,'kind':kind,'reason':reason,'status':status})
    return {'plans':plans,'checked':len(items),'scheduled':len(active),'checkedAt':now.isoformat()}


def action(path, b):
    if path == '/v1/media/preflight':
        return preflight(b)
    if path == '/v1/media/prepare':
        return prepare(b)
    c = db()
    try:
        c.execute('BEGIN IMMEDIATE')
        if path == '/v1/lock':
            name, owner = b['name'], b['owner']
            old = c.execute('SELECT * FROM locks WHERE name=?', (name,)).fetchone()
            ok = not old or old['until'] < time.time() or old['owner'] == owner
            if ok:
                c.execute('INSERT OR REPLACE INTO locks VALUES(?,?,?)', (name, owner, time.time()+2100))
            out = {'acquired': ok}
        elif path == '/v1/unlock':
            c.execute('DELETE FROM locks WHERE name=? AND owner=?', (b['name'], b['owner']))
            out = {'released': True}
        elif path == '/v1/item/invalidate':
            item = str(b['itemId'])
            old = c.execute('SELECT * FROM item_state WHERE item=?',(item,)).fetchone()
            receipt = c.execute('SELECT stage FROM publications WHERE item=?',(item,)).fetchone()
            changed = old and (old['format'] != b['format'] or old['asset'] != b['assetKey'])
            if changed and receipt:
                raise ValueError('للعنصر محاولة نشر مسجلة؛ راجع نتيجتها قبل تغيير المحتوى')
            if changed:
                c.execute('DELETE FROM reservations WHERE item=?',(item,))
                c.execute('INSERT INTO audit_log(item,kind,detail,created) VALUES(?,?,?,?)',(item,'content_changed',json.dumps({'old':dict(old),'new':b},ensure_ascii=False),time.time()))
            c.execute('INSERT OR REPLACE INTO item_state VALUES(?,?,?)',(item,b['format'],b['assetKey']))
            reservation = c.execute('SELECT * FROM reservations WHERE item=?',(item,)).fetchone()
            media = c.execute('SELECT * FROM media WHERE id=? AND item=?',(b.get('mediaId',''),item)).fetchone()
            reuse = False
            if reservation and media and b.get('scheduled') and b.get('at') and not changed:
                info=json.loads(media['metadata'])
                reuse=bool(reservation['committed'] and reservation['format']==b['format'] and instant(reservation['at'])==instant(b['at']) and info.get('assetKey')==b['assetKey'] and not verified_media(info,b['format']) and Path(media['path']).exists())
            out={'ok':True,'changed':bool(changed),'reuseScheduled':reuse}
        elif path == '/v1/media/delivered':
            row = c.execute('SELECT * FROM media WHERE id=? AND item=?',(b['mediaId'],str(b['itemId']))).fetchone()
            if not row or not Path(row['path']).exists(): raise ValueError('Verified file not found')
            reason = verified_media(json.loads(row['metadata']),b['format'])
            if reason: raise ValueError(reason)
            safe_download_url(b['url'])
            metadata = {**json.loads(row['metadata']), 'url':b['url']}
            c.execute('UPDATE media SET metadata=? WHERE id=?',(json.dumps(metadata),b['mediaId']))
            out = {'ready':True, 'mediaId':b['mediaId'], **metadata}
        elif path == '/v1/schedule/reconcile':
            snapshot = {str(e['itemId']): e for e in b['items']}
            for row in c.execute('SELECT * FROM reservations').fetchall():
                e = snapshot.get(row['item'])
                if not e or e.get('status') not in ('Scheduled', 'Posted') or not e.get('at') or instant(e['at']) != instant(row['at']):
                    c.execute('DELETE FROM reservations WHERE item=?', (row['item'],))
            out = {'ok': True}
        elif path == '/v1/schedule/reserve':
            existing = c.execute('SELECT * FROM reservations WHERE item=?', (str(b['itemId']),)).fetchone()
            if existing and existing['format'] != b['format']:
                c.execute('DELETE FROM reservations WHERE item=?',(str(b['itemId']),))
                existing=None
            if existing and b.get('requestedAt') and instant(existing['at'])!=instant(b['requestedAt']):
                c.execute('DELETE FROM reservations WHERE item=?',(str(b['itemId']),))
                existing=None
            if existing:
                out = {'reserved': True, 'at': existing['at']}
            else:
                rows = c.execute('SELECT * FROM reservations').fetchall()
                occupied = b['occupied'] + [dict(r) for r in rows]
                if b.get('preserveSchedule'):
                    if not b.get('requestedAt'): raise ValueError('Existing schedule is missing; choose a publication time')
                    at = instant(b['requestedAt'])
                    if at <= datetime.now(timezone.utc)+timedelta(minutes=5):
                        raise ValueError('Existing publication time is due or past before verification; choose a new time')
                    if any(e.get('format')==b['format'] and str(e.get('itemId',e.get('item')))!=str(b['itemId']) and instant(e['at'])==at for e in occupied):
                        raise ValueError('Existing publication time collides with another item; choose a free slot')
                else:
                    at = next_slot(datetime.now(timezone.utc), b['format'], b['code'], occupied, b.get('styleWeekdays'), b.get('rotation'))
                if at is None:
                    out = {'reserved': False, 'reason': 'No free slot in the scheduling horizon'}
                else:
                    c.execute('INSERT INTO reservations(item,format,style,at) VALUES(?,?,?,?)',
                              (str(b['itemId']), b['format'], b.get('rotation') or style(b['code']), at.isoformat()))
                    local = at.astimezone(TZ)
                    out = {'reserved': True, 'at': at.isoformat(), 'date': local.date().isoformat(), 'hour': local.hour, 'minute': local.minute}
            if out.get('reserved') and 'date' not in out:
                local = instant(out['at']).astimezone(TZ)
                out.update(date=local.date().isoformat(), hour=local.hour, minute=local.minute)
        elif path == '/v1/schedule/commit':
            c.execute('UPDATE reservations SET committed=1 WHERE item=? AND at=?', (str(b['itemId']), b['at']))
            out = {'ok': True}
        elif path == '/v1/publish/snapshot':
            out = {'receipts': [{**dict(r), 'data': json.loads(r['data'])} for r in c.execute('SELECT * FROM publications')]}
        elif path == '/v1/publish/claim':
            item = str(b['itemId'])
            row = c.execute('SELECT * FROM publications WHERE item=?',(item,)).fetchone()
            late = (datetime.now(timezone.utc)-instant(b['at'])).total_seconds()
            media = c.execute('SELECT * FROM media WHERE id=? AND item=?',(b['mediaId'],item)).fetchone()
            reservation=c.execute('SELECT * FROM reservations WHERE item=?',(item,)).fetchone()
            if not reservation or not reservation['committed'] or reservation['format']!=b['format'] or instant(reservation['at'])!=instant(b['at']):
                raise ValueError('موعد النشر غير معتمد أو تغير؛ أعد التحقق من الجدولة')
            if not media or not Path(media['path']).exists(): raise ValueError('Verified media is missing')
            info = json.loads(media['metadata'])
            reason = verified_media(info,b['format'])
            if b.get('topazed') is not True or b.get('assetKey') != info.get('assetKey'):
                reason = 'Topaz confirmation or source asset changed'
            if reason: raise ValueError(reason)
            if not info.get('url') or info['url']!=b.get('expectedUrl'): raise ValueError('Verified delivery URL changed')
            identity = str(b['account'])+'|'+b['format']+'|'+info['contentHash']
            duplicate = c.execute('SELECT item FROM assets WHERE identity=?',(identity,)).fetchone()
            if duplicate and duplicate['item']!=item:
                out = {'claimed':False,'stage':'duplicate_asset','note':'This exact file is reserved or published by item '+duplicate['item']}
            elif late<0 or late>7200:
                out = {'claimed':False,'stage':'outside_due_window'}
            elif row and row['stage'] in ('publish_requested','published','source_synced'):
                out = {'claimed':False,'stage':row['stage'],'receipt':json.loads(row['data'])}
            elif row and row['updated']+LEASE_SECONDS>time.time():
                out = {'claimed':False,'stage':'lease_busy','quiet':True}
            else:
                data = json.loads(row['data']) if row else {k:b[k] for k in ('itemId','at','mediaId','sourceProjectId','format')}
                if row and any(str(data.get(k))!=str(b.get(k)) for k in ('mediaId','sourceProjectId','format','at')):
                    raise ValueError('Claim content changed; reconcile the previous container first')
                data.update(sourceSynced=False,identity=identity)
                stage = row['stage'] if row else 'claimed'
                c.execute('INSERT OR REPLACE INTO publications VALUES(?,?,?,?,?)',(item,b['owner'],stage,json.dumps(data),time.time()))
                c.execute('INSERT OR IGNORE INTO assets VALUES(?,?)',(identity,item))
                out = {'claimed':True,'url':info['url'],'receipt':data,'stage':stage}
        elif path == '/v1/monitor/plan':
            out=monitor_plan(c,b['items'])
        elif path == '/v1/monitor/reserve':
            item=str(b['itemId'])
            if c.execute('SELECT 1 FROM publications WHERE item=?',(item,)).fetchone():
                raise ValueError('Existing publication must be reconciled manually')
            row=c.execute('SELECT * FROM media WHERE id=? AND item=?',(b['mediaId'],item)).fetchone()
            if not row:raise ValueError('Verified media missing')
            info=json.loads(row['metadata'])
            if verified_media(info,b['format']) or info.get('assetKey')!=b['assetKey'] or not Path(row['path']).exists():
                raise ValueError('Content no longer verified')
            if instant(b['at'])<=datetime.now(timezone.utc)+timedelta(minutes=10):
                raise ValueError('Never move a slot that is near publication')
            occupied=[e for e in b['occupied'] if str(e.get('itemId'))!=item]
            occupied += [dict(r) for r in c.execute('SELECT * FROM reservations WHERE item<>?',(item,))]
            new_at=next_slot(datetime.now(timezone.utc)+timedelta(minutes=10),b['format'],b['code'],occupied,rotation=b.get('rotation'))
            if not new_at:raise ValueError('No safe future slot')
            c.execute('DELETE FROM reservations WHERE item=?',(item,))
            c.execute('INSERT INTO reservations(item,format,style,at) VALUES(?,?,?,?)',(item,b['format'],b.get('rotation') or style(b['code']),new_at.isoformat()))
            local=new_at.astimezone(TZ)
            out={'at':new_at.isoformat(),'date':local.date().isoformat(),'hour':local.hour,'minute':local.minute}
            c.execute('INSERT INTO audit_log(item,kind,detail,created) VALUES(?,?,?,?)',(item,'monitor_reserved',json.dumps({'before':b['at'],'after':out['at'],'reason':b['reason']},ensure_ascii=False),time.time()))
        elif path == '/v1/monitor/log':
            c.execute('INSERT INTO audit_log(item,kind,detail,created) VALUES(?,?,?,?)',(str(b['itemId']),'monitor_applied',json.dumps(b,ensure_ascii=False),time.time()))
            out={'ok':True}
        elif path == '/v1/publish/heartbeat':
            row = c.execute('SELECT * FROM publications WHERE item=?',(str(b['itemId']),)).fetchone()
            if not row or row['owner']!=b['owner'] or row['stage'] not in ('claimed','container_created') or row['updated']+LEASE_SECONDS<time.time():
                raise ValueError('Publication lease expired or ownership changed')
            c.execute('UPDATE publications SET updated=? WHERE item=?',(time.time(),str(b['itemId'])))
            out = {'ok':True,'receipt':json.loads(row['data'])}
        elif path == '/v1/schedule/cancel':
            c.execute('DELETE FROM reservations WHERE item=? AND committed=0',(str(b['itemId']),))
            out = {'ok':True}
        elif path == '/v1/health':
            out = {'ok':True,'qaPolicy':QA_POLICY,'storage':str(ROOT),'pendingPublications':c.execute("SELECT COUNT(*) FROM publications WHERE stage NOT IN ('published','source_synced')").fetchone()[0]}
            c.execute('INSERT OR REPLACE INTO health VALUES(?,?,?)',('last_check',time.time(),json.dumps(out)))
        elif path == '/v1/maintenance':
            # Retain receipts indefinitely. Only old, confirmed-published local files are eligible.
            removed = 0
            for row in c.execute("SELECT m.path,p.updated FROM media m JOIN publications p ON m.item=p.item WHERE p.stage='source_synced'").fetchall():
                f = Path(row['path'])
                if row['updated']<time.time()-30*86400 and f.parent==ROOT/'media' and f.exists():
                    f.unlink(); removed += 1
            out = {'ok':True,'removedPublishedFiles':removed}
        elif path == '/v1/publish/checkpoint':
            row = c.execute('SELECT * FROM publications WHERE item=?', (str(b['itemId']),)).fetchone()
            if not row or row['owner'] != b['owner']:
                raise ValueError('Publication ownership mismatch')
            if b['stage'] in ('container_created','publish_requested') and row['updated']+LEASE_SECONDS<time.time():
                raise ValueError('Publication lease expired')
            if b['stage']=='publish_requested' and row['stage']=='publish_requested':
                raise ValueError('Publish intent already recorded; never repeat the publish call')
            stages = ['claimed', 'container_created', 'publish_requested', 'published', 'source_synced']
            old, new = stages.index(row['stage']), stages.index(b['stage'])
            if new < old or new > old+1:
                raise ValueError('Invalid publication transition')
            data = {**json.loads(row['data']), **b.get('data', {})}
            if b['stage']=='published' and not data.get('publishedAt'): data['publishedAt']=datetime.now(timezone.utc).isoformat()
            if b['stage'] == 'source_synced':
                data['sourceSynced'] = True
            c.execute('UPDATE publications SET stage=?,data=?,updated=? WHERE item=?', (b['stage'], json.dumps(data), time.time(), str(b['itemId'])))
            out = {'ok': True, 'receipt': data}
        else:
            raise ValueError('Unknown endpoint')
        c.execute('INSERT OR REPLACE INTO health VALUES(?,?,?)',(path,time.time(),json.dumps({'ok':True})))
        c.commit()
        return out
    except Exception as error:
        c.rollback()
        c.execute('INSERT OR REPLACE INTO health VALUES(?,?,?)',(path,time.time(),json.dumps({'ok':False,'error':str(error)[:300]})))
        raise
    finally:
        c.close()


if __name__ == '__main__':
    try:
        payload = json.loads(base64.b64decode(sys.argv[1], validate=True))
        ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        if payload['path'] in ('/internal/media-job','/internal/preflight-job'):
            try:
                result = (preflight_media if payload['path']=='/internal/preflight-job' else prepare_media)(payload['body'])
            except Exception as error:
                result = {'ready':False,'needsReview':True,'retryable':True,'attempts':payload.get('attempt',1),'retryAt':time.time()+min(3600,60*2**payload.get('attempt',1)),'reason':'Media preparation failed: '+str(error)[:600]}
            with closing(db()) as c:
                c.execute('INSERT OR REPLACE INTO jobs VALUES(?,?)',(payload['mid'],json.dumps(result)))
            for fd in payload['fds']: os.close(fd)
        else:
            print(json.dumps(action(payload['path'],payload['body'])),flush=True)
    except Exception as error:
        print(json.dumps({'error':str(error)[:900]}),flush=True)
        sys.exit(1)
