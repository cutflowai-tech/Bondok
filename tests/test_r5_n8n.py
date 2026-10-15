"""Round 5 n8n-layer, transport and monitoring traces (docs/AUDIT_ROUND5_2026-10-10.md), one class per entry.

The workflow tests execute the generated Code nodes (workflows/dist) with a small n8n Code-node runtime and send
the helper calls through the real ``src/helper.py`` exactly as n8n would: Input node -> (body file written by the
Read/Write Files node) -> Execute Command (``sh -c`` of the generated command) -> parse node. Synthetic data only;
"production-shaped" means the column layout and value sizes of the live boards, never their content.
"""
import base64
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from support import ROOT, OpsCase, monday_item

from waset_ops import board

DIST = ROOT / 'workflows' / 'dist'
WF = {p.name.split('__')[0]: json.loads(p.read_text()) for p in DIST.glob('*.v2.json')}
WF1, WF2, WF3 = WF['qI1N5VNgpRjnZAKH'], WF['pUIshuf16zIYoYRz'], WF['WasetSocialScheduleGuard']
HELPER = ROOT / 'src' / 'helper.py'
HELPER_SRC = HELPER.read_text()
CONTAINER_DIR = '/home/node/.n8n-files/waset-social'
NODE = shutil.which('node')
ARG_LIMIT = 120_000            # bytes per helper argument (Linux MAX_ARG_STRLEN is 131072)

RUNTIME = r"""
const ctx=JSON.parse(require('fs').readFileSync(0,'utf8'));
const pick=(v,i)=>{const a=Array.isArray(v)?v:[v];return a[Math.min(i,a.length-1)]};
const ref=v=>{const a=Array.isArray(v)?v:[v];return {item:{json:pick(v,ctx.index||0)},first:()=>({json:a[0]}),
  last:()=>({json:a[a.length-1]}),all:()=>a.map(j=>({json:j}))}};
const $=n=>{const v=(ctx.refs||{})[n];if(v===undefined)throw new Error('missing ref '+n);return ref(v)};
const items=ctx.items||[ctx.json];
const $input={all:()=>items.map(j=>({json:j})),first:()=>({json:items[0]}),item:{json:ctx.json}};
const $execution={id:ctx.execId||'4711'};
const $json=ctx.json;
const args=['$json','$','$input','$execution','Buffer'],vals=[$json,$,$input,$execution,Buffer];
let out;
if(ctx.expr!==undefined){
  let s=ctx.expr.slice(1),parts=[];
  for(;;){const a=s.indexOf('{{');if(a<0){parts.push(s);break}const b=s.indexOf('}}',a+2);
    parts.push(s.slice(0,a));parts.push(new Function(...args,'return ('+s.slice(a+2,b)+')')(...vals));s=s.slice(b+2)}
  out=parts.filter(p=>p!=='').length===1&&typeof parts.find(p=>p!=='')!=='string'?parts.find(p=>p!==''):parts.join('');
}else{out=new Function(...args,ctx.js)(...vals)}
process.stdout.write(JSON.stringify({out}));
"""


class NodeError(Exception):
    """A Code node threw (n8n would stop the execution or route the item to the error output)."""


def run_js(js=None, json_in=None, refs=None, items=None, index=0, expr=None):
    ctx = {'js': js, 'json': json_in if json_in is not None else {}, 'refs': refs or {}, 'items': items,
           'index': index}
    if expr is not None:
        ctx['expr'] = expr
    p = subprocess.run([NODE, '-e', RUNTIME], input=json.dumps(ctx), capture_output=True, text=True, timeout=60)
    if p.returncode:
        m = re.search(r'^(?:Error|TypeError|SyntaxError|ReferenceError|RangeError): (.*)$', p.stderr, re.M)
        raise NodeError(m.group(1) if m else p.stderr[-600:])
    return json.loads(p.stdout)['out']


def nodes(w):
    return {n['name']: n for n in w['nodes']}


def targets(w, name, out=0):
    outs = w['connections'].get(name, {}).get('main', [])
    return [c['node'] for c in (outs[out] if len(outs) > out else [])]


def decode_body(command, data_dir=None):
    """The helper request a generated command carries (argv body or the body file it names)."""
    payload = json.loads(base64.b64decode(command.split(' ')[-1]))
    if 'bodyFile' in payload and data_dir is not None:
        return payload, json.loads((Path(data_dir) / payload['bodyFile']).read_text())
    return payload, payload.get('body')


class N8nHelperPath(OpsCase):
    """Executes one generated helper call the way n8n does, against a scratch data directory."""

    WF = WF1

    def setUp(self):
        super().setUp()
        self.clock.set(datetime.now(timezone.utc).replace(microsecond=0))    # helper subprocesses use real time
        self.env = {**os.environ, 'WASET_SOCIAL_DATA_DIR': str(self.dir)}
        self.commands = []
        # WF1's first helper call (Run Start) precedes every body-file write; any helper call keeps inbox/ ready.
        arg = base64.b64encode(b'{"path":"/v2/health","body":{}}').decode()
        subprocess.run([sys.executable, str(HELPER), arg], capture_output=True, env=self.env, check=True, timeout=60)

    def host_path(self, p):
        self.assertTrue(p.startswith(CONTAINER_DIR + '/'), p)
        return self.dir / p[len(CONTAINER_DIR) + 1:]

    def execute_command(self, node, command):
        """n8n Execute Command: ``sh -c <command>``; a non-zero exit fails the node unless onError continues."""
        self.commands.append(command)
        m = re.fullmatch(re.escape('python3 ' + CONTAINER_DIR + '/helper.py ') + r'([A-Za-z0-9+/=]+)', command)
        self.assertIsNotNone(m, 'helper command must be the fixed prefix plus one base64 argument (no shell text)')
        self.assertLessEqual(len(command.encode()), ARG_LIMIT)
        p = subprocess.run([sys.executable, str(HELPER), m.group(1)], capture_output=True, text=True, env=self.env,
                           timeout=120)
        if p.returncode:
            if node.get('onError') in ('continueRegularOutput', 'continueErrorOutput'):
                return {'error': f'Command failed: {command}\n{p.stderr.strip()}'}
            raise NodeError(f'Execute Command failed (exit {p.returncode}): {p.stderr[-300:]}')
        return {'exitCode': 0, 'stdout': p.stdout.strip(), 'stderr': p.stderr.strip()}

    def helper_call(self, name, json_in=None, refs=None, items=None, w=None):
        """Run '<name> — Input' -> [Body File] -> 'Local n8n' -> '<name>' and return the parse node's items."""
        w = w or self.WF
        ns = nodes(w)
        inp = ns[name + ' — Input']
        mode_each = inp['parameters'].get('mode') == 'runOnceForEachItem'
        inputs = items if (mode_each and items) else [json_in if json_in is not None else {}]
        results = []
        for idx, j in enumerate(inputs):
            out = run_js(inp['parameters']['jsCode'], j, refs, items=None if mode_each else items, index=idx)
            produced = [out] if isinstance(out, dict) else out
            for item in produced:
                local_refs = {**(refs or {}), inp['name']: item['json']}
                nxt = ns[targets(w, inp['name'])[0]]
                if nxt['type'] == 'n8n-nodes-base.readWriteFile':
                    prm = nxt['parameters']
                    self.assertEqual(prm['operation'], 'write')
                    path = run_js(expr=prm['fileName'], json_in=item['json'], refs=local_refs)
                    data = item['binary'][prm.get('dataPropertyName', 'data')]['data']
                    target = self.host_path(path)
                    self.assertTrue(target.parent.is_dir(), 'the helper keeps inbox/ in place for n8n')
                    target.write_bytes(base64.b64decode(data))
                    item = {'json': {**item['json'], 'fileName': path}}     # n8n keeps the json, adds fileName
                    nxt = ns[targets(w, nxt['name'])[0]]
                self.assertEqual(nxt['type'], 'n8n-nodes-base.executeCommand')
                command = run_js(expr=nxt['parameters']['command'], json_in=item['json'], refs=local_refs)
                res = self.execute_command(nxt, command)
                parse = ns[targets(w, nxt['name'])[0]]
                r = run_js(parse['parameters']['jsCode'], res, local_refs)
                results.extend([r] if isinstance(r, dict) else r)
        return [r['json'] for r in results]

    def inbox(self):
        d = self.dir / 'inbox'
        return sorted(p.name for p in d.iterdir()) if d.exists() else []


# ------------------------------------------------------------------------------------------- synthetic boards
AR_WORDS = ['فيديو', 'عقار', 'شقة', 'فيلا', 'تصوير', 'مشروع', 'إعلان', 'الساحل', 'القاهرة', 'تجهيز', 'مونتاج',
            'الشيخ', 'زايد', 'التجمع', 'الخامس', 'كمبوند', 'جولة', 'داخلية']


def ar_text(rnd, words):
    return ' '.join(rnd.choice(AR_WORDS) for _ in range(words))


def dropbox_folder(rnd):
    key = ''.join(rnd.choice('abcdefghijklmnopqrstuvwxyz0123456789') for _ in range(21))
    rl = ''.join(rnd.choice('abcdefghijklmnopqrstuvwxyz0123456789') for _ in range(25))
    return f'https://www.dropbox.com/scl/fo/{key}/AAB{key[:20]}?rlkey={rl}&dl=0'


def source_board(scale, rnd):
    """Customer Projects board as WF1's 'Source Snapshot' sees it (four columns per project)."""
    items = []
    for i in range(int(140 * scale)):
        fmt = 'Story' if i % 3 else 'Post'
        kind = 'team' if i % 7 == 0 else 'person'
        owner = {'personsAndTeams': [{'id': 40000000 + i % 9, 'kind': kind}]}
        link = dropbox_folder(rnd)
        items.append({'id': str(5091200000 + i), 'name': ar_text(rnd, 6), 'column_values': [
            {'id': 'text_mm066x8y', 'text': f'LIP{i}', 'value': json.dumps(f'LIP{i}')},
            {'id': 'link_mm06bswn', 'text': link, 'value': json.dumps({'url': link, 'text': link})},
            {'id': 'color_mm1ryfcb', 'text': fmt, 'value': json.dumps({'index': 1})},
            {'id': 'project_owner', 'text': 'فريق المونتاج' if kind == 'team' else 'محرر', 'value': json.dumps(owner)}]})
    for i in range(int(40 * scale)):        # projects that are no longer social work (not eligible)
        items.append({'id': str(5091900000 + i), 'name': ar_text(rnd, 6), 'column_values': [
            {'id': 'text_mm066x8y', 'text': f'OLD{i}', 'value': None},
            {'id': 'color_mm1ryfcb', 'text': 'Posted', 'value': None}]})
    return items


def social_board(scale, rnd, mapped):
    """Social board compact snapshot: one item per already-imported project (caption, notes, system text)."""
    items = []
    for i in range(mapped):
        cap = ar_text(rnd, 40)
        items.append({'id': str(5105600000 + i), 'name': ar_text(rnd, 4), 'group': {'id': 'topics'}, 'column_values': [
            {'id': board.COL['source_item'], 'text': str(5091200000 + i), 'value': None},
            {'id': board.COL['code'], 'text': f'LIP{i}', 'value': None},
            {'id': board.COL['format'], 'text': 'Story' if i % 3 else 'Post', 'value': None},
            {'id': board.COL['caption'], 'text': cap, 'value': json.dumps({'text': cap})}]})
    return items


@unittest.skipUnless(NODE, 'node not installed')
class R5_A12_ImportPlanTransport(N8nHelperPath):
    """A12: the full-board Import Plan request travelled as one base64 argument (~60 KB of the 120 KB guard)."""

    def plan_at(self, scale):
        rnd = random.Random(int(scale * 10))
        src = source_board(scale, rnd)
        n_social = int(130 * scale)
        soc = social_board(scale, rnd, n_social)
        out = self.helper_call('Import Plan', {'items': soc},
                               refs={'Source Snapshot': {'items': src}, 'Configuration': {'runId': self.run_id},
                                     'Run Start': {'fence': self.fence, 'run_id': self.run_id}})
        return out[0], int(140 * scale) - n_social

    def test_production_shaped_board_at_1x_2x_and_5x_through_the_helper(self):
        for scale in (1, 2, 5):
            with self.subTest(scale=scale):
                plan, expected_creates = self.plan_at(scale)
                self.assertTrue(plan['ok'], plan)
                self.assertEqual(len(plan['creates']), expected_creates)
                self.assertEqual(self.inbox(), [], 'the body file is removed after it was read')
        # The Import Plan request is no longer an argument: it is a body file under inbox/.
        payload, _ = decode_body(self.commands[-1])
        self.assertIn('bodyFile', payload)
        self.assertRegex(payload['bodyFile'], r'^inbox/[A-Za-z0-9._-]+\.json$')

    def test_size_bound_is_measured_in_utf8_bytes_and_enforced_by_the_helper(self):
        (self.dir / 'inbox').mkdir(exist_ok=True)
        cap = int(re.search(r'MAX_BODY_FILE_BYTES = ([0-9_]+)', HELPER_SRC).group(1).replace('_', ''))
        js = nodes(WF1)['Import Plan — Input']['parameters']['jsCode']
        self.assertIn(f'{cap}', js.replace('_', ''), 'n8n and the helper use the same byte bound')
        self.assertIn('Buffer.byteLength', js)
        big = self.dir / 'inbox' / 'big.json'
        big.write_bytes(b'{"sources":[],"social":[],"pad":"' + b'x' * cap + b'"}')
        arg = base64.b64encode(json.dumps({'path': '/v2/import/plan', 'bodyFile': 'inbox/big.json'}).encode()).decode()
        p = subprocess.run([sys.executable, str(HELPER), arg], capture_output=True, text=True, env=self.env, timeout=60)
        out = json.loads(p.stdout)
        self.assertEqual((p.returncode, out['ok'], out['kind']), (0, False, 'payload_too_large'))
        self.assertFalse(big.exists(), 'a refused body file is still removed')

    def test_body_file_must_be_a_plain_inbox_file(self):
        (self.dir / 'inbox').mkdir(exist_ok=True)
        outside = self.dir / 'state-copy.json'
        outside.write_text('{"sources":[],"social":[]}')
        (self.dir / 'inbox' / 'link.json').symlink_to(outside)
        for ref in ('state-copy.json', '../state-copy.json', 'inbox/link.json', 'inbox/x.txt', 'inbox/../x.json'):
            with self.subTest(ref=ref):
                arg = base64.b64encode(json.dumps({'path': '/v2/import/plan', 'bodyFile': ref}).encode()).decode()
                p = subprocess.run([sys.executable, str(HELPER), arg], capture_output=True, text=True, env=self.env)
                out = json.loads(p.stdout)
                self.assertEqual((p.returncode, out['ok'], out['kind']), (0, False, 'bad_payload'))
        self.assertTrue(outside.exists())

    def test_stale_inbox_files_are_swept(self):
        (self.dir / 'inbox').mkdir(exist_ok=True)
        old = self.dir / 'inbox' / '1-import-plan-x.json'
        old.write_text('{}')
        os.utime(old, (time.time() - 7200, time.time() - 7200))
        fresh = self.dir / 'inbox' / '2-import-plan-y.json'
        fresh.write_text('{}')
        arg = base64.b64encode(json.dumps({'path': '/v2/health', 'body': {}}).encode()).decode()
        subprocess.run([sys.executable, str(HELPER), arg], capture_output=True, text=True, env=self.env, check=True)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists(), 'a file another execution is about to read is kept')

    def test_hostile_text_never_reaches_the_shell(self):
        rnd = random.Random(3)
        src = source_board(0.1, rnd)
        src[0]['name'] = "x'; rm -rf / ; $(touch pwn) `id` \"\n"
        out = self.helper_call('Import Plan', {'items': []},
                               refs={'Source Snapshot': {'items': src}, 'Configuration': {'runId': self.run_id},
                                     'Run Start': {'fence': self.fence, 'run_id': self.run_id}})
        self.assertTrue(out[0]['ok'])
        self.assertFalse((self.dir / 'pwn').exists())


@unittest.skipUnless(NODE, 'node not installed')
class R5_LOW01_ChunksSizedInBytes(N8nHelperPath):
    """LOW-01: chunks were cut at 60,000 UTF-16 characters while the guard counts base64 bytes."""

    def test_arabic_heavy_chunks_stay_under_the_argument_bound_through_the_helper(self):
        cap = 'هذا نص عربي طويل لوصف الفيديو مع هاشتاجات #واسط ' * 40          # ~2,000 characters
        items = [monday_item(str(10000 + k), fmt='Post', name='منتج %d' % k, code=f'LIP{k}') for k in range(60)]
        for i in items:     # Monday returns long-text values with raw (unescaped) Unicode, as the live board does
            i['column_values'].append({'id': board.COL['caption'], 'text': cap,
                                       'value': json.dumps({'text': cap}, ensure_ascii=False)})
        chunks = run_js(nodes(WF1)['Board Chunks']['parameters']['jsCode'], {},
                        {'Social Snapshot': {'items': items}, 'Source Snapshot': {'items': []}})
        self.assertGreater(len(chunks), 1)
        self.assertEqual(sum(len(c['json']['items']) for c in chunks), 60)
        out = self.helper_call('Observe Board', items=[c['json'] for c in chunks],
                               refs={'Configuration': {'runId': self.run_id}, 'Run Start': {'fence': self.fence}})
        self.assertTrue(all(o['ok'] for o in out), out)
        self.assertEqual(sum(len(o['imported']) for o in out), 60)
        for cmd in self.commands:
            self.assertLessEqual(len(cmd.encode()), ARG_LIMIT)


class SourceMixin:
    def source(self, sid, fmt, name='Calli', code='CAL1', owners=None, link=None):
        cv = [{'id': 'text_mm066x8y', 'text': code, 'value': None},
              {'id': 'color_mm1ryfcb', 'text': fmt, 'value': None}]
        if owners:
            cv.append({'id': 'project_owner', 'text': 'x', 'value': json.dumps({'personsAndTeams': owners})})
        if link:
            cv.append({'id': 'link_mm06bswn', 'text': link, 'value': json.dumps({'url': link})})
        return {'id': sid, 'name': name, 'column_values': cv}


@unittest.skipUnless(NODE, 'node not installed')
class R5_A11_ImportIsolation(SourceMixin, N8nHelperPath):
    """A11: one Monday error while creating social items aborted the whole WF1 run, every run."""

    def refs(self, src):
        return {'Source Snapshot': {'items': src}, 'Configuration': {'runId': self.run_id},
                'Run Start': {'fence': self.fence, 'run_id': self.run_id}}

    def plan(self, src, social=()):
        return self.helper_call('Import Plan', {'items': list(social)}, refs=self.refs(src))[0]

    def batches(self, plan):
        return run_js(nodes(WF1)['Build Create Mutations']['parameters']['jsCode'], plan)

    def record(self, batch, response):
        return self.helper_call('Record Created Items', items=[response],
                                refs={**self.refs([]), 'Build Create Mutations': batch['json']})

    def test_partial_create_records_successes_reports_the_rejection_and_continues(self):
        src = [self.source('111', 'Post', 'Calli 8', 'CAL8'),
               self.source('222', 'Post', 'Calli 9', 'CAL9', owners=[{'id': 123, 'kind': 'person'}])]
        plan = self.plan(src)
        self.assertEqual(sorted(c['key'] for c in plan['creates']), ['111', '222'])
        batch = self.batches(plan)[0]
        resp = {'data': {'s111': {'id': '9001'}, 's222': None},
                'errors': [{'message': 'The person or team does not exist', 'path': ['s222']}]}
        out = self.record(batch, resp)                         # used to throw 'Item creation failed'
        self.assertTrue(out[0]['ok'], out)
        self.assertIn('Board Chunks', targets(WF1, 'Record Created Items'))   # the run continues to observation
        with self.ops.store.read() as c:
            mapped = {r['source_item_id']: dict(r) for r in c.execute('SELECT * FROM ops_source_map')}
            fnd = [dict(r) for r in c.execute("SELECT * FROM ops_findings WHERE resolved IS NULL")]
        self.assertEqual(mapped['111']['social_item_id'], '9001')
        self.assertTrue(any('222' in f['fingerprint'] and 'person or team does not exist' in f['detail'] for f in fnd))
        slack = [json.loads(o['payload'])['text'] for o in self.outbox('slack')]
        self.assertEqual(sum('Calli 9' in t for t in slack), 1, slack)
        # Next run (10 min later): the created item is recorded, the rejected one waits for its retry window.
        nxt = self.ops.import_plan([{'id': '111', 'name': 'Calli 8', 'code': 'CAL8', 'format': 'Post'},
                                    {'id': '222', 'name': 'Calli 9', 'code': 'CAL9', 'format': 'Post'}],
                                   [{'id': '9001', 'source_item': '111', 'code': 'CAL8', 'format': 'Post'}])
        self.assertEqual(nxt['creates'], [])
        self.clock.advance(3700)                              # retried after the window, notified only once
        again = self.ops.import_plan([{'id': '222', 'name': 'Calli 9', 'code': 'CAL9', 'format': 'Post'}], [])
        self.assertEqual([c['key'] for c in again['creates']], ['222'])
        self.ops.repair()                                     # WF3 does not resolve-and-renotify it
        self.ops.import_record([], failed=[{'source': '222', 'name': 'Calli 9', 'error': 'The person or team does '
                                                                                         'not exist', 'scope': 'item'}])
        slack = [json.loads(o['payload'])['text'] for o in self.outbox('slack')]
        self.assertEqual(sum('Calli 9' in t for t in slack), 1, slack)

    def test_uncertain_create_is_reconciled_from_the_board_not_created_twice(self):
        src = [self.source('333', 'Story', 'Calli 10', 'CAL10')]
        plan = self.plan(src)
        batch = self.batches(plan)[0]
        out = self.record(batch, {'error': {'message': 'timeout of 60000ms exceeded', 'name': 'AxiosError'}})
        self.assertTrue(out[0]['ok'], out)
        eligible = [{'id': '333', 'name': 'Calli 10', 'code': 'CAL10', 'format': 'Story'}]
        self.assertEqual(self.ops.import_plan(eligible, [])['creates'], [], 'no blind re-create inside the window')
        # The create did land: the next complete snapshot shows it and it is recorded (no duplicate).
        r = self.ops.import_plan(eligible, [{'id': '9333', 'source_item': '333', 'code': 'CAL10', 'format': 'Story'}])
        self.assertEqual((r['creates'], r['recorded']), ([], 1))
        with self.ops.store.read() as c:
            self.assertEqual(c.execute("SELECT social_item_id FROM ops_source_map WHERE source_item_id='333'")
                             .fetchone()[0], '9333')
            self.assertEqual(c.execute("SELECT COUNT(*) FROM ops_findings WHERE resolved IS NULL AND "
                                       "fingerprint LIKE 'import:%333'").fetchone()[0], 0)

    def test_uncertain_create_that_never_appeared_is_created_once_after_the_window(self):
        src = [self.source('444', 'Story', 'Calli 11', 'CAL11')]
        batch = self.batches(self.plan(src))[0]
        self.record(batch, {'error': {'message': 'socket hang up'}})
        eligible = [{'id': '444', 'name': 'Calli 11', 'code': 'CAL11', 'format': 'Story'}]
        self.clock.advance(1900)
        self.assertEqual([c['key'] for c in self.ops.import_plan(eligible, [])['creates']], ['444'])

    def test_team_owner_is_sent_as_a_team(self):
        src = [self.source('555', 'Post', 'Calli 12', 'CAL12', owners=[{'id': 777, 'kind': 'team'},
                                                                      {'id': 5, 'kind': 'person'}])]
        plan = self.plan(src)
        owner = plan['creates'][0]['columns'][board.COL['owner']]['personsAndTeams']
        self.assertEqual(owner, [{'id': 777, 'kind': 'team'}, {'id': 5, 'kind': 'person'}])


class R5_M15_ImportFindingsKept(OpsCase):
    """M15: duplicate/invalid codes and two social items per source were returned to WF1 and dropped."""

    def test_findings_are_stored_notified_once_and_unrelated_imports_continue(self):
        self.ops.import_record([{'source': '4', 'social': '999'}])          # mapped social item later deleted
        sources = [{'id': '1', 'name': 'Calli 3', 'code': 'CAL3', 'format': 'Post', 'duplicate': True},
                   {'id': '2', 'name': 'Calli 3 (story)', 'code': 'CAL3', 'format': 'Story', 'duplicate': True},
                   {'id': '3', 'name': 'Bad', 'code': 'X1', 'format': 'Post'},
                   {'id': '4', 'name': 'Calli 4', 'code': 'CAL4', 'format': 'Post'},
                   {'id': '5', 'name': 'Calli 5', 'code': 'CAL5', 'format': 'Post'},
                   {'id': '6', 'name': 'Calli 6', 'code': 'CAL6', 'format': 'Story'}]
        social = [{'id': '61', 'source_item': '', 'code': 'CAL6', 'format': 'Story'},
                  {'id': '62', 'source_item': '', 'code': 'CAL6', 'format': 'Story'}]
        plan = self.ops.import_plan(sources, social)
        self.assertEqual([c['key'] for c in plan['creates']], ['5'])          # unrelated project still imported
        with self.ops.store.read() as c:
            kinds = sorted(r['kind'] for r in c.execute('SELECT kind FROM ops_findings WHERE resolved IS NULL'))
        self.assertEqual(kinds, ['import_ambiguous_social_items', 'import_ambiguous_source_code',
                                 'import_invalid_code', 'import_mapped_social_item_missing'])
        n = len(self.outbox('slack'))
        self.assertGreaterEqual(n, 3)
        for _ in range(3):                                                  # WF1 every 10 min, WF3 every 30
            self.clock.advance(600)
            self.ops.import_plan(sources, social)
            self.ops.repair()
        self.assertEqual(len(self.outbox('slack')), n, 'unchanged findings stay quiet')
        # The owner fixes the duplicate code: that finding resolves, the others stay.
        sources[1] = {**sources[1], 'code': 'CAL33', 'duplicate': False}
        sources[0] = {**sources[0], 'duplicate': False}
        self.ops.import_plan(sources, social)
        with self.ops.store.read() as c:
            open_kinds = {r['kind'] for r in c.execute('SELECT kind FROM ops_findings WHERE resolved IS NULL')}
        self.assertNotIn('import_ambiguous_source_code', open_kinds)
        self.assertIn('import_invalid_code', open_kinds)


@unittest.skipUnless(NODE, 'node not installed')
class R5_M8_CancellationInEveryChunk(OpsCase):
    """M8: Canceled source projects reached only the first observation chunk."""

    def test_canceled_projects_in_first_middle_and_last_chunk_are_all_applied(self):
        notes = 'ملاحظات المونتاج للمشروع ' * 60
        items = [monday_item(str(8000 + i), fmt='Post', name=f'Calli {i}', code=f'CAL{i}',
                             extra={'source_item': (f'S{i}', None), 'notes': (notes, {'text': notes})})
                 for i in range(60)]
        canceled = {'S0', 'S30', 'S59'}
        source = [{'id': s, 'name': s, 'column_values': [{'id': 'color_mm1ryfcb', 'text': 'Canceled', 'value': None}]}
                  for s in sorted(canceled)] + [{'id': 'S1', 'name': 'S1', 'column_values': [
                      {'id': 'color_mm1ryfcb', 'text': 'Post', 'value': None}]}]
        js = nodes(WF1)['Board Chunks']['parameters']['jsCode']
        chunks = run_js(js, {}, {'Social Snapshot': {'items': items}, 'Source Snapshot': {'items': source}})
        self.assertGreaterEqual(len(chunks), 3)
        where = {i['id']: k for k, ch in enumerate(chunks) for i in ch['json']['items']}
        self.assertEqual({where['8000'], where['8059']}, {0, len(chunks) - 1})
        self.assertNotIn(where['8030'], (0, len(chunks) - 1))
        for _ in range(2):                                        # first cycle imports, second observes
            for ch in chunks:
                self.ops.observe(ch['json']['items'], sources=ch['json']['sources'], actor='service:wf1')
        for i in ('8000', '8030', '8059'):
            self.assertEqual(self.item(i)['owner_state'], 'skipped', i)
        self.assertEqual(self.item('8001')['owner_state'], 'active')
        queued = {w['item_id'] for w in self.ops.work_queue(limit=100)}
        self.assertFalse(queued & {'8000', '8030', '8059'})
        for ch in chunks:                                         # only the chunk's own sources travel with it
            ids = {(c['text'] or '') for i in ch['json']['items'] for c in i['column_values']
                   if c['id'] == board.COL['source_item']}
            self.assertTrue({s['id'] for s in ch['json']['sources']} <= ids)


@unittest.skipUnless(NODE, 'node not installed')
class R5_M11_DeletedEditorSubitem(OpsCase):
    """M11: a stored editor task id won over the lookup, so a deleted subitem made every later task fail."""

    JS = nodes(WF1)['Editor Task Mutation']['parameters']['jsCode']

    def mutation(self, job, subitems):
        return run_js(self.JS, {'data': {'items': [{'id': 'S-1', 'subitems': subitems}]}}, {'Each Editor Job': job})[0]['json']

    def job(self, task_id):
        return {'id': 1, 'payload': {'item_id': '7900', 'source_item_id': 'S-1', 'task_id': task_id,
                                     'issue_key': 'editor:x', 'task_name': 'Social 7900 — تجهيز أو استبدال الفيديو',
                                     'body': 'new instructions'}}

    def test_deleted_subitem_is_recreated_not_retargeted(self):
        out = self.mutation(self.job('5550001'), [{'id': '777', 'name': 'Other task', 'updates': []}])
        self.assertIn('create_subitem', out['gql']['query'])
        self.assertIsNone(out['taskId'])
        self.assertEqual(out.get('staleTaskId'), '5550001')

    def test_renamed_subitem_keeps_its_id(self):
        out = self.mutation(self.job('5550001'), [{'id': '5550001', 'name': 'Renamed by the editor', 'updates': []}])
        self.assertEqual(out['taskId'], '5550001')
        self.assertIn('change_column_value', out['gql']['query'])

    def test_deleted_subitem_with_an_existing_valid_task_reuses_it(self):
        out = self.mutation(self.job('5550001'), [{'id': '888', 'name': 'Social 7900 — تجهيز أو استبدال الفيديو',
                                                   'updates': []}])
        self.assertEqual(out['taskId'], '888')
        self.assertNotIn('create_subitem', out['gql']['query'])

    def test_end_to_end_recovery_records_the_new_task(self):
        iid = '7900'
        self.observe(monday_item(iid, fmt='Post', extra={'source_item': ('S-1', None)}))
        self.select(iid, 1)
        j = self.ops.outbox_take(['editor'], 'wf1-a')[0]
        self.ops.outbox_ack(j['id'], 'wf1-a', True, result={'task_id': '5550001'})
        self.select(iid, 2)                                     # the editor uploads v2; a person deleted 5550001
        j = self.ops.outbox_take(['editor'], 'wf1-b')[0]
        out = self.mutation(j, [])
        self.assertIn('create_subitem', out['gql']['query'])
        self.ops.outbox_ack(j['id'], 'wf1-b', True, result={'task_id': '6660001'})
        with self.ops.store.read() as c:
            ids = {r['task_id'] for r in c.execute("SELECT task_id FROM ops_editor_tasks WHERE item_id=?", (iid,))}
        self.assertIn('6660001', ids)


class R5_LOW02_HelperOutcomes(N8nHelperPath):
    """LOW-02: non-object payloads, failed outbox acks and a newer schema had no structured, logged outcome."""

    def raw(self, payload_bytes):
        arg = base64.b64encode(payload_bytes).decode()
        p = subprocess.run([sys.executable, str(HELPER), arg], capture_output=True, text=True, env=self.env, timeout=60)
        return p.returncode, (json.loads(p.stdout) if p.stdout.strip() else None)

    def log(self):
        f = self.dir / 'errors.log'
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    def test_non_object_payload_and_body_are_structured_and_logged(self):
        for payload in (b'[1,2]', b'"x"', b'null', b'7', json.dumps({'path': '/v2/health', 'body': [1]}).encode()):
            with self.subTest(payload=payload):
                rc, out = self.raw(payload)
                self.assertEqual((rc, out and out['ok'], out and out['kind']), (0, False, 'bad_payload'))
        self.assertGreaterEqual(sum(x['kind'] == 'bad_payload' for x in self.log()), 5)

    def test_failed_and_refused_outbox_acks_are_logged(self):
        self.observe(monday_item('42', fmt='Story'))
        with self.ops.store.tx() as c:
            self.ops.enqueue(c, 'monday', 'monday:42:test', {'item_id': '42', 'columns': {}, 'compare': {}}, '42')
        job = [j for j in self.ops.outbox_take(['monday'], 'wf2-9', 50) if j['payload'].get('compare') == {}][0]
        rc, out = self.raw(json.dumps({'path': '/v2/outbox/ack', 'body': {
            'id': job['id'], 'worker': 'wf2-9', 'ok': False, 'error': 'Monday 500: internal error'}}).encode())
        self.assertEqual(rc, 0)
        rc, out = self.raw(json.dumps({'path': '/v2/outbox/ack', 'body': {
            'id': 999999, 'worker': 'wf2-9', 'ok': True}}).encode())
        self.assertFalse(out['ok'])
        log = self.log()
        self.assertTrue(any(x['kind'] == 'outbox_failed' and 'Monday 500' in x['error'] for x in log), log)
        self.assertTrue(any(x['kind'] == 'outbox_ack_refused' and 'unknown job' in x['error'] for x in log), log)

    def test_newer_schema_is_refused_by_the_helper(self):
        with self.ops.store.tx() as c:
            c.execute("UPDATE ops_meta SET value='99' WHERE key='schema_version'")
        rc, out = self.raw(json.dumps({'path': '/v2/publish/due', 'body': {'worker': 'w'}}).encode())
        self.assertEqual((rc, out['ok'], out['kind']), (0, False, 'schema_newer'))
        self.assertTrue(any(x['kind'] == 'schema_newer' for x in self.log()))


@unittest.skipUnless(NODE, 'node not installed')
class R5_LOW07_ExecuteCommandFailureBoundary(N8nHelperPath):
    """LOW-07: exitCode checks after Execute Command were dead code (the node itself fails on a non-zero exit)."""

    def test_every_helper_command_continues_into_its_parser(self):
        for wid, w in WF.items():
            for n in w['nodes']:
                if n['type'] == 'n8n-nodes-base.executeCommand':
                    self.assertEqual(n.get('onError'), 'continueRegularOutput', f'{wid}/{n["name"]}')

    def test_helper_process_failure_keeps_a_bounded_diagnostic(self):
        broken = self.dir / 'broken'
        broken.mkdir()
        shutil.copy(HELPER, broken / 'helper.py')                 # no waset_ops beside it: ImportError, exit 1
        arg = base64.b64encode(b'{"path":"/v2/health","body":{}}').decode()
        p = subprocess.run([sys.executable, '-I', str(broken / 'helper.py'), arg], capture_output=True, text=True)
        self.assertNotEqual(p.returncode, 0)
        n8n_item = {'error': f'Command failed: python3 {CONTAINER_DIR}/helper.py {arg * 200}\n{p.stderr.strip()}'}
        ns = nodes(WF1)
        with self.assertRaises(NodeError) as ctx:                  # fail=True: the run stops with the cause
            run_js(ns['Run Start']['parameters']['jsCode'], n8n_item)
        self.assertIn('Helper process failed', str(ctx.exception))
        self.assertIn('ModuleNotFoundError', str(ctx.exception))
        self.assertNotIn(arg * 3, str(ctx.exception))             # the (large) payload is not repeated
        r = run_js(ns['Editor Task — Ack']['parameters']['jsCode'], n8n_item)   # fail=False: typed outcome
        r = r['json'] if isinstance(r, dict) else r[0]['json']
        self.assertEqual((r['ok'], r['kind']), (False, 'helper_process_failed'))
        self.assertIn('ModuleNotFoundError', r['error'])
        self.assertLessEqual(len(r['error']), 1000)


class R5_LOW20_SuccessRetention(unittest.TestCase):
    """LOW-20: WF2 (every minute) saved every successful execution on a nearly full disk."""

    def test_wf2_keeps_failures_not_every_success(self):
        s = WF2['settings']
        self.assertEqual(s['saveDataSuccessExecution'], 'none')
        self.assertEqual(s['saveDataErrorExecution'], 'all')
        self.assertTrue(s.get('saveManualExecutions'))


class R5_A13_RunLifecycle(OpsCase):
    """A13: WF1 dying after run_start raised no alert (heartbeat at start only, no completion record)."""

    def kinds(self):
        return {f['fingerprint'].split(':')[0] + ':' + f['fingerprint'].split(':')[1]
                for f in self.ops.inspect()['findings'] if f['kind'].startswith('run_')}

    def test_death_after_start_is_detected_without_false_completion(self):
        self.ops.run_finish('wf1', self.run_id)
        self.clock.advance(600)
        self.ops.run_start('wf1', 'wf1-dies')                  # WF1 dies right after its heartbeat
        self.clock.advance(25 * 60)
        self.assertIn('run_stalled:wf1', self.kinds())
        for i in range(6):                                   # every later run dies the same way (A11 pattern)
            self.clock.advance(600)
            self.ops.run_start('wf1', f'wf1-dies-{i}')
        found = self.kinds()
        self.assertIn('run_failed:wf1', found)
        self.assertIn('run_not_completed:wf1', found)
        self.ops.repair()
        n = len(self.outbox('slack'))
        for i in range(3):
            self.clock.advance(600)
            self.ops.run_start('wf1', f'wf1-again-{i}')
            self.ops.repair()
        self.assertEqual(len(self.outbox('slack')), n, 'one alert per episode, not one per run')
        self.clock.advance(1000)
        self.ops.run_start('wf1', 'wf1-ok')
        self.assertTrue(self.ops.run_finish('wf1', 'wf1-ok')['released'])
        self.ops.repair()
        self.assertEqual(self.kinds(), set())

    def test_superseded_run_cannot_complete_or_commit(self):
        self.clock.advance(1000)                              # run-1's lease expired
        self.ops.run_start('wf1', 'run-2')
        self.assertFalse(self.ops.run_finish('wf1', self.run_id)['released'])
        self.observe(monday_item('77', fmt='Story'))
        r = self.select('77')                                 # carries run-1's id and fence
        self.assertEqual((r['state'], r['code']), ('rejected', 'fenced'))
        with self.ops.store.read() as c:
            done = c.execute("SELECT detail FROM ops_heartbeat WHERE name='run:wf1:completed'").fetchone()
        self.assertTrue(done is None or json.loads(done[0])['run'] != self.run_id)

    def test_progress_and_completion_are_recorded_with_run_identity(self):
        self.ops.run_progress('wf1', self.run_id, self.fence, '/v2/board/observe')
        with self.ops.store.read() as c:
            rows = {r['name']: json.loads(r['detail']) for r in c.execute(
                "SELECT * FROM ops_heartbeat WHERE name LIKE 'run:wf1:%'")}
        self.assertEqual(rows['run:wf1:started']['run'], self.run_id)
        self.assertEqual((rows['run:wf1:progress']['run'], rows['run:wf1:progress']['step']),
                         (self.run_id, '/v2/board/observe'))
        self.ops.run_finish('wf1', self.run_id, fence=self.fence)
        with self.ops.store.read() as c:
            done = json.loads(c.execute("SELECT detail FROM ops_heartbeat WHERE name='run:wf1:completed'").fetchone()[0])
        self.assertEqual(done['run'], self.run_id)


class R5_A13_HelperRunContext(N8nHelperPath):
    def call(self, path, body):
        arg = base64.b64encode(json.dumps({'path': path, 'body': body}).encode()).decode()
        p = subprocess.run([sys.executable, str(HELPER), arg], capture_output=True, text=True, env=self.env, timeout=60)
        return json.loads(p.stdout)

    def test_run_scoped_calls_record_progress_and_superseded_runs_are_refused(self):
        self.ops.run_finish('wf1', self.run_id)
        lease = self.call('/v2/run/start', {'kind': 'wf1', 'runId': 'wf1-70'})
        run = {'kind': 'wf1', 'id': 'wf1-70', 'fence': lease['fence']}
        r = self.call('/v2/board/missing', {'ids': ['1'], 'run': run})
        self.assertTrue(r['ok'], r)
        with self.ops.store.read() as c:
            prog = json.loads(c.execute("SELECT detail FROM ops_heartbeat WHERE name='run:wf1:progress'").fetchone()[0])
        self.assertEqual((prog['run'], prog['step']), ('wf1-70', '/v2/board/missing'))
        r = self.call('/v2/board/missing', {'ids': ['1'], 'run': {**run, 'fence': lease['fence'] - 1}})
        self.assertEqual((r['ok'], r['kind']), (False, 'fenced'))

    def test_wf3_crash_is_recorded_as_failed_not_completed(self):
        bad = self.dir / 'state.sqlite'
        with self.ops.store.tx() as c:                        # a reservation the inspection cannot read
            c.execute("INSERT INTO ops_items(item_id,name,format,created,updated) VALUES('x','x','Story',0,0)")
            c.execute("INSERT INTO ops_reservations VALUES('x','acct','Story','not-a-time',1,'fp','auto',0,0,0)")
        r = self.call('/v2/monitor/run', {'runId': 'wf3-crash'})
        self.assertFalse(r['ok'])
        with self.ops.store.read() as c:
            rows = {x['name']: json.loads(x['detail']) for x in c.execute(
                "SELECT * FROM ops_heartbeat WHERE name LIKE 'run:wf3:%'")}
        self.assertEqual(rows['run:wf3:failed']['run'], 'wf3-crash')
        self.assertNotEqual((rows.get('run:wf3:completed') or {}).get('run'), 'wf3-crash')
        self.assertTrue(bad.exists())


class WorkflowRunContext(unittest.TestCase):
    """A13 (graph): WF1's run-scoped helper calls carry the run identity; Run Finish carries the fence."""

    def test_run_scoped_wf1_calls_carry_the_run(self):
        ns = nodes(WF1)
        for name in ('Import Plan', 'Record Created Items', 'Observe Board', 'Board Missing Check',
                     'Preparation Queue', 'Editor Tasks — Take', 'Run Finish'):
            js = ns[name + ' — Input']['parameters']['jsCode']
            self.assertIn("$('Run Start').first().json.fence", js, name)


if __name__ == '__main__':
    unittest.main()
