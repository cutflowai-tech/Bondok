"""Round 5 caption/brief and status-precedence traces (M18, LOW-13). Synthetic data only."""
import json
import unittest
from datetime import timedelta

from support import OpsCase, board_from_projection, monday_item, owner_cmd, rules


class R5_M18_NoBriefIsRememberedNotReRead(OpsCase):
    """M18: a Post whose source item has no usable brief was offered for a brief read (a Monday request) on every
    WF1 visit, forever, and the owner was never told why no caption draft appeared."""

    def needs(self):
        return [w for w in self.ops.work_queue(limit=50) if w['item_id'] == '995'][0]['needs_caption']

    def brief_msgs(self):
        return [json.loads(o['payload'])['text'] for o in self.outbox('slack') if 'brief' in o['payload'].lower()]

    def test_many_no_brief_cycles_give_one_request_and_bounded_reads(self):
        self.observe(monday_item('995', fmt='Post'))
        reads = 0
        for _ in range(6 * 24):                                       # one day of WF1 visits every 10 minutes
            if self.needs():
                reads += 1
                r = self.ops.caption_needed('995', 'Calli 3', '   ')
                self.assertFalse(r['needed'])
            self.clock.advance(600)
        self.assertLessEqual(reads, 5)                                # bounded revisit, not 144 reads
        self.assertGreaterEqual(reads, 2)                             # a brief added later is still found
        msgs = self.brief_msgs()
        self.assertEqual(len(msgs), 1, msgs)                          # one actionable request
        self.assertIn('995', msgs[0])
        self.assertIn('caption', msgs[0].lower())

    def test_a_brief_added_later_gets_a_draft(self):
        self.observe(monday_item('995', fmt='Post'))
        self.assertFalse(self.ops.caption_needed('995', 'Calli 3', '')['needed'])
        self.clock.advance(7 * 3600)
        self.assertTrue(self.needs())
        self.assertTrue(self.ops.caption_needed('995', 'Calli 3', 'Sunset reel, warm tone')['needed'])

    def test_a_draft_asked_for_anyway_is_not_blocked_by_the_marker(self):
        self.observe(monday_item('995', fmt='Post'))
        h = self.ops.caption_needed('995', 'Calli 3', '')['input_hash']
        r = self.wf1('caption_draft', '995', input_hash=h, text='Golden hour in Siwa. DM us. \U0001F305\n\n' + ' '.join(f'#tag{i}' for i in range(12)),
                     model='m')
        self.assertEqual(r['draft_state'], 'pending_approval', r)


class R5_LOW13_PausedWithOwnerReportedPost(OpsCase):
    """LOW-13: a paused item whose post link shows it may already be on Instagram displayed only 'Paused', hiding
    the open question; the explanation must be truthful and the display must never act as a Resume."""

    def proj(self, iid):
        with self.ops.store.read() as c:
            return json.loads(self.ops.item(c, iid)['projected'] or '{}')

    def test_hold_is_explained_and_the_pause_is_kept(self):
        self.make_ready('406', 'Story')
        with self.ops.store.tx() as c:                                # a v1 receipt: it may already be posted
            c.execute("INSERT INTO publications VALUES('406','v1-wf2','published','{}',0)")
        self.clock.set(rules.instant(self.res('406')['slot']) + timedelta(seconds=30))
        self.assertTrue(self.ops.claim('406', 'w').get('held'))
        self.assertEqual(self.ops.submit(owner_cmd('p406', 'pause', '406'))['state'], 'completed')
        self.drain_monday()
        p = self.proj('406')
        self.assertEqual(p['status'], 'Needs Review')
        self.assertIn('paused', (p.get('action') or '').lower())
        self.observe(board_from_projection(self.ops, '406'))          # our own display write coming back
        self.assertEqual(self.item('406')['owner_state'], 'paused')
        self.assertIsNone(self.res('406'))
        r = self.ops.submit(owner_cmd('r406', 'resolve_outcome', '406', explicit=True, outcome='not_published'))
        self.assertEqual(r['state'], 'completed', r)
        self.drain_monday()
        self.assertEqual(self.item('406')['owner_state'], 'paused')   # answering the question is not a Resume
        self.assertIsNone(self.res('406'))
        self.assertEqual(self.proj('406')['status'], 'Paused')


if __name__ == '__main__':
    unittest.main()
