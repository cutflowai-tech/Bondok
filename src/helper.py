"""Waset social helper (v2): command-line dispatcher into waset_ops.

Invocation (unchanged pattern): python3 helper.py <base64 JSON {path, body}>
Large bodies: {path, bodyFile: "inbox/<name>.json"} written by n8n beside this file.
This is not an HTTP service; base64 is transport encoding, not authentication.
Trust boundary: whoever can execute commands on the n8n host.

Output contract:
* stdout carries exactly one JSON object.
* Handled failures exit 0 with {"ok": false, "error", "kind"} so n8n keeps the
  diagnostic (exit 1 used to discard stdout). Exit 1 only if no JSON could be written.
* Every failure is also appended to errors.log (JSON lines), which does not
  depend on SQLite, so a database outage stays observable.
"""
import base64
import json
import os
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from waset_ops import Command, Ops, Rejected, __version__  # noqa: E402
from waset_ops import media  # noqa: E402

ROOT = media.ROOT
SERVICE = {'wf1': 'service:wf1', 'wf2': 'service:wf2', 'wf3': 'service:wf3'}


def ops():
    o = Ops(ROOT / 'state.sqlite')
    if o.store.schema_version() < 1:     # read-only check first; DDL only when needed
        o.store.migrate()
    o.apply_data_fixes()                 # cheap once applied: one indexed read per call
    return o


def log_error(path, error, kind):
    try:
        with (ROOT / 'errors.log').open('a') as f:
            f.write(json.dumps({'at': time.time(), 'path': path, 'kind': kind, 'error': str(error)[:900],
                                'version': __version__}) + '\n')
    except OSError:
        pass


def read_body(payload):
    if 'bodyFile' not in payload:
        return payload.get('body') or {}
    inbox = (ROOT / 'inbox').resolve()
    p = (ROOT / payload['bodyFile']).resolve()
    if p.parent != inbox or not p.name.endswith('.json'):
        raise ValueError('bodyFile must be inbox/<name>.json')
    try:
        return json.loads(p.read_text())
    finally:
        p.unlink(missing_ok=True)


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


def action(path, b):
    o = ops()
    if path == '/v2/run/start':
        return o.run_start(b['kind'], str(b['runId']))
    if path == '/v2/run/finish':
        return o.run_finish(b['kind'], str(b['runId']), b.get('result'))
    if path == '/v2/import/plan':
        return o.import_plan(b['sources'], b['social'])
    if path == '/v2/import/record':
        return o.import_record(b['pairs'])
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
        return o.outbox_ack(int(b['id']), b['worker'], bool(b['ok']), b.get('error'), b.get('result'))
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
        lease = o.run_start('wf3', str(b['runId']))
        if not lease['acquired']:
            return {'deferred': True, **lease}
        try:
            return {**o.repair(str(b['runId']), lease['fence']), 'deferred': False}
        finally:
            o.run_finish('wf3', str(b['runId']))
    if path == '/v2/monitor/inspect':
        return o.inspect()
    if path == '/v2/health':
        return o.health()
    if path.startswith('/v1/'):
        raise Rejected('Legacy route retired by the v2 cutover; refusing to bypass the command handler', 'retired')
    raise Rejected('Unknown route', 'unknown_route')


def main(argv):
    try:
        payload = json.loads(base64.b64decode(argv[1], validate=True))
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
        out = action(path, read_body(payload))
        print(json.dumps({'ok': True, **out} if isinstance(out, dict) else {'ok': True, 'result': out},
                         ensure_ascii=False), flush=True)
        return 0
    except Rejected as e:
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
