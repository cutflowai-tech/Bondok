"""Run the real helper.py the way n8n does (base64 argv, JSON stdout)."""
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from support import ROOT, Clock, OpsCase, monday_item, owner_cmd, rules

from waset_ops import media
from waset_ops.db import Store

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


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg not installed')
class StoryTrimFfmpeg(unittest.TestCase):
    """Real FFmpeg: the 60-65 s Story trim on actual video files (download replaced by a local copy)."""

    @classmethod
    def setUpClass(cls):
        cls.src = tempfile.TemporaryDirectory()
        cls.videos = {}
        for secs in (58.0, 61.247, 65.0, 66.0):
            path = Path(cls.src.name) / f'src-{secs}.mp4'
            subprocess.run(['ffmpeg', '-nostdin', '-y', '-v', 'error', '-f', 'lavfi', '-i',
                            f'testsrc2=size=1080x1920:rate=10:duration={secs}', '-f', 'lavfi', '-i',
                            f'sine=frequency=440:duration={secs}', '-c:v', 'libx264', '-preset', 'ultrafast',
                            '-c:a', 'aac', '-shortest', str(path)], check=True)
            cls.videos[secs] = path

    @classmethod
    def tearDownClass(cls):
        cls.src.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = media.ROOT, media._download, media.raw_url
        media.ROOT = Path(self.tmp.name)
        Store(media.ROOT / 'state.sqlite').migrate()

        def fake_download(source, original, content_hash):
            shutil.copyfile(source, original)
            if media.dropbox_hash(original) != content_hash:
                raise ValueError('Dropbox file changed after selection')
        media._download, media.raw_url = fake_download, (lambda url: url)

    def tearDown(self):
        media.ROOT, media._download, media.raw_url = self.saved
        self.tmp.cleanup()

    def body(self, secs):
        path = self.videos[secs]
        return {'itemId': '7' + str(int(secs)), 'format': 'Story', 'sourceUrl': str(path), 'fileId': 'id:F' + str(secs),
                'revision': 'r1', 'contentHash': media.dropbox_hash(path), 'assetKey': f'id:F{secs}@r1'}

    def streams(self, path):
        d = json.loads(subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                                       'format=duration,start_time:stream=codec_type,duration,width,height',
                                       '-of', 'json', str(path)], capture_output=True, text=True, check=True).stdout)
        return float(d['format']['duration']), {s['codec_type']: s for s in d['streams']}

    def test_61_seconds_is_trimmed_at_the_end_to_59_9(self):
        b = self.body(61.247)
        before = media.sha256_file(self.videos[61.247])
        pre = media.job_preflight(b)
        self.assertTrue(pre['ready'], pre)
        self.assertEqual(pre['trimTo'], 59.9)
        r = media.job_prepare(b)
        self.assertTrue(r['ready'], r)
        total, st = self.streams(r['filePath'])
        self.assertTrue(59.5 <= total < 60.0, total)
        self.assertAlmostEqual(float(st['video']['duration']), 59.9, delta=0.15)
        self.assertLess(float(st['audio']['duration']), 60.0)
        self.assertEqual((st['video']['width'], st['video']['height']), (1080, 1920))
        self.assertAlmostEqual(r['trimmedFrom'], 61.247, places=2)
        self.assertEqual(r['trimmedTo'], 59.9)
        self.assertIsNone(rules.media_failure(r, 'Story'))
        self.assertEqual(media.sha256_file(self.videos[61.247]), before)       # original untouched
        # The start is kept: the first output frame matches the source's first frame (same picture).
        def first_frame(path):
            return subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-frames:v', '1', '-vf',
                                   'scale=27:48,format=gray', '-f', 'rawvideo', '-'], capture_output=True, check=True).stdout
        a, o = first_frame(self.videos[61.247]), first_frame(r['filePath'])
        self.assertLess(sum(abs(x - y) for x, y in zip(a, o)) / len(a), 8)

    def test_65_seconds_inclusive_is_trimmed(self):
        r = media.job_prepare(self.body(65.0))
        self.assertTrue(r['ready'], r)
        self.assertTrue(59.5 <= self.streams(r['filePath'])[0] < 60.0)

    def test_under_60_is_not_trimmed(self):
        r = media.job_prepare(self.body(58.0))
        self.assertTrue(r['ready'], r)
        self.assertNotIn('trimmedFrom', r)
        self.assertAlmostEqual(self.streams(r['filePath'])[0], 58.0, delta=0.15)
        self.assertIsNone(media.job_preflight(self.body(58.0))['trimTo'])

    def test_above_65_needs_the_editor_and_produces_no_file(self):
        b = self.body(66.0)
        pre = media.job_preflight(b)
        self.assertFalse(pre['ready'])
        self.assertTrue(pre['replaceRequired'])
        r = media.job_prepare(b)
        self.assertFalse(r['ready'])
        self.assertIn('automatic trimming covers up to 65 seconds', r['reason'])
        self.assertEqual(list((media.ROOT / 'media').glob('*.mp4')), [])


if __name__ == '__main__':
    unittest.main()
