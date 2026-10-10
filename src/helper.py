"""Waset social helper (v2): command-line dispatcher into waset_ops.

Invocation (unchanged pattern): python3 helper.py <base64 JSON {path, body}>
Large bodies (R5 A12): {path, bodyFile: "inbox/<name>.json"}. n8n writes the JSON body with its Read/Write Files
node into the data directory's inbox/ (the only directory it may write there); the helper validates the name
(plain file, no path, no link), refuses bodies over MAX_BODY_FILE_BYTES (UTF-8 bytes), reads it once and deletes
it. Files left behind by an execution that never reached the helper are swept after INBOX_STALE_SECONDS.
The command line itself is always the fixed helper path plus one base64 argument (no payload text reaches the
shell). This is not an HTTP service; base64 is transport encoding, not authentication.
Trust boundary: whoever can execute commands on the n8n host.

Output contract:
* stdout carries exactly one JSON object.
* Handled failures exit 0 with {"ok": false, "error", "kind"} so n8n keeps the
  diagnostic (exit 1 used to discard stdout). Exit 1 only if no JSON could be written.
* Every failure is also appended to errors.log (JSON lines), which does not
  depend on SQLite, so a database outage stays observable.

Deployment fence: while ``deploy-fence.json`` exists in the data directory (written by deploy/deploy.py) new work
is not started (runs and the supervisor are deferred, queues answer empty, claims are refused); a run that
started before the fence, and attempts already claimed, may finish and report results.
"""
import base64
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from waset_ops import Command, Ops, Rejected, __version__  # noqa: E402
from waset_ops import media  # noqa: E402
from waset_ops.db import SCHEMA_VERSION  # noqa: E402

ROOT = media.ROOT
SERVICE = {'wf1': 'service:wf1', 'wf2': 'service:wf2', 'wf3': 'service:wf3'}
MAX_BODY_FILE_BYTES = 8_000_000           # serialized UTF-8 bytes; the n8n Input nodes use the same bound
INBOX_STALE_SECONDS = 3600
BODY_FILE = re.compile(r'inbox/[A-Za-z0-9][A-Za-z0-9._-]{0,200}\.json')
FENCE_FILE = 'deploy-fence.json'
# Results of work already in flight, and read-only routes: allowed while the deployment fence is up.
FENCE_ALLOWED = {'/v2/health', '/v2/monitor/inspect', '/v2/run/finish', '/v2/run/fail',
                 '/v2/publish/container', '/v2/publish/renew', '/v2/publish/commit', '/v2/publish/result',
                 '/v2/publish/evidence', '/v2/publish/reconcile', '/v2/publish/abandon', '/v2/outbox/ack',
                 '/v2/import/record'}
# New work: answered as "nothing to do now" so the workflows end cleanly instead of failing every cycle.
FENCE_IDLE = {
    '/v2/run/start': {'acquired': False, 'deferred': True, 'reason': 'deployment in progress; this cycle is deferred'},
    '/v2/monitor/run': {'acquired': False, 'deferred': True, 'reason': 'deployment in progress; supervision deferred'},
    '/v2/publish/due': {'work': []},
    '/v2/outbox/take': {'jobs': []},
    '/v2/prep/queue': {'work': []},
}


def ops():
    o = Ops(ROOT / 'state.sqlite')
    v = o.store.schema_version()         # read-only check first; DDL only when needed
    if v > SCHEMA_VERSION:
        # R5 LOW-02: a database migrated by newer code is never written by older code (rollback safety).
        raise Rejected(f'Database schema {v} is newer than this helper ({SCHEMA_VERSION}); refusing to run',
                       'schema_newer')
    if v < SCHEMA_VERSION:
        o.store.migrate()
    o.apply_data_fixes()                 # cheap once applied: one indexed read per call
    return o


def release_info():
    """Identity of the code that is running (RELEASE.json is written by deploy/build_release.py)."""
    try:
        return json.loads((HERE / 'RELEASE.json').read_text())
    except (OSError, ValueError):
        return None


def deploy_fence():
    p = ROOT / FENCE_FILE
    if not p.exists():
        return None
    try:
        fence = json.loads(p.read_text())
        return fence if isinstance(fence, dict) else {'unreadable': True, 'since': 0}
    except (OSError, ValueError):
        return {'unreadable': True, 'since': 0}       # fail closed: an unreadable fence is still a fence


def run_context(b):
    """(kind, run id, fence) of a run-scoped request, if it carries one."""
    r = b.get('run')
    if isinstance(r, dict) and r.get('id') is not None:
        return str(r.get('kind') or 'wf1'), str(r['id']), r.get('fence')
    if b.get('runId') is not None and b.get('fence') is not None:
        return 'wf1', str(b['runId']), b.get('fence')
    return None


def in_flight_run(o, ctx, fence) -> bool:
    """The request belongs to the run that held the lease when the fence went up (it may finish)."""
    if not ctx:
        return False
    kind, run_id, run_fence = ctx
    with o.store.read() as c:
        r = c.execute('SELECT * FROM ops_runs WHERE kind=?', (kind,)).fetchone()
    return bool(r and r['run_id'] == run_id and (run_fence is None or r['fence'] == int(run_fence)) and
                (r['lease_until'] or 0) > time.time() and (r['started'] or 0) < float(fence.get('since') or 0))


def sweep_inbox():
    d = ROOT / 'inbox'
    try:
        d.mkdir(mode=0o700, exist_ok=True)
        cutoff = time.time() - INBOX_STALE_SECONDS
        for p in d.iterdir():
            try:
                if p.name.endswith('.json') and p.lstat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass
    except OSError:
        pass


ERRORS_LOG_MAX = 5_000_000


def log_error(path, error, kind):
    try:
        log = ROOT / 'errors.log'
        if log.exists() and log.stat().st_size > ERRORS_LOG_MAX:
            log.replace(ROOT / 'errors.log.1')     # one generation kept; Bondok resets its offset on shrink
        with log.open('a') as f:
            f.write(json.dumps({'at': time.time(), 'path': path, 'kind': kind, 'error': str(error)[:900],
                                'version': __version__}) + '\n')
    except OSError:
        pass


def read_body(payload):
    if 'bodyFile' not in payload:
        body = payload.get('body')
    else:
        ref = payload['bodyFile']
        if not isinstance(ref, str) or not BODY_FILE.fullmatch(ref):
            raise Rejected('bodyFile must be inbox/<name>.json', 'bad_payload')
        inbox = ROOT / 'inbox'
        p = inbox / ref.split('/', 1)[1]
        if p.is_symlink() or not p.is_file() or p.resolve().parent != inbox.resolve():
            raise Rejected('bodyFile must be a regular file in inbox/', 'bad_payload')
        try:
            size = p.stat().st_size
            if size > MAX_BODY_FILE_BYTES:
                raise Rejected(f'Request body is {size} bytes; the limit is {MAX_BODY_FILE_BYTES}', 'payload_too_large')
            with p.open('rb') as f:
                raw = f.read(MAX_BODY_FILE_BYTES + 1)
            try:
                body = json.loads(raw.decode('utf-8'))
            except ValueError as e:
                raise Rejected(f'bodyFile is not UTF-8 JSON ({e})', 'bad_payload') from None
        finally:
            p.unlink(missing_ok=True)
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise Rejected('Request body must be a JSON object', 'bad_payload')
    return body


def cmd(o, b, op, item, args, kind):
    """Service command with a stable idempotency id supplied by the workflow."""
    return o.submit(Command(str(b['requestId']), op, SERVICE[kind], SERVICE[kind], str(item), args))


def prep_step(o, b):
    """WF1 per-item preparation step. Network/media work happens between
    handler transactions, never inside one."""
    base = {'run_id': b['runId'], 'fence': b.get('fence'), 'expected_format': b.get('expectedFormat')}
    args = {**base, 'file': b.get('file') or {}, 'url': b.get('url'), 'folder_url': b.get('folderUrl'),
            'error': b.get('error'), 'error_kind': b.get('errorKind')}
    rid = str(b['requestId'])
    r = o.submit(Command(rid + ':source', 'prep_source', 'service:wf1', 'service:wf1', b['itemId'], args))
    for step in range(3):
        if r.get('state') != 'completed' or r.get('next') not in ('preflight', 'prepare'):
            return r
        body = r['media']
        if body.get('recheck'):
            with o.store.read() as c:
                chk = c.execute("SELECT requested FROM ops_checks WHERE item_id=? AND kind='media'",
                                (str(b['itemId']),)).fetchone()
            body['recheckRequestedAt'] = chk['requested'] if chk else time.time()
        if r['next'] == 'preflight':
            res = media.preflight(body)
            r = o.submit(Command(f'{rid}:preflight:{step}', 'prep_preflight', 'service:wf1', 'service:wf1',
                                 b['itemId'], {**base, 'result': res}))
            if r.get('next') == 'continue':
                r = o.submit(Command(f'{rid}:source:{step}', 'prep_source', 'service:wf1', 'service:wf1',
                                     b['itemId'], args))
            continue
        res = media.prepare(body)
        return o.submit(Command(rid + ':media', 'prep_media', 'service:wf1', 'service:wf1', b['itemId'],
                                {**base, 'result': res}))
    return r


def gate(o, path, b):
    """Deployment fence and run fencing, before any route acts. Returns a response to send instead, or None."""
    fence = deploy_fence()
    ctx = run_context(b)
    if fence is not None and path not in FENCE_ALLOWED:
        if path in FENCE_IDLE:              # also for a run already in flight: it finishes without new work
            return {**FENCE_IDLE[path], 'maintenance': True}
        if not in_flight_run(o, ctx, fence):
            raise Rejected('A deployment is in progress; new work is not started until it ends', 'maintenance',
                           release=fence.get('release'))
    if isinstance(b.get('run'), dict) and ctx:
        o.run_progress(ctx[0], ctx[1], ctx[2], step=path)   # superseded runs are refused here (fenced)
    return None


def action(path, b):
    o = ops()
    early = gate(o, path, b)
    if early is not None:
        return early
    if path == '/v2/run/start':
        return o.run_start(b['kind'], str(b['runId']))
    if path == '/v2/run/finish':
        return o.run_finish(b['kind'], str(b['runId']), b.get('result'), fence=b.get('fence'))
    if path == '/v2/run/fail':
        return o.run_fail(b['kind'], str(b['runId']), b.get('reason') or 'reported by the workflow',
                          step=b.get('step'), fence=b.get('fence'))
    if path == '/v2/import/plan':
        return o.import_plan(b['sources'], b['social'])
    if path == '/v2/import/record':
        return o.import_record(b.get('pairs') or [], failed=b.get('failed'), uncertain=b.get('uncertain'))
    if path == '/v2/board/observe':
        return o.observe(b['items'], sources=b.get('sources'), complete=bool(b.get('complete')),
                         actor=SERVICE.get(b.get('caller'), 'monday-poll'), limit=int(b.get('limit', 20)))
    if path == '/v2/board/missing':
        return {'missing': o.board_missing(b['ids'])}
    if path == '/v2/prep/queue':
        return {'work': o.work_queue(limit=int(b.get('limit', 20)))}
    if path == '/v2/maintenance':
        return media.maintenance()
    if path == '/v2/publish/abandon':
        return o.abandon(b['attemptId'], b['worker'], b['fence'], b.get('reason'))
    if path == '/v2/publish/failure':
        return o.precommit_failure(b['attemptId'], b['worker'], b['fence'], b.get('stage') or 'unknown',
                                   error=b.get('error'), http_status=b.get('httpStatus'), status_code=b.get('statusCode'))
    if path == '/v2/publish/read_failure':
        return o.read_failure(b['itemId'], b.get('stage') or 'read', b.get('error'), b.get('httpStatus'))
    if path == '/v2/publish/probe':
        return o.provider_probe(bool(b.get('ok')), b.get('error'))
    if path == '/v2/prep/step':
        return prep_step(o, b)
    if path == '/v2/prep/delivered':
        return cmd(o, b, 'prep_delivered', b['itemId'], {'run_id': b['runId'], 'fence': b.get('fence'),
                                                          'mediaId': b['mediaId'], 'url': b['url']}, 'wf1')
    if path == '/v2/caption/needed':
        return o.caption_needed(b['itemId'], b.get('title'), b.get('brief'))
    if path == '/v2/caption/draft':
        return cmd(o, b, 'caption_draft', b['itemId'], {'input_hash': b['inputHash'], 'text': b.get('text'),
                                                         'model': b.get('model'), 'error': b.get('error')}, 'wf1')
    if path == '/v2/outbox/take':
        return {'jobs': o.outbox_take(b['kinds'], b['worker'], int(b.get('limit', 10)))}
    if path == '/v2/outbox/ack':
        out = o.outbox_ack(int(b['id']), b['worker'], bool(b['ok']), b.get('error'), b.get('result'))
        # R5 LOW-02: a failed delivery, and an acknowledgment the handler refused, reach errors.log (watchdog).
        # A compare-before-write conflict is the owner's newer edit, not a failure: kept in the outbox row only.
        if not b['ok'] and not out.get('conflict') and not str(b.get('error') or '').startswith('conflict'):
            log_error(path, f"job {b['id']} failed ({b['worker']}): {b.get('error')} -> "
                            f"{'escalated' if out.get('escalated') else 'superseded' if out.get('superseded') else 'retry'}",
                      'outbox_failed')
        elif out.get('ok') is False and (out.get('reason') or out.get('stale_lease')):
            log_error(path, f"job {b['id']} ({b['worker']}): {out.get('reason') or 'lease held by another worker'}",
                      'outbox_ack_refused')
        return out
    if path == '/v2/publish/due':
        return o.due(b['worker'], int(b.get('limit', 5)))
    if path == '/v2/publish/claim':
        if b.get('freshItem'):
            o.observe([b['freshItem']], actor='service:wf2')
        return o.claim(b['itemId'], b['worker'], source_status=b.get('sourceStatus'))
    if path == '/v2/publish/container':
        return o.container(b['attemptId'], b['worker'], b['fence'], b['containerId'])
    if path == '/v2/publish/renew':
        return o.renew(b['attemptId'], b['worker'], b['fence'])
    if path == '/v2/publish/commit':
        return o.commit(b['attemptId'], b['worker'], b['fence'], source_asset=b.get('sourceAsset'),
                        container_status=b.get('containerStatus'))
    if path == '/v2/publish/result':
        return o.result(b['attemptId'], b['worker'], media_id=b.get('mediaId'), error=b.get('error'),
                        http_status=b.get('httpStatus'), definitive=bool(b.get('definitive')))
    if path == '/v2/publish/evidence':
        return o.evidence(b['attemptId'], b.get('permalink'))
    if path == '/v2/publish/reconcile':
        return o.reconcile(b['attemptId'], b.get('containerStatus'), b.get('error'))
    if path == '/v2/monitor/run':
        rid = str(b['runId'])
        lease = o.run_start('wf3', rid)
        if not lease['acquired']:
            return {'deferred': True, **lease}
        try:
            out = {**o.repair(rid, lease['fence']), 'deferred': False}
        except Exception as e:
            # R5 A13: a supervisor run that raised is a failed run, never a completed one.
            o.run_fail('wf3', rid, f'{type(e).__name__}: {str(e)[:300]}', step='repair', fence=lease['fence'])
            raise
        o.run_finish('wf3', rid, {'findings': out.get('findings'), 'repairs': len(out.get('repairs') or [])},
                     fence=lease['fence'])
        return out
    if path == '/v2/monitor/inspect':
        return o.inspect()
    if path == '/v2/health':
        return {**o.health(), 'helper': {'version': __version__, 'schema_supported': SCHEMA_VERSION,
                                         'release': release_info()}, 'deploy_fence': deploy_fence()}
    if path.startswith('/v1/'):
        raise Rejected('Legacy route retired by the v2 cutover; refusing to bypass the command handler', 'retired')
    raise Rejected('Unknown route', 'unknown_route')


def main(argv):
    try:
        payload = json.loads(base64.b64decode(argv[1], validate=True))
        if not isinstance(payload, dict) or not isinstance(payload.get('path', '?'), str):
            raise ValueError('payload must be a JSON object with a string path')     # R5 LOW-02
    except Exception as error:
        log_error('?', error, 'bad_payload')
        print(json.dumps({'ok': False, 'kind': 'bad_payload', 'error': str(error)[:300]}), flush=True)
        return 0
    path = payload.get('path', '?')
    try:
        ROOT.mkdir(parents=True, exist_ok=True)
        if path == '/internal/job':
            media.run_job(payload)
            return 0
        sweep_inbox()
        out = action(path, read_body(payload))
        if isinstance(out, dict) and out.get('state') == 'failed' and out.get('code') == 'storage':
            # A storage failure inside a command (read-only database, locked, disk full) is a failure, not a
            # result: report it to n8n and to errors.log so the watchdog sees it (audit I3).
            log_error(path, out.get('reason', 'storage error'), 'storage')
            print(json.dumps({'ok': False, 'kind': 'storage', 'error': out.get('reason', 'storage error')},
                             ensure_ascii=False), flush=True)
            return 0
        print(json.dumps({'ok': True, **out} if isinstance(out, dict) else {'ok': True, 'result': out},
                         ensure_ascii=False), flush=True)
        return 0
    except Rejected as e:
        if e.code != 'maintenance':          # announced by the deployment itself; the JSON below is the record
            log_error(path, e.reason, e.code)
        print(json.dumps({'ok': False, 'kind': e.code, 'error': e.reason, **e.extra}, ensure_ascii=False), flush=True)
        return 0
    except Exception as error:
        kind = type(error).__name__
        log_error(path, f'{error} | {traceback.format_exc(limit=3)[-600:]}', kind)
        try:
            print(json.dumps({'ok': False, 'kind': kind, 'error': str(error)[:900]}, ensure_ascii=False), flush=True)
            return 0
        except Exception:
            return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
