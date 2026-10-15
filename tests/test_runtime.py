"""Bondok runtime (agent migration P2/P4/P5): the scheduled work without n8n, end to end on fakes.

Acceptance ids from bondok-agent-migration/06-acceptance.md. The handler is real; Monday, Dropbox, Instagram and the
media worker are in-memory fakes (tests/runtime_fakes.py). No network, no model.
"""
import json
import threading
import unittest
from datetime import timedelta
from unittest import mock

from runtime_fakes import FakeDropbox, FakeInstagram, FakeMediaWorker, FakeMonday, source_item
from support import OpsCase, owner_cmd, rules
from waset_ops import board as B
from waset_ops import media

from waset_runtime.cycles import Runner, guard_conflict
from waset_runtime.monday import MondayError
from waset_runtime.service import Scheduler

FOLDER = '/Social Media/Production/LIP12/Story'


class RuntimeCase(OpsCase):
    def setUp(self):
        super().setUp()
        self.ops.run_finish('wf1', self.run_id, fence=self.fence)       # the fixture's run lease is not ours
        self.dbx = FakeDropbox()
        self.url = self.dbx.add_folder(FOLDER)
        self.dbx.add_file(FOLDER + '/Calli 3 v1.mp4', content='srchash1')
        self.monday = FakeMonday(sources=[source_item('5001', link=self.url, name='Calli 3')])
        self.ig = FakeInstagram()
        self.worker = FakeMediaWorker(self.ops, self.dir)
        for name in ('preflight', 'prepare'):
            p = mock.patch.object(media, name, getattr(self.worker, name))
            p.start()
            self.addCleanup(p.stop)
        self.runner = Runner(self.ops, self.monday, self.dbx, self.ig, name='t', sleep=lambda s: None,
                             maintenance=lambda: {})

    def cycle(self, n=1):
        for _ in range(n):
            self.runner.prepare_cycle()
            self.clock.advance(700)

    def social_id(self):
        ids = list(self.monday.board.items)
        self.assertEqual(len(ids), 1)
        return ids[0]

    def ready_item(self):
        """Import -> selection -> Topaz confirmed by the owner -> prepared, delivered, scheduled."""
        self.cycle(2)                       # import; the next cycle observes the new item and selects its file
        iid = self.social_id()
        self.assertTrue(self.item(iid)['block_key'].startswith('topaz:'), self.item(iid))
        self.ops.submit(owner_cmd('tz', 'confirm_topaz', iid, explicit=True, confirmed=True))
        self.runner.prepare_cycle()
        self.assertEqual(self.item(iid)['readiness'], 'ready', self.item(iid))
        self.assertIsNotNone(self.res(iid))
        return iid

    def at_slot(self, iid, seconds=10):
        self.clock.set(rules.instant(self.res(iid)['slot']) + timedelta(seconds=seconds))


class P4_02_ImportSelectPrepareDeliver(RuntimeCase):
    def test_wf1_cycle_end_to_end(self):
        iid = self.ready_item()
        it = self.item(iid)
        self.assertEqual(it['file_name'], 'Calli 3 v1.mp4')
        self.assertEqual(self.dbx.uploads, [f'/Social Media/Prepared/{iid}-{it["verification_id"]}.mp4'])
        self.runner.publish_cycle()                                   # display sync
        self.assertEqual(self.monday.text(iid, 'status'), 'Scheduled')
        with self.ops.store.read() as c:
            src = c.execute('SELECT * FROM ops_source_map').fetchall()
        self.assertEqual([(r['source_item_id'], r['social_item_id']) for r in src], [('5001', iid)])

    def test_second_cycle_does_not_recreate_items(self):
        self.runner.prepare_cycle()
        self.clock.advance(700)
        self.runner.prepare_cycle()
        self.assertEqual(len(self.monday.board.items), 1)

    def test_run_lease_excludes_a_concurrent_n8n_cycle(self):
        lease = self.ops.run_start('wf1', 'wf1-n8n-exec')
        self.assertTrue(lease['acquired'])
        self.assertTrue(self.runner.prepare_cycle().get('deferred'))
        self.assertEqual(self.monday.board.items, {})

    def test_monday_read_failure_fails_the_run_visibly(self):
        self.monday.fail['social_snapshot'] = MondayError('Monday HTTP 503', answered=True, status=503)
        with self.assertRaises(MondayError):
            self.runner.prepare_cycle()
        with self.ops.store.read() as c:
            rec = self.ops.run_records(c, 'wf1')
        self.assertEqual(rec['failed']['step'], 'snapshot')

    def test_missing_folder_link_is_a_config_problem(self):
        self.dbx.links.clear()
        self.cycle(2)
        it = self.item(self.social_id())
        self.assertEqual((it['readiness'], it['block_kind']), ('blocked', 'config'))


class A02_ExemptionThroughTheRuntime(RuntimeCase):
    def test_short_edge_exemption_unblocks_a_720p_source(self):
        self.worker.width, self.worker.height = 720, 1280
        self.cycle(2)
        iid = self.social_id()
        self.ops.submit(owner_cmd('tz', 'confirm_topaz', iid, explicit=True, confirmed=True))
        self.runner.prepare_cycle()
        it = self.item(iid)
        self.assertEqual(it['readiness'], 'blocked')
        self.assertIn('short edge', it['block_reason'])
        self.ops.submit(owner_cmd('w', 'waive_rule', iid, explicit=True, rule='min_short_edge'))
        self.clock.advance(60)
        self.runner.prepare_cycle()
        self.assertEqual(self.item(iid)['readiness'], 'ready', self.item(iid))
        self.assertIsNotNone(self.res(iid))


class A13_A24_PublishWithoutModelOrChat(RuntimeCase):
    def test_scheduled_item_publishes_once_and_the_intent_is_in_the_store(self):
        iid = self.ready_item()
        self.runner.publish_cycle()
        self.at_slot(iid)
        out = self.runner.publish_cycle()
        self.assertEqual(out['published'][0]['stage'], 'published', out)
        self.assertEqual(len(self.ig.publish_calls()), 1)
        with self.ops.store.read() as c:
            ev = json.loads(c.execute("SELECT evidence FROM ops_attempts WHERE item_id=?", (iid,)).fetchone()[0])
        self.assertTrue(ev['intent']['store'])                       # recorded before media_publish
        self.assertEqual(ev['intent']['asset'], self.item(iid)['asset_key'])
        self.assertEqual(self.item(iid)['publication'], 'published')
        self.clock.advance(60)
        self.runner.publish_cycle()
        self.assertEqual(self.monday.text(iid, 'status'), 'Posted')
        self.assertIn('instagram.com', self.monday.text(iid, 'post_link'))
        self.assertEqual(self.monday.source_status('5001'), 'Posted')
        for _ in range(3):
            self.clock.advance(60)
            self.runner.publish_cycle()
        self.assertEqual(len(self.ig.publish_calls()), 1)

    def test_story_payload_has_no_caption(self):
        iid = self.ready_item()
        self.at_slot(iid)
        self.runner.publish_cycle()
        self.assertEqual(list(self.ig.containers.values())[0]['media_type'], 'STORIES')


class A15_TwoRunnersOneAttempt(RuntimeCase):
    def test_parallel_publish_cycles_send_one_request(self):
        iid = self.ready_item()
        self.runner.publish_cycle()
        self.at_slot(iid)
        other = Runner(self.ops, self.monday, self.dbx, self.ig, name='u', sleep=lambda s: None)
        barrier = threading.Barrier(2)

        def run(r):
            barrier.wait()
            r.publish_cycle()
        ts = [threading.Thread(target=run, args=(r,)) for r in (self.runner, other)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(len(self.ig.publish_calls()), 1)
        self.assertEqual(len([c for c in self.ig.calls if c[0] == 'create']), 1)


class A16_A17_PauseAndCommitmentPoint(RuntimeCase):
    def test_pause_before_commit_publishes_nothing(self):
        iid = self.ready_item()
        self.at_slot(iid)
        real = self.ig.container_status

        def paused_meanwhile(cid):
            self.ops.submit(owner_cmd('p1', 'pause', iid, reason='owner'))
            return real(cid)
        self.ig.container_status = paused_meanwhile
        self.runner.publish_cycle()
        self.assertEqual(self.ig.publish_calls(), [])
        self.assertEqual(self.item(iid)['publication'], 'not_started')

    def test_pause_typed_on_the_board_before_the_intent_write_wins(self):
        iid = self.ready_item()
        self.runner.publish_cycle()
        self.at_slot(iid)
        real = self.ig.container_status

        def owner_types_paused(cid):
            self.monday.board.set_raw(iid, 'status', {'id': 'status', 'text': 'Paused', 'value': '{"index":2}'})
            return real(cid)
        self.ig.container_status = owner_types_paused
        self.runner.publish_cycle()
        self.assertEqual(self.ig.publish_calls(), [])
        self.assertEqual(self.monday.text(iid, 'status'), 'Paused')

    def test_pause_after_commit_says_it_may_already_be_in_progress(self):
        iid = self.ready_item()
        self.at_slot(iid)
        seen = {}
        real = self.ig.publish

        def pause_then_publish(cid, timeout=120):
            seen['pause'] = self.ops.submit(owner_cmd('p2', 'pause', iid, reason='owner'))
            return real(cid)
        self.ig.publish = pause_then_publish
        self.runner.publish_cycle()
        self.assertEqual(seen['pause'].get('publication'), 'may_already_be_in_progress', seen)
        self.assertEqual(len(self.ig.publish_calls()), 1)


class A18_A19_A20_UnknownOutcomes(RuntimeCase):
    def test_lost_answer_is_reconciled_never_republished(self):
        iid = self.ready_item()
        self.at_slot(iid)
        self.ig.publish_mode = 'lost'
        self.runner.publish_cycle()
        self.assertEqual(self.item(iid)['publication'], 'outcome_unknown')
        self.assertNotEqual(self.monday.text(iid, 'status'), 'Posted')        # A20: no "Posted" without evidence
        for _ in range(4):
            self.clock.advance(400)
            self.runner.publish_cycle()
        self.assertEqual(self.item(iid)['publication'], 'published')          # container status PUBLISHED
        self.assertEqual(len(self.ig.publish_calls()), 1)

    def test_process_dies_after_commit(self):
        iid = self.ready_item()
        self.at_slot(iid)

        def crash(cid, timeout=120):
            self.ig.published.append(cid)
            raise SystemExit('killed')                    # the process stops between the request and the record
        self.ig.publish = crash
        with self.assertRaises(SystemExit):
            self.runner.publish_one({'item_id': iid}, 'dying-worker')
        self.assertEqual(self.ops.active_attempt(self.ops.store.connect(), iid)['stage'], 'committed')
        self.ig.publish = lambda cid, timeout=120: self.fail('must not publish again')
        self.clock.advance(1000)                          # commit lease (900 s) ends -> outcome unknown
        self.runner.publish_cycle()
        self.clock.advance(400)
        self.runner.publish_cycle()
        self.assertEqual(self.item(iid)['publication'], 'published')

    def test_server_error_is_unknown_not_failed(self):
        iid = self.ready_item()
        self.at_slot(iid)
        self.ig.publish_mode = 'server_error'
        self.runner.publish_cycle()
        self.assertEqual(self.item(iid)['publication'], 'outcome_unknown')


class A21_ProviderFailures(RuntimeCase):
    def test_definitive_refusal_is_recorded_and_nothing_retried_blindly(self):
        iid = self.ready_item()
        self.at_slot(iid)
        self.ig.publish_mode = 'refused'
        self.runner.publish_cycle()
        self.assertNotEqual(self.item(iid)['publication'], 'published')
        self.assertEqual(len(self.ig.publish_calls()), 1)

    def test_container_error_abandons_before_commit(self):
        iid = self.ready_item()
        self.at_slot(iid)
        self.ig.statuses['C1'] = ['ERROR']
        self.runner.publish_cycle()
        self.assertEqual(self.ig.publish_calls(), [])
        with self.ops.store.read() as c:
            st = [r['stage'] for r in c.execute('SELECT stage FROM ops_attempts')]
        self.assertEqual(st, ['abandoned'])

    def test_fresh_read_failure_publishes_nothing_by_default(self):
        iid = self.ready_item()
        self.at_slot(iid)
        self.monday.fail['fresh_item'] = MondayError('no answer', answered=False)
        self.runner.publish_cycle()
        self.assertEqual(self.ig.publish_calls(), [])

    def test_owner_freshness_rule_decides_when_monday_cannot_be_read(self):
        iid = self.ready_item()
        self.at_slot(iid)
        self.ops.submit(owner_cmd('pol', 'set_policy', None, explicit=True, rule='board_freshness_minutes',
                                  value=7 * 24 * 60))
        self.monday.fail['fresh_item'] = [MondayError('no answer', answered=False)] * 2
        self.runner.publish_cycle()
        self.assertEqual(len(self.ig.publish_calls()), 1)
        self.assertTrue(any('last read' in json.loads(j['payload'])['text'] for j in self.outbox('slack')))

    def test_stale_board_is_not_published_under_the_default_rule(self):
        iid = self.ready_item()
        self.at_slot(iid)
        self.monday.fail['fresh_item'] = MondayError('no answer', answered=False)
        self.runner.publish_cycle()
        self.assertEqual(self.ig.publish_calls(), [])
        self.assertEqual(self.ops.board_fresh(iid)['limit_minutes'], 15)
        self.assertIsNotNone(self.ops.board_fresh(iid)['read_at'])     # what was last received is shown


class A22_DisplayFailureAfterPublish(RuntimeCase):
    def test_publication_stays_published_and_display_is_retried(self):
        iid = self.ready_item()
        self.at_slot(iid)
        self.runner.publish_cycle()
        self.monday.fail['change_columns'] = [MondayError('Monday HTTP 500', status=500)] * 3
        self.clock.advance(60)
        self.runner.publish_cycle()
        self.assertEqual(self.item(iid)['publication'], 'published')
        for _ in range(8):
            self.clock.advance(900)
            self.runner.publish_cycle()
        self.assertEqual(self.monday.text(iid, 'status'), 'Posted')
        self.assertEqual(len(self.ig.publish_calls()), 1)


class GuardPort(unittest.TestCase):
    def test_guard_matches_the_workflow_rules(self):
        g = {'status': {'kind': 'status', 'was': ['Scheduled', None], 'new': 'Publishing'}}
        self.assertEqual(guard_conflict(g, {'status': {'id': 'status', 'text': 'Scheduled'}}), (None, None))
        self.assertEqual(guard_conflict(g, {}), (None, None))                       # empty cell was expected
        self.assertEqual(guard_conflict(g, {'status': {'id': 'status', 'text': 'Publishing'}}), (None, None))
        self.assertIn('conflict', guard_conflict(g, {'status': {'id': 'status', 'text': 'Paused'}})[0])
        self.assertEqual(guard_conflict(g, None)[1], 'item not found on the board')
        lg = {'long_text_mm7ysrbz': {'kind': 'long', 'was': 'a', 'new': 'b'}}
        cur = {'long_text_mm7ysrbz': {'id': 'x', 'text': 'zzz', 'value': json.dumps({'text': ' a '})}}
        self.assertEqual(guard_conflict(lg, cur), (None, None))


class A29_A30_RuntimeOwnership(RuntimeCase):
    def test_scheduler_idle_until_owner_switches(self):
        self.assertEqual(self.ops.runtime_owner(), 'bondok')         # n8n is retired: Bondok's by default
        self.ops.submit(owner_cmd('back', 'set_runtime_owner', None, explicit=True, owner='n8n'))   # a rollback
        s = Scheduler(self.ops, self.runner, clock=self.clock)
        self.assertEqual(s.tick(), [])
        r = self.ops.submit(owner_cmd('own', 'set_runtime_owner', None, explicit=True, owner='bondok'))
        self.assertEqual(r['state'], 'completed', r)
        self.assertEqual(sorted(s.tick()), ['prepare', 'publish', 'supervise', 'sync'])
        for t in s.threads:
            t.join()
        self.assertEqual(len(self.monday.board.items), 1)       # the runtime imported the item

    def test_helper_starts_no_new_work_once_bondok_owns_it(self):
        import importlib
        import os
        import sys
        from support import ROOT
        sys.path.insert(0, str(ROOT / 'src'))
        os.environ['WASET_SOCIAL_DATA_DIR'] = str(self.dir)
        helper = importlib.import_module('helper')
        helper = importlib.reload(helper)
        self.ops.submit(owner_cmd('own', 'set_runtime_owner', None, explicit=True, owner='bondok'))
        with mock.patch.object(helper, 'ops', return_value=self.ops):
            self.assertEqual(helper.action('/v2/publish/due', {'worker': 'wf2-1'})['work'], [])
            self.assertFalse(helper.action('/v2/run/start', {'kind': 'wf1', 'runId': 'wf1-9'})['acquired'])
            self.assertEqual(helper.action('/v2/outbox/take', {'kinds': ['monday'], 'worker': 'w'})['jobs'], [])
            self.assertIn('heartbeat_age_seconds', helper.action('/v2/health', {}))        # reads still work
            self.assertEqual(helper.action('/v2/publish/due', {'worker': 'wf2-1'}).get('reason'),
                             'scheduled work runs on the Bondok runtime')

    def test_long_preparation_does_not_block_publication(self):
        iid = self.ready_item()
        self.ops.submit(owner_cmd('own', 'set_runtime_owner', None, explicit=True, owner='bondok'))
        self.at_slot(iid)
        gate = threading.Event()
        slow = Runner(self.ops, self.monday, self.dbx, self.ig, name='slow', sleep=lambda s: None,
                      maintenance=lambda: gate.wait(5) or {})
        s = Scheduler(self.ops, slow, clock=self.clock)
        s.last['supervise'] = self.clock()
        s.tick()                                              # prepare (blocked in maintenance) + publish start
        deadline = threading.Event()
        for _ in range(50):
            if self.ig.publish_calls():
                break
            deadline.wait(0.1)
        self.assertEqual(len(self.ig.publish_calls()), 1)     # published while preparation is still running
        self.assertTrue(s.running['prepare'])
        gate.set()
        s.shutdown()


class Selection(unittest.TestCase):
    def test_version_then_topaz_then_newer(self):
        from waset_runtime import selection as S
        plan = {'folderPath': '/p/lip12', 'parent': '/p', 'useParent': False, 'directPath': None}

        def f(name, modified='2026-10-01'):
            return {'.tag': 'file', 'name': name, 'path_lower': '/p/lip12/' + name.lower(), 'id': 'id:' + name,
                    'rev': 'r', 'content_hash': 'h', 'client_modified': modified}
        entries = [f('Lip12 v2.mp4'), f('Lip12 v2 topaz.mp4'), f('Lip12 v1.mp4', '2026-10-09'), f('Lip12 test.mp4'),
                   f('Lip12 v3 captions.mp4')]
        self.assertEqual(S.candidates('LIP12 x', plan, entries)['file']['name'], 'Lip12 v2 topaz.mp4')
        with self.assertRaises(S.SelectionProblem) as e:
            S.candidates('LIP13', plan, entries)
        self.assertEqual(e.exception.kind, 'config')
        with self.assertRaises(S.SelectionProblem) as e:
            S.candidates('LIP12', plan, [f('Lip12 draft.mp4')])
        self.assertEqual(e.exception.kind, 'editor')


if __name__ == '__main__':
    unittest.main()


class ReviewFindings(RuntimeCase):
    """Regression tests for the independent review of 2026-10-14."""

    def test_monday_writes_down_publication_goes_ahead_and_syncs_later(self):   # owner decision 2026-10-14
        iid = self.ready_item()
        self.runner.publish_cycle()
        self.at_slot(iid)
        self.monday.fail['change_columns'] = MondayError('Monday HTTP 503', status=503)   # every write fails
        self.runner.publish_cycle()
        self.assertEqual(len(self.ig.publish_calls()), 1)
        self.assertEqual(self.item(iid)['publication'], 'published')
        self.assertNotEqual(self.monday.text(iid, 'status'), 'Posted')
        for _ in range(3):
            self.clock.advance(1200)
            self.runner.publish_cycle()
        del self.monday.fail['change_columns']                             # Monday is back
        for _ in range(8):
            self.clock.advance(1800)
            self.runner.publish_cycle()
        self.assertEqual(self.monday.text(iid, 'status'), 'Posted')
        self.assertEqual(len(self.ig.publish_calls()), 1)

    def test_successful_fresh_read_counts_under_the_lenient_policy(self):  # finding 4
        iid = self.ready_item()
        self.runner.publish_cycle()
        self.ops.submit(owner_cmd('pol', 'set_policy', None, explicit=True, rule='board_freshness_minutes',
                                  value=7 * 24 * 60))
        self.at_slot(iid)
        self.monday.board.set_raw(iid, 'status', {'id': 'status', 'text': 'Paused', 'value': '{"index":2}'})
        self.monday.source_status = lambda sid: (_ for _ in ()).throw(MondayError('timeout', answered=False))
        self.runner.publish_cycle()
        self.assertEqual(self.ig.publish_calls(), [])
        self.assertEqual(self.item(iid)['owner_state'], 'paused')

    def test_deploy_fence_keeps_the_runtime_idle(self):                 # finding 3
        self.ops.submit(owner_cmd('own', 'set_runtime_owner', None, explicit=True, owner='bondok'))
        (self.dir / 'deploy-fence.json').write_text('{}')
        s = Scheduler(self.ops, self.runner, clock=self.clock)
        self.assertEqual(s.tick(), [])
        (self.dir / 'deploy-fence.json').unlink()
        self.assertTrue(s.tick())
        s.shutdown()

    def test_dropbox_credential_failure_is_not_an_item_problem(self):    # finding 8
        from waset_runtime.dropbox import DropboxError
        self.dbx.fail['link_metadata'] = DropboxError('Dropbox credential refresh failed (400)', kind='infra')
        self.cycle(2)
        it = self.item(self.social_id())
        self.assertNotEqual(it['block_kind'], 'config')

    def test_legacy_off_grid_reservation_without_created_is_still_moved(self):   # finding 9
        iid = self.ready_item()
        with self.ops.store.tx() as c:
            c.execute("UPDATE ops_reservations SET slot='2026-10-10T13:17:00Z', created=NULL, owner_pinned=0 "
                      'WHERE item_id=?', (iid,))
        f = [x for x in self.ops.inspect()['findings'] if x.get('kind') == 'off_grid'] \
            if isinstance(self.ops.inspect().get('findings'), list) else []
        self.assertTrue(f, self.ops.inspect())


class PermanenceWords(unittest.TestCase):                              # finding 2
    def test_one_item_sentence_is_never_a_lasting_rule(self):
        import sys
        from support import ROOT
        sys.path.insert(0, str(ROOT / 'bondok'))
        import language as L
        self.assertFalse(L.permanent('انشر الفيديو ده خلاص مش لازم توباز'))
        self.assertFalse(L.permanent('من النهارده LIP12 مش محتاج توباز'))
        self.assertTrue(L.permanent('من النهارده توباز مش شرط'))
