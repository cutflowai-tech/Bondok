"""Round 5 scheduling/owner-control traces (A1, A5, A8, A15, A16, A17, M4, M5, M29, LOW-08, LOW-09) against the
contract in docs/OWNER_AUTHORITY_CONTRACT.md §3/§5/§7. Synthetic data only; T0 = Sat 2026-10-10 15:00 Cairo."""
import json
import unittest
from datetime import timedelta

from support import OpsCase, board_from_projection, monday_item, owner_cmd, rules, set_cell

from waset_ops import board


def cairo(d, h, m=0):
    return rules.cairo_local(2026, 10, d, h, m)


class Base(OpsCase):
    def wf3(self):
        rid = self.rid('wf3')
        try:
            return self.ops.repair(rid, self.ops.run_start('wf3', rid)['fence'])
        finally:
            self.ops.run_finish('wf3', rid)

    def hold(self, iid):
        return json.loads(self.item(iid)['hold'] or 'null') or {}

    def notices(self, needle):
        return [o for o in self.outbox('slack') if needle in o['payload']]

    def label(self, iid):
        with self.ops.store.read() as c:
            return self.ops.desired_display(c, self.ops.item(c, iid))['status']


class R5_A1_MissedOwnerTimeIsClosedAndAsked(Base):
    """A1: a missed owner-pinned slot stayed 'Scheduled' in the past for ever; the owner's fixes were refused."""

    def missed(self, iid='1'):
        self.make_ready(iid)
        at = cairo(11, 14)
        self.ops.submit(owner_cmd(self.rid(), 'request_reschedule', iid, at=rules.iso(at)))
        self.assertEqual(self.res(iid)['owner_pinned'], 1)
        self.clock.set(at + timedelta(hours=3))          # the publisher did not run over the slot
        return at

    def test_supervisor_closes_the_missed_time_with_one_question(self):
        at = self.missed()
        self.wf3()
        self.assertIsNone(self.res('1'))
        h = self.hold('1')
        self.assertEqual(h['kind'], 'missed_time')
        self.assertIn(rules.display(at), h['reason'])
        self.assertNotEqual(self.label('1'), board.LABELS['scheduled'])
        self.wf3()
        self.assertEqual(len(self.notices('passed without publication')), 1)
        self.assertEqual(self.notices('will be rescheduled'), [])

    def test_owner_fixes_work_after_a_missed_time(self):
        self.missed()
        new = rules.iso(self.ops.now_dt().replace(second=0) + timedelta(hours=5))
        r = self.ops.submit(owner_cmd(self.rid(), 'request_reschedule', '1', at=new))     # before WF3 even ran
        self.assertEqual(r['state'], 'completed', r)
        self.assertEqual(self.res('1')['slot'], new)
        self.assertIsNone(self.item('1')['hold'])

    def test_publish_it_after_a_missed_time_is_not_already_scheduled(self):
        self.missed()
        r = self.ops.submit(owner_cmd(self.rid(), 'request_publish', '1'))
        self.assertEqual(r['state'], 'completed', r)
        self.assertNotIn('Already scheduled', r.get('message', ''))
        self.assertGreater(rules.instant(self.res('1')['slot']), self.ops.now_dt())

    def test_missed_automatic_slot_is_rescheduled_with_a_truthful_notice(self):
        self.make_ready('2')
        slot = self.res('2')['slot']
        self.clock.set(rules.instant(slot) + timedelta(hours=3))
        self.wf3()
        res = self.res('2')
        self.assertIsNotNone(res)
        self.assertGreater(rules.instant(res['slot']), self.ops.now_dt())
        msgs = self.notices('missed')
        self.assertEqual(len(msgs), 1)
        self.assertIn(rules.display(rules.instant(res['slot'])), msgs[0]['payload'])


class R5_A5_OwnerRequestProtected(Base):
    """A5: an owner-requested time was given to an automatic item while the owner's item was still preparing."""

    def test_automatic_allocation_skips_owner_requested_time(self):
        first = next(rules.grid(self.ops.now_dt(), 'Story'))
        self.observe(monday_item('10', name='Item10 LIP12'))
        r = self.ops.submit(owner_cmd(self.rid(), 'request_reschedule', '10', at=rules.iso(first)))
        self.assertEqual(r['state'], 'accepted', r)
        self.make_ready('11')
        self.assertNotEqual(self.res('11')['slot'], rules.iso(first))
        self.make_ready('10')                             # becomes ready later and gets its requested time
        self.assertEqual(self.res('10')['slot'], rules.iso(first))

    def test_request_that_passes_while_preparing_is_closed_not_waiting_for_ever(self):
        at = cairo(10, 18)
        self.observe(monday_item('12', name='Item12 LIP12'))
        self.ops.submit(owner_cmd(self.rid(), 'request_reschedule', '12', at=rules.iso(at)))
        self.clock.set(at + timedelta(hours=1))
        self.make_ready('12')
        self.assertIsNone(self.res('12'))
        self.assertEqual(self.hold('12')['kind'], 'missed_time')


class R5_A15_ConstraintsTravelWithTheInstruction(Base):
    """A15: "انشرهم في موعدهم بكرا بس مش اكثر" resumed with the earliest free slot today."""

    def test_resume_for_tomorrow_never_schedules_today(self):
        self.make_ready('20')
        self.ops.submit(owner_cmd(self.rid(), 'pause', '20'))
        r = self.ops.submit(owner_cmd(self.rid(), 'resume', '20', on_date='2026-10-11'))
        self.assertEqual(r['state'], 'completed', r)
        slot = rules.instant(self.res('20')['slot'])
        self.assertEqual(slot.astimezone(rules.TZ).date().isoformat(), '2026-10-11')

    def test_not_before_tomorrow_moves_a_today_reservation(self):
        self.make_ready('21')
        self.assertEqual(rules.instant(self.res('21')['slot']).astimezone(rules.TZ).date().isoformat(), '2026-10-10')
        nb = rules.iso(cairo(11, 0))
        r = self.ops.submit(owner_cmd(self.rid(), 'set_window', '21', not_before=nb))
        self.assertEqual(r['state'], 'completed', r)
        self.assertGreaterEqual(self.res('21')['slot'], nb)

    def test_publish_tomorrow_uses_only_tomorrow(self):
        self.make_ready('22')
        self.ops.submit(owner_cmd(self.rid(), 'request_reschedule', '22', at=None))       # unscheduled
        r = self.ops.submit(owner_cmd(self.rid(), 'request_publish', '22', on_date='2026-10-11'))
        self.assertEqual(r['state'], 'completed', r)
        self.assertEqual(rules.instant(self.res('22')['slot']).astimezone(rules.TZ).date().isoformat(), '2026-10-11')


class R5_A16_ClearedTimeIsVisibleAndRecoverable(Base):
    """A16: clearing Publish at made a silent 'unscheduled' hold that request_publish refused."""

    def test_clear_notifies_once_and_publish_or_resume_recover(self):
        for iid, recover in (('30', 'request_publish'), ('31', 'resume')):
            with self.subTest(recover=recover):
                self.make_ready(iid)
                self.drain_monday()
                self.clock.advance(180)                   # the next board read comes after the write landed
                b = board_from_projection(self.ops, iid)
                b['column_values'] = [x for x in b['column_values'] if x['id'] != board.COL['publish_at']]
                self.observe(b)
                self.assertEqual(self.hold(iid)['kind'], 'unscheduled')
                self.assertIsNone(self.res(iid))
                self.observe(b)
                self.assertEqual(len([o for o in self.notices('Publish at was cleared') if o['item_id'] == iid]), 1)
                r = self.ops.submit(owner_cmd(self.rid(), recover, iid, explicit=True))
                self.assertEqual(r['state'], 'completed', r)
                self.assertIsNotNone(self.res(iid))
                self.assertIsNone(self.item(iid)['hold'])

    def test_cleared_time_is_not_recreated_automatically(self):
        self.make_ready('32')
        self.ops.submit(owner_cmd(self.rid(), 'request_reschedule', '32', at=None))
        self.clock.advance(3600)
        self.wf3()
        self.assertIsNone(self.res('32'))


class R5_A17_ReleasedReadyItemsAreRescheduled(Base):
    """A17: reauthorize() after a passed slot released it and nothing scheduled the ready item again."""

    def test_reauthorize_after_passed_slot_schedules_again(self):
        self.make_ready('40', 'Post', code='LIP40')
        slot = self.res('40')['slot']
        self.clock.set(rules.instant(slot) + timedelta(hours=3))
        self.ops.submit(owner_cmd(self.rid(), 'update_caption', '40', text='A fresh caption, DM us today. 🔥\n\n#reels'))
        res = self.res('40')
        self.assertIsNotNone(res)
        self.assertGreater(rules.instant(res['slot']), self.ops.now_dt())

    def test_supervisor_reconciles_ready_unreserved_items(self):
        self.make_ready('41')
        with self.ops.store.tx() as c:                    # e.g. released by an older release without rescheduling
            c.execute("DELETE FROM ops_reservations WHERE item_id='41'")
        self.wf3()
        self.assertIsNotNone(self.res('41'))


class R5_M5_TrueSwap(Base):
    """M5: the displaced item went to the earliest free slot, became owner-pinned, and the requester's old slot
    was left empty."""

    def test_approved_swap_exchanges_the_two_times(self):
        self.make_ready('50')
        self.make_ready('51')
        s50, s51 = self.res('50')['slot'], self.res('51')['slot']
        p = self.ops.submit(owner_cmd(self.rid(), 'request_reschedule', '50', at=s51))
        self.assertEqual(p['state'], 'awaiting_approval', p)
        self.assertIn('swapped', p['summary'])
        a = self.ops.submit(owner_cmd(self.rid(), 'approve_proposal', None, proposal_id=p['proposal_id']))
        self.assertEqual(a['state'], 'completed', a)
        self.assertEqual(self.res('50')['slot'], s51)
        self.assertEqual(self.res('51')['slot'], s50)
        self.assertEqual(self.res('51')['owner_pinned'], 0)      # an automatic slot stays automatic
        self.assertEqual(self.res('50')['owner_pinned'], 1)


class R5_M29_LateWindowStopsAtTheNextSlot(Base):
    """M29: the 2-hour late window ignored the 1-hour spacing of the 21:00/22:00 Stories."""

    def test_late_item_does_not_collide_with_the_next_reservation(self):
        for iid, h in (('60', 21), ('61', 22)):          # adjacent Story slots, one hour apart
            self.observe(monday_item(iid, name=f'Item{iid} LIP12'))
            self.ops.submit(owner_cmd(self.rid(), 'request_reschedule', iid, at=rules.iso(cairo(10, h))))
            self.make_ready(iid)
        a, b = sorted([self.res('60'), self.res('61')], key=lambda r: r['slot'])
        self.assertEqual(rules.instant(b['slot']) - rules.instant(a['slot']), timedelta(hours=1))
        self.clock.set(rules.instant(b['slot']) + timedelta(minutes=1))
        due = [w['item_id'] for w in self.ops.due('wf2')['work']]
        self.assertNotIn(a['item_id'], due)
        self.assertIn(b['item_id'], due)


class R5_LOW08_ScheduleListsOnlyPendingWork(Base):
    def test_published_items_are_not_listed_as_scheduled(self):
        self.make_ready('70')
        self.clock.set(rules.instant(self.res('70')['slot']) + timedelta(seconds=10))
        cl = self.ops.claim('70', 'w')
        self.ops.container(cl['attempt_id'], 'w', cl['fence'], 'C')
        self.ops.commit(cl['attempt_id'], 'w', cl['fence'], container_status='FINISHED')
        self.ops.result(cl['attempt_id'], 'w', media_id='IG70')
        self.assertEqual([r for r in self.ops.schedule(14) if r['item_id'] == '70'], [])
        self.assertIsNone(self.ops.item_status('70')['reservation'])


class R5_OwnerNearDueCorrection(Base):
    """Postmortem 05: the near-due rule blocked the owner from correcting an uncommitted item."""

    def test_owner_can_move_an_item_five_minutes_before_its_slot(self):
        self.make_ready('80')
        slot = rules.instant(self.res('80')['slot'])
        self.clock.set(slot - timedelta(minutes=5))
        new = rules.iso(slot + timedelta(minutes=40))
        r = self.ops.submit(owner_cmd(self.rid(), 'request_reschedule', '80', at=new))
        self.assertEqual(r['state'], 'completed', r)
        self.assertEqual(self.res('80')['slot'], new)

    def test_after_the_commitment_point_the_owner_is_told_it_may_be_in_progress(self):
        self.make_ready('81')
        self.clock.set(rules.instant(self.res('81')['slot']) + timedelta(seconds=10))
        cl = self.ops.claim('81', 'w')
        self.ops.container(cl['attempt_id'], 'w', cl['fence'], 'C')
        self.ops.commit(cl['attempt_id'], 'w', cl['fence'], container_status='FINISHED')
        r = self.ops.submit(owner_cmd(self.rid(), 'request_reschedule', '81',
                                      at=rules.iso(self.ops.now_dt().replace(second=0) + timedelta(hours=2))))
        self.assertEqual(r['state'], 'rejected')
        self.assertIn('may already be in progress', r['reason'])


class R5_ReworkRequest(Base):
    """"رجعها للمونتير" is a request for a new edit, never a resume."""

    def test_rework_holds_until_a_new_version_arrives(self):
        self.make_ready('90')
        with self.ops.store.tx() as c:                    # a source project exists, so the editor gets a subitem task
            c.execute("UPDATE ops_items SET source_item_id='9090' WHERE item_id='90'")
        r = self.ops.submit(owner_cmd(self.rid(), 'request_rework', '90', reason='colour is off'))
        self.assertEqual(r['state'], 'completed', r)
        self.assertIsNone(self.res('90'))
        self.assertEqual(self.hold('90')['kind'], 'rework')
        self.assertTrue(any('colour is off' in o['payload'] for o in self.outbox('editor')))
        # The editor delivers a new version: confirmed, prepared, verified -> hold ends, scheduled again.
        self.select('90', 2)
        self.duration('90', 30.0, 2)
        self.topaz('90', 2)
        self.select('90', 2)
        self.wf1('prep_media', '90', result={'ready': True, 'mediaId': self.add_media('90', 'Story', 2)})
        self.assertIsNone(self.item('90')['hold'])
        self.assertIsNotNone(self.res('90'))


class R5_A8_DraftApprovalDoesNotExpire(Base):
    """A8: an expired caption draft stranded the Post; Bondok could not reach the draft."""

    TEXT = 'Golden hour on the bay, DM us for the tour today. ✨\n\n' + ' '.join(f'#tag{i}' for i in range(13))

    def test_current_draft_is_approvable_after_the_interaction_expired(self):
        self.make_ready('100', 'Post', code='LIP100', approve_caption=False)
        r = self.ops.submit(__import__('support').Command(self.rid(), 'caption_draft', 'service:wf1', 'service:wf1',
                                                          '100', {'input_hash': 'h100', 'text': self.TEXT}))
        self.assertEqual(r['draft_state'], 'pending_approval', r)
        self.clock.advance(3 * 3600)
        r = self.ops.submit(owner_cmd(self.rid(), 'approve_caption_draft', '100', text=self.TEXT))
        self.assertEqual(r['state'], 'completed', r)
        self.assertEqual(self.item('100')['caption_state'], 'approved')
        self.assertIsNotNone(self.res('100'))
        r = self.ops.submit(owner_cmd(self.rid(), 'approve_caption_draft', '100'))
        self.assertEqual((r['state'], r['code']), ('rejected', 'no_draft'))


if __name__ == '__main__':
    unittest.main()
