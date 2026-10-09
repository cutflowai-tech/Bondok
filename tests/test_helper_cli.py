"""Run the real helper.py the way n8n does (base64 argv, JSON stdout)."""
import base64
import json
import os
import subprocess
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone

from support import ROOT, Clock, OpsCase, monday_item, owner_cmd, rules

HELPER = ROOT / 'src' / 'helper.py'


class HelperCli(OpsCase):
    def setUp(self):
        super().setUp()
        self.clock.set(datetime.now(timezone.utc).replace(microsecond=0))
        self.env = {**os.environ, 'WASET_SOCIAL_DATA_DIR': str(self.dir)}

    def call(self, path, body):
        arg = base64.b64encode(json.dumps({'path': path, 'body': body}).encode()).decode()
        p = subprocess.run([sys.executable, str(HELPER), arg], capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def due_now(self, iid):
        """Move the confirmed reservation to one minute ago (synthetic fixture)."""
        self.ops.clock = time.time
        slot = rules.iso(datetime.now(timezone.utc) - timedelta(minutes=1))
        with self.ops.store.tx() as c:
            it = self.ops.item(c, iid)
            c.execute('UPDATE ops_reservations SET slot=?, payload_fp=? WHERE item_id=?',
                      (slot, self.ops.payload_fp(c, it), iid))

    def test_full_publication_sequence_via_cli(self):
        self.make_ready('900', 'Story')
        self.due_now('900')
        due = self.call('/v2/publish/due', {'worker': 'wf2-1'})
        self.assertTrue(due['ok'])
        self.assertEqual([w['item_id'] for w in due['work']], ['900'])
        fresh = monday_item('900', fmt='Story')
        claim = self.call('/v2/publish/claim', {'itemId': '900', 'worker': 'wf2-1', 'freshItem': fresh,
                                                'sourceStatus': 'Story'})
        self.assertTrue(claim['claimed'], claim)
        self.assertEqual(claim['payload']['media_type'], 'STORIES')
        self.assertNotIn('caption', claim['payload'])
        base = {'attemptId': claim['attempt_id'], 'worker': 'wf2-1', 'fence': claim['fence']}
        self.assertTrue(self.call('/v2/publish/container', {**base, 'containerId': 'C-900'})['ok'])
        self.assertTrue(self.call('/v2/publish/renew', base)['ok'])
        commit = self.call('/v2/publish/commit', {**base, 'sourceAsset': claim['source_asset'],
                                                  'containerStatus': 'FINISHED'})
        self.assertTrue(commit['committed'], commit)
        res = self.call('/v2/publish/result', {'attemptId': claim['attempt_id'], 'worker': 'wf2-1',
                                               'mediaId': '17999', 'httpStatus': 200})
        self.assertEqual(res['stage'], 'published')
        self.assertTrue(self.call('/v2/publish/evidence', {'attemptId': claim['attempt_id'],
                                                           'permalink': 'https://instagram.com/stories/x'})['ok'])
        jobs = self.call('/v2/outbox/take', {'kinds': ['monday', 'source_monday'], 'worker': 's', 'limit': 50})['jobs']
        self.assertTrue(any(j['payload'].get('group') == 'group_title' for j in jobs if j['kind'] == 'monday'))
        for j in jobs:
            self.assertTrue(self.call('/v2/outbox/ack', {'id': j['id'], 'worker': 's', 'ok': True})['ok'])
        again = self.call('/v2/publish/claim', {'itemId': '900', 'worker': 'wf2-2'})
        self.assertFalse(again['claimed'])

    def test_fresh_pause_on_board_blocks_claim(self):
        self.make_ready('901', 'Story')
        self.due_now('901')
        self.drain_monday()
        fresh = monday_item('901', fmt='Story', status='Paused')
        with self.ops.store.read() as c:
            it = self.ops.item(c, '901')
        # carry our projected values so only the status differs
        proj = json.loads(it['projected'])
        fresh['column_values'] = [cv for cv in fresh['column_values'] if cv['id'] != 'status']
        fresh['column_values'].append({'id': 'status', 'text': 'Paused', 'value': '{}'})
        claim = self.call('/v2/publish/claim', {'itemId': '901', 'worker': 'w', 'freshItem': fresh})
        self.assertFalse(claim['claimed'])
        self.assertEqual(self.item('901')['owner_state'], 'paused')

    def test_retired_and_unknown_routes_keep_diagnostics(self):
        r = self.call('/v1/lock', {'name': 'preparation', 'owner': 'x'})
        self.assertEqual((r['ok'], r['kind']), (False, 'retired'))
        r = self.call('/v2/publish/claim', {})
        self.assertFalse(r['ok'])
        self.assertIn('KeyError', r['kind'])
        log = (self.dir / 'errors.log').read_text().strip().splitlines()
        self.assertGreaterEqual(len(log), 2)

    def test_health_is_read_only(self):
        self.call('/v2/health', {})
        before = (self.dir / 'state.sqlite').stat().st_mtime_ns
        with self.ops.store.read() as c:
            n = c.execute('SELECT COUNT(*) FROM ops_audit').fetchone()[0]
        self.call('/v2/health', {})
        with self.ops.store.read() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM ops_audit').fetchone()[0], n)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM health').fetchone()[0], 0)

    def test_wf3_lock_contention_is_deferred(self):
        self.ops.run_start('wf3', 'other-run')
        r = self.call('/v2/monitor/run', {'runId': 'wf3-2'})
        self.assertTrue(r['deferred'])
        self.assertFalse(r['acquired'])

    def test_prep_step_post_without_topaz_blocks_without_media_work(self):
        self.ops.clock = time.time
        self.ops.observe([monday_item('902', fmt='Post')])
        self.ops.run_finish('wf1', self.run_id)       # fixture run ends; a new WF1 run may start
        lease = self.call('/v2/run/start', {'kind': 'wf1', 'runId': 'wf1-77'})
        r = self.call('/v2/prep/step', {'requestId': 'wf1-77:902', 'itemId': '902', 'runId': 'wf1-77',
                                        'fence': lease['fence'], 'expectedFormat': 'Post',
                                        'file': {'id': 'id:F', 'rev': 'r1', 'content_hash': 'h', 'name': 'v.mp4'},
                                        'url': 'https://www.dropbox.com/s/a/v.mp4'})
        self.assertEqual(r['blocked'], 'topaz')
        self.assertFalse(any(p.name.endswith('.lock') and p.name.startswith('media-capacity') for p in self.dir.iterdir()))


if __name__ == '__main__':
    unittest.main()
