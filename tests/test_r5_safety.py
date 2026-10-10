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


def proposals(case, state=None):
    with case.ops.store.read() as c:
        q = 'SELECT * FROM ops_proposals' + (' WHERE state=?' if state else '')
        return [dict(r) for r in c.execute(q, (state,) if state else ())]


def board_status(iid, label, name=None):
    return monday_item(iid, status=label, name=name or f'Item{iid} LIP12')


class R5_B2_OwnerPostedIsTerminal(OpsCase):
    """B2: Paused/Skipped item marked Posted on the board became a Resume proposal; approving it republished."""

    def _posted_after(self, iid, first):
        self.make_ready(iid)
        self.drain_monday()
        self.observe(board_status(iid, first))
        self.assertEqual(self.item(iid)['owner_state'], {'Paused': 'paused', 'Skipped': 'skipped'}[first])
        self.drain_monday()
        r = self.observe(board_status(iid, 'Posted'))
        return r

    def test_paused_or_skipped_then_posted_never_resumes_or_publishes(self):
        for iid, first in (('10', 'Paused'), ('11', 'Skipped')):
            with self.subTest(first=first):
                self._posted_after(iid, first)
                it = self.item(iid)
                self.assertEqual(it['publication'], 'published')     # owner-reported, terminal
                self.assertEqual(it['legacy_posted'], 1)
                self.assertEqual([p for p in proposals(self) if p['kind'] == 'resume'], [])
                self.assertIsNone(self.res(iid))
                # An ordinary resume afterwards (Slack or board) cannot clear the protection.
                self.ops.submit(owner_cmd(self.rid(), 'resume', iid, explicit=True))
                self.observe(board_status(iid, 'Redy For Scheduled'))
                self.assertEqual(self.item(iid)['publication'], 'published')
                self.assertIsNone(self.res(iid))
                self.clock.advance(7 * 86400)
                due = self.ops.due('wf2-x')['work']
                self.assertEqual([w for w in due if w['item_id'] == iid], [])

    def test_scheduled_item_marked_posted_releases_its_slot_once(self):
        self.make_ready('12')
        self.drain_monday()
        self.observe(board_status('12', 'Posted'))
        self.assertEqual(self.item('12')['publication'], 'published')
        self.assertIsNone(self.res('12'))
        n = len(self.outbox('slack'))
        self.observe(board_status('12', 'Posted'))                    # unchanged board: no new notice
        self.assertEqual(len(self.outbox('slack')), n)

    def test_typed_post_link_on_paused_item_is_terminal(self):
        self.make_ready('13')
        self.drain_monday()
        self.observe(board_status('13', 'Paused'))
        self.drain_monday()
        item = set_cell(board_status('13', 'Paused'), 'post_link', 'x', {'url': 'https://www.instagram.com/p/abc/'})
        self.observe(item)
        self.assertEqual(self.item('13')['publication'], 'published')
        self.assertEqual([p for p in proposals(self) if p['kind'] == 'resume'], [])


class R5_BoardResumeIsDirect(OpsCase):
    """Contract §3: a genuine owner change away from Paused/Skipped on the board resumes the item directly."""

    def test_board_label_change_resumes_without_a_proposal(self):
        self.make_ready('20')
        slot = self.res('20')['slot']
        self.drain_monday()
        self.observe(board_status('20', 'Paused'))
        self.assertIsNone(self.res('20'))
        self.drain_monday()
        self.observe(board_status('20', 'Redy For Scheduled'))
        it = self.item('20')
        self.assertEqual(it['owner_state'], 'active')
        self.assertEqual(proposals(self), [])
        self.assertIsNotNone(self.res('20'))


class R5_B7_StatusWriteGuardedWhenEmpty(OpsCase):
    """B7: when the confirmed board status was empty, the display write was unguarded and could overwrite a
    Pause typed in between."""

    def test_status_write_carries_a_guard_even_from_an_empty_cell(self):
        self.observe(monday_item('30', status=''))
        self.select('30')
        self.duration('30', 30.0)
        self.topaz('30')
        jobs = [j for j in self.ops.outbox_take(['monday'], 'w', 50) if board.COL['status'] in j['payload']['columns']]
        self.assertTrue(jobs)
        g = jobs[-1]['payload'].get('guard', {}).get(board.COL['status'])
        self.assertIsNotNone(g)
        self.assertIn(None, g['was'])


class R5_OwnerReportCorrection(OpsCase):
    """An owner can correct their own mistaken report; provider evidence and legacy Posted stay protected."""

    def test_owner_report_without_evidence_can_be_corrected_once(self):
        self.make_ready('40')
        self.drain_monday()
        self.observe(board_status('40', 'Posted'))
        r = self.ops.submit(owner_cmd(self.rid(), 'resolve_outcome', '40', explicit=True, outcome='not_published'))
        self.assertEqual(r['state'], 'completed', r)
        self.assertEqual(self.item('40')['publication'], 'not_started')
        self.assertIsNotNone(self.res('40'))

    def test_legacy_posted_and_provider_evidence_cannot_be_reset(self):
        self.observe(monday_item('41', status='Posted'))           # imported as posted (v1/legacy)
        r = self.ops.submit(owner_cmd(self.rid(), 'resolve_outcome', '41', explicit=True, outcome='not_published'))
        self.assertEqual(r['state'], 'rejected')
        self.make_ready('42')
        self.clock.set(rules.instant(self.res('42')['slot']) + timedelta(seconds=10))
        cl = self.ops.claim('42', 'w')
        self.ops.container(cl['attempt_id'], 'w', cl['fence'], 'C')
        self.ops.commit(cl['attempt_id'], 'w', cl['fence'], container_status='FINISHED')
        self.ops.result(cl['attempt_id'], 'w', media_id='IG-42')
        self.ops.submit(owner_cmd(self.rid(), 'report_published', '42', source='slack'))
        r = self.ops.submit(owner_cmd(self.rid(), 'resolve_outcome', '42', explicit=True, outcome='not_published'))
        self.assertEqual(r['state'], 'rejected')
        self.assertEqual(self.item('42')['publication'], 'published')


if __name__ == '__main__':
    unittest.main()
