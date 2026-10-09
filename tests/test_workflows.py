"""Workflow contract tests for the built v2 exports (no n8n, no network)."""
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from support import ROOT

DIST = ROOT / 'workflows' / 'dist'
WF = {p.name.split('__')[0]: json.loads(p.read_text()) for p in DIST.glob('*.v2.json')}
HELPER_SRC = (ROOT / 'src' / 'helper.py').read_text()
ROUTES = set(re.findall(r"path == '(/v2/[a-z/]+)'", HELPER_SRC))
TRIGGERS = ('scheduleTrigger', 'manualTrigger')
NODE = shutil.which('node')


def code_nodes(w):
    return [n for n in w['nodes'] if n['type'] == 'n8n-nodes-base.code']


def all_text(w):
    return json.dumps(w['nodes'], ensure_ascii=False)


def run_js(js, json_in, refs=None, items=None, each=False):
    """Execute an n8n Code node body with a minimal, explicit runtime mock."""
    harness = r"""
const ctx=JSON.parse(require('fs').readFileSync(0,'utf8'));
const $json=ctx.json;const refs=ctx.refs||{};
const $=n=>{const v=refs[n];if(v===undefined)throw new Error('missing ref '+n);return {item:{json:v},first:()=>({json:v}),all:()=>[{json:v}]}};
const $input={all:()=>(ctx.items||[ctx.json]).map(j=>({json:j})),first:()=>({json:ctx.json})};
const $execution={id:'9'};
const out=(function(){ """ + js + r""" })();
process.stdout.write(JSON.stringify(out));"""
    p = subprocess.run([NODE, '-e', harness], input=json.dumps({'json': json_in, 'refs': refs, 'items': items}),
                       capture_output=True, text=True, timeout=20)
    if p.returncode:
        raise AssertionError(p.stderr[-800:])
    return json.loads(p.stdout)


class GraphContracts(unittest.TestCase):
    def test_three_workflows_keep_ids(self):
        self.assertEqual(set(WF), {'qI1N5VNgpRjnZAKH', 'pUIshuf16zIYoYRz', 'WasetSocialScheduleGuard'})
        for wid, w in WF.items():
            self.assertEqual(w['id'], wid)
            self.assertFalse(w['active'], 'exports must not self-activate')

    def test_connections_resolve_and_everything_reachable(self):
        for wid, w in WF.items():
            names = {n['name'] for n in w['nodes']}
            graph = {}
            for src, outs in w['connections'].items():
                self.assertIn(src, names, wid)
                for kind, lists in outs.items():
                    for lst in lists:
                        for c in lst:
                            self.assertIn(c['node'], names, f'{wid}: {src} -> {c["node"]}')
                            graph.setdefault(src, set()).add(c['node'])
            seen = {n['name'] for n in w['nodes'] if n['type'].split('.')[-1] in TRIGGERS}
            seen |= {n['name'] for n in w['nodes'] if n['type'].endswith('lmChatOpenRouter')}
            stack = list(seen)
            while stack:
                for nxt in graph.get(stack.pop(), ()):
                    if nxt not in seen:
                        seen.add(nxt)
                        stack.append(nxt)
            self.assertEqual(names - seen, set(), f'{wid}: unreachable nodes')

    def test_node_references_exist(self):
        for wid, w in WF.items():
            names = {n['name'] for n in w['nodes']}
            for ref in set(re.findall(r"\$\('([^']+)'\)", all_text(w))):
                self.assertIn(ref, names, f'{wid}: $(\'{ref}\') does not exist')

    def test_only_v2_routes_and_all_exist(self):
        for wid, w in WF.items():
            text = all_text(w)
            self.assertNotIn('/v1/', text, wid)
            for route in set(re.findall(r'path:\\"(/v2/[a-z/]+)\\"', text)):
                self.assertIn(route, ROUTES, f'{wid} calls unknown route {route}')

    def test_single_publisher(self):
        for wid, w in WF.items():
            has_publish = 'media_publish' in all_text(w)
            self.assertEqual(has_publish, wid == 'pUIshuf16zIYoYRz', wid)

    def test_social_board_writes_only_through_sync_or_creation(self):
        """No workflow decides state by writing Monday columns directly."""
        for wid, w in WF.items():
            for n in w['nodes']:
                t = json.dumps(n.get('parameters', {}))
                if 'change_multiple_column_values' in t:
                    self.assertEqual((wid, n['name']), ('pUIshuf16zIYoYRz', 'Build Sync Mutation'))
                if 'create_item(' in t:
                    self.assertEqual((wid, n['name']), ('qI1N5VNgpRjnZAKH', 'Build Create Mutations'))

    def test_llm_only_on_caption_draft_branch(self):
        for wid, w in WF.items():
            llm = [n['name'] for n in w['nodes'] if 'langchain' in n['type']]
            if wid != 'qI1N5VNgpRjnZAKH':
                self.assertEqual(llm, [], wid)      # publisher and supervisor: zero model calls
            else:
                self.assertEqual(sorted(llm), ['OpenRouter Model', 'Write Caption'])
                into = [s for s, o in w['connections'].items() for l in o.get('main', []) for c in l
                        if c['node'] == 'Write Caption']
                self.assertEqual(into, ['Generate Draft?'])
                write = next(n for n in w['nodes'] if n['name'] == 'Write Caption')
                self.assertEqual(write.get('onError'), 'continueRegularOutput')

    def test_manual_trigger_is_read_only(self):
        w = WF['WasetSocialScheduleGuard']
        targets = [c['node'] for l in w['connections']['Manual Read-only Inspection']['main'] for c in l]
        self.assertEqual(targets, ['Inspect Schedule (read-only) — Input'])
        inp = next(n for n in w['nodes'] if n['name'] == 'Inspect Schedule (read-only) — Input')
        self.assertIn('/v2/monitor/inspect', inp['parameters']['jsCode'])

    def test_payload_guard_on_every_helper_call(self):
        for wid, w in WF.items():
            for n in code_nodes(w):
                if 'helper.py' in n['parameters']['jsCode']:
                    self.assertIn('Helper payload too large', n['parameters']['jsCode'], n['name'])

    def test_no_secrets_or_credential_ids(self):
        for wid, w in WF.items():
            t = all_text(w)
            self.assertNotRegex(t, r'xox[abp]-|sk-or-v1|Bearer [A-Za-z0-9]{20}')
            for n in w['nodes']:
                for cred in (n.get('credentials') or {}).values():
                    self.assertEqual(cred.get('id'), '<REDACTED>', n['name'])


@unittest.skipUnless(NODE, 'node not installed')
class CodeNodeBehaviour(unittest.TestCase):
    def node(self, wid, name):
        return next(n for n in WF[wid]['nodes'] if n['name'] == name)['parameters']['jsCode']

    def test_all_code_nodes_parse(self):
        with tempfile.TemporaryDirectory() as d:
            for wid, w in WF.items():
                for n in code_nodes(w):
                    p = Path(d) / 'x.js'
                    p.write_text('async function __n8n(){\n' + n['parameters']['jsCode'] + '\n}\n')
                    r = subprocess.run([NODE, '--check', str(p)], capture_output=True, text=True)
                    self.assertEqual(r.returncode, 0, f"{wid}/{n['name']}: {r.stderr[-400:]}")

    def result_body(self, response):
        js = self.node('pUIshuf16zIYoYRz', 'Record Publication Result — Input')
        refs = {'Claim Publication': {'attempt_id': 'A-1'}, 'Configuration': {'worker': 'w'}}
        out = run_js(js, response, refs)
        import base64
        return json.loads(base64.b64decode(out[0]['json']['command'].split(' ')[-1]))['body']

    def test_publish_outcome_classification(self):
        ok = self.result_body({'statusCode': 200, 'body': {'id': '17900'}})
        self.assertEqual((ok['mediaId'], ok['definitive']), ('17900', False))
        rej = self.result_body({'statusCode': 400, 'body': {'error': {'message': 'Invalid media', 'code': 9004}}})
        self.assertEqual((rej['mediaId'], rej['definitive']), (None, True))
        timeout = self.result_body({'error': {'message': 'ETIMEDOUT'}})
        self.assertEqual((timeout['mediaId'], timeout['definitive']), (None, False))
        server = self.result_body({'statusCode': 503, 'body': {}})
        self.assertFalse(server['definitive'])
        ok_no_id = self.result_body({'statusCode': 200, 'body': {}})
        self.assertFalse(ok_no_id['definitive'])   # success status without id is unknown, not failure

    def test_empty_due_queue_emits_nothing(self):
        out = run_js(self.node('pUIshuf16zIYoYRz', 'Due Items'), {'ok': True, 'work': []})
        self.assertEqual(out, [])

    def test_sync_mutation_shapes(self):
        js = self.node('pUIshuf16zIYoYRz', 'Build Sync Mutation')
        m = run_js(js, {'id': 7, 'kind': 'monday', 'payload': {'item_id': '55', 'columns': {'status': {'label': 'Posted'}},
                                                               'group': 'group_title'}})
        self.assertIn('move_item_to_group', m['json']['gql']['query'])
        self.assertEqual(m['json']['gql']['variables']['b'], '5105608159')
        s = run_js(js, {'id': 8, 'kind': 'source_monday', 'payload': {'source_item_id': '99', 'label': 'Posted'}})
        self.assertEqual(s['json']['gql']['variables']['b'], '5091110326')

    def test_dropbox_problem_classification(self):
        js = self.node('qI1N5VNgpRjnZAKH', 'Dropbox Problem')
        self.assertEqual(run_js(js, {'error': {'message': 'socket hang up'}})[0]['json']['errorKind'], 'infra')
        self.assertEqual(run_js(js, {'error': {'message': 'x', 'httpCode': '503'}})[0]['json']['errorKind'], 'infra')
        self.assertEqual(run_js(js, {'error': {'message': 'shared_link_not_found', 'httpCode': '409'}})[0]['json']['errorKind'], 'config')

    def test_board_chunks_stay_under_argument_limit(self):
        items = [{'id': str(i), 'name': 'Item %d' % i, 'group': {'id': 'topics'},
                  'column_values': [{'id': 'long_text_mm7x2ay1', 'text': 'x' * 900, 'value': json.dumps({'text': 'x' * 900})}] * 3}
                 for i in range(300)]
        js = self.node('qI1N5VNgpRjnZAKH', 'Board Chunks')
        out = run_js(js, {}, {'Social Snapshot': {'items': items}, 'Source Snapshot': {'items': []}})
        self.assertGreater(len(out), 1)
        self.assertEqual(sum(len(o['json']['items']) for o in out), 300)
        for o in out:
            import base64
            cmd = 'python3 /home/node/.n8n-files/waset-social/helper.py ' + base64.b64encode(
                json.dumps({'path': '/v2/board/observe', 'body': o['json']}).encode()).decode()
            self.assertLess(len(cmd), 131072)


if __name__ == '__main__':
    unittest.main()


@unittest.skipUnless(NODE, 'node not installed')
class EditorTaskReuse(unittest.TestCase):
    """Cutover safety: v2 must reuse editor subitems created by v1 (same name prefix)."""

    def js(self):
        return next(n for n in WF['qI1N5VNgpRjnZAKH']['nodes'] if n['name'] == 'Editor Task Mutation')['parameters']['jsCode']

    def test_existing_v1_subitem_is_updated_not_duplicated(self):
        job = {'payload': {'item_id': '3267478554', 'source_item_id': '99', 'task_id': None,
                           'task_name': 'Social 3267478554 — تجهيز أو استبدال الفيديو', 'body': 'x'}}
        lookup = {'data': {'items': [{'id': '99', 'subitems': [{'id': '555', 'name': 'Social 3267478554 — تجهيز أو استبدال الفيديو'},
                                                                {'id': '556', 'name': 'Social 1 — other'}]}]}}
        out = run_js(self.js(), lookup, {'Each Editor Job': job})
        self.assertEqual(out[0]['json']['taskId'], '555')
        self.assertIn('change_column_value', out[0]['json']['gql']['query'])

    def test_no_existing_subitem_creates_one(self):
        job = {'payload': {'item_id': '7', 'source_item_id': '99', 'task_id': None, 'task_name': 'Social 7 — t', 'body': 'x'}}
        out = run_js(self.js(), {'data': {'items': [{'id': '99', 'subitems': []}]}}, {'Each Editor Job': job})
        self.assertIsNone(out[0]['json']['taskId'])
        self.assertIn('create_subitem', out[0]['json']['gql']['query'])
