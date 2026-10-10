"""Round 5 release-critical traces (docs/AUDIT_ROUND5_2026-10-10.md), one class per register entry.

Each test reproduces the original counterexample against the handler and asserts the owner-visible outcome
required by docs/OWNER_AUTHORITY_CONTRACT.md. Synthetic data only.
"""
import unittest
from datetime import timedelta

from support import OWNER_ID, Command, OpsCase, monday_item, owner_cmd, rules, set_cell

from waset_ops import board


class R5_A3_IncompleteValuesStayItemLocal(OpsCase):
    """A3: a date picked without a time, or a malformed link, must not stop the board observation."""

    def test_date_only_and_malformed_links_do_not_abort_other_items(self):
        self.observe(monday_item('1'), monday_item('2', code='LIP13'))
        cases = {'publish_at': {'date': '2026-10-12'}, 'published_at': {'date': '2026-10-12'},
                 'checked': {'date': '2026-10-12'}, 'dropbox': {'url': 'https://www.dropbox.com:99999/s/x'},
                 'folder': {'url': 'https://[::1'}}
        for k, v in cases.items():
            with self.subTest(column=k):
                other = set_cell(monday_item('2', code='LIP13'), 'notes', 'n-' + k)
                r = self.observe(set_cell(monday_item('1'), k, 'x', v), other)
                edits = {(e['item_id'], e['edit']) for e in r['edits']}
                self.assertIn(('2', 'notes'), edits)          # the other item still progressed
                self.assertEqual(self.item('2')['notes'], 'n-' + k)

    def test_date_only_publish_at_is_incomplete_intent_not_midnight(self):
        self.make_ready('3')
        self.assertIsNotNone(self.res('3'))
        self.drain_monday()
        r = self.observe(set_cell(monday_item('3', name='Item3 LIP12'), 'publish_at', 'x', {'date': '2026-10-12'}))
        e = [x for x in r['edits'] if x['edit'] == 'publish_at'][0]
        self.assertNotEqual(e.get('state'), 'failed')
        it = self.item('3')
        hold = __import__('json').loads(it['hold'] or 'null')
        self.assertEqual((hold or {}).get('kind'), 'incomplete_time')
        self.assertIn('2026-10-12', hold['reason'])
        self.assertIsNone(self.res('3'))                     # not published at the old or an invented time
        self.assertNotIn('T00:00', str(it['requested_at']))
        # The owner completes the time: the hold ends and the item is scheduled at that time.
        at = rules.cairo_local(2026, 10, 12, 18, 0)
        r = self.observe(set_cell(monday_item('3', name='Item3 LIP12'), 'publish_at', 'x',
                                  rules.monday_publish_at_value(at)))
        self.assertIsNone(self.item('3')['hold'])
        self.assertEqual(self.res('3')['slot'], rules.iso(at))


if __name__ == '__main__':
    unittest.main()
