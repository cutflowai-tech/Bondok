"""Acceptance scenarios 1-32 (brief §9 + handoff §19) against the shared handler.

Scenarios needing Bondok/Slack or workflow JSON live in test_bondok.py and
test_workflows.py; they are referenced here by number.
"""
import json
import sqlite3
import threading
import unittest
from datetime import datetime, timedelta, timezone

from support import (OWNER_ID, ROOT, T0, Command, OpsCase, board_from_projection, monday_item, owner_cmd, rules,
                     set_cell)

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

    def test_60_and_63_are_trimmed_not_blocked(self):
        # Owner policy 2026-10-09: 60-65 s inclusive -> automatic end trim to 59.9 s during preparation.
        for iid, secs in (('2', 60.0), ('3', 63.0), ('4', 65.0)):
            self.observe(monday_item(iid, fmt='Story'))
            self.select(iid)
            r = self.duration(iid, secs)
            self.assertEqual(r['next'], 'continue', r)
            r = self.select(iid)
            self.assertEqual(r.get('blocked'), 'topaz')          # Topaz is still required
            self.assertIsNone(self.res(iid))

    def test_above_65_fails_before_scheduling_and_processing(self):
        for iid, secs in (('5', 65.001), ('6', 77.4774)):
            self.observe(monday_item(iid, fmt='Story'))
            self.select(iid)
            r = self.duration(iid, secs)
            self.assertEqual(r['blocked'], 'story_duration')
            it = self.item(iid)
            self.assertEqual(it['readiness'], 'blocked')
            self.assertIn('automatic trimming covers up to 65 seconds', it['block_reason'])
            self.assertIsNone(self.res(iid))
            # A later cycle with the same file does not start encoding.
            self.assertEqual(self.select(iid).get('next'), 'none')
        # The final prepared file stays strictly under 60 seconds.
        self.assertIsNotNone(rules.story_duration_failure(60.000))
        self.assertIsNone(rules.story_duration_failure(59.999))


class StoryTrimPolicy(OpsCase):
    def test_policy_boundaries(self):
        for d, trim, fail in ((59.999, None, False), (60.0, 59.9, False), (62.5, 59.9, False), (65.0, 59.9, False),
                              (65.0001, None, True), (90, None, True), (None, None, True), ('nan', None, True),
                              (0, None, True)):
            self.assertEqual(rules.story_trim_target(d), trim, d)
            self.assertEqual(rules.story_source_failure(d) is not None, fail, d)
        # The prepared-file gate is unchanged: a trimmed result must be strictly under 60 s.
        self.assertIsNone(rules.media_failure({'width': 1080, 'height': 1920, 'bytes': 9e7, 'duration': 59.92}, 'Story'))
        self.assertIsNotNone(rules.media_failure({'width': 1080, 'height': 1920, 'bytes': 9e7, 'duration': 61.2}, 'Story'))

    def test_trimmed_story_needs_topaz_then_schedules_with_trimmed_media(self):
        self.observe(monday_item('20', fmt='Story', extra={'source_item': ('9020', '"9020"')}))
        self.select('20')
        self.assertEqual(self.duration('20', 61.247)['next'], 'continue')
        r = self.select('20')
        self.assertEqual(r.get('blocked'), 'topaz')
        with self.ops.store.read() as c:
            body = c.execute("SELECT payload FROM ops_outbox WHERE kind='editor'").fetchone()['payload']
        self.assertIn('trimmed automatically to 59.9 seconds; no shorter edit is needed', body)
        with self.ops.store.read() as c:
            self.assertIn('Story is 61.247 s; it will be trimmed automatically to 59.9 s',
                          self.ops.desired_display(c, self.item('20'))['system'])
        self.topaz('20')
        r = self.select('20')
        self.assertEqual(r.get('next'), 'prepare')
        self.assertEqual(r['media']['format'], 'Story')            # never converted to Post
        mid = self.add_media('20', 'Story', duration=59.92)
        with self.ops.store.tx() as c:
            info = json.loads(c.execute('SELECT metadata FROM media WHERE id=?', (mid,)).fetchone()[0])
            info.update(trimmedFrom=61.247, trimmedTo=59.9)
            c.execute('UPDATE media SET metadata=? WHERE id=?', (json.dumps(info), mid))
        r = self.wf1('prep_media', '20', result={'ready': True, 'mediaId': mid})
        self.assertEqual(self.item('20')['readiness'], 'ready', r)
        self.assertIsNotNone(self.res('20'))
        with self.ops.store.read() as c:
            d = self.ops.desired_display(c, self.item('20'))
        self.assertIn('trimmed automatically from 61.247 s to 59.920 s', d['system'])
        self.assertIn('59.920 s', d['measurements'])

    def test_untrimmed_long_prepared_file_is_still_refused(self):
        self.observe(monday_item('21', fmt='Story'))
        self.select('21')
        self.duration('21', 62.0)
        self.topaz('21')
        self.assertEqual(self.select('21').get('next'), 'prepare')
        mid = self.add_media('21', 'Story', duration=62.0)          # e.g. a trim that did not happen
        self.wf1('prep_media', '21', result={'ready': True, 'mediaId': mid})
        self.assertNotEqual(self.item('21')['readiness'], 'ready')
        self.assertIsNone(self.res('21'))

    def test_item_blocked_under_old_policy_is_released_once(self):
        self.observe(monday_item('22', fmt='Story'))
        self.select('22')
        # Simulate the pre-trim verdict stored in production: blocked, editor asked for a shorter edit.
        with self.ops.store.tx() as c:
            key = 'id:FILE1@rev1|hash1'
            c.execute("INSERT OR REPLACE INTO ops_checks(item_id,kind,key,requested,requested_by,state,result,updated) "
                      "VALUES('22','duration',?,0,'x','done',?,0)", (key, json.dumps({'ok': False, 'duration': 60.479})))
            c.execute("UPDATE ops_items SET readiness='blocked', block_kind='content', block_key='story_duration:id:FILE1@rev1', "
                      "block_reason='Story is 60.479 seconds; it must be strictly under 60 seconds' WHERE item_id='22'")
        self.clock.advance(60)
        self.assertEqual(self.ops.apply_data_fixes().count('story_trim_nudge'), 1)
        self.assertEqual(self.ops.apply_data_fixes().count('story_trim_nudge'), 0)     # once
        q = [w['item_id'] for w in self.ops.work_queue(limit=5)]
        self.assertEqual(q[:1], ['22'])
        r = self.select('22')                     # no re-download: the stored measurement is re-evaluated
        self.assertEqual(r.get('blocked'), 'topaz', r)
        self.assertNotEqual(r.get('next'), 'preflight')
        it = self.item('22')
        self.assertEqual(it['block_key'], 'topaz:id:FILE1@rev1')
        # Over-65 item blocked the same way stays blocked and is not nudged.
        self.observe(monday_item('23', fmt='Story'))
        self.select('23')
        self.duration('23', 77.0)
        self.assertEqual(self.select('23').get('next'), 'none')
        self.assertEqual(self.item('23')['readiness'], 'blocked')


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
        self.assertIn('ours_backfill', self.ops.apply_data_fixes())
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


class StaleBoardSnapshot(OpsCase):
    """Audit M-1 (critical): WF1 reads the board, WF2 lands a display write, then WF1 observes the old
    snapshot. v2's own previous values must not be read back as human edits."""

    OLD = 'Old caption from the board, DM us for edits. 🔥\n\n#reels'
    NEW = 'Owner approved caption in Slack, DM us. 🔥\n\n#reels'

    def test_owner_caption_not_reverted_by_stale_snapshot(self):
        self.make_ready('190', 'Post', caption=self.OLD)
        self.drain_monday()
        stale = board_from_projection(self.ops, '190', fmt='Post', caption=self.OLD)
        self.observe(stale)
        self.ops.submit(owner_cmd('cap2', 'update_caption', '190', text=self.NEW))
        self.drain_monday()
        r = self.observe(stale)
        self.assertEqual(r['edits'], [])
        self.assertEqual(self.item('190')['caption'], self.NEW)

    def test_owner_reschedule_not_reverted_and_new_slot_not_released(self):
        self.make_ready('191', 'Story')
        self.drain_monday()
        stale = board_from_projection(self.ops, '191')
        a = self.res('191')['slot']
        b = rules.iso(rules.instant(a) + timedelta(days=1))
        self.assertEqual(self.ops.submit(owner_cmd('mv', 'request_reschedule', '191', at=b))['state'], 'completed')
        self.drain_monday()
        r = self.observe(stale)
        self.assertEqual(r['edits'], [])
        self.assertEqual(self.res('191')['slot'], b)
        self.assertIsNone(self.item('191')['hold'])

    def test_genuine_edit_after_window_is_still_applied(self):
        self.make_ready('192', 'Post', caption=self.OLD)
        self.drain_monday()
        self.ops.submit(owner_cmd('cap2', 'update_caption', '192', text=self.NEW))
        self.drain_monday()
        self.clock.advance(25 * 60)                 # a later snapshot: the person really typed the old text again
        r = self.observe(board_from_projection(self.ops, '192', fmt='Post', caption=self.OLD))
        self.assertEqual(len(r['edits']), 1, r)
        self.assertEqual(self.item('192')['caption'], self.OLD)


class PublishingAuditCritical(OpsCase):
    """Audit P1/P2/P6: board evidence of a manual post, already-published items, imported unknown outcomes."""

    def publish_once(self, iid, worker, media=None):
        self.clock.set(rules.instant(self.res(iid)['slot']) + timedelta(seconds=30))
        cl = self.ops.claim(iid, worker)
        self.ops.container(cl['attempt_id'], worker, cl['fence'], 'C-' + worker)
        self.ops.commit(cl['attempt_id'], worker, cl['fence'], container_status='FINISHED')
        if media:
            return self.ops.result(cl['attempt_id'], worker, media_id=media)
        return self.ops.result(cl['attempt_id'], worker, error='{"code":100}', http_status=400, definitive=True)

    def test_board_posted_holds_and_releases_the_slot(self):
        self.make_ready('400', 'Story')
        self.drain_monday()
        b = board_from_projection(self.ops, '400')
        set_cell(b, 'status', 'Posted')
        r = self.observe(b)
        self.assertEqual(r['edits'][0]['state'], 'completed', r)
        self.assertEqual(json.loads(self.item('400')['hold'])['kind'], 'external_posted')
        self.assertIsNone(self.res('400'))

    def test_board_post_link_holds(self):
        self.make_ready('401', 'Story')
        r = self.ops._apply_edit('401', {'key': 'post_link', 'new': 'https://instagram.com/p/x', 'human': False,
                                         'snap': {}})
        self.assertEqual(r['state'], 'completed', r)
        self.assertIsNone(self.res('401'))

    def test_board_cannot_place_other_holds(self):
        self.make_ready('402', 'Story')
        r = self.ops.submit(Command('m-h', 'hold', 'monday', 'monday', '402', {'kind': 'owner_review', 'reason': 'x'}))
        self.assertEqual(r['state'], 'rejected')

    def test_published_item_cannot_be_reset_by_old_failed_attempt(self):
        self.make_ready('403', 'Story')
        self.publish_once('403', 'w1')                                         # definitive failure
        self.ops.submit(owner_cmd('o1', 'resolve_outcome', '403', explicit=True, outcome='not_published'))
        self.publish_once('403', 'w2', media='IG-123')
        self.assertEqual(self.item('403')['publication'], 'published')
        r = self.ops.submit(owner_cmd('o2', 'resolve_outcome', '403', explicit=True, outcome='not_published'))
        self.assertEqual((r['state'], r.get('code')), ('rejected', 'already_published'))
        self.assertEqual(self.item('403')['publication'], 'published')
        self.assertEqual(self.ops.due('w3')['work'], [])

    def test_imported_unknown_outcome_can_be_resolved_and_is_monitored(self):
        for iid, outcome, final in (('404', 'not_published', 'not_started'), ('405', 'published', 'published')):
            self.observe(monday_item(iid, status='Publishing'))
            self.assertEqual(self.item(iid)['publication'], 'outcome_unknown')
            self.assertGreaterEqual(self.ops.health()['outcome_unknown'], 1)
            self.assertIn('outcome_unknown', [f['kind'] for f in self.ops.inspect()['findings']])
            r = self.ops.submit(owner_cmd('r' + iid, 'resolve_outcome', iid, explicit=True, outcome=outcome))
            self.assertEqual(r['state'], 'completed', r)
            self.assertEqual(self.item(iid)['publication'], final)

    def test_external_posted_hold_resolved_not_published_returns_to_scheduling(self):
        self.make_ready('406', 'Story')
        self.ops._apply_edit('406', {'key': 'post_link', 'new': 'https://instagram.com/p/x', 'human': False, 'snap': {}})
        r = self.ops.submit(owner_cmd('r406', 'resolve_outcome', '406', explicit=True, outcome='not_published'))
        self.assertEqual(r['state'], 'completed', r)
        self.assertIsNone(self.item('406')['hold'])
        self.assertIsNotNone(self.res('406'))

    def test_v1_publication_receipt_holds_until_owner_answers(self):
        self.make_ready('407', 'Story')
        with self.ops.store.tx() as c:
            c.execute("INSERT INTO publications VALUES('407','v1-wf2','published','{}',0)")
        self.clock.set(rules.instant(self.res('407')['slot']) + timedelta(seconds=30))
        r = self.ops.claim('407', 'w')
        self.assertFalse(r['claimed'])
        self.assertTrue(r.get('held'))
        self.assertIsNone(self.res('407'))
        with self.ops.store.read() as c:
            self.assertEqual(c.execute("SELECT owner FROM publications WHERE item='407'").fetchone()[0], 'v1-wf2')
        r = self.ops.submit(owner_cmd('r407', 'resolve_outcome', '407', explicit=True, outcome='not_published'))
        self.assertEqual(r['state'], 'completed', r)
        res = self.res('407')
        self.assertIsNotNone(res)
        self.clock.set(rules.instant(res['slot']) + timedelta(seconds=30))
        self.assertTrue(self.ops.claim('407', 'w2')['claimed'])


class PublishingAuditHigh(OpsCase):
    """Audit P3/P4/P5 and unclearable holds."""

    def test_container_errors_stop_after_three_attempts_and_notify(self):
        self.make_ready('410', 'Story')
        self.clock.set(rules.instant(self.res('410')['slot']) + timedelta(seconds=30))
        containers = 0
        for cycle in range(130):
            w = 'wf2-%d' % cycle
            if not [x for x in self.ops.due(w)['work'] if x['kind'] == 'publish']:
                break
            cl = self.ops.claim('410', w)
            if not cl.get('claimed'):
                break
            self.ops.container(cl['attempt_id'], w, cl['fence'], 'C%d' % cycle)
            containers += 1
            self.ops.abandon(cl['attempt_id'], w, cl['fence'], 'Container status ERROR after 2 polls')
            self.clock.advance(60)
        self.assertEqual(containers, 3)
        it = self.item('410')
        self.assertEqual(json.loads(it['hold'])['kind'], 'publish_retry_limit')
        self.assertIsNone(self.res('410'))
        self.assertTrue(any('failed 3 times' in o['payload'] for o in self.outbox('slack')))
        r = self.ops.submit(owner_cmd('res410', 'resume', '410', explicit=True))
        self.assertTrue(r['resumed'], r)
        self.assertIsNotNone(self.res('410'))

    def test_ready_item_rescheduled_after_dead_pre_commit_attempt(self):
        self.make_ready('411', 'Story')
        self.clock.set(rules.instant(self.res('411')['slot']) + timedelta(seconds=30))
        self.ops.claim('411', 'w')                                    # worker dies right after claim
        self.ops.submit(owner_cmd('p', 'pause', '411'))
        self.clock.advance(300)
        self.ops.submit(owner_cmd('r', 'resume', '411', explicit=True))
        self.clock.advance(3 * 3600)
        self.ops.due('w2')                                            # abandons the dead attempt
        self.assertIsNotNone(self.res('411'))

    def test_unverifiable_source_keeps_verification_and_slot(self):
        self.make_ready('412', 'Story')
        slot = self.res('412')['slot']
        self.clock.set(rules.instant(slot) + timedelta(seconds=30))
        cl = self.ops.claim('412', 'w')
        self.ops.container(cl['attempt_id'], 'w', cl['fence'], 'C')
        r = self.ops.commit(cl['attempt_id'], 'w', cl['fence'], source_asset='unverifiable', container_status='FINISHED')
        self.assertFalse(r['committed'])
        it = self.item('412')
        self.assertEqual((it['readiness'], self.res('412')['slot']), ('ready', slot))
        self.assertIsNotNone(it['verification_id'])
        self.assertFalse(any('changed after it was verified' in o['payload'] for o in self.outbox('slack')))

    def test_rejected_edit_hold_on_active_item_cleared_by_resume(self):
        self.make_ready('413', 'Story')
        with self.ops.store.tx() as c:
            self.ops.update_item(c, '413', 'monday', 'x', hold=json.dumps({'kind': 'rejected_edit', 'reason': 'x'}))
            self.ops.release(c, self.ops.item(c, '413'), 'x')
        r = self.ops.submit(owner_cmd('r413', 'resume', '413', explicit=True))
        self.assertTrue(r['resumed'], r)
        self.assertIsNone(self.item('413')['hold'])
        self.assertIsNotNone(self.res('413'))


class SchedulingAudit(OpsCase):
    """Audit S1-S8 (scheduling, reservations, approvals)."""

    CAP = 'Sunlight sets the pace, DM us for edits. 🔥\n\n#reels'

    def board(self, iid, fmt='Post'):
        return board_from_projection(self.ops, iid, fmt=fmt, caption=self.CAP if fmt == 'Post' else '')

    def test_new_time_after_unschedule_reserves(self):                         # S1 (TypeError before)
        self.make_ready('500', 'Post')
        self.ops.submit(owner_cmd('u', 'request_reschedule', '500', at=None))
        at = rules.iso(rules.cairo_local(2026, 10, 15, 21, 0))
        r = self.ops.submit(owner_cmd('q', 'request_reschedule', '500', at=at))
        self.assertEqual((r['state'], r.get('scheduled')), ('completed', at), r)
        self.assertIsNone(self.item('500')['hold'])

    def test_clearing_publish_at_on_board_unschedules(self):                  # S2
        self.make_ready('501', 'Post')
        self.drain_monday()
        self.clock.advance(25 * 60)                       # outside the stale-snapshot window of the last write
        b = self.board('501')
        b['column_values'] = [x for x in b['column_values'] if x['id'] != board.COL['publish_at']]
        self.observe(b)
        self.assertIsNone(self.res('501'))
        self.assertEqual(json.loads(self.item('501')['hold'])['kind'], 'unscheduled')

    def test_pause_resume_auto_slot_is_only_a_preference(self):              # S3
        self.make_ready('502', 'Post', code='LIP1')
        self.ops.submit(owner_cmd('p', 'pause', '502'))
        self.make_ready('503', 'Post', code='KE2')        # takes 502's old slot
        r = self.ops.submit(owner_cmd('r', 'resume', '502', explicit=True))
        self.assertIsNotNone(r.get('scheduled'), r)
        self.assertEqual(self.res('502')['origin'], 'auto')
        self.assertFalse(any('Tell Bondok which time' in o['payload'] for o in self.outbox('slack')))

    def test_owner_pinned_slot_survives_pause_resume(self):
        self.make_ready('504', 'Post')
        at = rules.iso(rules.cairo_local(2026, 10, 15, 21, 0))
        self.ops.submit(owner_cmd('q', 'request_reschedule', '504', at=at))
        self.ops.submit(owner_cmd('p', 'pause', '504'))
        self.ops.submit(owner_cmd('r', 'resume', '504', explicit=True))
        self.assertEqual((self.res('504')['slot'], self.res('504')['owner_pinned']), (at, 1))

    def test_swap_from_unscheduled_item_can_be_approved(self):              # S4
        self.make_ready('505', 'Post', code='LIP1')
        self.make_ready('506', 'Post', code='KE2')
        other = self.res('506')['slot']
        self.ops.submit(owner_cmd('u', 'request_reschedule', '505', at=None))
        p = self.ops.submit(owner_cmd('s', 'request_reschedule', '505', at=other))
        self.assertEqual(p['state'], 'awaiting_approval', p)
        a = self.ops.submit(owner_cmd('a', 'approve_proposal', None, proposal_id=p['proposal_id']))
        self.assertEqual(a['state'], 'completed', a)
        self.assertEqual(self.res('505')['slot'], other)

    def test_requested_time_for_not_ready_item_is_not_resubmitted(self):    # S5
        self.observe(monday_item('507', fmt='Post', code='LIP1'))
        self.drain_monday()
        b = self.board('507')
        set_cell(b, 'publish_at', 'x', rules.monday_publish_at_value(rules.cairo_local(2026, 10, 12, 21, 0)))
        self.observe(b)
        v = self.item('507')['version']
        for _ in range(3):
            self.assertEqual(self.observe(b)['edits'], [])
        self.assertEqual(self.item('507')['version'], v)

    def test_not_ready_item_cannot_take_a_reserved_slot(self):              # S6
        self.make_ready('508', 'Post', code='KE2')
        other = self.res('508')['slot']
        self.observe(monday_item('509', fmt='Post', code='LIP1'))       # not ready
        r = self.ops.submit(owner_cmd('s', 'request_reschedule', '509', at=other))
        self.assertEqual((r['state'], r['code']), ('rejected', 'slot_taken'), r)
        self.assertEqual(self.res('508')['slot'], other)

    def test_rejected_board_time_is_reverted_with_notice(self):             # S7
        self.make_ready('510', 'Post')
        self.drain_monday()
        self.clock.advance(25 * 60)
        b = self.board('510')
        set_cell(b, 'publish_at', 'x', rules.monday_publish_at_value(rules.cairo_local(2026, 10, 13, 20, 0)))
        r = self.observe(b)
        self.assertEqual(r['edits'][0]['state'], 'rejected')
        jobs = [json.loads(o['payload']) for o in self.outbox('monday') if o['state'] == 'pending']
        self.assertTrue(any(board.COL['publish_at'] in j['columns'] for j in jobs), jobs)
        self.assertIn('Board change not applied', json.loads(self.item('510')['observed'])['_notice']['text'])

    def test_swap_approval_respects_near_due(self):                         # S8
        self.make_ready('511', 'Post', code='LIP1')
        self.make_ready('512', 'Post', code='KE2')
        p = self.ops.submit(owner_cmd('s', 'request_reschedule', '511', at=self.res('512')['slot']))
        self.clock.set(rules.instant(min(self.res('511')['slot'], self.res('512')['slot'])) - timedelta(minutes=5))
        a = self.ops.submit(owner_cmd('a', 'approve_proposal', None, proposal_id=p['proposal_id']))
        self.assertEqual(a['state'], 'rejected', a)


class OutboxRecovery(OpsCase):
    """Audit I1/MS5/MS7/I7: display jobs recover after outages, never land stale values last."""

    def status_on_board(self, iid):
        return json.loads(self.item(iid)['projected']).get('status')

    def test_escalated_display_job_is_retried_after_outage(self):
        self.make_ready('600', 'Story')
        self.drain_monday()
        self.ops.submit(owner_cmd('p', 'pause', '600'))
        for _ in range(6):
            for j in self.ops.outbox_take(['monday'], 'w', 10):
                self.ops.outbox_ack(j['id'], 'w', False, 'Monday 500')
            self.clock.advance(3700)
        self.assertTrue(any(o['state'] == 'escalated' for o in self.outbox('monday')))
        self.clock.advance(3700)
        self.drain_monday()                                   # Monday is back
        self.assertEqual(self.status_on_board('600'), board.LABELS['paused'])

    def test_failed_older_job_never_overwrites_newer_state(self):
        self.make_ready('601', 'Story')
        first = self.ops.outbox_take(['monday'], 'wf2-a', 10)     # Scheduled + slot, in flight
        self.ops.submit(owner_cmd('p', 'pause', '601'))            # newer job: Paused
        for j in first:
            self.ops.outbox_ack(j['id'], 'wf2-a', False, 'timeout')
        self.clock.advance(30)
        self.drain_monday()                                       # the newer Paused job lands first
        self.clock.advance(200)
        self.drain_monday()                                       # then the failed older job would be retried
        proj = json.loads(self.item('601')['projected'])
        self.assertEqual(proj.get('status'), board.LABELS['paused'])
        self.assertIsNone(proj.get('publish_at'))

    def test_older_success_landing_last_is_corrected(self):
        self.make_ready('602', 'Story')
        a = self.ops.outbox_take(['monday'], 'wf2-a', 10)
        self.ops.submit(owner_cmd('p', 'pause', '602'))
        for j in self.ops.outbox_take(['monday'], 'wf2-b', 10):
            self.ops.outbox_ack(j['id'], 'wf2-b', True)
        for j in a:                                               # the slow older write lands last
            self.ops.outbox_ack(j['id'], 'wf2-a', True)
        self.drain_monday()
        self.assertEqual(self.status_on_board('602'), board.LABELS['paused'])

    def test_worker_crashes_escalate_and_stale_failure_ack_is_ignored(self):
        self.observe(monday_item('603', fmt='Story'))
        with self.ops.store.tx() as c:
            self.ops.enqueue(c, 'editor', 'poison', {'item_id': '603'}, '603')
        for _ in range(10):
            self.ops.outbox_take(['editor'], 'w', 10)
            self.clock.advance(400)
        st = [o for o in self.outbox('editor') if o['dedupe_key'] == 'poison'][0]
        self.assertEqual(st['state'], 'escalated')
        with self.ops.store.tx() as c:
            self.ops.enqueue(c, 'editor', 'shared', {'item_id': '603'}, '603')
        j = [x for x in self.ops.outbox_take(['editor'], 'A', 10) if x['payload'] == {'item_id': '603'}][-1]
        self.clock.advance(400)
        self.ops.outbox_take(['editor'], 'B', 10)                 # A's lease expired, B re-took it
        self.assertTrue(self.ops.outbox_ack(j['id'], 'A', False, 'late')['stale_lease'])

    def test_value_toggled_back_is_written_again(self):
        self.make_ready('604', 'Story')
        self.drain_monday()
        slot = self.res('604')['slot']
        self.ops.submit(owner_cmd('p', 'pause', '604'))
        self.drain_monday()
        self.ops.submit(owner_cmd('r', 'resume', '604', explicit=True))
        self.drain_monday()
        self.assertEqual(self.res('604')['slot'], slot)
        proj = json.loads(self.item('604')['projected'])
        self.assertEqual((proj['status'], proj['publish_at']), (board.LABELS['scheduled'], slot))


class MediaAudit(OpsCase):
    """Audit MP1-MP3 and queue starvation."""

    def test_temporary_failures_keep_retrying_after_backoff(self):            # MP2
        from waset_ops import media
        prior = {'retryable': True, 'attempts': 7, 'retryAt': 0}
        self.assertIsNone(media._reuse(prior, False))
        self.assertIs(media._reuse({**prior, 'retryAt': 9e12}, False)['retryable'], True)

    def test_missing_prepared_file_is_prepared_again(self):                   # MP3
        from waset_ops import media
        saved = media.ROOT, media._launch
        media.ROOT = self.dir
        launched = []
        media._launch = lambda kind, job_id, b, attempt: launched.append(kind) or {'ready': False, 'pending': True}
        try:
            b = {'itemId': '800', 'fileId': 'id:F', 'revision': 'r', 'contentHash': 'h', 'format': 'Story', 'assetKey': 'id:F@r'}
            mid = media.media_key(b)
            with self.ops.store.tx() as c:
                c.execute('INSERT INTO media VALUES(?,?,?,?,?)', (mid, '800', str(self.dir / 'gone.mp4'), 'src', '{}'))
                c.execute('INSERT INTO jobs VALUES(?,?)', (mid, json.dumps({'ready': True, 'filePath': 'gone.mp4'})))
            self.assertFalse(media.prepare(b)['ready'])
            self.assertEqual(launched, ['prepare'])
            self.assertFalse(media.prepare(b)['ready'])           # old success never reused once the file is gone
        finally:
            media.ROOT, media._launch = saved

    def test_recheck_of_ready_item_stays_in_progress_until_verdict(self):     # MP1
        self.make_ready('801', 'Story')
        self.ops.submit(owner_cmd('rc', 'request_recheck', '801'))
        r1 = self.select('801')
        self.assertTrue(r1['media']['recheck'], r1)
        r2 = self.select('801')                               # verdict not consumed yet: still a recheck
        self.assertEqual(r2.get('next'), 'prepare', r2)
        self.assertTrue(r2['media']['recheck'])

    def test_waiting_items_do_not_starve_new_items(self):
        for i in range(1, 25):
            iid = str(820 + i)
            self.observe(monday_item(iid, fmt='Post', name=f'Item{i} LIP{i}', code=f'LIP{i}'))
            self.select(iid)
            self.topaz(iid)
            self.select(iid)
            self.wf1('prep_media', iid, result={'ready': False, 'retryable': True, 'attempts': 3, 'reason': 'x',
                                                'assetKey': 'id:FILE1@rev1', 'format': 'Post'})
        self.observe(monday_item('899', fmt='Post', name='Fresh LIP99', code='LIP99'))
        self.clock.advance(60)
        self.assertIn('899', [w['item_id'] for w in self.ops.work_queue(limit=20)])


class CaptionDraftAndRepairAudit(OpsCase):
    """Audit S9 and I6."""

    TEXT = 'Golden hour on the water 🌅\n\n' + ' '.join('#tag%d' % i for i in range(13))

    def test_draft_on_scheduled_post_keeps_approved_caption_and_slot(self):
        self.make_ready('900', 'Post')
        slot = self.res('900')['slot']
        r = self.wf1('caption_draft', '900', input_hash='h900', text=self.TEXT, model='m')
        self.assertEqual(r['draft_state'], 'pending_approval', r)
        self.assertEqual(self.item('900')['caption_state'], 'approved')
        self.assertEqual(self.ops.repair()['repairs'], [])
        self.assertEqual(self.res('900')['slot'], slot)
        a = self.ops.submit(owner_cmd('a', 'approve_proposal', None, proposal_id=r['proposal_id']))
        self.assertEqual(a['state'], 'completed', a)
        self.assertEqual(self.item('900')['caption'], self.TEXT)

    def test_rejected_draft_keeps_board_text_unapproved_not_missing(self):
        self.observe(monday_item('901', fmt='Post', caption='Old board text from v1, DM us 🔥\n\n#reels'))
        r = self.wf1('caption_draft', '901', input_hash='h901', text=self.TEXT, model='m')
        self.ops.submit(owner_cmd('rj', 'reject_proposal', None, proposal_id=r['proposal_id']))
        self.assertNotEqual(self.item('901')['caption_state'], 'missing')

    def test_repair_never_moves_a_slot_pinned_after_inspection(self):
        self.make_ready('902', 'Story')
        with self.ops.store.tx() as c:                               # an off-grid automatic slot (legacy data)
            c.execute("UPDATE ops_reservations SET slot='2026-10-10T15:07:00Z' WHERE item_id='902'")
        report = self.ops.inspect()
        self.assertTrue(any(f['action'] == 'release' for f in report['findings']))
        at = rules.iso(rules.cairo_local(2026, 10, 11, 22, 0))
        self.assertEqual(self.ops.submit(owner_cmd('pin', 'request_reschedule', '902', at=at))['state'], 'completed')
        self.ops.inspect = lambda: report                            # repair acts on the earlier snapshot
        self.ops.repair()
        self.assertEqual((self.res('902')['slot'], self.res('902')['owner_pinned']), (at, 1))


class DisplaySyncGuardCore(OpsCase):
    """Audit MS4 (core side): status jobs carry the last written value; a conflict waits for observation."""

    def test_status_job_guarded_and_conflict_retried_without_escalation(self):
        self.make_ready('950', 'Story')
        self.drain_monday()
        self.ops.submit(owner_cmd('p', 'pause', '950'))
        job = [j for j in self.ops.outbox_take(['monday'], 'w', 10) if j['payload'].get('guard')][0]
        g = job['payload']['guard'][board.COL['status']]
        self.assertEqual((g['was'], g['new']), (board.LABELS['scheduled'], board.LABELS['paused']))
        for _ in range(8):
            r = self.ops.outbox_ack(job['id'], 'w', False, 'conflict: changed on the board by a person (status)')
            self.assertTrue(r.get('conflict'), r)
            with self.ops.store.tx() as c:
                c.execute("UPDATE ops_outbox SET state='in_flight', lease_owner='w' WHERE id=?", (job['id'],))
        st = [o for o in self.outbox('monday') if o['id'] == job['id']][0]
        self.assertNotEqual(st['state'], 'escalated')


class MondaySyncMedium(OpsCase):
    """Audit MS9-MS12."""

    def test_expired_notice_cleared_by_wf3(self):                                   # MS10
        self.observe(monday_item('960', fmt='Story'))           # not scheduled: nothing else re-projects it
        self.drain_monday()
        with self.ops.store.tx() as c:
            self.ops.set_notice(c, '960', 'Requested time rejected: x')
            self.ops.project(c, '960')
        self.drain_monday()
        self.assertEqual(json.loads(self.item('960')['projected']).get('action'), 'Requested time rejected: x')
        self.clock.advance(25 * 3600)
        self.ops.repair()
        jobs = [json.loads(o['payload']) for o in self.outbox('monday') if o['state'] == 'pending']
        self.assertTrue(any(board.COL['action'] in j['columns'] for j in jobs), jobs)

    def test_missing_item_notified_once_and_resolved_when_back(self):               # MS11
        self.observe(monday_item('961', fmt='Story'), monday_item('962', fmt='Story'))
        for _ in range(3):
            self.observe(monday_item('962', fmt='Story'), complete=True)
            self.ops.repair()
            self.clock.advance(1200)
        self.assertEqual(sum('961' in o['payload'] and 'not on the board' in o['payload'] for o in self.outbox('slack')), 1)
        self.observe(monday_item('961', fmt='Story'), monday_item('962', fmt='Story'), complete=True)
        with self.ops.store.read() as c:
            self.assertIsNotNone(c.execute("SELECT resolved FROM ops_findings WHERE fingerprint='missing_on_board:961'")
                                 .fetchone()[0])

    def test_topaz_label_reset_when_file_changes(self):                             # MS9
        self.make_ready('963', 'Story')
        self.drain_monday()
        with self.ops.store.tx() as c:
            obs = json.loads(self.ops.item(c, '963')['observed'])
            obs['topaz'] = 'Topazed'
            c.execute("UPDATE ops_items SET observed=? WHERE item_id='963'", (json.dumps(obs),))
        self.select('963', 2)
        jobs = [json.loads(o['payload']) for o in self.outbox('monday') if o['state'] == 'pending']
        self.assertTrue(any(j['columns'].get(board.COL['topaz']) == {'label': 'Not yet'} for j in jobs), jobs)

    def test_legacy_posted_item_not_moved_between_groups(self):                    # MS12
        self.observe(monday_item('964', fmt='Story', status='Posted', group='group_story_custom'))
        self.drain_monday()
        b = monday_item('964', fmt='Story', status='Posted', group='group_story_custom', extra={'notes': ('x', None)})
        self.observe(b)
        jobs = [json.loads(o['payload']) for o in self.outbox('monday') if o['state'] == 'pending']
        self.assertFalse(any(j.get('group') for j in jobs), jobs)


class TransientProviderErrors(OpsCase):
    """Audit P10: a rate-limit / transient Meta error is not a permanent failure."""

    def fail_once(self, iid, err):
        self.clock.set(rules.instant(self.res(iid)['slot']) + timedelta(seconds=30))
        cl = self.ops.claim(iid, 'w')
        self.ops.container(cl['attempt_id'], 'w', cl['fence'], 'C')
        self.ops.commit(cl['attempt_id'], 'w', cl['fence'], container_status='FINISHED')
        return self.ops.result(cl['attempt_id'], 'w', error=err, http_status=400, definitive=True)

    def test_transient_error_reschedules_then_gives_up(self):
        self.make_ready('970', 'Story')
        err = json.dumps({'error': {'code': 4, 'is_transient': True, 'message': 'Application request limit reached'}})
        for _ in range(3):
            self.fail_once('970', err)
            self.assertEqual(self.item('970')['publication'], 'not_started')
            self.assertIsNotNone(self.res('970'))
        self.fail_once('970', err)
        self.assertEqual(self.item('970')['publication'], 'failed')

    def test_real_rejection_still_fails(self):
        self.make_ready('971', 'Story')
        self.fail_once('971', json.dumps({'error': {'code': 100, 'message': 'Invalid parameter'}}))
        self.assertEqual(self.item('971')['publication'], 'failed')
        self.assertIsNone(self.res('971'))

    def test_canceled_source_releases_at_claim(self):
        self.make_ready('972', 'Story')
        self.clock.set(rules.instant(self.res('972')['slot']) + timedelta(seconds=30))
        self.assertFalse(self.ops.claim('972', 'w', source_status='Canceled')['claimed'])
        self.assertIsNone(self.res('972'))
        self.assertEqual(self.item('972')['owner_state'], 'skipped')


class MediaMaintenance(OpsCase):
    """Audit MP6: prepared files of superseded versions are removed; current and referenced ones are kept."""

    def test_superseded_prepared_files_removed_after_a_week(self):
        import os
        from waset_ops import media
        saved = media.ROOT
        media.ROOT = self.dir
        try:
            (self.dir / 'media').mkdir()
            self.make_ready('980', 'Story')                          # verified media for FILE1
            self.select('980', 2)                                    # editor replaced the file: FILE1 superseded
            files = {}
            with self.ops.store.tx() as c:
                for n in (1, 2):
                    f = self.dir / 'media' / f'm{n}.mp4'
                    f.write_bytes(b'x')
                    os.utime(f, (0, 0))
                    files[n] = f
                    c.execute('INSERT OR REPLACE INTO media VALUES(?,?,?,?,?)', (f'm{n}', '980', str(f), 's', json.dumps(
                        {'assetKey': f'id:FILE{n}@rev{n}', 'format': 'Story'})))
            out = media.maintenance()
            self.assertEqual(out['removedSupersededFiles'], 1)
            self.assertFalse(files[1].exists())
            self.assertTrue(files[2].exists())                       # current version kept
        finally:
            media.ROOT = saved


class CairoTimeInput(unittest.TestCase):
    def test_gap_overlap_and_invalid_dates(self):
        with self.assertRaisesRegex(rules.RuleError, 'does not exist'):
            rules.cairo_local(2026, 4, 24, 0, 30)                    # spring forward gap (tzdata 2026)
        with self.assertRaisesRegex(rules.RuleError, 'ambiguous'):
            rules.cairo_local(2026, 10, 29, 23, 30)                  # fall back overlap
        with self.assertRaisesRegex(rules.RuleError, 'Invalid date'):
            rules.cairo_local(2026, 2, 30, 21, 0)
        self.assertEqual(rules.iso(rules.cairo_local(2026, 10, 15, 21, 0)), '2026-10-15T18:00:00Z')


class OwnerJourneySequence(OpsCase):
    """The owner's end-to-end sequence from the audit brief, with the state verified after every transition:
    schedule -> change caption -> replace video -> pause -> resume -> reschedule -> publish -> provider
    timeout -> restart -> reconcile."""

    CAP1 = 'Sunlight sets the pace, DM us for edits. 🔥\n\n#reels'
    CAP2 = 'New angle on the same view, DM us. 🌇\n\n#reels'

    def published_count(self, iid):
        with self.ops.store.read() as c:
            return c.execute("SELECT COUNT(*) FROM ops_attempts WHERE item_id=? AND stage='published'", (iid,)).fetchone()[0]

    def ready_again(self, iid, n):
        self.select(iid, n)
        self.topaz(iid, n)
        self.assertEqual(self.select(iid, n).get('next'), 'prepare')
        mid = self.add_media(iid, 'Post', n)
        self.wf1('prep_media', iid, result={'ready': True, 'mediaId': mid})

    def test_full_sequence(self):
        iid = '990'
        # 1 schedule
        self.make_ready(iid, 'Post', caption=self.CAP1)
        s1 = self.res(iid)['slot']
        rev1 = self.res(iid)['content_rev']
        # 2 caption change: same slot, re-bound to the new content revision
        self.assertEqual(self.ops.submit(owner_cmd('cap', 'update_caption', iid, text=self.CAP2))['state'], 'completed')
        r = self.res(iid)
        self.assertEqual(r['slot'], s1)
        self.assertGreater(r['content_rev'], rev1)
        # 3 editor replaces the video: authorization dropped until the new file is verified
        self.select(iid, 2)
        self.assertIsNone(self.res(iid))
        self.assertNotEqual(self.item(iid)['readiness'], 'ready')
        self.ready_again(iid, 2)
        self.assertIsNotNone(self.res(iid))
        # 4 pause: no reservation, nothing due
        self.ops.submit(owner_cmd('p', 'pause', iid))
        self.assertIsNone(self.res(iid))
        self.assertEqual(self.ops.due('w')['work'], [])
        # 5 resume: scheduled again
        self.assertTrue(self.ops.submit(owner_cmd('r', 'resume', iid, explicit=True))['resumed'])
        self.assertIsNotNone(self.res(iid))
        # 6 reschedule to an owner time
        at = rules.iso(rules.cairo_local(2026, 10, 15, 21, 0))
        self.assertEqual(self.ops.submit(owner_cmd('rs', 'request_reschedule', iid, at=at))['scheduled'], at)
        # 7 publish: claim, container, commit
        self.clock.set(rules.instant(at) + timedelta(seconds=30))
        cl = self.ops.claim(iid, 'wf2-a')
        self.assertTrue(cl['claimed'])
        self.assertEqual(cl['payload']['caption'], self.CAP2)
        self.ops.container(cl['attempt_id'], 'wf2-a', cl['fence'], 'C1')
        self.assertTrue(self.ops.commit(cl['attempt_id'], 'wf2-a', cl['fence'], source_asset='id:FILE2@rev2',
                                        container_status='FINISHED')['committed'])
        # 8 provider timeout: no result arrives; 9 restart: the worker is gone, a new WF2 run starts
        self.clock.advance(20 * 60)
        self.ops.due('wf2-b')
        self.assertEqual(self.item(iid)['publication'], 'outcome_unknown')
        self.assertFalse(self.ops.claim(iid, 'wf2-b').get('claimed'))       # never re-claimed while unknown
        self.assertFalse(any(w['kind'] == 'publish' for w in self.ops.due('wf2-c')['work']))
        # 10 reconcile: container not yet PUBLISHED stays unknown; PUBLISHED confirms exactly once
        self.assertEqual(self.ops.reconcile(cl['attempt_id'], 'FINISHED')['stage'], 'outcome_unknown')
        self.assertEqual(self.ops.reconcile(cl['attempt_id'], 'PUBLISHED')['stage'], 'published')
        self.assertEqual(self.item(iid)['publication'], 'published')
        self.assertEqual(self.published_count(iid), 1)
        self.clock.advance(3 * 86400)
        self.ops.repair()
        self.assertEqual(self.ops.due('wf2-d')['work'], [])
        self.assertEqual(self.published_count(iid), 1)


class CaptionBriefRequests(OpsCase):
    """Audit (performance): no client-brief fetch every cycle after a rejected or failed draft."""

    def test_brief_rechecked_at_most_daily_after_draft(self):
        self.observe(monday_item('995', fmt='Post'))
        q = lambda: [w for w in self.ops.work_queue(limit=50) if w['item_id'] == '995'][0]['needs_caption']
        self.assertTrue(q())
        self.wf1('caption_draft', '995', input_hash='h995', text=None, model='m', error='model down')
        self.clock.advance(3600)
        self.assertFalse(q())
        self.clock.advance(86400)
        self.assertTrue(q())


class RoundTwoRegressions(OpsCase):
    """Round-2 review of the audit fixes (#1-#7, #9, #10, #12)."""

    def test_genuine_pause_after_recent_write_is_applied(self):                      # 1
        self.make_ready('1100', 'Story')
        self.drain_monday()
        slot = rules.instant(self.res('1100')['slot'])
        self.ops.submit(owner_cmd('p', 'pause', '1100'))
        self.drain_monday()
        self.ops.submit(owner_cmd('r', 'resume', '1100', explicit=True))
        self.drain_monday()                                  # resume's status write acked now
        self.clock.advance(60)
        self.ops.run_finish('wf1', self.run_id)
        self.run_id = 'run-2'
        self.fence = self.ops.run_start('wf1', self.run_id)['fence']   # a WF1 run that starts after the ack
        b = board_from_projection(self.ops, '1100')
        set_cell(b, 'status', 'Paused')
        self.ops.observe([b], actor='service:wf1')
        self.assertEqual(self.item('1100')['owner_state'], 'paused')
        self.clock.set(slot + timedelta(seconds=30))
        self.assertFalse(self.ops.claim('1100', 'w').get('claimed'))

    def test_board_posted_holds_once_and_owner_answer_sticks(self):                  # 2
        self.make_ready('1101', 'Story')
        self.drain_monday()
        b = board_from_projection(self.ops, '1101')
        set_cell(b, 'status', 'Posted')
        for _ in range(4):
            self.observe(b)
            self.clock.advance(11 * 60)
        self.assertEqual(sum('Held' in o['payload'] for o in self.outbox('slack')), 1)
        self.ops.submit(owner_cmd('ro', 'resolve_outcome', '1101', explicit=True, outcome='not_published'))
        self.observe(b)
        self.assertIsNone(self.item('1101')['hold'])
        self.assertIsNotNone(self.res('1101'))

    def test_typed_post_link_holds_once(self):                                         # 3
        self.make_ready('1102', 'Story')
        self.drain_monday()
        b = board_from_projection(self.ops, '1102')
        set_cell(b, 'post_link', 'https://www.instagram.com/p/abc/', {'url': 'https://www.instagram.com/p/abc/'})
        for _ in range(3):
            self.observe(b)
            self.clock.advance(11 * 60)
        self.assertEqual(sum('Held' in o['payload'] for o in self.outbox('slack')), 1)

    def test_rejected_format_write_back_is_sent_unguarded(self):                       # 4
        self.make_ready('1103', 'Story')
        self.drain_monday()
        b = board_from_projection(self.ops, '1103')
        set_cell(b, 'format', 'Carousel', {'index': 9})
        self.observe(b)
        jobs = [json.loads(o['payload']) for o in self.outbox('monday') if o['state'] == 'pending'
                and o['dedupe_key'].startswith('monday-h:')]
        self.assertTrue(jobs)
        self.assertFalse(any(j.get('guard') for j in jobs))

    def test_failed_human_write_not_dropped_by_newer_display_job(self):              # 6
        self.make_ready('1104', 'Post')
        self.drain_monday()
        self.ops.submit(owner_cmd('c', 'update_caption', '1104', text='Brand new caption, DM us 🔥\n\n#reels'))
        h = [j for j in self.ops.outbox_take(['monday'], 'w', 10) if j['payload']['compare'].get('h:caption')][0]
        self.ops.submit(owner_cmd('p', 'pause', '1104'))
        self.assertFalse(self.ops.outbox_ack(h['id'], 'w', False, 'Monday 500').get('superseded'))

    def test_first_file_selection_is_not_a_recent_change(self):                       # 9
        self.observe(monday_item('1105', fmt='Story'))
        self.select('1105', 1)
        self.assertNotIn('_file_changed_at', json.loads(self.item('1105')['observed']))

    def test_generic_temporary_code_is_not_auto_retried(self):                         # 8
        from waset_ops.publish import _transient_provider_error
        self.assertFalse(_transient_provider_error(json.dumps({'error': {'code': 1, 'is_transient': True}})))
        self.assertTrue(_transient_provider_error(json.dumps({'error': {'code': 4}})))

    def test_dropbox_outage_at_slot_moves_on_without_hold(self):                       # 10
        self.make_ready('1106', 'Story')
        self.clock.set(rules.instant(self.res('1106')['slot']) + timedelta(seconds=30))
        for i in range(4):
            cl = self.ops.claim('1106', 'w%d' % i)
            if not cl.get('claimed'):
                break
            self.ops.container(cl['attempt_id'], 'w%d' % i, cl['fence'], 'C%d' % i)
            self.ops.commit(cl['attempt_id'], 'w%d' % i, cl['fence'], source_asset='unverifiable', container_status='FINISHED')
            self.clock.advance(60)
        self.assertIsNone(self.item('1106')['hold'])
        self.assertIsNotNone(self.res('1106'))

    def test_intent_phrasings(self):                                                     # 12
        import sys
        sys.path.insert(0, str(ROOT / 'bondok'))
        from bridge import explicit
        self.assertTrue(explicit('confirm_topaz', {}, 'التوباز خلاص'))
        self.assertTrue(explicit('skip', {}, 'اتخطاه'))
        self.assertTrue(explicit('change_format', {'format': 'Post'}, 'خليها بوست زي ما قلتلك'))
        self.assertFalse(explicit('resolve_outcome', {'outcome': 'published'}, 'نزلها'))
        self.assertFalse(explicit('resolve_outcome', {'outcome': 'published'}, 'ما اتنشرش'))
        self.assertTrue(explicit('resolve_outcome', {'outcome': 'published'}, 'نزلت خلاص'))
