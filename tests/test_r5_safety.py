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


class R5_B1_CrossItemDuplicateGuard(OpsCase):
    """B1 (critical): a duplicated board item (same link/file/Topaz) published the same video twice."""

    def publish(self, iid, media='IG'):
        self.clock.set(rules.instant(self.res(iid)['slot']) + timedelta(seconds=10))
        cl = self.ops.claim(iid, 'w-' + iid)
        self.assertTrue(cl['claimed'], cl)
        self.ops.container(cl['attempt_id'], 'w-' + iid, cl['fence'], 'C-' + iid)
        r = self.ops.commit(cl['attempt_id'], 'w-' + iid, cl['fence'], container_status='FINISHED')
        self.assertTrue(r['committed'], r)
        self.ops.result(cl['attempt_id'], 'w-' + iid, media_id=media + iid)

    def test_duplicate_item_with_same_file_is_blocked_visibly(self):
        self.make_ready('50', name='Calli 3 Horizontal', content='hash')
        self.make_ready('51', name='Calli 3 Reel', content='hash')           # same file, same Topaz confirmation copied
        self.assertIsNotNone(self.res('50'))
        self.assertIsNone(self.res('51'))
        it = self.item('51')
        self.assertEqual(it['readiness'], 'blocked')
        self.assertTrue(it['block_key'].startswith('duplicate:'))
        self.assertIn('50', it['block_reason'])
        self.assertEqual(sum('Same video' in o['payload'] for o in self.outbox('slack')), 1)

    def test_same_bytes_under_another_file_id_is_a_duplicate(self):
        self.make_ready('52', content='hash')
        self.observe(monday_item('53', name='Item53 LIP12'))
        f = {'id': 'id:OTHER', 'rev': 'revX', 'content_hash': 'hash1', 'name': 'copy.mp4'}   # same bytes as FILE1
        self.wf1('prep_source', '53', file=f, url='https://www.dropbox.com/s/copy/v.mp4')
        self.wf1('prep_preflight', '53', result={'ready': True, 'duration': 30.0, 'assetKey': 'id:OTHER@revX',
                                                'contentHash': 'hash1'})
        self.ops.submit(owner_cmd(self.rid(), 'confirm_topaz', '53', explicit=True, confirmed=True,
                                  asset_key='id:OTHER@revX'))
        self.wf1('prep_source', '53', file=f, url='https://www.dropbox.com/s/copy/v.mp4')
        mid = 'media-53'
        self.add_media('53', 'Story')
        with self.ops.store.tx() as c:
            c.execute("UPDATE media SET id=?, metadata=json_set(metadata,'$.assetKey','id:OTHER@revX') WHERE item='53'",
                      (mid,))
        self.wf1('prep_media', '53', result={'ready': True, 'mediaId': mid})
        self.assertIsNone(self.res('53'))
        self.assertTrue(self.item('53')['block_key'].startswith('duplicate:'))

    def test_published_original_blocks_duplicate_even_after_removal_from_board(self):
        self.make_ready('54', content='hash')
        self.publish('54')
        self.assertEqual(self.item('54')['publication'], 'published')
        self.ops.board_missing(['55'])                           # original deleted from the board
        self.make_ready('55', content='hash')
        self.assertIsNone(self.res('55'))
        self.assertTrue(self.item('55')['block_key'].startswith('duplicate:'))

    def test_story_and_post_of_the_same_video_are_different_publications(self):
        self.make_ready('56', 'Story', content='hash')
        self.make_ready('57', 'Post', code='LIP57', content='hash')
        self.assertIsNotNone(self.res('56'))
        self.assertIsNotNone(self.res('57'))

    def test_skipped_unpublished_original_does_not_block(self):
        self.make_ready('58', content='hash')
        self.ops.submit(owner_cmd(self.rid(), 'skip', '58', explicit=True))
        self.make_ready('59', content='hash')
        self.assertIsNotNone(self.res('59'))

    def test_both_reserved_before_the_guard_only_the_earlier_publishes(self):
        self.make_ready('60', content='hash')
        first = self.res('60')['slot']
        # Simulate a second reservation made by the deployed release (no guard) for a duplicate item.
        self.observe(monday_item('61', name='Item61 LIP12'))
        self.add_media('61', 'Story')
        later = rules.iso(rules.instant(first) + timedelta(days=1))
        with self.ops.store.tx() as c:
            c.execute("UPDATE ops_items SET format='Story', asset_key='id:FILE1@rev1', content_hash='hash1', "
                      "topaz_asset='id:FILE1@rev1', readiness='ready', verification_id='media-61-1-Story' "
                      "WHERE item_id='61'")
            it = self.ops.item(c, '61')
            c.execute('INSERT INTO ops_reservations VALUES(?,?,?,?,?,?,?,?,?,?)',
                      ('61', rules.ACCOUNT, 'Story', later, it['content_rev'], self.ops.payload_fp(c, it), 'auto', 0, 0, 0))
        self.publish('60')
        self.clock.set(rules.instant(later) + timedelta(seconds=10))
        cl = self.ops.claim('61', 'w2')
        self.assertFalse(cl['claimed'])
        self.assertIn('Same video', cl['reason'])

    def test_commit_rechecks_duplicates(self):
        self.make_ready('62', content='hash')
        self.clock.set(rules.instant(self.res('62')['slot']) + timedelta(seconds=10))
        cl = self.ops.claim('62', 'w')
        self.ops.container(cl['attempt_id'], 'w', cl['fence'], 'C')
        # Meanwhile the owner reports another item with the same file as already posted.
        self.observe(monday_item('63', name='Item63 LIP12'))
        with self.ops.store.tx() as c:
            c.execute("UPDATE ops_items SET format='Story', content_hash='hash1', asset_key='id:FILE1@rev1' "
                      "WHERE item_id='63'")
        self.ops.submit(owner_cmd(self.rid(), 'report_published', '63', source='slack'))
        r = self.ops.commit(cl['attempt_id'], 'w', cl['fence'], container_status='FINISHED')
        self.assertFalse(r['committed'])
        self.assertTrue(any('Same video' in x for x in r['reasons']))

    def test_owner_reported_original_blocks_duplicate(self):
        self.make_ready('64', content='hash')
        self.ops.submit(owner_cmd(self.rid(), 'report_published', '64', source='slack'))
        self.make_ready('65', content='hash')
        self.assertIsNone(self.res('65'))


if __name__ == '__main__':
    unittest.main()
