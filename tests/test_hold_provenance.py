"""Who stopped an item (2026-10-15 owner review): an explicit owner cancellation, a board read with an empty
Publish at (Monday does not say who), or the system (missed or occupied time). Only the first is the owner's will;
the others are never shown as the owner's decision, and nothing the owner cancelled is scheduled again by itself."""
import json
import unittest

from support import OpsCase, board_cmd, owner_cmd, rules
from waset_ops.db import dumps


class HoldProvenance(OpsCase):
    def test_owner_cancellation_is_the_owners_and_is_not_rescheduled(self):
        self.make_ready('700', 'Story', code='HP1')
        r = self.ops.submit(owner_cmd('c1', 'request_reschedule', '700', explicit=True, at=None))
        self.assertEqual(r['state'], 'completed', r)
        st = self.ops.item_status('700')
        self.assertEqual(st['hold']['source'], 'owner')
        for _ in range(3):
            with self.ops.store.tx() as c:
                self.ops.try_schedule(c, '700', 'service:wf1')
            self.clock.advance(3600)
        self.assertIsNone(self.res('700'))

    def test_board_read_is_not_the_owners_decision(self):
        self.make_ready('701', 'Story', code='HP2')
        self.ops.submit(board_cmd('m1', 'request_reschedule', '701', 'publish_at', at=None))
        st = self.ops.item_status('701')
        self.assertEqual(st['hold']['source'], 'board')
        self.assertIn("not the owner's decision", st['hold']['note'])
        ex = self.ops.explain('701')
        self.assertIn("not the owner's decision", ex['reason'])

    def test_legacy_hold_marked_owner_is_traced_to_the_board_read(self):
        """Holds written before the fix said origin "owner" whoever cleared the time."""
        self.make_ready('702', 'Story', code='HP3')
        self.ops.submit(owner_cmd('t1', 'request_reschedule', '702', explicit=True,
                                  at=rules.iso(rules.cairo_local(2026, 10, 20, 14, 0))))
        self.ops.submit(board_cmd('m2', 'request_reschedule', '702', 'publish_at', at=None))
        with self.ops.store.tx() as c:            # the old record: origin owner, no set_by
            h = json.loads(self.ops.item(c, '702')['hold'])
            h = {k: v for k, v in h.items() if k not in ('set_by', 'command', 'set_at')}
            h['origin'] = 'owner'
            c.execute('UPDATE ops_items SET hold=? WHERE item_id=?', (dumps(h), '702'))
        st = self.ops.item_status('702')
        self.assertEqual(st['hold']['source'], 'board')
        self.assertEqual(st['hold']['owner_last_time_instruction']['requested'], '2026-10-20T11:00:00Z')
        self.assertIn('2026-10-20T11:00:00Z', self.ops.explain('702')['reason'])

    def test_missed_time_is_the_system(self):
        self.make_ready('703', 'Story', code='HP4')
        with self.ops.store.tx() as c:
            it = self.ops.item(c, '703')
            self.ops.release(c, it, 'test', keep_request=False)
            self.ops.update_item(c, '703', 'service:wf1', 'x', requested_at=rules.iso(self.ops.now_dt()),
                                 requested_by='owner')
            self.ops._missed_request(c, self.ops.item(c, '703'), 'service:wf1', 'it was not ready')
        st = self.ops.item_status('703')
        self.assertEqual(st['hold']['source'], 'system')


class CaptionApprovalExemption(OpsCase):
    def test_owner_can_exempt_one_post_from_caption_approval(self):
        self.make_ready('710', 'Post', code='HP10', approve_caption=False)
        with self.ops.store.tx() as c:                   # a drafted caption waiting for approval
            c.execute("UPDATE ops_items SET caption='A valid caption', caption_state='pending_approval' "
                      "WHERE item_id='710'")
        self.assertIsNone(self.res('710'))
        r = self.ops.submit(owner_cmd('w', 'waive_rule', '710', explicit=True, rule='post_caption_approval'))
        self.assertEqual(r['state'], 'completed', r)
        self.assertIsNotNone(self.res('710'))
        with self.ops.store.read() as c:
            self.assertTrue(self.ops.policy(c)['post_caption_approval'])       # the rule itself is unchanged
        rep = {x['rule']: x for x in self.ops.policy_report()['rules']}
        self.assertEqual(rep['occupied_slot_needs_approval']['source']['kind'], 'owner')
        self.assertEqual(rep['late_window_minutes']['source']['kind'], 'developer')



class ConcessiveIsNotACondition(unittest.TestCase):
    def test_even_if(self):
        import sys
        from support import ROOT
        sys.path.insert(0, str(ROOT / 'bondok'))
        import language as L
        self.assertFalse(L.markers('متنفذش نشر فعلي له حتى لو حجزناله موعد', 'متنفذش نشر فعلي له')['conditional'])
        self.assertFalse(L.markers('publish it even if it is late', 'publish it')['conditional'])
        self.assertTrue(L.markers('انشره لو جاهز', 'انشره')['conditional'])


if __name__ == '__main__':
    unittest.main()
