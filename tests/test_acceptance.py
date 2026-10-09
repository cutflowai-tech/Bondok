"""Acceptance scenarios 1-32 (brief §9 + handoff §19) against the shared handler.

Scenarios needing Bondok/Slack or workflow JSON live in test_bondok.py and
test_workflows.py; they are referenced here by number.
"""
import json
import sqlite3
import threading
import unittest
from datetime import datetime, timedelta, timezone

from support import (OWNER_ID, T0, Command, OpsCase, monday_item, owner_cmd, rules, set_cell)

from waset_ops import board


def at_cairo(y, mo, d, h, mi):
    return rules.cairo_local(y, mo, d, h, mi)


class Scenario01StoryDuration(OpsCase):
    def test_59_9_passes_duration_gate_only(self):
        self.observe(monday_item('1', fmt='Story'))
        self.assertEqual(self.select('1')['next'], 'preflight')
        r = self.duration('1', 59.9)
        self.assertEqual(r['next'], 'continue')
        # Duration alone is not readiness: Topaz is still missing.
        r = self.select('1')
        self.assertEqual(r.get('blocked'), 'topaz')
        self.assertIsNone(self.res('1'))

    def test_60_and_63_fail_before_scheduling_and_processing(self):
        for iid, secs in (('2', 60.0), ('3', 63.0)):
            self.observe(monday_item(iid, fmt='Story'))
            self.select(iid)
            r = self.duration(iid, secs)
            self.assertEqual(r['blocked'], 'story_duration')
            it = self.item(iid)
            self.assertEqual(it['readiness'], 'blocked')
            self.assertIn('strictly under 60', it['block_reason'])
            self.assertIsNone(self.res(iid))
            # A later cycle with the same file does not start encoding.
            self.assertEqual(self.select(iid).get('next'), 'none')
            self.assertEqual(rules.story_duration_failure(60.000) is not None, True)
            self.assertIsNone(rules.story_duration_failure(59.999))


class Scenario02StoryToPost(OpsCase):
    def test_owner_conversion_keeps_item_and_invalidates(self):
        self.observe(monday_item('10', fmt='Story'))
        self.select('10')
        self.duration('10', 63.0)
        r = self.ops.submit(owner_cmd('c1', 'change_format', '10', explicit=True, format='Post'))
        self.assertEqual(r['state'], 'completed', r)
        it = self.item('10')
        self.assertEqual((it['item_id'], it['format'], it['readiness']), ('10', 'Post', 'checking'))
        self.assertIsNone(it['verification_id'])
        # Post checks: no Story duration limit, but Topaz/quality still required.
        r = self.select('10')
        self.assertEqual(r.get('blocked'), 'topaz')
        self.topaz('10')
        self.assertEqual(self.select('10')['next'], 'prepare')
        with self.ops.store.read() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM ops_items').fetchone()[0], 1)

    def test_conversion_cancels_existing_reservation(self):
        self.make_ready('11', 'Story')
        self.assertIsNotNone(self.res('11'))
        self.ops.submit(owner_cmd('c2', 'change_format', '11', explicit=True, format='Post'))
        self.assertIsNone(self.res('11'))
        self.assertIsNone(self.item('11')['verification_id'])


class Scenario03FileChange(OpsCase):
    def test_file_change_after_scheduling_blocks_old_version(self):
        self.make_ready('20', 'Story')
        old = self.res('20')
        self.assertIsNotNone(old)
        r = self.select('20', n=2)          # editor uploaded a new revision
        self.assertEqual(r['next'], 'preflight')
        self.assertIsNone(self.res('20'))
        it = self.item('20')
        self.assertIsNone(it['verification_id'])
        self.assertNotEqual(it['topaz_asset'], it['asset_key'])
        self.clock.set(rules.instant(old['slot']) + timedelta(minutes=1))
        self.assertFalse(self.ops.claim('20', 'w')['claimed'])

    def test_file_change_detected_at_commit_point(self):
        self.make_ready('21', 'Story')
        slot = rules.instant(self.res('21')['slot'])
        self.clock.set(slot + timedelta(seconds=30))
        cl = self.ops.claim('21', 'w')
        self.ops.container(cl['attempt_id'], 'w', cl['fence'], 'C1')
        r = self.ops.commit(cl['attempt_id'], 'w', cl['fence'], source_asset='id:FILE1@rev2',
                            container_status='FINISHED')
        self.assertFalse(r['committed'])
        self.assertIn('Dropbox file version changed after verification', r['reasons'])
        # Replay finding: the authorization must be dropped, otherwise WF2 re-claims every minute and
        # creates a new Instagram container each time.
        self.assertIsNone(self.res('21'))
        it = self.item('21')
        self.assertEqual((it['readiness'], it['verification_id']), ('checking', None))
        self.clock.advance(60)
        self.assertFalse(self.ops.claim('21', 'w2').get('claimed'))
        self.assertEqual(self.ops.due('w2')['work'], [])
        notes = [o for o in self.outbox('slack') if 'Dropbox file changed after it was verified' in o['payload']]
        self.assertEqual(len(notes), 1)


class Scenario04ConflictingSchedulers(OpsCase):
    def test_bondok_and_monitor_serialize(self):
        self.make_ready('30', 'Post')
        self.drain_monday()
        target = at_cairo(2026, 10, 12, 21, 0)    # Monday 21:00
        # Monitor sees an off-grid legacy reservation and wants to move it while
        # the owner asks for a specific valid slot at the same time.
        results = []

        def owner():
            results.append(self.ops.submit(owner_cmd('o-1', 'request_reschedule', '30', at=rules.iso(target))))

        def monitor():
            results.append(self.ops.repair())

        ts = [threading.Thread(target=owner), threading.Thread(target=monitor)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        res = self.res('30')
        self.assertTrue(rules.on_grid('Post', rules.instant(res['slot'])))
        self.assertEqual(res['slot'], rules.iso(target))
        with self.ops.store.read() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM ops_reservations WHERE item_id=?', ('30',)).fetchone()[0], 1)


class Scenario05SlotUniqueness(OpsCase):
    def test_two_items_cannot_hold_same_slot(self):
        self.make_ready('40', 'Post', code='LIP1')
        self.make_ready('41', 'Post', code='KE2')
        self.assertNotEqual(self.res('40')['slot'], self.res('41')['slot'])
        with self.ops.store.tx() as c, self.assertRaises(sqlite3.IntegrityError):
            c.execute('UPDATE ops_reservations SET slot=? WHERE item_id=?', (self.res('40')['slot'], '41'))

    def test_equivalent_instants_collide(self):
        self.make_ready('42', 'Post', code='LIP1')
        slot = self.res('42')['slot']
        cairo_text = rules.instant(slot).astimezone(rules.TZ).isoformat()   # same instant, +03:00
        self.make_ready('43', 'Post', code='KE2')
        r = self.ops.submit(owner_cmd('x', 'request_reschedule', '43', at=cairo_text))
        self.assertEqual(r['state'], 'awaiting_approval')   # recognised as the occupied slot


class Scenario06StaleApproval(OpsCase):
    def test_approval_after_change_does_not_execute(self):
        self.make_ready('50', 'Post', code='LIP1')
        self.make_ready('51', 'Post', code='KE2')
        target = self.res('50')['slot']
        p = self.ops.submit(owner_cmd('p', 'request_reschedule', '51', at=target))
        self.assertEqual(p['state'], 'awaiting_approval')
        self.ops.submit(owner_cmd('n', 'update_caption', '50', text='New hook, DM us. ⚡️\n\n#reel'))
        r = self.ops.submit(owner_cmd('a', 'approve_proposal', None, proposal_id=p['proposal_id']))
        self.assertEqual(r['state'], 'rejected')
        self.assertEqual(r['code'], 'stale')
        self.assertEqual(self.res('50')['slot'], target)
        with self.ops.store.read() as c:
            self.assertEqual(c.execute('SELECT state FROM ops_proposals WHERE id=?',
                                       (p['proposal_id'],)).fetchone()[0], 'stale')

    def test_expired_approval(self):
        self.make_ready('52', 'Post', code='LIP1')
        self.make_ready('53', 'Post', code='KE2')
        p = self.ops.submit(owner_cmd('p', 'request_reschedule', '53', at=self.res('52')['slot']))
        self.clock.advance(31 * 60)
        r = self.ops.submit(owner_cmd('a', 'approve_proposal', None, proposal_id=p['proposal_id']))
        self.assertEqual(r['code'], 'expired')


class Scenario07Duplicates(OpsCase):
    def test_duplicate_command_delivery(self):
        self.make_ready('60', 'Post')
        cmd = owner_cmd('slack:T:C:1700000000.1', 'pause', '60')
        a, b = self.ops.submit(cmd), self.ops.submit(cmd)
        self.assertEqual(a['state'], 'completed')
        self.assertTrue(b['duplicate'])
        with self.ops.store.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM ops_audit WHERE kind='item_update' AND detail LIKE '%pause%'"
                                       ).fetchone()[0], 1)

    def test_id_reuse_with_different_payload_rejected(self):
        self.make_ready('61', 'Post')
        self.ops.submit(owner_cmd('same', 'pause', '61'))
        r = self.ops.submit(owner_cmd('same', 'skip', '61'))
        self.assertEqual(r['code'], 'id_reuse')

    def test_workflow_retry_does_not_duplicate_attempt(self):
        self.make_ready('62', 'Story')
        self.clock.set(rules.instant(self.res('62')['slot']) + timedelta(seconds=5))
        a = self.ops.claim('62', 'exec-1')
        b = self.ops.claim('62', 'exec-2')
        self.assertTrue(a['claimed'])
        self.assertFalse(b['claimed'])
        with self.ops.store.read() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM ops_attempts').fetchone()[0], 1)


class Scenario08MondayEdits(OpsCase):
    def test_board_reschedule_goes_through_checks_without_loop(self):
        self.make_ready('70', 'Post')
        self.drain_monday()
        board_item = monday_item('70', fmt='Post', code='LIP12', status='Scheduled')
        # Re-observe exactly what we projected: no commands.
        with self.ops.store.read() as c:
            it = self.ops.item(c, '70')
        proj = __import__('json').loads(it['projected'])
        set_cell(board_item, 'publish_at', 'x', rules.monday_publish_at_value(rules.instant(proj['publish_at'])))
        set_cell(board_item, 'status', proj['status'])
        for k in ('action', 'system', 'media', 'video', 'measurements', 'processed', 'asset', 'version_check', 'dropbox',
                  'source_item', 'style', 'post_date', 'post_time', 'folder', 'checked'):
            if proj.get(k) is not None:
                if board.KIND.get(k) == 'link':
                    set_cell(board_item, k, proj[k], {'url': proj[k]})
                elif board.KIND.get(k) == 'long':
                    set_cell(board_item, k, proj[k], {'text': proj[k]})
                elif k == 'post_date':
                    set_cell(board_item, k, proj[k], {'date': proj[k]})
                elif k == 'post_time':
                    h, m = proj[k].split(':')
                    set_cell(board_item, k, proj[k], {'hour': int(h), 'minute': int(m)})
                else:
                    set_cell(board_item, k, proj[k])
        set_cell(board_item, 'caption', 'Sunlight sets the pace, DM us for edits. 🔥\n\n#reels',
                 {'text': 'Sunlight sets the pace, DM us for edits. 🔥\n\n#reels'})
        set_cell(board_item, 'topaz', 'Topazed')
        set_cell(board_item, 'asset', 'id:FILE1@rev1')
        r = self.observe(board_item)
        self.assertEqual(r['edits'], [], r['edits'])
        # Human types an off-grid time: rejected with alternatives, display reverted.
        set_cell(board_item, 'publish_at', 'x', rules.monday_publish_at_value(at_cairo(2026, 10, 13, 20, 0)))
        r = self.observe(board_item)
        self.assertEqual(r['edits'][0]['state'], 'rejected')
        self.assertIn('outside the agreed', r['edits'][0]['reason'])
        self.assertEqual(self.res('70')['slot'], proj['publish_at'])
        # Same observation again: same idempotency id, no new action.
        r2 = self.observe(board_item)
        self.assertTrue(all(e.get('duplicate') or e['state'] == 'rejected' for e in r2['edits']))


class Scenario09PauseRace(OpsCase):
    def _to_container(self, iid):
        self.make_ready(iid, 'Story')
        self.clock.set(rules.instant(self.res(iid)['slot']) + timedelta(seconds=10))
        cl = self.ops.claim(iid, 'w')
        self.ops.container(cl['attempt_id'], 'w', cl['fence'], 'C-' + iid)
        return cl

    def test_pause_before_commitment_point_wins(self):
        cl = self._to_container('80')
        r = self.ops.submit(owner_cmd('p', 'pause', '80'))
        self.assertEqual(r['publication'], 'stopped_before_commit')
        c = self.ops.commit(cl['attempt_id'], 'w', cl['fence'], container_status='FINISHED')
        self.assertFalse(c['committed'])

    def test_pause_after_commitment_point_is_truthful(self):
        cl = self._to_container('81')
        self.assertTrue(self.ops.commit(cl['attempt_id'], 'w', cl['fence'], container_status='FINISHED')['committed'])
        r = self.ops.submit(owner_cmd('p', 'pause', '81'))
        self.assertEqual(r['publication'], 'may_already_be_in_progress')
        self.assertNotIn('stopped', r['message'].lower().replace('stopped before', ''))
        # The result still lands and is preserved.
        self.assertEqual(self.ops.result(cl['attempt_id'], 'w', media_id='IG1')['stage'], 'published')


class Scenario10CrashReconcile(OpsCase):
    def test_worker_death_after_commit_becomes_unknown_not_republished(self):
        self.make_ready('90', 'Story')
        self.clock.set(rules.instant(self.res('90')['slot']) + timedelta(seconds=10))
        cl = self.ops.claim('90', 'w')
        self.ops.container(cl['attempt_id'], 'w', cl['fence'], 'C90')
        self.ops.commit(cl['attempt_id'], 'w', cl['fence'], container_status='FINISHED')
        self.clock.advance(1000)   # worker died; commit lease passed
        due = self.ops.due('w2')
        self.assertEqual(self.item('90')['publication'], 'outcome_unknown')
        self.assertFalse(any(w['kind'] == 'publish' and w['item_id'] == '90' for w in due['work']))
        self.assertFalse(self.ops.claim('90', 'w2')['claimed'])
        # Reconcile: an inconclusive check stays unknown; PUBLISHED confirms.
        self.clock.advance(400)
        work = [w for w in self.ops.due('w2')['work'] if w['kind'] == 'reconcile']
        self.assertEqual(work[0]['container_id'], 'C90')
        self.assertEqual(self.ops.reconcile(cl['attempt_id'], None, 'timeout')['stage'], 'outcome_unknown')
        self.assertEqual(self.ops.reconcile(cl['attempt_id'], 'PUBLISHED')['stage'], 'published')

    def test_timeout_on_publish_is_unknown(self):
        self.make_ready('91', 'Story')
        self.clock.set(rules.instant(self.res('91')['slot']) + timedelta(seconds=10))
        cl = self.ops.claim('91', 'w')
        self.ops.container(cl['attempt_id'], 'w', cl['fence'], 'C91')
        self.ops.commit(cl['attempt_id'], 'w', cl['fence'], container_status='FINISHED')
        r = self.ops.result(cl['attempt_id'], 'w', error='ETIMEDOUT', http_status=None)
        self.assertEqual(r['stage'], 'outcome_unknown')
        with self.ops.store.read() as c:
            legacy = c.execute('SELECT stage FROM publications WHERE item=?', ('91',)).fetchone()[0]
        self.assertEqual(legacy, 'publish_requested')   # rollback-safe guard


class Scenario11SyncFailure(OpsCase):
    def test_receipt_survives_monday_failure(self):
        self.make_ready('100', 'Story')
        self.drain_monday()
        self.clock.set(rules.instant(self.res('100')['slot']) + timedelta(seconds=10))
        cl = self.ops.claim('100', 'w')
        self.ops.container(cl['attempt_id'], 'w', cl['fence'], 'C100')
        self.ops.commit(cl['attempt_id'], 'w', cl['fence'], container_status='FINISHED')
        self.ops.result(cl['attempt_id'], 'w', media_id='IG100')
        for _ in range(3):
            for j in self.ops.outbox_take(['monday'], 'sync', 10):
                self.ops.outbox_ack(j['id'], 'sync', False, 'Monday 500')
            self.clock.advance(4000)
        self.assertEqual(self.item('100')['publication'], 'published')
        self.assertFalse(self.ops.claim('100', 'w')['claimed'])
        with self.ops.store.read() as c:
            self.assertEqual(c.execute("SELECT stage FROM ops_attempts").fetchone()[0], 'published')
        # Only display sync retries, then escalates once.
        for _ in range(5):
            for j in self.ops.outbox_take(['monday'], 'sync', 10):
                self.ops.outbox_ack(j['id'], 'sync', False, 'Monday 500')
            self.clock.advance(4000)
        esc = [o for o in self.outbox('slack') if 'Display/sync' in o['payload']]
        self.assertEqual(len(esc), 1)


class Scenario12Protections(OpsCase):
    def test_paused_skipped_published_unknown_keep_protection(self):
        self.make_ready('110', 'Story', code='LIP1')
        self.ops.submit(owner_cmd('p', 'pause', '110'))
        self.make_ready('111', 'Story', code='KE2')
        self.ops.submit(owner_cmd('s', 'skip', '111'))
        for iid in ('110', '111'):
            self.assertIsNone(self.res(iid))
            self.assertEqual(self.select(iid, n=3).get('note'), 'protected')   # infra fix does not resume
            self.ops.repair()
            self.assertIsNone(self.res(iid))
        r = self.ops.submit(Command('m', 'resume', 'monday', 'monday', '110', {}))
        self.assertEqual(r['code'], 'forbidden')
        r = self.ops.submit(owner_cmd('r', 'resume', '110'))
        self.assertTrue(r['resumed'])

    def test_published_item_cannot_be_reset(self):
        self.make_ready('112', 'Story')
        self.clock.set(rules.instant(self.res('112')['slot']) + timedelta(seconds=10))
        cl = self.ops.claim('112', 'w')
        self.ops.container(cl['attempt_id'], 'w', cl['fence'], 'C')
        self.ops.commit(cl['attempt_id'], 'w', cl['fence'], container_status='FINISHED')
        self.ops.result(cl['attempt_id'], 'w', media_id='IG')
        for op, args in (('replace_source', {'url': 'https://www.dropbox.com/s/x/y.mp4'}),
                         ('request_reschedule', {'at': rules.iso(at_cairo(2026, 10, 20, 11, 0))})):
            self.assertEqual(self.ops.submit(owner_cmd(self.rid(), op, '112', **args))['state'], 'rejected')


class Scenario13NotesAndCaption(OpsCase):
    def test_notes_do_not_reprocess(self):
        self.make_ready('120', 'Post')
        before = self.item('120')
        self.ops.submit(Command('n', 'set_notes', 'monday', 'monday', '120', {'value': 'call client'}))
        after = self.item('120')
        self.assertEqual((before['content_rev'], before['verification_id']),
                         (after['content_rev'], after['verification_id']))
        self.assertEqual(self.res('120')['content_rev'], after['content_rev'])

    def test_caption_edit_cannot_publish_obsolete_payload(self):
        self.make_ready('121', 'Post')
        self.clock.set(rules.instant(self.res('121')['slot']) + timedelta(seconds=10))
        cl = self.ops.claim('121', 'w')
        self.ops.container(cl['attempt_id'], 'w', cl['fence'], 'C-old')
        self.ops.submit(Command('cap', 'update_caption', 'monday', 'monday', '121',
                                {'text': 'Fresh hook, DM us now. 🚀\n\n#realestate'}))
        r = self.ops.commit(cl['attempt_id'], 'w', cl['fence'], container_status='FINISHED')
        self.assertFalse(r['committed'])
        cl2 = self.ops.claim('121', 'w')
        self.assertTrue(cl2['claimed'])
        self.assertIsNone(cl2['container_id'])      # old container never reused
        self.assertIn('Fresh hook', cl2['payload']['caption'])


class Scenario14Unauthorized(OpsCase):
    def test_actor_kind_enforced(self):
        self.make_ready('130', 'Story')
        for cmd in (Command('b1', 'resume', 'U-OTHER', 'bondok', '130', {}),
                    Command('b2', 'change_format', 'bondok', 'bondok', '130', {'format': 'Post'}),
                    Command('b3', 'resolve_outcome', 'monday', 'monday', '130', {'outcome': 'published'})):
            self.assertEqual(self.ops.submit(cmd)['code'], 'forbidden')

    def test_foreign_item_rejected(self):
        r = self.ops.submit(owner_cmd('f', 'pause', '999999'))
        self.assertEqual(r['code'], 'unknown_item')


class Scenario15QuietMonitor(OpsCase):
    def test_unchanged_cycles_quiet(self):
        self.make_ready('140', 'Story')
        with self.ops.store.tx() as c:   # an off-grid legacy reservation, not owner pinned
            c.execute('UPDATE ops_reservations SET slot=? WHERE item_id=?', ('2026-10-11T09:30:00Z', '140'))
        first = self.ops.repair()
        self.assertEqual(len(first['repairs']), 1)
        n1 = len(self.outbox('slack'))
        for _ in range(3):
            r = self.ops.repair()
            self.assertEqual(r['repairs'], [])
            self.assertEqual(r['new_findings'], 0)
        self.assertEqual(len(self.outbox('slack')), n1)


class Scenario16MigrationHistory(OpsCase):
    def test_legacy_posted_imported_without_receipt_and_not_due(self):
        posted = monday_item('150', fmt='Post', status='Posted', publish_at=T0 - timedelta(days=3),
                             extra={'post_link': ('https://instagram.com/p/x', {'url': 'https://instagram.com/p/x'})})
        self.observe(posted)
        it = self.item('150')
        self.assertEqual((it['publication'], it['legacy_posted']), ('published', 1))
        with self.ops.store.read() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM ops_attempts').fetchone()[0], 0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM publications').fetchone()[0], 0)
        self.assertEqual(self.ops.due('w')['work'], [])
        self.assertEqual(self.outbox('monday'), [])   # bootstrap does not rewrite the board


class Scenario17AutonomousFormat(OpsCase):
    def test_model_claimed_permission_ignored(self):
        self.make_ready('160', 'Story', duration=63.0) if False else self.observe(monday_item('160', fmt='Story'))
        cmd = Command('m', 'change_format', OWNER_ID, 'owner', '160', {'format': 'Post', 'owner_approved': True},
                      auth={})     # model text said "owner approved"; adapter gave no evidence
        r = self.ops.submit(cmd)
        self.assertEqual(r['code'], 'needs_owner')
        self.assertEqual(self.item('160')['format'], 'Story')
        # Monday-originated format edit only creates a hold + approval request.
        r = self.ops.submit(Command('mm', 'propose', 'monday', 'monday', '160', {'kind': 'change_format',
                                                                                   'format': 'Post'}))
        self.assertEqual(r['state'], 'awaiting_approval')
        self.assertEqual(self.item('160')['format'], 'Story')
        a = self.ops.submit(owner_cmd('ap', 'approve_proposal', None, proposal_id=r['proposal_id']))
        self.assertEqual(a['state'], 'completed', a)
        self.assertEqual(self.item('160')['format'], 'Post')


class Scenario18OccupiedSlot(OpsCase):
    def test_no_change_before_approval_and_invalidation(self):
        self.make_ready('170', 'Post', code='LIP1')
        self.make_ready('171', 'Post', code='KE2')
        s170, s171 = self.res('170')['slot'], self.res('171')['slot']
        p = self.ops.submit(owner_cmd('p', 'request_reschedule', '171', at=s170))
        self.assertEqual(p['state'], 'awaiting_approval')
        self.assertEqual(len(p['affected']), 2)
        self.assertEqual((self.res('170')['slot'], self.res('171')['slot']), (s170, s171))
        a = self.ops.submit(owner_cmd('a', 'approve_proposal', None, proposal_id=p['proposal_id']))
        self.assertEqual(a['state'], 'completed', a)
        self.assertEqual(self.res('171')['slot'], s170)
        self.assertNotEqual(self.res('170')['slot'], s170)
        self.assertTrue(rules.on_grid('Post', rules.instant(self.res('170')['slot'])))
        again = self.ops.submit(owner_cmd('a2', 'approve_proposal', None, proposal_id=p['proposal_id']))
        self.assertEqual(again['code'], 'decided')


class Scenario20EditorSpam(OpsCase):
    def test_unchanged_blocked_item_creates_one_task(self):
        self.observe(monday_item('180', fmt='Post', extra={'source_item': ('9180', '"9180"')}))
        for _ in range(5):
            self.select('180')
            self.clock.advance(3600)
        self.assertEqual(len(self.outbox('editor')), 1)
        self.select('180', n=2)     # new version with the same issue -> material change
        self.assertEqual(len(self.outbox('editor')), 2)


class Scenario21EmptyQueue(OpsCase):
    def test_empty_due_queue(self):
        self.assertEqual(self.ops.due('w'), {'work': [], 'more': False})


class Scenario22Outage(OpsCase):
    def test_infra_error_keeps_schedule_and_evidence(self):
        self.make_ready('190', 'Story')
        slot, ver = self.res('190')['slot'], self.item('190')['verification_id']
        self.clock.advance(3600)
        r = self.wf1('prep_source', '190', error='Dropbox 503', error_kind='infra')
        self.assertTrue(r['infra'])
        it = self.item('190')
        self.assertEqual((it['readiness'], it['verification_id']), ('ready', ver))
        self.assertEqual(self.res('190')['slot'], slot)
        self.assertIn('Dropbox 503', it['infra_issue'])

    def test_helper_failure_logged_outside_sqlite(self):
        import base64, json, os, subprocess, sys
        env = {**os.environ, 'WASET_SOCIAL_DATA_DIR': str(self.dir / 'broken')}
        (self.dir / 'broken').mkdir()
        (self.dir / 'broken' / 'state.sqlite').write_text('not a database')
        arg = base64.b64encode(json.dumps({'path': '/v2/health', 'body': {}}).encode()).decode()
        p = subprocess.run([sys.executable, str(self.dir.parents[0] / 'x')] if False else
                           [sys.executable, str(__import__('support').ROOT / 'src' / 'helper.py'), arg],
                           capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0)
        out = json.loads(p.stdout)
        self.assertFalse(out['ok'])
        self.assertTrue((self.dir / 'broken' / 'errors.log').read_text().strip())


class SourceProblemRecovery(OpsCase):
    """Cutover finding: a Dropbox share failure blocked items permanently, even after the file resolved."""

    def test_resolved_source_problem_unblocks_same_file(self):
        self.observe(monday_item('195', fmt='Story', code='LIP12'))
        self.select('195')                                  # file known before the failure
        r = self.wf1('prep_source', '195', error='Dropbox request failed', error_kind='config')
        self.assertEqual(r['blocked'], 'config')
        self.assertEqual(self.item('195')['readiness'], 'blocked')
        r = self.select('195')                              # same file now resolves with a link
        self.assertNotEqual(r.get('unchanged'), True)
        it = self.item('195')
        self.assertNotEqual(it['block_reason'], 'Dropbox request failed')
        self.assertEqual(r['next'], 'preflight')            # normal checks resume (Story duration)

    def test_other_blocks_are_not_cleared_by_source_success(self):
        self.observe(monday_item('196', fmt='Story', code='LIP12'))
        self.select('196')
        self.duration('196', 75.0)                          # Story too long: content block on this asset
        self.assertEqual(self.item('196')['block_kind'], 'content')
        r = self.select('196')
        self.assertTrue(r.get('unchanged'))
        self.assertEqual(self.item('196')['block_kind'], 'content')


class CaptionDraftApproval(OpsCase):
    """Cutover finding: WF1 drafts were bound to an item version the system itself bumped, so the owner's
    approval could never execute."""

    TAGS = ' '.join('#tag' + str(i) for i in range(12))
    TEXT = 'Golden hour on the water, DM us to book. 🌅\n\n' + TAGS

    def draft(self, iid, h='h1', text=TEXT):
        return self.ops.submit(Command(self.rid('draft'), 'caption_draft', 'service:wf1', 'service:wf1', iid,
                                       {'input_hash': h, 'text': text, 'model': 'm'}))

    def approve(self, pid):
        return self.ops.submit(owner_cmd(self.rid('ok'), 'approve_proposal', None, proposal_id=pid))

    def test_approval_survives_system_updates(self):
        self.observe(monday_item('197', fmt='Post', code='LIP12'))
        pid = self.draft('197')['proposal_id']
        self.wf1('prep_source', '197', error='Dropbox request failed', error_kind='config')   # version bump
        self.select('197')                                                                   # another bump
        r = self.approve(pid)
        self.assertEqual(r['state'], 'completed', r)
        it = self.item('197')
        self.assertEqual((it['caption'], it['caption_state']), (self.TEXT, 'approved'))

    def test_approval_rejected_after_caption_changed(self):
        self.observe(monday_item('198', fmt='Post', code='LIP12'))
        pid = self.draft('198')['proposal_id']
        self.ops.submit(owner_cmd(self.rid('cap'), 'update_caption', '198', text='Owner wrote this one. ✨\n\n#reel'))
        r = self.approve(pid)
        self.assertEqual((r['state'], r['code']), ('rejected', 'stale'))
        self.assertEqual(self.item('198')['caption'], 'Owner wrote this one. ✨\n\n#reel')

    def test_newer_draft_supersedes_older(self):
        self.observe(monday_item('199', fmt='Post', code='LIP12'))
        old = self.draft('199', 'h1')['proposal_id']
        new = self.draft('199', 'h2', 'Second take, DM us. 🎬\n\n' + self.TAGS)['proposal_id']
        self.assertEqual(self.approve(old)['code'], 'stale')
        self.assertEqual(self.approve(new)['state'], 'completed')

    def test_still_expires(self):
        self.observe(monday_item('200', fmt='Post', code='LIP12'))
        pid = self.draft('200')['proposal_id']
        self.clock.advance(31 * 60)
        self.assertEqual(self.approve(pid)['code'], 'expired')


class PreparationCoverageAndQuietness(OpsCase):
    """Replay findings: the same 20 items were re-picked every cycle; unchanged problems were re-applied;
    editor jobs without a projects item could never complete."""

    def test_every_eligible_item_is_reached(self):
        for i in range(30):
            self.observe(monday_item(str(300 + i), fmt='Story', code='LIP12', name=f'Item {i} LIP12'))
        first = [x['item_id'] for x in self.ops.work_queue(limit=20)]
        for iid in first:
            self.wf1('prep_source', iid, error='No final video found in the project folder', error_kind='editor')
        self.clock.advance(7200)                       # every backoff has expired
        second = [x['item_id'] for x in self.ops.work_queue(limit=20)]
        never = {str(300 + i) for i in range(30)} - set(first)
        self.assertEqual(len(never), 10)
        self.assertTrue(never <= set(second), 'never-checked items must come first')

    def test_items_waiting_for_a_result_come_first(self):
        self.observe(monday_item('340', fmt='Story', code='LIP12'), monday_item('341', fmt='Story', code='LIP12'),
                     monday_item('342', fmt='Story', code='LIP12'))
        self.wf1('prep_source', '340', error='No final video found in the project folder', error_kind='editor')
        self.clock.advance(60)
        self.select('341')                               # Story: duration measurement pending -> 'checking'
        self.assertEqual(self.item('341')['readiness'], 'checking')
        self.clock.advance(7200)
        self.assertEqual([x['item_id'] for x in self.ops.work_queue(limit=3)], ['341', '342', '340'])

    def test_topaz_confirmation_is_prepared_on_the_next_cycle(self):
        for i in range(25):
            self.observe(monday_item(str(350 + i), fmt='Story', code='LIP12', name=f'Item {i} LIP12'))
        self.observe(monday_item('380', fmt='Post', code='LIP12'))
        self.select('380')                                     # Post: blocked on Topaz for this version
        self.assertEqual(self.item('380')['block_kind'], 'editor')
        self.clock.advance(60)
        self.topaz('380')
        self.assertEqual(self.ops.work_queue(limit=20)[0]['item_id'], '380')
        self.clock.advance(60)
        r = self.select('380')                                 # prepared once; the nudge is consumed
        self.assertEqual(r['next'], 'prepare')
        q = {x['item_id']: x for x in self.ops.work_queue(limit=30)}
        self.assertEqual(q['380']['priority'], 1)              # now simply waiting for its media result

    def test_same_problem_is_not_reapplied(self):
        self.observe(monday_item('330', fmt='Story', code='LIP12', extra={'source_item': ('9330', '"9330"')}))
        self.wf1('prep_source', '330', error='Folder "x" does not match this item', error_kind='config')
        v = self.item('330')['version']
        jobs = len(self.outbox())
        for _ in range(3):
            self.clock.advance(3700)
            r = self.wf1('prep_source', '330', error='Folder "x" does not match this item', error_kind='config')
            self.assertTrue(r.get('unchanged'))
        self.assertEqual(self.item('330')['version'], v)
        self.assertEqual(len(self.outbox()), jobs)
        self.wf1('prep_source', '330', error='Another problem', error_kind='config')   # a new problem still applies
        self.assertEqual(self.item('330')['block_reason'], 'Another problem')

    def test_no_editor_job_without_projects_item(self):
        self.observe(monday_item('331', fmt='Story', code='LIP12'))
        self.wf1('prep_source', '331', error='No final video found in the project folder', error_kind='editor')
        self.assertEqual(self.outbox('editor'), [])
        self.assertEqual(self.item('331')['block_reason'], 'No final video found in the project folder')


class OwnershipBackfill(OpsCase):
    """Replay finding: Action texts v2 delivered before `_ours` existed could never be cleared by v2."""

    def test_backfill_from_delivered_jobs_only(self):
        self.observe(monday_item('390', fmt='Story', code='LIP12'))
        self.wf1('prep_source', '390', error='Folder "x" does not match this item', error_kind='config')
        self.drain_monday()                                   # delivered: action written by v2
        with self.ops.store.tx() as c:                        # simulate a pre-`_ours` acknowledgement
            p = json.loads(self.item('390')['projected']); p.pop('_ours', None)
            c.execute('UPDATE ops_items SET projected=? WHERE item_id=?', (json.dumps(p), '390'))
        self.assertEqual(self.ops.apply_data_fixes(), ['ours_backfill'])
        self.assertEqual(self.ops.apply_data_fixes(), [])     # once only
        ours = json.loads(self.item('390')['projected'])['_ours']
        self.assertIn('action', ours)
        self.select('390')                                    # source now resolves: old message must go
        jobs = self.ops.outbox_take(['monday'], 't', 50)
        cleared = {k for j in jobs for k, v in j['payload']['columns'].items() if v in ({}, '', None, {'text': ''})}
        self.assertIn(board.COL['action'], cleared)


class Scenario23ZeroRow(OpsCase):
    def test_retired_commit_and_normalization(self):
        self.assertEqual(rules.iso('2026-10-12T21:00:00+03:00'), rules.iso('2026-10-12T18:00:00Z'))
        self.assertEqual(rules.iso('2026-10-12T18:00:00.000+00:00'), '2026-10-12T18:00:00Z')
        # There is no separate commit step any more: a reservation either inserts
        # (unique constraint) or the request is rejected; nothing reports success
        # without a row.
        self.make_ready('200', 'Post', code='LIP1')
        self.make_ready('201', 'Post', code='KE2')
        with self.ops.store.tx() as c:
            it = self.ops.item(c, '201')
            c.execute('DELETE FROM ops_reservations WHERE item_id=?', ('201',))
            from waset_ops.core import Rejected
            with self.assertRaises(Rejected):
                self.ops._reserve(c, it, rules.instant(self.res('200')['slot']), 'owner', True, 'test')


class Scenario24StaleWorker(OpsCase):
    def test_fenced_worker_cannot_commit_but_evidence_kept(self):
        self.make_ready('210', 'Story')
        self.clock.set(rules.instant(self.res('210')['slot']) + timedelta(seconds=10))
        old = self.ops.claim('210', 'w-old')
        self.ops.container(old['attempt_id'], 'w-old', old['fence'], 'C210')
        self.clock.advance(200)    # lease expired; a new execution takes over
        new = self.ops.claim('210', 'w-new')
        self.assertTrue(new['resumed'])
        self.assertEqual(new['container_id'], 'C210')
        with self.assertRaises(Exception):
            self.ops.commit(old['attempt_id'], 'w-old', old['fence'], container_status='FINISHED')
        self.assertTrue(self.ops.commit(new['attempt_id'], 'w-new', new['fence'], container_status='FINISHED')['committed'])
        self.clock.advance(5000)   # result arrives after the commit lease: still recorded
        self.assertEqual(self.ops.result(new['attempt_id'], 'w-new', media_id='IG210')['stage'], 'published')

    def test_superseded_wf1_run_discarded(self):
        self.observe(monday_item('211', fmt='Story'))
        self.ops.run_finish('wf1', self.run_id)
        self.ops.run_start('wf1', 'run-2')
        r = self.select('211')
        self.assertEqual(r['code'], 'fenced')


class Scenario25HumanEditsDuringRun(OpsCase):
    def test_human_values_never_overwritten(self):
        self.make_ready('220', 'Story')
        self.drain_monday()
        jobs = []
        for _ in range(3):
            self.select('220', n=1)
            jobs += self.ops.outbox_take(['monday'], 'sync', 50)
        for j in jobs:
            for col in (board.COL['caption'], board.COL['topaz'], board.COL['variety'], board.COL['notes']):
                self.assertNotIn(col, j['payload']['columns'])


class Scenario26TimeContract(OpsCase):
    def test_cairo_roundtrip_and_dst(self):
        d = at_cairo(2026, 10, 10, 21, 0)
        self.assertEqual(rules.iso(d), '2026-10-10T18:00:00Z')            # summer time UTC+3
        w = at_cairo(2026, 12, 5, 21, 0)
        self.assertEqual(rules.iso(w), '2026-12-05T19:00:00Z')            # winter UTC+2
        v = rules.monday_publish_at_value(d)
        self.assertEqual(rules.parse_monday_publish_at(v), d)
        legacy = rules.monday_legacy_values(d)
        self.assertEqual(rules.parse_monday_legacy(legacy['date4'], legacy['hour_mm7xy9cf']), d)
        with self.assertRaises(rules.RuleError):
            rules.instant('2026-10-10T21:00:00')                           # naive refused

    def test_dst_gap_refused_not_shifted(self):
        # Egypt springs forward at 00:00 on the last Friday of April (2026-04-24).
        with self.assertRaises(rules.RuleError):
            rules.cairo_local(2026, 4, 24, 0, 30)

    def test_grid_has_every_story_slot_across_transition(self):
        self.clock.set(datetime(2026, 10, 28, 6, 0, tzinfo=timezone.utc))
        slots = list(rules.grid(self.ops.now_dt(), 'Story', horizon=4))
        self.assertTrue(all(rules.on_grid('Story', s) for s in slots))
        self.assertEqual(len(slots), 4 * 5)

    def test_publish_at_wins_over_legacy_and_no_stale_fallback(self):
        snap = {'publish_at': '2026-10-12T18:00:00Z', 'post_date': '2026-10-10', 'post_time': '21:00'}
        self.assertEqual(rules.iso(board.requested_instant(snap)), '2026-10-12T18:00:00Z')
        self.assertIsNone(board.requested_instant({'publish_at': None, 'post_date': None, 'post_time': None}))

    def test_off_grid_rejected_with_alternatives(self):
        self.make_ready('230', 'Story')
        r = self.ops.submit(owner_cmd('o', 'request_reschedule', '230', at=rules.iso(at_cairo(2026, 10, 11, 12, 0))))
        self.assertEqual(r['state'], 'rejected')
        self.assertEqual(len(r['alternatives']), 3)


class Scenario27CaptionNoReencode(OpsCase):
    def test_caption_change_keeps_media(self):
        self.make_ready('240', 'Post')
        ver = self.item('240')['verification_id']
        old_fp = self.res('240')['payload_fp']
        self.ops.submit(owner_cmd('c', 'update_caption', '240', text='Golden hour glow, DM us. ✨\n\n#reels'))
        self.assertEqual(self.item('240')['verification_id'], ver)
        self.assertNotEqual(self.res('240')['payload_fp'], old_fp)
        self.assertEqual(self.res('240')['content_rev'], self.item('240')['content_rev'])


class Scenario28BoardBoundary(OpsCase):
    def test_only_registered_social_items(self):
        # Items enter only through the social-board snapshot adapter; any other
        # id (source board, subitem board) is unknown to the handler.
        for iid in ('5091110326', '1234'):
            self.assertEqual(self.ops.submit(owner_cmd(self.rid(), 'request_recheck', iid))['code'], 'unknown_item')


class Scenario30Collab(OpsCase):
    def test_collab_blocks_and_is_kept(self):
        self.make_ready('250', 'Post')
        r = self.ops.submit(Command('cb', 'set_collab', 'monday', 'monday', '250', {'value': '@partner'}))
        self.assertEqual(r['readiness'], 'blocked')
        it = self.item('250')
        self.assertEqual(it['collab'], '@partner')
        self.assertIsNone(self.res('250'))
        self.assertIn('IG Collab', it['block_reason'])


class Scenario31HistoricalPosted(OpsCase):
    def test_scheduled_legacy_without_receipt_not_republished(self):
        self.observe(monday_item('260', fmt='Story', status='Posted'))
        self.observe(monday_item('261', fmt='Story', status='Publishing'))
        self.assertEqual(self.item('260')['publication'], 'published')
        self.assertEqual(self.item('261')['publication'], 'outcome_unknown')
        self.assertEqual(self.select('260').get('note'), 'protected')
        self.assertEqual(self.ops.due('w')['work'], [])


class Scenario32Recheck(OpsCase):
    def test_explicit_recheck_runs_or_reports_running(self):
        self.make_ready('270', 'Story')
        r = self.ops.submit(owner_cmd('rc', 'request_recheck', '270'))
        self.assertEqual(r['state'], 'accepted')
        self.assertIn('not yet performed', r['message'])
        step = self.select('270')
        self.assertEqual(step['next'], 'prepare')          # not "unchanged"
        self.assertTrue(step['media']['recheck'])
        r2 = self.ops.submit(owner_cmd('rc2', 'request_recheck', '270'))
        self.assertTrue(r2['already_running'])


if __name__ == '__main__':
    unittest.main()


class LegacyMigrationBehaviour(OpsCase):
    def test_legacy_requested_time_is_quiet_preference(self):
        off_grid = rules.iso(at_cairo(2026, 10, 11, 12, 0))
        self.observe(monday_item('400', fmt='Story', status='Scheduled', publish_at=off_grid))
        self.assertEqual(self.item('400')['requested_by'], 'legacy-board')
        self.select('400')
        self.duration('400', 30.0)
        self.topaz('400')
        self.select('400')
        self.wf1('prep_media', '400', result={'ready': True, 'mediaId': self.add_media('400', 'Story')})
        res = self.res('400')
        self.assertIsNotNone(res)
        self.assertEqual(res['owner_pinned'], 0)
        self.assertEqual([o for o in self.outbox('slack') if 'no longer valid' in o['payload']], [])

    def test_bulk_legacy_caption_approval_binds_exact_text(self):
        for iid, cap in (('410', 'Old hook one, DM us. 🔥\n\n#a'), ('411', 'Old hook two, DM us. ⚡️\n\n#b')):
            self.observe(monday_item(iid, fmt='Post', caption=cap, code='LIP' + iid))
        with self.ops.store.tx() as c:
            p = self.ops.propose_legacy_captions(c, 'U-OWNER')
        # one caption changes on the board before approval -> that item is skipped
        self.ops.submit(Command('ed', 'update_caption', 'monday', 'monday', '411', {'text': 'Changed by human, DM. ✨\n\n#c'}))
        r = self.ops.submit(owner_cmd('ap', 'approve_proposal', None, proposal_id=p['proposal_id']))
        self.assertEqual(r['state'], 'rejected')     # bindings changed (item 411 version moved)
        with self.ops.store.tx() as c:
            p2 = self.ops.propose_legacy_captions(c, 'U-OWNER')
        r = self.ops.submit(owner_cmd('ap2', 'approve_proposal', None, proposal_id=p2['proposal_id']))
        self.assertEqual(r['approved'], ['410'])
        self.assertEqual(self.item('410')['caption_state'], 'approved')


class CutoverPreservesLegacyBoardValues(OpsCase):
    """Found at the first live WF1 run: v2 must not clear v1/human dates it never wrote."""

    def test_legacy_post_date_kept_when_item_blocks(self):
        legacy = {'post_date': ('2026-10-17', {'date': '2026-10-17'}), 'post_time': ('11:00 AM', {'hour': 11, 'minute': 0}),
                  'video': ('x', {'url': 'https://www.dropbox.com/s/old/v.mp4'}),
                  'system': ('v1 note', {'text': 'v1 note'}), 'measurements': ('1080x1920 | 90 MB', None),
                  'processed': ('Story', None), 'version_check': ('v.mp4 | rev', None)}
        self.observe(monday_item('500', fmt='Story', status='Needs Review', extra=legacy))
        self.select('500')
        self.duration('500', 63.0)           # item becomes blocked; no reservation
        jobs = self.ops.outbox_take(['monday'], 't', 50)
        cols = {k for j in jobs for k in j['payload']['columns']}
        empty = [(k, v) for j in jobs for k, v in j['payload']['columns'].items() if v in ({}, '', None, {'text': ''})]
        self.assertEqual(empty, [])            # nothing the projection did not write is ever cleared
        for key in ('date4', 'hour_mm7xy9cf', 'date_mm7y8s9t', 'link_mm7ywc0w'):
            self.assertNotIn(key, cols)
        self.assertIn('status', cols)          # display status still updates

    def test_slot_we_wrote_is_cleared_when_released(self):
        self.make_ready('501', 'Story')
        self.drain_monday()
        self.ops.submit(owner_cmd('p', 'pause', '501'))
        jobs = self.ops.outbox_take(['monday'], 't', 50)
        cols = {k for j in jobs for k in j['payload']['columns']}
        self.assertIn('date_mm7y8s9t', cols)   # our own reservation display is cleared
