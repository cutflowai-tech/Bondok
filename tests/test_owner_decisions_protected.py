"""The owner's decisions are not erased or replaced by board reads and automatic sync (owner review 2026-10-15).

Production 2026-10-10..13: board reads of the legacy Post Date/Time columns (empty cells that Bondok itself had
cleared, and stale values) were converted into owner-kind time decisions that overwrote explicit owner time
instructions and were shown to the owner as the owner's own actions. Each test pins one way that can happen.
"""
import shutil
import sqlite3
import threading
import unittest
from datetime import timedelta

from support import OpsCase, board_from_projection, owner_cmd, rules, set_cell
from waset_ops import Ops


class Base(OpsCase):
    def owner_time(self, iid, days=3, hour=19, cid=None):
        at = (self.ops.now_dt() + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)
        r = self.ops.submit(owner_cmd(cid or self.rid('own'), 'request_reschedule', iid, explicit=True,
                                      at=rules.iso(at)))
        self.assertEqual(r['state'], 'completed', r)
        return rules.iso(at)

    def board(self, iid):
        return board_from_projection(self.ops, iid, code=self.item(iid)['code'])

    def commands(self, actor_kind='monday'):
        with self.ops.store.read() as c:
            return c.execute('SELECT COUNT(*) FROM ops_commands WHERE actor_kind=?', (actor_kind,)).fetchone()[0]

    def cleared_board(self, iid, keys=('publish_at', 'post_date', 'post_time')):
        b = self.board(iid)
        for k in keys:
            set_cell(b, k, '', None)
        return b

    def evidence(self, events):
        return [self.ops.resolve_board_evidence(j, events)
                for j in self.ops.outbox_take(['board_evidence'], 'service:wf1', 100)]


class ReadsAreNotDecisions(Base):
    def test_the_same_board_read_hundreds_of_times_makes_no_decision(self):
        self.make_ready('801', 'Story', code='OD1')
        slot = self.owner_time('801')
        self.drain_monday()
        self.clock.advance(1800)
        before = self.commands()
        for _ in range(300):
            self.observe(self.board('801'))
            self.drain_monday()
            self.clock.advance(600)
        self.assertEqual(self.commands(), before)
        self.assertEqual(self.res('801')['slot'], slot)

    def test_absent_column_or_partial_response_is_not_a_cancellation(self):
        self.make_ready('802', 'Story', code='OD2')
        slot = self.owner_time('802')
        self.drain_monday()
        self.clock.advance(1800)
        b = self.board('802')
        b['column_values'] = [x for x in b['column_values'] if x['id'] not in ('date_mm7y8s9t', 'date4',
                                                                                 'hour_mm7xy9cf', 'status')]
        for _ in range(5):
            self.observe(b)
        self.assertEqual(self.res('802')['slot'], slot)
        self.assertIsNone(self.item('802')['hold'])
        self.assertEqual(self.outbox('board_evidence'), [])

    def test_an_empty_value_alone_keeps_the_instruction_and_says_so(self):
        """No edit in Monday's activity log (or only Bondok's own write): the owner's time stays, the display is
        restored and the owner is told once; it is never recorded as the owner's decision."""
        self.make_ready('803', 'Story', code='OD3')
        slot = self.owner_time('803')
        self.drain_monday()
        self.clock.advance(1800)
        self.observe(self.cleared_board('803'))
        self.assertEqual(self.res('803')['slot'], slot)
        out = self.evidence([])                                # the log shows no edit after the instruction
        self.assertFalse(out[0]['proven'])
        self.assertEqual(self.res('803')['slot'], slot)
        notes = [j for j in self.outbox('slack') if 'was not treated as your decision' in j['payload']]
        self.assertEqual(len(notes), 1)
        for _ in range(3):                                     # the board still empty next cycles: no new decision
            self.clock.advance(600)
            self.observe(self.cleared_board('803'))
        self.assertEqual(self.res('803')['slot'], slot)

    def test_bondoks_own_write_coming_back_is_not_a_person(self):
        self.make_ready('804', 'Story', code='OD4')
        slot = self.owner_time('804')
        self.drain_monday()
        with self.ops.store.read() as c:
            wrote = c.execute("SELECT MAX(updated) FROM ops_outbox WHERE kind='monday' AND item_id='804'").fetchone()[0]
        self.clock.advance(1800)
        self.observe(self.cleared_board('804'))
        out = self.evidence([{'id': 'a1', 'at': wrote + 5, 'user_id': 'shared', 'cleared': True,
                              'column_id': 'date_mm7y8s9t'}])   # the only event is Bondok's own write
        self.assertFalse(out[0]['proven'])
        self.assertEqual(self.res('804')['slot'], slot)

    def test_proven_human_clear_is_respected(self):
        self.make_ready('805', 'Story', code='OD5')
        self.owner_time('805')
        self.drain_monday()
        self.clock.advance(1800)
        self.observe(self.cleared_board('805'))
        out = self.evidence([{'id': 'a2', 'at': self.ops.now() - 30, 'user_id': 'shared', 'cleared': True,
                              'column_id': 'date_mm7y8s9t'}])
        self.assertTrue(out[0]['proven'])
        self.assertIsNone(self.res('805'))
        st = self.ops.item_status('805')
        self.assertEqual(st['hold']['source'], 'board')        # a person's board edit, not a Slack instruction


class DecisionOrder(Base):
    def test_a_stale_snapshot_arriving_after_the_owner_does_not_replace_the_instruction(self):
        self.make_ready('810', 'Story', code='OD10')
        self.drain_monday()
        self.clock.advance(1800)
        read_at = self.ops.now()
        old = self.board('810')             # read before the owner's instruction
        set_cell(old, 'publish_at', 'x', rules.monday_publish_at_value(rules.instant(self.res('810')['slot']) +
                                                                          timedelta(days=5)))
        self.clock.advance(5)
        slot = self.owner_time('810')
        self.clock.advance(5)
        r = self.observe(old, read_at=read_at)
        self.assertEqual(self.res('810')['slot'], slot)
        self.assertIn('stale_board_read', str(r['edits']))

    def test_a_new_owner_instruction_replaces_the_previous_one(self):
        self.make_ready('811', 'Story', code='OD11')
        self.owner_time('811', days=3)
        self.clock.advance(60)
        b = self.owner_time('811', days=4)
        self.assertEqual(self.res('811')['slot'], b)

    def test_redelivered_observation_and_evidence_job_apply_once(self):
        self.make_ready('812', 'Story', code='OD12')
        self.owner_time('812')
        self.drain_monday()
        self.clock.advance(1800)
        b = self.cleared_board('812')
        self.observe(b)
        self.observe(b)                                          # redelivered / re-read
        self.assertEqual(len(self.outbox('board_evidence')), 1)
        jobs = self.ops.outbox_take(['board_evidence'], 'service:wf1', 10)
        ev = [{'id': 'a3', 'at': self.ops.now() - 30, 'user_id': 'shared', 'cleared': True,
               'column_id': 'date_mm7y8s9t'}]
        first = self.ops.resolve_board_evidence(jobs[0], ev)
        again = self.ops.resolve_board_evidence(jobs[0], ev)      # the worker restarted before its ack
        self.assertTrue(first['proven'])
        self.assertTrue(again['applied'].get('duplicate'))
        self.assertEqual(self.commands(), 1)                    # held reads make no command; one proven application

    def test_sync_and_owner_commands_concurrently(self):
        """A sync loop re-reading an old board state while the owner keeps changing the time: the owner's last
        instruction wins and no stale read is applied."""
        self.make_ready('813', 'Story', code='OD13')
        self.drain_monday()
        self.clock.advance(1800)
        read_at = self.ops.now()
        stale = self.board('813')
        set_cell(stale, 'publish_at', '', None)
        self.clock.advance(5)
        stop = threading.Event()
        errors = []

        def sync():
            ops = Ops(self.dir / 'state.sqlite', clock=self.clock)
            while not stop.is_set():
                try:
                    ops.observe([stale], read_at=read_at)
                except Exception as e:  # noqa: BLE001
                    errors.append(e)
        t = threading.Thread(target=sync)
        t.start()
        last = None
        for d in range(2, 8):
            last = self.owner_time('813', days=d, cid=f'own-{d}')
        stop.set()
        t.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.res('813')['slot'], last)
        with self.ops.store.read() as c:
            applied = c.execute("SELECT COUNT(*) FROM ops_commands WHERE actor_kind='monday' AND state IN "
                                "('completed','accepted')").fetchone()[0]
        self.assertEqual(applied, 0)


class RestoreAndCorrection(Base):
    def backup(self):
        dst = self.dir / 'backup.sqlite'
        src = sqlite3.connect(self.dir / 'state.sqlite')
        out = sqlite3.connect(dst)
        with out:
            src.backup(out)
        src.close()
        out.close()
        return dst

    def test_restoring_a_backup_revives_no_cancelled_decision_and_republishes_nothing(self):
        self.make_ready('820', 'Story', code='OD20')
        self.owner_time('820')
        self.drain_monday()
        path, at = self.backup(), self.ops.now()
        self.clock.advance(600)
        self.ops.submit(owner_cmd('cancel', 'request_reschedule', '820', explicit=True, at=None))   # lost by the restore
        self.drain_monday()
        for suffix in ('', '-wal', '-shm'):
            p = self.dir / ('state.sqlite' + suffix)
            if p.exists():
                p.unlink()
        shutil.copy(path, self.dir / 'state.sqlite')
        self.ops.mark_restored(at)
        self.assertIsNotNone(self.res('820'))                     # the old store still has the reservation ...
        self.assertTrue(self.ops.due('w', 5).get('restore_gap'))  # ... but publishing waits for reconciliation
        self.clock.advance(1800)
        self.observe(self.cleared_board('820'))                   # the board shows the owner's later cancellation
        self.assertFalse([w for w in self.ops.due('w', 5).get('work') or [] if w.get('item_id') == '820'])
        # the owner closes the post-restore check; the next read applies what the board shows, with evidence:
        # Bondok's write of the cancellation is not in the restored outbox, so the log shows a non-Bondok clear.
        self.ops.submit(owner_cmd('gap', 'resolve_restore_gap', None, explicit=True))
        self.clock.advance(600)
        self.observe(self.cleared_board('820'))
        self.evidence([{'id': 'a4', 'at': at + 610, 'user_id': 'shared', 'cleared': True,
                        'column_id': 'date_mm7y8s9t'}])
        self.assertIsNone(self.res('820'))

    def overwritten(self, iid, days):
        """The production pattern: an owner time, then a board read that cleared it (before the fix)."""
        self.make_ready(iid, 'Story', code='OD' + iid)
        t = self.owner_time(iid, days=days)
        self.clock.advance(600)
        from waset_ops.core import Command
        r = self.ops.submit(Command(f'monday:{iid}:post_date:1:0:x', 'request_reschedule', 'monday', 'monday', iid,
                                    {'at': None}, auth={'board': {'source': 'observation', 'column': 'post_date'}}))
        self.assertEqual(r['state'], 'completed', r)
        self.assertIsNone(self.res(iid))
        return t

    def test_correction_restores_the_owner_time_and_is_idempotent(self):
        t = self.overwritten('830', days=4)
        plan = self.ops.correction_plan({'830': []})
        entry = [e for e in plan['corrections'] if e['item_id'] == '830']
        self.assertEqual(entry[0]['kind'], 'restore_owner_time')
        out = self.ops.apply_corrections(plan, 'owner chat 2026-10-15')
        self.assertEqual(out[0]['state'], 'completed', out)
        self.assertEqual(self.res('830')['slot'], t)
        self.assertEqual(self.res('830')['origin'], 'owner')
        with self.ops.store.read() as c:
            snap = c.execute('SELECT * FROM ops_items WHERE item_id=?', ('830',)).fetchone()
            corr = c.execute("SELECT COUNT(*) FROM ops_audit WHERE kind='attribution_correction'").fetchone()[0]
            original = c.execute("SELECT COUNT(*) FROM ops_commands WHERE id LIKE 'monday:830:%'").fetchone()[0]
        self.assertEqual((corr, original), (1, 1))                # the error stays recorded beside its correction
        again = self.ops.apply_corrections(plan, 'owner chat 2026-10-15')
        self.assertTrue(again[0]['duplicate'])
        self.assertEqual([e for e in self.ops.correction_plan({'830': []})['corrections']
                          if e['item_id'] == '830'], [])
        with self.ops.store.read() as c:
            self.assertEqual(dict(c.execute('SELECT * FROM ops_items WHERE item_id=?', ('830',)).fetchone()),
                             dict(snap))

    def test_correction_of_a_passed_time_applies_the_missed_time_policy(self):
        self.make_ready('831', 'Story', code='OD31')
        t = self.owner_time('831', days=1)
        self.clock.advance(600)
        from waset_ops.core import Command
        self.ops.submit(Command('monday:831:post_date:1:0:x', 'request_reschedule', 'monday', 'monday', '831',
                                {'at': None}, auth={'board': {'source': 'observation', 'column': 'post_date'}}))
        self.clock.advance(3 * 86400)                             # the owner's time passed meanwhile
        self.ops.apply_corrections(self.ops.correction_plan({'831': []}), 'owner chat 2026-10-15')
        self.assertIsNone(self.res('831'))                        # not published now
        import json
        hold = json.loads(self.item('831')['hold'])
        self.assertEqual((hold['kind'], hold['time']), ('missed_time', t))
        self.assertEqual([e for e in self.ops.correction_plan({'831': []})['corrections'] if e['item_id'] == '831'],
                         [])                                       # planned once: a second run changes nothing

    def test_correction_into_an_occupied_slot_keeps_the_request_and_displaces_nobody(self):
        t = self.overwritten('832', days=5)
        self.make_ready('833', 'Story', code='OE33')
        self.ops.submit(owner_cmd('o833', 'request_reschedule', '833', explicit=True, at=t))
        self.assertEqual(self.res('833')['slot'], t)
        self.ops.apply_corrections(self.ops.correction_plan({'832': []}), 'owner chat 2026-10-15')
        self.assertEqual(self.res('833')['slot'], t)               # the other item keeps its slot
        self.assertIsNone(self.res('832'))
        self.assertEqual(self.item('832')['requested_at'], t)      # the request is kept, conflict shown

    def test_an_edit_after_the_instruction_is_kept_not_corrected(self):
        self.overwritten('834', days=4)
        plan = self.ops.correction_plan({'834': [{'id': 'p', 'at': self.ops.now() + 1, 'cleared': True}]})
        self.assertEqual([e['item_id'] for e in plan['kept']], ['834'])
        self.assertFalse([e for e in plan['corrections'] if e['item_id'] == '834'])

    def test_without_the_activity_log_nothing_is_guessed(self):
        self.overwritten('835', days=4)
        plan = self.ops.correction_plan(None)
        self.assertEqual([e['item_id'] for e in plan['unresolved']], ['835'])


class RuntimeEvidence(unittest.TestCase):
    """The runtime answers held board values from Monday's activity log on its display-sync cycle."""

    def test_sync_cycle_resolves_held_values(self):
        from test_runtime import RuntimeCase
        case = RuntimeCase('run')
        case.setUp()
        try:
            iid = case.ready_item()
            slot = case.res(iid)['slot']
            case.runner.sync_cycle()                        # the display shows the reserved time
            case.clock.advance(1800)
            b = case.monday.board.items[iid]
            from waset_ops import board as B
            b['column_values'] = [x for x in b['column_values'] if x['id'] != B.COL['publish_at']] + \
                [{'id': B.COL['publish_at'], 'text': '', 'value': None}]
            case.runner.prepare_cycle()                     # the read is held, not applied
            self.assertEqual(case.res(iid)['slot'], slot)
            case.monday.fail['activity'] = [__import__('waset_runtime.monday', fromlist=['MondayError']).MondayError(
                'activity log unavailable')]
            case.runner.sync_cycle()                        # log unreadable: nothing decided, retried later
            self.assertEqual(case.res(iid)['slot'], slot)
            case.clock.advance(600)
            case.runner.sync_cycle()                        # log readable, no edit: kept and the owner is told
            self.assertEqual(case.res(iid)['slot'], slot)
            self.assertTrue(any('was not treated as your decision' in j['payload'] for j in case.outbox('slack')))
        finally:
            case.tearDown()
            case.doCleanups()


class AcceptanceIsolation(Base):
    """Test items are never sent to Instagram while the owner's commands are tested for real (2026-10-16)."""

    def isolate(self):
        from waset_ops.core import Command
        r = self.ops.submit(Command('iso', 'set_isolation', 'service:acceptance', 'service:acceptance', None,
                                    {'code_prefixes': ['ACC'], 'name_prefixes': ['Acceptance '],
                                     'reason': 'acceptance test item'}))
        self.assertEqual(r['state'], 'completed', r)

    def test_isolated_item_is_scheduled_but_never_claimed_or_committed(self):
        self.make_ready('840', 'Story', code='ACC40')
        self.make_ready('841', 'Story', code='RL1')
        self.isolate()
        slot = rules.instant(self.res('840')['slot'])
        self.clock.set(slot + timedelta(seconds=5))
        due = [w['item_id'] for w in self.ops.due('w', 10).get('work') or []]
        self.assertNotIn('840', due)
        self.assertFalse(self.ops.claim('840', 'w')['claimed'])
        self.assertIn('never sent', self.ops.claim('840', 'w')['reason'])
        # the owner lifting a do-not-publish directive does not lift the isolation
        self.ops.submit(owner_cmd('b', 'publish_block', '840', explicit=True, reason='test'))
        self.ops.submit(owner_cmd('a', 'publish_allow', '840', explicit=True))
        self.assertFalse(self.ops.claim('840', 'w')['claimed'])
        self.assertTrue(self.ops.item_status('840')['test_isolation'])

    def test_cleanup_releases_only_test_reservations(self):
        self.make_ready('842', 'Story', code='ACC42')
        self.make_ready('843', 'Story', code='RL2')
        self.isolate()
        from waset_ops.core import Command
        r = self.ops.submit(Command('rel', 'release_isolated', 'service:acceptance', 'service:acceptance', None, {}))
        self.assertEqual([x['item_id'] for x in r['released']], ['842'])
        self.assertIsNone(self.res('842'))
        self.assertIsNotNone(self.res('843'))                  # real content keeps its slot


class ReadSpanningTheOwnersCommand(unittest.TestCase):
    """A Monday read that STARTS before the owner's command and COMPLETES after it does not erase the command."""

    def test_prepare_cycle_read_in_flight_during_the_owner_command(self):
        from test_runtime import RuntimeCase
        from waset_ops import board as B
        case = RuntimeCase('run')
        case.setUp()
        try:
            iid = case.ready_item()
            case.runner.sync_cycle()
            case.clock.advance(1800)
            stale = case.monday.board.items[iid]
            stale['column_values'] = [x for x in stale['column_values'] if x['id'] != B.COL['publish_at']] + \
                [{'id': B.COL['publish_at'], 'text': '', 'value': None}]          # what the board showed at read start
            owner_at = (case.ops.now_dt() + timedelta(days=6)).replace(hour=19, minute=0, second=0, microsecond=0)
            real = case.monday.social_snapshot

            def read_spanning():
                snap = real()                                  # the read has started (and captured the old state) ...
                case.clock.advance(3)
                r = case.ops.submit(owner_cmd('own-mid', 'request_reschedule', iid, explicit=True,
                                              at=rules.iso(owner_at)))       # ... the owner instructs meanwhile ...
                assert r['state'] == 'completed', r
                case.clock.advance(3)
                return snap                                     # ... and the read completes after the command
            case.monday.social_snapshot = read_spanning
            out = case.runner.prepare_cycle()
            self.assertEqual(case.res(iid)['slot'], rules.iso(owner_at))
            self.assertEqual(case.outbox('board_evidence'), [])   # refused as stale, not even held
            self.assertIn('edits', out)
        finally:
            case.tearDown()
            case.doCleanups()


class TestCapacityIsolation(AcceptanceIsolation):
    def test_test_reservations_never_take_or_move_real_slots(self):
        self.isolate()
        self.make_ready('850', 'Story', code='ACC50')                 # test item first: takes the first grid slot ...
        test_slot = self.res('850')['slot']
        self.assertTrue(self.res('850')['account'].endswith(':acceptance'))
        self.make_ready('851', 'Story', code='RL51')                  # ... real content still gets that same slot
        self.assertEqual(self.res('851')['slot'], test_slot)
        self.assertEqual(self.res('851')['account'], rules.ACCOUNT)
        self.make_ready('854', 'Story', code='RL54')
        real_slot = self.res('854')['slot']
        # a test request for a time held by real content: reported, no swap, nothing moves
        r = self.ops.submit(owner_cmd('t', 'request_reschedule', '850', explicit=True, at=real_slot))
        self.assertIn('real content', str(r))
        self.assertEqual(self.res('854')['slot'], real_slot)
        self.assertEqual(self.res('850')['slot'], test_slot)
        with self.ops.store.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM ops_proposals WHERE state='pending'").fetchone()[0], 0)

    def test_due_test_item_records_whether_the_directive_would_have_stopped_it(self):
        self.isolate()
        self.make_ready('852', 'Story', code='ACC52')
        slot = rules.instant(self.res('852')['slot'])
        self.clock.set(slot + timedelta(seconds=5))
        self.ops.due('w', 10)
        with self.ops.store.read() as c:
            row = c.execute("SELECT detail FROM ops_audit WHERE item_id='852' AND kind='test_send_prevented'").fetchone()
        import json
        self.assertFalse(json.loads(row[0])['owner_directive_active'])   # no directive: the test reports the gap

    def test_isolation_moves_existing_test_reservations_out_of_real_capacity(self):
        self.make_ready('853', 'Story', code='ACC53')
        self.assertEqual(self.res('853')['account'], rules.ACCOUNT)
        self.isolate()
        self.assertTrue(self.res('853')['account'].endswith(':acceptance'))


class StaleCheckScope(Base):
    def test_board_pause_is_not_lost_to_an_unrelated_owner_command_during_the_read(self):
        """Task-path audit H1: an owner Slack note arriving during the read must not make a typed Pause stale."""
        self.make_ready('870', 'Story', code='SC1')
        self.drain_monday()
        self.clock.advance(1800)
        read_at = self.ops.now()
        b = self.board('870')
        set_cell(b, 'status', 'Paused', {'index': 2})
        self.clock.advance(3)
        self.ops.submit(owner_cmd('note', 'board_set_field', '870', explicit=True, field='notes', value='x'))
        self.clock.advance(3)
        self.observe(b, read_at=read_at)
        self.assertEqual(self.item('870')['owner_state'], 'paused')

    def test_board_time_older_than_the_owners_time_instruction_is_refused(self):
        self.make_ready('871', 'Story', code='SC2')
        self.drain_monday()
        self.clock.advance(1800)
        read_at = self.ops.now()
        b = self.board('871')
        set_cell(b, 'publish_at', 'x', rules.monday_publish_at_value(rules.instant(self.res('871')['slot']) +
                                                                       timedelta(days=9)))
        self.clock.advance(3)
        slot = self.owner_time('871', days=5)
        self.clock.advance(3)
        self.observe(b, read_at=read_at)
        self.assertEqual(self.res('871')['slot'], slot)


class IsolationHardening(AcceptanceIsolation):
    def test_a_test_reservation_never_publishes_even_after_isolation_ends_or_the_code_changes(self):
        self.isolate()
        self.make_ready('880', 'Story', code='ACC80')
        slot = rules.instant(self.res('880')['slot'])
        with self.ops.store.tx() as c:                        # renamed out of the pattern by someone
            c.execute("UPDATE ops_items SET code='RL80', name='Renamed' WHERE item_id='880'")
        self.clock.set(slot + timedelta(seconds=5))
        self.assertNotIn('880', [w['item_id'] for w in self.ops.due('w', 10).get('work') or []])
        self.assertFalse(self.ops.claim('880', 'w')['claimed'])

    def test_clearing_isolation_releases_test_reservations_first(self):
        self.isolate()
        self.make_ready('881', 'Story', code='ACC81')
        self.assertIsNotNone(self.res('881'))
        from waset_ops.core import Command
        r = self.ops.submit(Command('clr', 'set_isolation', 'service:acceptance', 'service:acceptance', None, {}))
        self.assertEqual(r['released'], ['881'])
        self.assertIsNone(self.res('881'))

    def test_cleanup_is_not_undone_by_the_scheduler(self):
        self.isolate()
        self.make_ready('882', 'Story', code='ACC82')
        from waset_ops.core import Command
        self.ops.submit(Command('rel2', 'release_isolated', 'service:acceptance', 'service:acceptance', None, {}))
        for _ in range(3):
            with self.ops.store.tx() as c:
                self.ops.try_schedule(c, '882', 'service:wf3')
            self.clock.advance(3600)
        self.assertIsNone(self.res('882'))

    def test_prefix_matches_only_prefix_and_digits(self):
        self.isolate()
        self.make_ready('883', 'Story', code='ACM5')
        with self.ops.store.tx() as c:
            self.assertIsNone(self.ops.send_isolated(c, '883'))
            c.execute("UPDATE ops_items SET code='ACCOUNT1', name='Real 1' WHERE item_id='883'")
            self.assertIsNone(self.ops.send_isolated(c, '883'))
            c.execute("UPDATE ops_items SET code='acc12' WHERE item_id='883'")
            self.assertIsNotNone(self.ops.send_isolated(c, '883'))

    def test_test_items_and_real_content_never_block_each_other_as_duplicates(self):
        self.isolate()
        self.make_ready('885', 'Story', code='ACC85', content='same-')
        self.make_ready('886', 'Story', code='RL86', content='same-')
        self.assertIsNotNone(self.res('886'))


class RestoreDetection(Base):
    def test_an_older_store_opens_the_restore_gap_by_itself(self):
        from waset_runtime.service import detect_restore, write_generation
        marker = self.dir / 'store-generation.json'
        self.make_ready('890', 'Story', code='RD1')
        write_generation(self.ops, marker)
        import json as _json
        seen = _json.loads(marker.read_text())['last_command_at']
        marker.write_text(_json.dumps({'last_command_at': seen + 3600}))      # the host saw later commands
        gap = detect_restore(self.ops, marker)
        self.assertIsNotNone(gap)
        with self.ops.store.read() as c:
            self.assertTrue(self.ops.restore_gap(c))
        self.assertIsNone(detect_restore(self.ops, marker))                    # once

    def test_board_system_columns_are_not_decisions_while_the_gap_is_open(self):
        self.make_ready('891', 'Story', code='RD2')
        self.drain_monday()
        self.clock.advance(1800)
        slot = self.res('891')['slot']
        self.ops.mark_restored(self.ops.now())
        b = self.board('891')
        set_cell(b, 'status', 'Publishing', {'index': 3})
        set_cell(b, 'publish_at', '', None)
        self.observe(b)
        self.assertEqual(self.res('891')['slot'], slot)
        with self.ops.store.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM ops_commands WHERE actor_kind='monday'").fetchone()[0], 0)

    def test_gap_older_than_the_story_listing_waits_for_the_owner(self):
        self.ops.mark_restored(self.ops.now() - 3 * 86400)
        r = self.ops.restore_gap_check([], stories_cover_since=self.ops.now() - 86400)
        self.assertTrue(r['open'])
        self.assertTrue(r['stories_not_verifiable'])


class PreparedCopiesCleanup(unittest.TestCase):
    def test_published_copies_go_after_30_days_current_ones_stay(self):
        from test_runtime import RuntimeCase
        case = RuntimeCase('run')
        case.setUp()
        try:
            iid = case.ready_item()
            case.at_slot(iid)
            case.runner.publish_cycle()
            self.assertEqual(case.item(iid)['publication'], 'published')
            prepared = [k for k in case.dbx.files if k.startswith('/social media/prepared/')]
            self.assertEqual(len(prepared), 1)
            def run():
                with case.ops.store.tx() as c:
                    c.execute("DELETE FROM ops_meta WHERE key='prepared_cleanup_at'")
                return case.runner.prepared_cleanup()
            self.assertEqual(run(), {'deleted': 0})                                # recent: kept
            self.assertIsNone(case.runner.prepared_cleanup())                      # once a day only
            case.clock.advance(31 * 86400)
            self.assertEqual(run(), {'deleted': 1})
            self.assertFalse([k for k in case.dbx.files if k.startswith('/social media/prepared/')])
        finally:
            case.tearDown()
            case.doCleanups()


if __name__ == '__main__':
    unittest.main()
