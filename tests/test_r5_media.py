"""Round 5 media/preparation traces (docs/AUDIT_ROUND5_2026-10-10.md), one class per register entry.

B3, A4, A9, A10, M9, M10, M13, M16, M17, R5-LOW-06, R5-LOW-15. Each test reproduces the original counterexample
against the handler / generated workflow and asserts the owner-visible outcome required by
docs/OWNER_AUTHORITY_CONTRACT.md §6. Synthetic data only, no network. Real FFmpeg/ffprobe (small lavfi media)
where the trace depends on actual media; those tests are skipped when FFmpeg is not installed.
"""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from support import OpsCase, board_from_projection, monday_item, owner_cmd, rules, set_cell
from test_workflows import NODE, WF, run_js

import helper  # noqa: E402  (src/helper.py: the real WF1 preparation step)
from waset_ops import media
from waset_ops.db import dumps, loads

FFMPEG = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))
WF1, WF2 = WF['qI1N5VNgpRjnZAKH'], WF['pUIshuf16zIYoYRz']
FOLDER = 'https://www.dropbox.com/scl/fo/abc/proj?rlkey=x'
NEW = 'https://www.dropbox.com/scl/fi/zzz/approved_v9.mp4?rlkey=y'


def node(w, name):
    return next(n for n in w['nodes'] if n['name'] == name)


def js(w, name):
    return node(w, name)['parameters']['jsCode']


def targets(w, name, out=0):
    outs = w['connections'].get(name, {}).get('main', [])
    return [c['node'] for c in outs[out]] if len(outs) > out else []


def helper_body(out):
    """Body of the helper command built by an n8n '… — Input' Code node."""
    return json.loads(base64.b64decode(out[0]['json']['command'].split(' ')[-1]))['body']


def video(path, *, size='1080x1920', rate=10, secs=4.0, audio=True, audio_src='sine=frequency=440', ch=None):
    args = ['ffmpeg', '-nostdin', '-y', '-v', 'error', '-f', 'lavfi', '-i', f'testsrc2=size={size}:rate={rate}:duration={secs}']
    if audio:
        args += ['-f', 'lavfi', '-i', f'{audio_src}:duration={secs}']
        if ch:
            args += ['-ac', str(ch)]
        args += ['-c:a', 'aac', '-shortest']
    args += ['-c:v', 'libx264', '-preset', 'ultrafast', str(path)]
    subprocess.run(args, check=True)
    return path


def slack(case):
    return [json.loads(o['payload'])['text'] for o in case.outbox('slack')]


def editor_jobs(case, iid, prefix=''):
    return [o for o in case.outbox('editor') if o['item_id'] == iid and
            json.loads(o['payload'])['issue_key'].startswith(prefix)]


class MediaCase(OpsCase):
    """media.* bound to this test's data root and clock; downloads are local copies; detached jobs run on demand
    (as the real detached worker would finish between two WF1 cycles)."""

    def setUp(self):
        super().setUp()
        self.saved = (media.ROOT, media._download, media.raw_url, media._launch, getattr(media, '_now', time.time))
        media.ROOT = self.dir
        self.downloads = 0
        self.t = 1_900_000_000.0
        media._now = lambda: self.t

        def fake_download(source, original, content_hash):
            self.downloads += 1
            shutil.copyfile(source, original)
            if media.dropbox_hash(original) != content_hash:
                raise ValueError('Dropbox file changed after selection; resolve the latest revision again')
        media._download, media.raw_url = fake_download, (lambda url: url)
        self.queued = []

        def launch(kind, job_id, body, attempt):
            self.queued.append((kind, job_id, body, attempt))
            return {'ready': False, 'pending': True, 'reason': kind + ' job started; result on the next cycle'}
        media._launch = launch
        self.src = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.src, True)

    def tearDown(self):
        (media.ROOT, media._download, media.raw_url, media._launch, media._now) = self.saved
        super().tearDown()

    def tick(self, seconds):
        self.clock.advance(seconds)
        self.t += seconds

    def finish_jobs(self):
        while self.queued:
            kind, job_id, body, attempt = self.queued.pop(0)
            media.run_job({'kind': kind, 'body': body, 'mid': job_id, 'attempt': attempt})

    def file_of(self, path, n=1):
        return {'id': f'id:FILE{n}', 'rev': f'rev{n}', 'content_hash': media.dropbox_hash(path), 'name': path.name}

    def visit(self, iid, file, url, fmt):
        """One WF1 visit of the item through the real helper route (prep_source -> preflight/prepare -> result)."""
        self.seq += 1
        return helper.prep_step(self.ops, {'requestId': f'{self.run_id}:{iid}:{self.seq}', 'itemId': iid,
                                           'runId': self.run_id, 'fence': self.fence, 'expectedFormat': fmt,
                                           'file': file, 'url': url})

    def due(self, iid):
        return any(w['item_id'] == iid for w in self.ops.work_queue(limit=50))

    def cycles(self, iid, file, url, fmt, n, step=3600):
        """n WF1 cycles: the detached job finishes, the item is visited only when the queue lists it."""
        out = []
        for _ in range(n):
            self.finish_jobs()
            if self.due(iid):
                out.append(self.visit(iid, file, url, fmt))
            self.tick(step)
        return out

    def topazed_item(self, iid, path, fmt='Post'):
        self.observe(monday_item(iid, fmt=fmt, code='LIP' + iid, name=f'Item{iid} LIP{iid}',
                                 extra={'source_item': ('S' + iid, None)}))
        f = self.file_of(path)
        self.wf1('prep_source', iid, file=f, url=str(path))
        self.topaz(iid)
        return f


# ============================================================================ B3
class R5_B3_OwnerSelectedFileOutranksFolder(OpsCase):
    """B3: 'Replace source' was ignored whenever the item had a project folder: WF1 listed the folder, re-selected the
    rejected file and the old Topaz confirmation still applied."""

    def replaced(self, iid, via):
        self.make_ready(iid)                                      # FILE1 from the project folder, Topazed, scheduled
        with self.ops.store.tx() as c:
            c.execute('UPDATE ops_items SET folder_url=? WHERE item_id=?', (FOLDER, iid))
        self.assertIsNotNone(self.res(iid))
        self.drain_monday()
        if via == 'slack':
            r = self.ops.submit(owner_cmd(self.rid(), 'replace_source', iid, url=NEW))
            self.assertEqual(r['state'], 'completed', r)
        else:                                                     # the owner pastes the link into Dropbox Link
            bi = board_from_projection(self.ops, iid, code='LIP12')
            self.observe(set_cell(bi, 'dropbox', NEW, {'url': NEW, 'text': 'approved'}))

    def test_owner_selection_is_persisted_and_routed_to_the_exact_file(self):
        for iid, via in (('100', 'slack'), ('101', 'board')):
            with self.subTest(via=via):
                self.replaced(iid, via)
                with self.ops.store.read() as c:
                    sel = c.execute("SELECT * FROM ops_checks WHERE item_id=? AND kind='selection'", (iid,)).fetchone()
                self.assertIsNotNone(sel, 'selection mode must be persisted')
                self.assertEqual(sel['key'], 'owner_selected_file')
                self.assertEqual(loads(sel['result'])['url'], NEW)
                self.assertIsNone(self.res(iid))
                w = [x for x in self.ops.work_queue(50) if x['item_id'] == iid][0]
                self.assertEqual((w.get('selection_mode'), w.get('selected_url')), ('owner_selected_file', NEW))
                if NODE:
                    ctx = run_js(js(WF1, 'Item Context'), w, refs={'Source Snapshot': {'items': []}})[0]['json']
                    self.assertTrue(ctx['ownerSelected'])
                    self.assertEqual(ctx['selectedUrl'], NEW)
                    self.assertNotEqual(ctx['folderUrl'], FOLDER)         # the folder is never listed for it
        # Graph: the owner branch resolves the selected link itself, before any folder listing.
        self.assertEqual(targets(WF1, 'Continue Preparation'), ['Owner Selected File?'])
        self.assertEqual(targets(WF1, 'Owner Selected File?', 0), ['Owner File Link Metadata'])
        self.assertEqual(targets(WF1, 'Owner Selected File?', 1), ['Has Folder?'])
        meta = node(WF1, 'Owner File Link Metadata')['parameters']
        self.assertIn('get_shared_link_metadata', meta['url'])
        self.assertIn('selectedUrl', meta['jsonBody'])
        self.assertEqual(targets(WF1, 'Owner File Link Metadata'), ['Owner File Link'])
        self.assertEqual(targets(WF1, 'Owner File Link'), ['Owner File Version'])
        self.assertEqual(targets(WF1, 'Owner File Version'), ['Owner Selected File'])
        self.assertEqual(targets(WF1, 'Owner Selected File'), ['Preparation Step — Input'])

    def test_folder_result_is_refused_and_only_the_selected_identity_is_authorized(self):
        self.replaced('102', 'slack')
        old = self.file(1, 'c102-')
        r = self.wf1('prep_source', '102', file=old, url='https://www.dropbox.com/s/f1/v.mp4')   # folder listing
        it = self.item('102')
        self.assertIsNone(it['asset_key'], r)                        # the rejected file is not selected again
        self.assertEqual(it['file_url'], NEW)
        self.assertEqual(r.get('refused'), 'not_owner_selected', r)
        sel = {'mode': 'owner_selected_file', 'url': NEW}
        new = self.file(9, 'c102-')
        r = self.wf1('prep_source', '102', file=new, url=NEW, selection=sel)
        self.assertEqual(self.item('102')['asset_key'], 'id:FILE9@rev9')
        self.duration('102', 30.0, n=9)
        r = self.wf1('prep_source', '102', file=new, url=NEW, selection=sel)
        self.assertEqual(r.get('blocked'), 'topaz', r)              # FILE1's Topaz does not carry over
        self.topaz('102', 9)
        r = self.wf1('prep_source', '102', file=new, url=NEW, selection=sel)
        self.assertEqual(r.get('next'), 'prepare', r)
        mid = self.add_media('102', 'Story', 9)
        self.wf1('prep_media', '102', result={'ready': True, 'mediaId': mid})
        self.assertIsNotNone(self.res('102'))
        # A late folder result (old workflow version) cannot switch the item back to the old file.
        r = self.wf1('prep_source', '102', file=old, url='https://www.dropbox.com/s/f1/v.mp4')
        self.assertEqual(self.item('102')['asset_key'], 'id:FILE9@rev9')
        self.assertIsNotNone(self.res('102'))
        self.clock.set(rules.instant(self.res('102')['slot']) + timedelta(seconds=5))
        cl = self.ops.claim('102', 'w')
        self.assertTrue(cl['claimed'], cl)
        self.assertEqual(cl['source_asset'], 'id:FILE9@rev9')
        self.assertEqual(cl['payload']['verification_id'], mid)

    @unittest.skipUnless(NODE, 'node not installed')
    def test_owner_branch_resolves_the_link_and_sends_the_selection(self):
        ctx = {'itemId': '103', 'format': 'Story', 'selection_mode': 'owner_selected_file', 'selected_url': NEW,
               'selectedUrl': NEW, 'ownerSelected': True}
        link = {'.tag': 'file', 'id': 'id:NEW', 'rev': 'r9', 'name': 'approved_v9.mp4',
                'path_lower': '/projects/x/approved_v9.mp4', 'url': NEW}
        out = run_js(js(WF1, 'Owner File Link'), link, refs={'Item Context': ctx})[0]['json']
        self.assertEqual(out['path'], '/projects/x/approved_v9.mp4')
        for bad in ({**link, '.tag': 'folder'}, {k: v for k, v in link.items() if k != 'path_lower'}):
            with self.assertRaises(AssertionError) as e:
                run_js(js(WF1, 'Owner File Link'), bad, refs={'Item Context': ctx})
            self.assertIn('[config]', str(e.exception))
        meta = {'.tag': 'file', 'id': 'id:NEW', 'rev': 'r9', 'content_hash': 'h9', 'name': 'approved_v9.mp4',
                'path_lower': '/projects/x/approved_v9.mp4'}
        sel = run_js(js(WF1, 'Owner Selected File'), meta, refs={'Item Context': ctx, 'Owner File Link Metadata': link})
        sel = sel[0]['json']
        self.assertEqual((sel['ok'], sel['url'], sel['file']['id']), (True, NEW, 'id:NEW'))
        refs = {'Item Context': ctx, 'Configuration': {'runId': 'wf1-1'}, 'Run Start': {'fence': 3}}
        body = helper_body(run_js(js(WF1, 'Preparation Step — Input'), sel, refs=refs))
        self.assertEqual(body['selection'], {'mode': 'owner_selected_file', 'url': NEW})
        self.assertEqual(body['file']['id'], 'id:NEW')


# ============================================================================ A4
class R5_A4_PortableCacheIdentity(OpsCase):
    """A4: media.path is the n8n container path; Bondok (ProtectHome=true) sees the volume elsewhere, so every
    prepared file looked missing and a Slack "Topaz done" un-verified a ready item and released its slot."""

    N8N = '/home/node/.n8n-files/waset-social/media/'

    def ready(self, iid, *, key=True, delivered=False):
        self.make_ready(iid)
        with self.ops.store.tx() as c:
            m = dict(c.execute('SELECT * FROM media WHERE item=?', (iid,)).fetchone())
            (self.dir / 'media').mkdir(exist_ok=True)
            shutil.move(m['path'], self.dir / 'media' / (m['id'] + '.mp4'))
            info = loads(m['metadata'])
            if key:
                info['cacheKey'] = 'media/' + m['id'] + '.mp4'
            if delivered:
                info['delivered'] = {'id': 'id:DLV' + iid, 'rev': 'd1', 'content_hash': 'dh', 'path': '/x.mp4'}
            c.execute('UPDATE media SET path=?, metadata=? WHERE id=?', (self.N8N + m['id'] + '.mp4', dumps(info), m['id']))
        return m['id']

    def topaz_from_slack(self, ops, iid):
        return ops.submit(owner_cmd(self.rid('slack'), 'confirm_topaz', iid, explicit=True, confirmed=True,
                                    asset_key=self.item(iid)['asset_key']))

    def test_legacy_absolute_row_is_found_by_file_name(self):
        mid = self.ready('200', key=False)
        slot = self.res('200')['slot']
        r = self.topaz_from_slack(self.ops, '200')
        self.assertEqual(r['state'], 'completed', r)
        it = self.item('200')
        self.assertEqual((it['readiness'], it['verification_id']), ('ready', mid))
        self.assertEqual(self.res('200')['slot'], slot)

    def test_bondok_view_with_a_different_root(self):
        from waset_ops import Ops
        mid = self.ready('201')
        slot = self.res('201')['slot']
        bondok_root = Path(tempfile.mkdtemp()) / 'pipeline'          # e.g. /var/lib/bondok/pipeline (bind mount)
        self.addCleanup(shutil.rmtree, bondok_root.parent, True)
        shutil.copytree(self.dir, bondok_root)
        bondok = Ops(bondok_root / 'state.sqlite', clock=self.clock)
        with bondok.store.read() as c:
            m = bondok.media_row(c, mid)
            self.assertEqual(media.local_file(m, bondok.data_roots()), bondok_root / 'media' / (mid + '.mp4'))
        r = self.topaz_from_slack(bondok, '201')
        self.assertEqual(r['state'], 'completed', r)
        with bondok.store.read() as c:
            it = bondok.item(c, '201')
            self.assertEqual((it['readiness'], it['verification_id']), ('ready', mid))
            self.assertEqual(bondok.reservation(c, '201')['slot'], slot)
        # Same answer in the n8n runtime (its own root).
        with self.ops.store.read() as c:
            self.assertEqual(media.local_file(self.ops.media_row(c, mid), self.ops.data_roots()),
                             self.dir / 'media' / (mid + '.mp4'))

    def test_missing_cache_is_not_a_lost_delivery_when_the_delivered_copy_is_bound(self):
        mid = self.ready('202', delivered=True)
        (self.dir / 'media' / (mid + '.mp4')).unlink()                 # local cache gone (cleanup, other namespace)
        slot = self.res('202')['slot']
        self.topaz_from_slack(self.ops, '202')
        self.assertEqual(self.item('202')['readiness'], 'ready')
        self.assertEqual(self.res('202')['slot'], slot)
        self.clock.set(rules.instant(slot) + timedelta(seconds=5))
        self.assertTrue(self.ops.claim('202', 'w')['claimed'])

    def test_unbound_legacy_delivery_with_missing_cache_still_needs_preparation(self):
        mid = self.ready('203')
        (self.dir / 'media' / (mid + '.mp4')).unlink()
        self.topaz_from_slack(self.ops, '203')
        self.assertIsNone(self.item('203')['verification_id'])
        self.assertIsNone(self.res('203'))


# ============================================================================ A9
@unittest.skipUnless(FFMPEG, 'FFmpeg not installed')
class R5_A9_RecheckTerminates(MediaCase):
    """A9: an explicit recheck of an item blocked by a media verdict never finished and re-downloaded the source on
    every WF1 visit."""

    def blocked_720p(self, iid):
        src = video(self.src / 'export_720p.mp4', size='720x1280')
        f = self.topazed_item(iid, src)
        self.cycles(iid, f, str(src), 'Post', 3)
        it = self.item(iid)
        self.assertEqual(it['readiness'], 'blocked', it)
        self.assertIn('1080', it['block_reason'])
        return src, f

    def test_recheck_of_blocked_item_is_one_inspection_with_a_typed_result(self):
        src, f = self.blocked_720p('300')
        before = self.downloads
        self.ops.submit(owner_cmd(self.rid(), 'request_recheck', '300'))
        self.cycles('300', f, str(src), 'Post', 8, step=600)
        with self.ops.store.read() as c:
            chk = dict(c.execute("SELECT * FROM ops_checks WHERE item_id='300' AND kind='media'").fetchone())
        self.assertEqual(chk['state'], 'done')
        self.assertEqual(loads(chk['result'])['kind'], 'blocked')
        self.assertEqual(self.downloads - before, 1)                    # one bounded inspection
        notes = [t for t in slack(self) if 'Recheck finished' in t]
        self.assertEqual(len(notes), 1, notes)
        self.assertIn('1080', notes[0])
        self.cycles('300', f, str(src), 'Post', 12)
        self.assertEqual(self.downloads - before, 1)                    # no re-download afterwards

    def test_recheck_with_a_temporary_failure_still_terminates(self):
        src, f = self.blocked_720p('301')
        self.ops.submit(owner_cmd(self.rid(), 'request_recheck', '301'))

        def offline(source, original, content_hash):
            self.downloads += 1
            raise ConnectionError('Dropbox unreachable')
        media._download = offline
        self.cycles('301', f, str(src), 'Post', 6, step=600)
        with self.ops.store.read() as c:
            chk = dict(c.execute("SELECT * FROM ops_checks WHERE item_id='301' AND kind='media'").fetchone())
        self.assertEqual(chk['state'], 'done')
        self.assertEqual(loads(chk['result'])['kind'], 'temporary_failure')
        self.assertEqual(len([t for t in slack(self) if 'Recheck finished' in t]), 1)


# ============================================================================ A10
@unittest.skipUnless(FFMPEG, 'FFmpeg not installed')
class R5_A10_DefectiveSourcesArePermanent(MediaCase):
    """A10: audio-only, truncated, duration-less and oversized sources and FFmpeg timeouts were 'temporary' for ever:
    never blocked, editor never told, re-downloaded hourly."""

    def sources(self):
        out = {}
        out['No video stream'] = video(self.src / 'audio_only.mp4', audio=True, size='1080x1920', secs=3)
        # Re-mux to audio only.
        a = self.src / 'audio.m4a'
        subprocess.run(['ffmpeg', '-nostdin', '-y', '-v', 'error', '-i', str(out['No video stream']), '-vn', '-c:a',
                        'copy', str(a)], check=True)
        out['No video stream'] = a
        good = video(self.src / 'good.mp4', secs=6)
        t = self.src / 'truncated.mp4'                               # export crashed: moov atom (at the end) missing
        t.write_bytes(good.read_bytes()[: good.stat().st_size // 2])
        out['cannot be read'] = t
        raw = self.src / 'raw_stream.mp4'                            # elementary H.264 stream: no container duration
        subprocess.run(['ffmpeg', '-nostdin', '-y', '-v', 'error', '-i', str(good), '-c:v', 'copy', '-f', 'h264',
                        str(raw)], check=True)
        out['duration'] = raw
        return out

    def test_file_defects_block_once_with_one_editor_task_and_no_redownload(self):
        for n, (expect, path) in enumerate(self.sources().items()):
            for fmt in ('Post', 'Story'):
                iid = f'5{n}{0 if fmt == "Post" else 1}'
                with self.subTest(defect=expect, format=fmt):
                    self.downloads = 0
                    f = self.topazed_item(iid, path, fmt)
                    self.cycles(iid, f, str(path), fmt, 8)
                    it = self.item(iid)
                    self.assertEqual((it['readiness'], it['block_kind']), ('blocked', 'editor'), it)
                    self.assertIn(expect, it['block_reason'])
                    self.assertIsNone(it['infra_issue'])
                    self.assertEqual(len(editor_jobs(self, iid, 'media:')), 1)
                    self.assertEqual(self.downloads, 1)

    def test_oversized_source_is_a_defect(self):
        path = video(self.src / 'big.mp4', secs=3)
        f = self.topazed_item('560', path)
        real = self.saved[1]
        opener = mock.Mock()
        opener.open.side_effect = lambda req, timeout=None: open(path, 'rb')
        media._download = real
        with mock.patch.object(media, 'build_opener', return_value=opener), \
                mock.patch.object(media, 'SOURCE_MAX_BYTES', 10_000):
            self.cycles('560', f, 'https://www.dropbox.com/s/big/v.mp4', 'Post', 6)
        it = self.item('560')
        self.assertEqual((it['readiness'], it['block_kind']), ('blocked', 'editor'), it)
        self.assertIn('5 GB', it['block_reason'])
        self.assertEqual(opener.open.call_count, 1)

    def test_repeated_ffmpeg_timeout_has_a_finite_budget_and_one_escalation(self):
        path = video(self.src / 'slow.mp4', secs=3)
        f = self.topazed_item('570', path)
        with mock.patch.object(media, 'ENCODE_TIMEOUT', 0.01):
            self.cycles('570', f, str(path), 'Post', 48)                # two simulated days of WF1 cycles
        it = self.item('570')
        self.assertEqual(self.downloads, media.MAX_TRANSIENT_ATTEMPTS)
        self.assertEqual(it['readiness'], 'blocked', it)
        self.assertEqual(it['block_kind'], 'review')
        self.assertIn('timed out', it['block_reason'])
        notes = [t for t in slack(self) if 'Preparation of' in t and 'stopped retrying' in t]
        self.assertEqual(len(notes), 1, slack(self))
        self.assertEqual(editor_jobs(self, '570', 'media:'), [])          # not the editor's fault
        # Explicit recheck: a fresh, bounded attempt.
        self.ops.submit(owner_cmd(self.rid(), 'request_recheck', '570'))
        out = self.cycles('570', f, str(path), 'Post', 4, step=600)
        self.assertEqual(out[-1].get('next'), 'upload', out)             # prepared; delivery follows
        self.assertEqual(self.item('570')['readiness'], 'checking')
        self.assertEqual(self.downloads, media.MAX_TRANSIENT_ATTEMPTS + 1)


# ============================================================================ M9
class R5_M9_DeliveryFailuresRecorded(OpsCase):
    """M9: upload/share/register failures went straight to 'Item Finished': nothing recorded, the 145 MB upload was
    repeated every 10 minutes for ever."""

    STAGES = ['Ensure Prepared Dropbox Folder', 'Prepared Folder Checked', 'Read Verified Video from n8n Disk',
              'Upload Verified 1080 Video', 'Record Upload', 'Share Verified Video', 'Find Verified Share',
              'Verified Delivery Link', 'Register Verified Delivery', 'Identify Delivered Copy']

    def test_every_delivery_error_output_is_recorded(self):
        for n in self.STAGES:
            with self.subTest(node=n):
                self.assertEqual(targets(WF1, n, 1), ['Delivery Problem'])
        self.assertEqual(targets(WF1, 'Delivery Problem'), ['Record Delivery Failure — Input'])
        self.assertIn('/v2/prep/delivered', js(WF1, 'Record Delivery Failure — Input'))

    @unittest.skipUnless(NODE, 'node not installed')
    def test_delivery_problem_names_stage_and_class(self):
        prog = js(WF1, 'Delivery Problem')
        cases = [('Upload Verified 1080 Video', {'error': {'message': 'x', 'httpCode': '503'}}, 'upload', 'infra'),
                 ('Prepared Folder Checked', {'error': {'message': 'path/insufficient_space/..'}}, 'folder', 'config'),
                 ('Share Verified Video', {'error': 'socket hang up'}, 'share', 'infra'),
                 ('Read Verified Video from n8n Disk', {'error': {'message': 'ENOENT'}}, 'read', 'infra')]
        for prev, err, stage, kind in cases:
            with self.subTest(prev=prev):
                out = run_js(f"const $prevNode={{name:{json.dumps(prev)}}};\n" + prog, err)[0]['json']
                self.assertEqual((out['stage'], out['errorKind']), (stage, kind), out)

    def prepared(self, iid):
        self.observe(monday_item(iid, fmt='Post', code='LIP4', name=f'Item{iid} LIP4'))
        self.select(iid)
        self.topaz(iid)
        self.assertEqual(self.select(iid).get('next'), 'prepare')
        mid = self.add_media(iid, 'Post', url=False)
        self.ops.submit(owner_cmd(self.rid('cap'), 'update_caption', iid, text='Sunlight. 🔥\n\n#reels'))
        with self.ops.store.tx() as c:
            c.execute("UPDATE media SET metadata=json_set(metadata,'$.dropboxHash','dh-' || id) WHERE id=?", (mid,))
        return mid

    def wf1_visit(self, iid, mid):
        self.select(iid)
        return self.wf1('prep_media', iid, result={'ready': True, 'mediaId': mid})

    def test_failed_uploads_are_bounded_visible_and_escalated_once(self):
        mid = self.prepared('400')
        uploads = 0
        for _ in range(24):                                            # four hours of WF1 runs
            if any(w['item_id'] == '400' for w in self.ops.work_queue(limit=20)):
                r = self.wf1_visit('400', mid)
                if r.get('next') == 'upload':
                    uploads += 1
                    self.assertEqual(r['deliveryPath'], f'/Social Media/Prepared/400-{mid}.mp4')
                    f = self.wf1('prep_delivered', '400', mediaId=mid, stage='upload', error_kind='infra',
                                 error='Temporary Dropbox problem (503): Service Unavailable')
                    self.assertEqual(f['state'], 'completed', f)
                    it = self.item('400')
                    self.assertIn('upload', (it['infra_issue'] or it['block_reason'] or ''))
            self.clock.advance(600)
        self.assertEqual(uploads, rules_max_delivery())
        it = self.item('400')
        self.assertEqual((it['readiness'], it['block_kind']), ('blocked', 'review'), it)
        notes = [t for t in slack(self) if 'could not be delivered' in t]
        self.assertEqual(len(notes), 1, slack(self))
        # Explicit recheck gives one more bounded attempt.
        self.ops.submit(owner_cmd(self.rid(), 'request_recheck', '400'))
        self.assertEqual(self.wf1_visit('400', mid).get('next'), 'upload')

    def test_permanent_destination_error_escalates_at_once(self):
        mid = self.prepared('401')
        self.assertEqual(self.wf1_visit('401', mid).get('next'), 'upload')
        self.wf1('prep_delivered', '401', mediaId=mid, stage='folder', error_kind='config',
                 error='Dropbox is full (Dropbox: path/insufficient_space)')
        it = self.item('401')
        self.assertEqual((it['readiness'], it['block_kind']), ('blocked', 'review'))
        self.assertIn('insufficient_space', it['block_reason'])

    def test_confirmed_upload_is_not_repeated_when_sharing_fails(self):
        mid = self.prepared('402')
        r = self.wf1_visit('402', mid)
        self.assertEqual(r.get('next'), 'upload')
        up = {'id': 'id:DLV402', 'rev': 'd1', 'content_hash': 'dh-' + mid, 'path_lower': r['deliveryPath'].lower(),
              'size': 1}
        rec = self.wf1('prep_delivered', '402', mediaId=mid, stage='uploaded', upload=up)
        self.assertEqual(rec.get('next'), 'share', rec)
        self.wf1('prep_delivered', '402', mediaId=mid, stage='share', error_kind='infra', error='socket hang up')
        self.clock.advance(3600)
        r = self.wf1_visit('402', mid)
        self.assertEqual(r.get('next'), 'share', r)                    # resume after the confirmed upload
        self.assertEqual(r['deliveryPath'], up['path_lower'])
        done = self.wf1('prep_delivered', '402', mediaId=mid, stage='shared',
                        url='https://www.dropbox.com/scl/fi/d/402.mp4?rlkey=k&raw=1')
        self.assertEqual(done.get('next'), 'done', done)
        self.assertIsNotNone(self.res('402'))
        self.assertIsNone(self.item('402')['infra_issue'])


def rules_max_delivery():
    from waset_ops import items
    return items.MAX_DELIVERY_ATTEMPTS


# ============================================================================ M10
class R5_M10_PermanentDropboxErrors(OpsCase):
    """M10: permanent selection failures raised by WF1's own Code nodes (no HTTP status) were classified 'infra'
    and retried every 20 minutes for ever."""

    @unittest.skipUnless(NODE, 'node not installed')
    def test_code_node_failures_are_classified(self):
        prog = js(WF1, 'Dropbox Problem')
        with self.assertRaises(AssertionError) as e:
            run_js(js(WF1, 'Plan Listing'), {'.tag': 'folder', 'name': 'Calli 3'},
                   refs={'Folder Context': {'itemId': '1', 'name': 'Calli 3', 'folderUrl': FOLDER}})
        thrown = re.search(r'Error: (\[config\][^\n]*)', str(e.exception))
        self.assertIsNotNone(thrown, str(e.exception))
        permanent = [thrown.group(1), 'path/not_found/..', 'path/no_write_permission/',
                     'path/malformed_path/.', '[config] The Dropbox link selected for this item is not a file']
        for msg in permanent:
            with self.subTest(msg=msg):
                out = run_js(prog, {'error': msg})[0]['json']
                self.assertEqual(out['errorKind'], 'config', out)
                self.assertNotIn('[config]', out['error'])
        for msg in ('socket hang up', 'other/..', 'list_folder failed for "Calli 403": other/...',
                    'Could not create final video link', 'too_many_write_operations/'):
            with self.subTest(msg=msg):
                self.assertEqual(run_js(prog, {'error': msg})[0]['json']['errorKind'], 'infra')

    def visits(self, iid, hours, err=None, kind=None):
        n = 0
        for _ in range(hours * 6):
            if any(w['item_id'] == iid for w in self.ops.work_queue(limit=20)):
                n += 1
                if err:
                    self.wf1('prep_source', iid, error=err, error_kind=kind)
                else:
                    self.select(iid)
            self.clock.advance(600)
        return n

    def test_permanent_error_waits_for_changed_evidence(self):
        self.observe(monday_item('700', fmt='Post'))
        err = 'The Dropbox link on the board is not in the connected Dropbox account (Dropbox: no path)'
        self.wf1('prep_source', '700', error=err, error_kind='config')
        self.assertEqual(self.item('700')['block_kind'], 'config')
        self.assertEqual(self.visits('700', 24, err, 'config'), 0)      # no timer-driven retries
        self.ops.submit(owner_cmd(self.rid(), 'replace_source', '700', url=NEW))
        self.assertTrue(any(w['item_id'] == '700' for w in self.ops.work_queue(limit=20)))
        sel = {'mode': 'owner_selected_file', 'url': NEW}               # the new link has the same problem
        self.wf1('prep_source', '700', error=err, error_kind='config', selection=sel)
        self.assertEqual(self.visits('700', 6, err, 'config'), 0)       # evidence consumed: no per-cycle loop

    def test_board_code_fix_is_changed_evidence(self):
        self.observe(monday_item('701', fmt='Post', code=''))
        self.wf1('prep_source', '701', error='Code is missing', error_kind='config')
        self.assertEqual(self.visits('701', 6, 'Code is missing', 'config'), 0)
        with self.ops.store.tx() as c:                                    # the owner typed the Code on the board
            c.execute("UPDATE ops_items SET code='LIP7' WHERE item_id='701'")
        self.assertTrue(any(w['item_id'] == '701' for w in self.ops.work_queue(limit=20)))

    def test_temporary_errors_back_off_and_escalate_once(self):
        self.observe(monday_item('702', fmt='Post'))
        n = self.visits('702', 24, 'Temporary Dropbox problem (503): x', 'infra')
        self.assertLessEqual(n, 10)
        notes = [t for t in slack(self) if 'keeps failing' in t]
        self.assertEqual(len(notes), 1, slack(self))
        self.select('702')                                                # recovered
        self.assertIsNone(self.item('702')['infra_issue'])
        with self.ops.store.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM ops_findings WHERE kind='prep_infra' AND resolved IS NULL")
                             .fetchone()[0], 0)


# ============================================================================ M13
class R5_M13_BlockedBackoffGrows(OpsCase):
    """M13: Topaz/duration/media blocks were re-listed in Dropbox every 10 minutes (143 listings/day per item)."""

    def day(self, iid, visit):
        n = 0
        for _ in range(24 * 6):
            if any(w['item_id'] == iid for w in self.ops.work_queue(limit=20)):
                n += 1
                visit()
            self.clock.advance(600)
        return n

    def test_unchanged_blocks_back_off(self):
        self.observe(monday_item('800', fmt='Post'))
        self.select('800')                                               # waiting for Topaz
        self.assertLessEqual(self.day('800', lambda: self.select('800')), 30)
        self.observe(monday_item('801', fmt='Story'))
        self.select('801')
        self.duration('801', 70.0)                                       # needs a shorter edit
        self.assertLessEqual(self.day('801', lambda: self.select('801')), 30)
        self.observe(monday_item('802', fmt='Post'))
        self.select('802')
        self.topaz('802')
        self.select('802')
        self.wf1('prep_media', '802', result={'ready': False, 'reason': 'Source short edge is below 1080 pixels',
                                              'failureClass': 'verdict'})
        self.assertLessEqual(self.day('802', lambda: self.select('802')), 30)

    def test_evidence_changes_still_act_immediately(self):
        self.observe(monday_item('803', fmt='Post'))
        self.select('803')
        self.day('803', lambda: self.select('803'))
        self.topaz('803')                                                # owner Topaz
        self.assertTrue(any(w['item_id'] == '803' for w in self.ops.work_queue(limit=20)))
        self.assertEqual(self.select('803').get('next'), 'prepare')
        self.observe(monday_item('804', fmt='Post'))
        self.select('804')
        self.day('804', lambda: self.select('804'))
        self.ops.submit(owner_cmd(self.rid(), 'request_recheck', '804'))
        self.assertTrue(any(w['item_id'] == '804' for w in self.ops.work_queue(limit=20)))


# ============================================================================ M16
class R5_M16_OrphanReaper(MediaCase):
    """M16: a media job killed mid-encode left its source (up to 5 GB), pass logs and partial output for ever."""

    def files(self, mid, folder='media', names=('.input', '-0.log', '-0.log.mbtree', '.mp4')):
        d = self.dir / folder
        d.mkdir(exist_ok=True)
        out = []
        for s in names:
            p = d / (mid + s)
            p.write_bytes(b'x' * 10)
            os.utime(p, (time.time() - 7200, time.time() - 7200))
            out.append(p)
        return out

    def test_reaper_removes_only_dead_unreferenced_job_files(self):
        dead, live, pre = 'a' * 64, 'b' * 64, 'c' * 64
        dead_files = self.files(dead)
        live_files = self.files(live)
        pre_files = self.files(pre, 'preflight', ('.input',))
        (self.dir / (dead + '.lock')).touch()
        (self.dir / ('preflight-' + pre + '.lock')).touch()
        self.make_ready('900')                                            # a verified, reserved file stays
        with self.ops.store.tx() as c:
            vid = self.item('900')['verification_id']
            c.execute('UPDATE media SET path=? WHERE id=?', (str(self.dir / 'media' / (vid + '.mp4')), vid))
        kept = self.files(vid, names=('.mp4',))[0]
        lock = self.dir / (live + '.lock')
        holder = subprocess.Popen([sys.executable, '-c', 'import fcntl,sys,time\nf=open(sys.argv[1],"a")\n'
                                   'fcntl.flock(f,fcntl.LOCK_EX)\nprint("held",flush=True)\ntime.sleep(120)', str(lock)],
                                  stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: holder.poll() is None and holder.kill())
        self.assertEqual(holder.stdout.readline().strip(), 'held')
        out = media.maintenance()
        self.assertEqual(out['removedOrphanFiles'], len(dead_files) + len(pre_files))
        self.assertFalse(any(p.exists() for p in dead_files + pre_files))
        self.assertTrue(all(p.exists() for p in live_files))             # worker alive: untouched
        self.assertTrue(kept.exists())
        holder.kill()                                                      # SIGKILL: the worker is now proven dead
        holder.wait()
        media.maintenance()
        self.assertFalse(any(p.exists() for p in live_files))
        self.assertTrue(kept.exists())

    def test_killed_job_is_retried_within_the_budget_not_reported_as_a_verdict(self):
        b = {'itemId': '901', 'fileId': 'id:F', 'revision': 'r', 'contentHash': 'h', 'format': 'Post', 'assetKey': 'id:F@r'}
        mid = media.media_key(b)
        with media.store().tx() as c:                                     # marker of a job that was SIGKILLed
            c.execute('INSERT INTO jobs VALUES(?,?)', (mid, dumps({'running': True, 'kind': 'prepare', 'attempts': 2})))
        r = media.prepare(b)
        self.assertTrue(r.get('pending'), r)
        self.assertEqual(self.queued[-1][3], 3)                           # counted as a failed attempt
        with media.store().tx() as c:
            c.execute('UPDATE jobs SET result=? WHERE id=?',
                      (dumps({'running': True, 'kind': 'prepare', 'attempts': media.MAX_TRANSIENT_ATTEMPTS}), mid))
        r = media.prepare(b)
        self.assertEqual(r.get('failureClass'), 'exhausted', r)
        self.assertIn('stopped before finishing', r['reason'])


# ============================================================================ M17
class R5_M17_DeliveredIdentityBound(OpsCase):
    """M17 (REASONED in R5, reproduced here): the delivered Dropbox copy that Instagram fetches was never bound or
    rechecked: the upload response was discarded and WF2 checked only the source revision."""

    def delivered(self, iid):
        self.observe(monday_item(iid, fmt='Story', name=f'Item{iid} LIP12'))
        self.select(iid)
        self.duration(iid, 30.0)
        self.topaz(iid)
        self.assertEqual(self.select(iid).get('next'), 'prepare')
        mid = self.add_media(iid, 'Story', url=False)
        with self.ops.store.tx() as c:
            c.execute("UPDATE media SET metadata=json_set(metadata,'$.dropboxHash','dh') WHERE id=?", (mid,))
        r = self.wf1('prep_media', iid, result={'ready': True, 'mediaId': mid})
        up = {'id': 'id:DLV', 'rev': 'd1', 'content_hash': 'dh', 'path_lower': r['deliveryPath'].lower(), 'size': 1}
        self.wf1('prep_delivered', iid, mediaId=mid, stage='uploaded', upload=up)
        self.wf1('prep_delivered', iid, mediaId=mid, stage='shared', url='https://www.dropbox.com/scl/fi/d/x.mp4?raw=1')
        self.assertIsNotNone(self.res(iid))
        return mid

    def at_commit(self, iid):
        self.clock.set(rules.instant(self.res(iid)['slot']) + timedelta(seconds=5))
        cl = self.ops.claim(iid, 'w')
        self.assertTrue(cl['claimed'], cl)
        self.ops.container(cl['attempt_id'], 'w', cl['fence'], 'C-' + iid)
        return cl

    def test_upload_identity_is_recorded(self):
        mid = self.delivered('1000')
        with self.ops.store.read() as c:
            info = loads(self.ops.media_row(c, mid)['metadata'])
        self.assertEqual({k: info['delivered'][k] for k in ('id', 'rev', 'content_hash')},
                         {'id': 'id:DLV', 'rev': 'd1', 'content_hash': 'dh'})

    def test_replaced_delivered_copy_blocks_publication(self):
        mid = self.delivered('1001')
        cl = self.at_commit('1001')
        r = self.ops.commit(cl['attempt_id'], 'w', cl['fence'], source_asset='id:FILE1@rev1',
                            delivered_asset='id:DLV@d2', container_status='FINISHED')
        self.assertFalse(r['committed'])
        self.assertTrue(any('Delivered Dropbox copy changed' in x for x in r['reasons']), r)
        it = self.item('1001')
        self.assertIsNone(it['verification_id'])
        self.assertIsNone(self.res('1001'))
        self.assertTrue(any('delivered copy' in t.lower() for t in slack(self)))
        # The local cache is intact, but the next preparation delivers again instead of trusting the old link.
        self.select('1001')
        self.assertEqual(self.wf1('prep_media', '1001', result={'ready': True, 'mediaId': mid}).get('next'), 'upload')

    def test_unreadable_delivered_copy_refuses_this_commit_only(self):
        self.delivered('1002')
        cl = self.at_commit('1002')
        r = self.ops.commit(cl['attempt_id'], 'w', cl['fence'], source_asset='id:FILE1@rev1',
                            delivered_asset='unverifiable', container_status='FINISHED')
        self.assertFalse(r['committed'])
        self.assertTrue(any('could not be checked' in x for x in r['reasons']), r)
        self.assertIsNotNone(self.item('1002')['verification_id'])

    def test_matching_delivered_copy_commits(self):
        self.delivered('1003')
        cl = self.at_commit('1003')
        r = self.ops.commit(cl['attempt_id'], 'w', cl['fence'], source_asset='id:FILE1@rev1',
                            delivered_asset='id:DLV@d1', container_status='FINISHED')
        self.assertTrue(r['committed'], r)

    def test_wf2_checks_the_delivered_copy_before_commit(self):
        self.assertEqual(targets(WF2, 'Verify Source Revision', 0), ['Source Revision Result'])
        self.assertEqual(targets(WF2, 'Verify Source Revision', 1), ['Source Revision Result'])
        self.assertEqual(targets(WF2, 'Source Revision Result'), ['Verify Delivered Copy'])
        v = node(WF2, 'Verify Delivered Copy')['parameters']
        self.assertIn('get_shared_link_metadata', v['url'])
        self.assertIn('video_url', v['jsonBody'])
        self.assertEqual(targets(WF2, 'Verify Delivered Copy', 0), ['Delivered Revision Result'])
        self.assertEqual(targets(WF2, 'Verify Delivered Copy', 1), ['Delivered Revision Result'])
        self.assertEqual(targets(WF2, 'Delivered Revision Result'), ['Commit Publication — Input'])
        self.assertIn('deliveredAsset', js(WF2, 'Commit Publication — Input'))
        if NODE:
            refs = {'Source Revision Result': {'sourceAsset': 'id:S@1'}}
            ok = run_js(js(WF2, 'Delivered Revision Result'), {'id': 'id:DLV', 'rev': 'd1'}, refs=refs)[0]['json']
            self.assertEqual((ok['sourceAsset'], ok['deliveredAsset']), ('id:S@1', 'id:DLV@d1'))
            bad = run_js(js(WF2, 'Delivered Revision Result'), {'error': {'message': 'x'}}, refs=refs)[0]['json']
            self.assertEqual(bad['deliveredAsset'], 'unverifiable')

    def test_legacy_ready_item_gets_its_delivered_copy_identified_once(self):
        self.make_ready('1004')                         # verified before identities were recorded (url only)
        with self.ops.store.read() as c:
            mid = self.item('1004')['verification_id']
            path = self.ops.media_row(c, mid)['path']
        r = self.select('1004')
        self.assertEqual(r.get('next'), 'prepare', r)
        r = self.wf1('prep_media', '1004', result={'ready': True, 'mediaId': mid})
        self.assertEqual(r.get('next'), 'identify', r)
        meta = {'id': 'id:OLD', 'rev': 'o1', 'content_hash': media.dropbox_hash(path), 'path_lower': r['deliveryPath'].lower()}
        rec = self.wf1('prep_delivered', '1004', mediaId=mid, stage='identified', upload=meta,
                       local_hash=media.dropbox_hash(path))
        self.assertEqual(rec.get('next'), 'done', rec)                  # the existing link is kept (same payload)
        self.assertIsNotNone(self.res('1004'))                           # stays ready and scheduled meanwhile
        with self.ops.store.read() as c:
            self.assertEqual(loads(self.ops.media_row(c, mid)['metadata'])['delivered']['rev'], 'o1')


# ============================================================================ LOW-06
class R5_LOW06_MissingCodeNoNullFolder(OpsCase):
    """R5-LOW-06: items without a Code shared /Social Media/Production/null/<format>."""

    @unittest.skipUnless(NODE, 'node not installed')
    def test_no_folder_path_without_a_code(self):
        cfg = {'Configuration': {'folderRoot': '/Social Media/Production'}}
        for code in (None, '', '   ', 'x'):
            with self.subTest(code=code):
                with self.assertRaises(AssertionError) as e:
                    run_js(js(WF1, 'New Folder Context'), {}, refs={**cfg, 'Item Context': {'code': code, 'format': 'Story'}})
                self.assertIn('[config]', str(e.exception))
        ok = run_js(js(WF1, 'New Folder Context'), {}, refs={**cfg, 'Item Context': {'code': 'LIP12', 'format': 'Story'}})
        self.assertEqual(ok[0]['json']['folderPath'], '/Social Media/Production/LIP12/Story')
        self.assertEqual(targets(WF1, 'Has Folder?', 1), ['Folder Creatable?'])
        self.assertEqual(targets(WF1, 'Folder Creatable?', 0), ['New Folder Context'])
        self.assertEqual(targets(WF1, 'Folder Creatable?', 1), ['Missing Source Identity'])
        self.assertEqual(targets(WF1, 'Missing Source Identity'), ['Preparation Step — Input'])
        out = run_js(js(WF1, 'Missing Source Identity'), {'code': None})[0]['json']
        self.assertEqual((out['ok'], out['errorKind']), (False, 'config'))

    def test_queue_flags_missing_code_and_handler_refuses_a_null_folder(self):
        self.observe(monday_item('1100', code=''), monday_item('1101', code=''))
        q = {w['item_id']: w for w in self.ops.work_queue(limit=20)}
        self.assertFalse(q['1100']['code_valid'])
        self.wf1('prep_source', '1100', error='Code is missing', error_kind='config',
                 folder_url='https://www.dropbox.com/scl/fo/n/null?rlkey=z')
        self.assertIsNone(self.item('1100')['folder_url'])


# ============================================================================ LOW-15
class R5_LOW15_PlatformRequirements(unittest.TestCase):
    """R5-LOW-15: prepared files were not normalised (120 fps, 96 kHz 5.1 and a 2 s Story passed the gate); >20
    @mentions were not checked. Limits verified 2026-10-10 against Meta's IG User Media reference (Reels and Story
    video specifications; caption parameter)."""

    BASE = {'width': 1080, 'height': 1920, 'duration': 20.0, 'bytes': 30_000_000, 'fps': 30.0, 'audioCodec': 'aac',
            'audioRate': 48000, 'audioChannels': 2}

    def test_platform_gate(self):
        self.assertIsNone(media.platform_failure(self.BASE, 'Post'))
        self.assertIsNone(media.platform_failure({**self.BASE, 'audioCodec': None, 'audioRate': None,
                                                  'audioChannels': None}, 'Story'))          # silent video is fine
        bad = {'fps': ({'fps': 120.0}, 'frame rate'), 'rate': ({'audioRate': 96000}, '48 kHz'),
               'channels': ({'audioChannels': 6}, 'channels'), 'short': ({'duration': 2.0}, '3-second'),
               'wide': ({'width': 2582, 'height': 1080}, '1920'), 'story_size': ({'bytes': 100_500_000}, '100 MB'),
               'reel_long': ({'duration': 901.0}, '15-minute')}
        for name, (patch, words) in bad.items():
            fmt = 'Story' if name == 'story_size' else 'Post'
            with self.subTest(name):
                self.assertIn(words.lower(), (media.platform_failure({**self.BASE, **patch}, fmt) or '').lower())

    def test_caption_mentions(self):
        twenty = ' '.join(f'@user{i}' for i in range(20))
        self.assertIsNone(rules.validate_caption('Hi ' + twenty + ' mail info@waset.example'))
        self.assertIn('20 @', rules.validate_caption('Hi ' + twenty + ' @one_more') or '')


@unittest.skipUnless(FFMPEG, 'FFmpeg not installed')
class R5_LOW15_PreparedFileIsNormalised(MediaCase):
    def body(self, path, fmt, iid):
        return {'itemId': iid, 'format': fmt, 'sourceUrl': str(path), 'fileId': 'id:' + iid, 'revision': 'r1',
                'contentHash': media.dropbox_hash(path), 'assetKey': f'id:{iid}@r1'}

    def streams(self, path):
        d = json.loads(subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(path)],
                                      capture_output=True, text=True, check=True).stdout)
        return {s['codec_type']: s for s in d['streams']}

    def test_high_frame_rate_multichannel_is_normalised(self):
        src = video(self.src / 'hfr.mp4', rate=120, secs=4, audio_src='sine=frequency=440:sample_rate=96000', ch=6)
        r = media.job_prepare(self.body(src, 'Post', 'h1'))
        self.assertTrue(r['ready'], r)
        st = self.streams(r['filePath'])
        num, den = map(int, st['video']['avg_frame_rate'].split('/'))
        self.assertTrue(23 <= num / den <= 60, st['video']['avg_frame_rate'])
        self.assertLessEqual(int(st['audio']['sample_rate']), 48000)
        self.assertLessEqual(int(st['audio']['channels']), 2)
        self.assertEqual(r['cacheKey'], 'media/' + r['mediaId'] + '.mp4')
        self.assertEqual(r['dropboxHash'], media.dropbox_hash(Path(r['filePath'])))
        self.assertIsNone(media.platform_failure(r, 'Post'))

    def test_too_short_story_and_too_wide_video_are_rejected_explicitly(self):
        short = video(self.src / 'short.mp4', secs=2)
        pre = media.job_preflight(self.body(short, 'Story', 's1'))
        self.assertFalse(pre['ready'])
        self.assertIn('3-second', pre['reason'])
        wide = video(self.src / 'scope.mp4', size='2592x1080', secs=4)
        r = media.job_prepare(self.body(wide, 'Post', 'w1'))
        self.assertFalse(r['ready'])
        self.assertIn('1920', r['reason'])
        self.assertEqual(list((self.dir / 'media').glob('*.mp4')), [])


if __name__ == '__main__':
    unittest.main()
