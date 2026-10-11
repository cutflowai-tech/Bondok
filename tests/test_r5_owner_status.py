"""Owner decision 2026-10-11: "Scheduled" typed by the owner on the board is an order to publish.

Before, the system reverted it ("Status is derived by the system"), which discarded an owner decision made on
Monday (contract §1). Now: with a time on the board it is a request for that time, without one the next free slot;
media safety and reservation conflicts still apply (another reserved item is never displaced), and the board says
why when it cannot be scheduled yet. Synthetic data only.
"""
import json
import unittest
from datetime import timedelta

from support import OpsCase, board_from_projection, monday_item, rules, set_cell

from waset_ops import board


class R5_OwnerScheduledStatusIsAnOrder(OpsCase):
    def proj(self, iid):
        with self.ops.store.read() as c:
            return json.loads(self.ops.item(c, iid)['projected'] or '{}')

    def action(self, iid):
        with self.ops.store.read() as c:
            return self.ops.desired_display(c, self.ops.item(c, iid))['action'] or ''

    def unscheduled(self, iid):
        """A ready item whose time was cleared on the board (no reservation, no time)."""
        self.make_ready(iid)
        self.drain_monday()
        self.clock.advance(180)
        b = board_from_projection(self.ops, iid)
        b['column_values'] = [x for x in b['column_values'] if x['id'] != board.COL['publish_at']]
        self.observe(b)
        self.assertIsNone(self.res(iid))
        self.drain_monday()
        self.clock.advance(180)
        b = board_from_projection(self.ops, iid)                         # the board as it looks now
        for k in ('publish_at', 'post_date', 'post_time'):
            b['column_values'] = [x for x in b['column_values'] if x['id'] != board.COL[k]]
        self.assertEqual(self.observe(b)['edits'], [])                    # quiet: nothing pending
        return b

    def test_scheduled_without_a_time_takes_the_next_free_slot(self):
        b = self.unscheduled('1')
        set_cell(b, 'status', 'Scheduled')
        self.observe(b)
        self.assertIsNotNone(self.res('1'))
        self.assertIsNone(self.item('1')['hold'])
        self.drain_monday()
        self.assertEqual(self.proj('1')['status'], 'Scheduled')

    def test_scheduled_with_a_time_keeps_that_time(self):
        b = self.unscheduled('2')
        at = (self.ops.now_dt() + timedelta(days=2)).replace(hour=19, minute=0, second=0, microsecond=0)
        set_cell(b, 'status', 'Scheduled')
        set_cell(b, 'publish_at', 'x', rules.monday_publish_at_value(at))
        self.observe(b)
        self.assertEqual(self.res('2')['slot'], rules.iso(at))

    def test_media_not_ready_records_the_order_and_says_why(self):
        self.observe(monday_item('3', fmt='Story'))                     # never prepared: still checking
        self.drain_monday()
        self.clock.advance(180)
        at = (self.ops.now_dt() + timedelta(days=1)).replace(hour=19, minute=0, second=0, microsecond=0)
        b = board_from_projection(self.ops, '3')
        set_cell(b, 'status', 'Scheduled')
        set_cell(b, 'publish_at', 'x', rules.monday_publish_at_value(at))
        r = self.observe(b)
        self.assertIn(self.item('3')['requested_at'], (rules.iso(at),), r)
        self.assertIsNone(self.res('3'))                                  # an unverified video never holds a slot
        self.assertIn('ready', self.action('3').lower())
        # Once the video is verified the order is carried out at that time.
        self.contents['3'] = 'c3-'
        self.select('3', 1, 'c3-')
        self.duration('3', 30.0, 1, 'c3-')
        self.topaz('3', 1)
        self.select('3', 1, 'c3-')
        mid = self.add_media('3', 'Story', 1)
        self.wf1('prep_media', '3', result={'ready': True, 'mediaId': mid})
        self.assertEqual(self.res('3')['slot'], rules.iso(at))

    def test_a_reserved_item_is_never_displaced(self):
        self.make_ready('4')
        taken = self.res('4')['slot']
        b = self.unscheduled('5')
        set_cell(b, 'status', 'Scheduled')
        set_cell(b, 'publish_at', 'x', rules.monday_publish_at_value(rules.instant(taken)))
        self.observe(b)
        self.assertEqual(self.res('4')['slot'], taken)
        self.assertNotEqual((self.res('5') or {}).get('slot'), taken)
        self.assertTrue(self.action('5'))                                 # the board says why

    def test_scheduled_on_a_scheduled_item_changes_nothing(self):
        self.make_ready('6')
        self.drain_monday()
        slot = self.res('6')['slot']
        self.clock.advance(180)
        b = board_from_projection(self.ops, '6')
        set_cell(b, 'status', 'Scheduled')
        r = self.observe(b)
        self.assertEqual(r['edits'], [])
        self.assertEqual(self.res('6')['slot'], slot)


if __name__ == '__main__':
    unittest.main()
