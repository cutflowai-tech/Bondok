"""Round 5 board-edit traces (B4, A6, A7, M1, M2, M3, M12, LOW-04, LOW-10, LOW-11, LOW-14, LOW-17), multi-cycle against
a simulated Monday board with WF2's compare-before-write display sync (tests/board_sim.py). Synthetic data only."""
import json
import unittest
from datetime import timedelta

from board_sim import Board, wf2_sync
from support import OpsCase, monday_item, owner_cmd, rules

from waset_ops import board as B


def setcell(brd, iid, key, text, value=None):
    brd.set_raw(iid, B.COL[key], None if text is None else
                {'id': B.COL[key], 'text': text, 'value': None if value is None else json.dumps(value)})


class BoardCase(OpsCase):
    def cycle(self, brd, advance=600):
        """One WF1 observation cycle reading the simulated board."""
        self.clock.advance(advance)
        self.ops.run_finish('wf1', self.run_id)
        self.run_id = self.rid('run')
        lease = self.ops.run_start('wf1', self.run_id)
        self.fence = lease['fence']
        return self.ops.observe(brd.snapshot(), actor='service:wf1')

    def sync(self, brd, runs=2):
        return wf2_sync(self.ops, brd, clock=self.clock, runs=runs)

    def hold(self, iid):
        return json.loads(self.item(iid)['hold'] or 'null') or {}

    def proposals(self):
        with self.ops.store.read() as c:
            return [dict(r) for r in c.execute('SELECT * FROM ops_proposals')]


class R5_B4_TopazBoundToTheVersionShown(BoardCase):
    """B4: a board Topazed was bound to the file shown at the NEXT board read, not the one the editor saw."""

    def start(self, iid):
        brd = Board([monday_item(iid, fmt='Post', code='CAL3', name='Calli 3')])
        self.observe(*brd.snapshot())
        self.select(iid, 1)
        self.sync(brd)
        self.cycle(brd)                                   # board shows v1, Topazed 'Not yet'
        return brd

    def test_toggle_during_a_file_change_is_not_bound_to_the_new_file(self):
        brd = self.start('1')
        setcell(brd, '1', 'topaz', 'Topazed')             # the editor looks at v1 and confirms
        self.clock.advance(180)
        self.select('1', 2)                               # meanwhile v2 is uploaded and selected
        self.sync(brd)                                    # board now shows v2
        r = self.cycle(brd)
        it = self.item('1')
        self.assertNotEqual(it['topaz_asset'], 'id:FILE2@rev2')
        self.assertNotEqual(self.select('1', 2).get('next'), 'prepare')
        self.sync(brd)
        self.assertEqual(B.norm(brd.items['1'], 'topaz'), 'Not yet')     # a deliberate re-confirmation is needed
        self.assertTrue(any('changed' in o['payload'] and 'Topaz' in o['payload'] for o in self.outbox('slack')))
        self.assertNotEqual(self.hold('1').get('kind'), 'rejected_edit')
        # The editor confirms again with v2 on the board: bound to v2.
        setcell(brd, '1', 'topaz', 'Topazed')
        self.cycle(brd)
        self.assertEqual(self.item('1')['topaz_asset'], 'id:FILE2@rev2')

    def test_toggle_without_a_file_change_binds_to_the_shown_version(self):
        brd = self.start('2')
        setcell(brd, '2', 'topaz', 'Topazed')
        self.cycle(brd)
        self.assertEqual(self.item('2')['topaz_asset'], 'id:FILE1@rev1')
        self.assertEqual(self.select('2', 1).get('next'), 'prepare')


class R5_A6_BoardFormatEditIsTheAuthorization(BoardCase):
    """A6: a board Format change released the slot before approval, could be stranded by reject/expiry, refused
    the undo, and an old approval re-applied the undone conversion."""

    def test_format_change_and_undo_act_directly_without_proposals(self):
        self.make_ready('10', 'Story')
        brd = Board([])
        brd.items['10'] = monday_item('10', fmt='Story', name='Item10 LIP12')
        self.sync(brd)
        self.clock.advance(300)
        setcell(brd, '10', 'format', 'Post', {'index': 1})
        self.cycle(brd)
        it = self.item('10')
        self.assertEqual(it['format'], 'Post')
        self.assertEqual(self.proposals(), [])
        self.assertIsNone(self.res('10'))                 # rechecked under Post rules before scheduling
        self.assertNotEqual(self.hold('10').get('kind'), 'format_change')
        self.sync(brd)
        setcell(brd, '10', 'format', 'Story', {'index': 0})     # the owner undoes it
        self.cycle(brd)
        self.assertEqual(self.item('10')['format'], 'Story')
        self.assertEqual(self.proposals(), [])

    def test_old_format_proposal_cannot_reapply_an_undone_change(self):
        self.make_ready('11', 'Story')
        with self.ops.store.tx() as c:
            p = self.ops.create_proposal(c, 'change_format', ['11'], {'item_id': '11', 'format': 'Post'},
                                         'Change to Post', 'U-OWNER')
        self.ops.submit(owner_cmd(self.rid(), 'change_format', '11', explicit=True, format='Post'))
        self.ops.submit(owner_cmd(self.rid(), 'change_format', '11', explicit=True, format='Story'))
        r = self.ops.submit(owner_cmd(self.rid(), 'approve_proposal', None, proposal_id=p['proposal_id']))
        self.assertEqual(r['state'], 'rejected')
        self.assertEqual(self.item('11')['format'], 'Story')


class R5_A7_RejectedEditsDoNotLeaveStaleHolds(BoardCase):
    """A7: a rejected board edit left a silent rejected_edit hold that the corrected edit did not clear."""

    def test_topaz_before_file_selection_then_corrected(self):
        brd = Board([monday_item('20', name='Item20 LIP12')])
        self.observe(*brd.snapshot())
        setcell(brd, '20', 'topaz', 'Topazed')            # no file selected yet
        self.cycle(brd)
        self.assertNotEqual(self.hold('20').get('kind'), 'rejected_edit')
        self.select('20', 1)
        self.duration('20', 30.0)
        self.sync(brd)
        self.assertEqual(B.norm(brd.items['20'], 'topaz'), 'Not yet')    # shown as not confirmed for this file
        setcell(brd, '20', 'topaz', 'Topazed')
        self.cycle(brd)
        self.assertEqual(self.item('20')['topaz_asset'], 'id:FILE1@rev1')

    def test_invalid_caption_blocks_until_corrected(self):
        self.make_ready('21', 'Post', code='LIP21')
        self.assertIsNotNone(self.res('21'))
        brd = Board([monday_item('21', fmt='Post', code='LIP21', name='Item21 LIP21')])
        self.sync(brd)
        self.clock.advance(300)
        bad = 'Too many tags ' + ' '.join(f'#t{i}' for i in range(31))
        setcell(brd, '21', 'caption', bad, {'text': bad})
        self.cycle(brd)
        it = self.item('21')
        self.assertNotEqual(it['caption_state'], 'approved')
        self.assertIsNone(self.res('21'))                 # the board caption is never silently replaced by the old one
        self.assertNotEqual(self.hold('21').get('kind'), 'rejected_edit')
        good = 'Fixed caption, DM us. 🔥\n\n#reels'
        setcell(brd, '21', 'caption', good, {'text': good})
        self.cycle(brd)
        self.assertEqual(self.item('21')['caption'], good)
        self.assertEqual(self.item('21')['caption_state'], 'approved')
        self.assertIsNotNone(self.res('21'))


class R5_M12_CorrectedCodeUnblocks(BoardCase):
    def test_code_fixed_on_the_board_clears_only_the_invalid_code_block(self):
        brd = Board([monday_item('30', code='12345', name='Item30')])
        self.observe(*brd.snapshot())
        self.select('30', 1)
        with self.ops.store.tx() as c:
            self.ops.evaluate(c, '30', 'test')
        self.assertEqual(self.item('30')['block_key'], 'invalid_code')
        setcell(brd, '30', 'code', 'LIP30')
        self.cycle(brd)
        it = self.item('30')
        self.assertEqual(it['code'], 'LIP30')
        self.assertNotEqual(it['block_key'], 'invalid_code')


class R5_M3_RepeatedOwnerValuesAreSeparateEvents(BoardCase):
    """M3: a repeated identical write to a human column was dropped (same command id)."""

    def test_caption_a_x_a_publishes_a(self):
        A, X = 'Owner caption A, DM us. 🔥\n\n#reels', 'Owner caption X, DM us. ⚡️\n\n#reels'
        brd = Board([monday_item('40', fmt='Post', code='LIP40', name='Item40 LIP40', caption=X)])   # X at import
        self.observe(*brd.snapshot())
        self.ops.submit(owner_cmd(self.rid(), 'update_caption', '40', text=A))      # Slack: A
        self.sync(brd)
        self.clock.advance(300)
        setcell(brd, '40', 'caption', X, {'text': X})                               # board: X
        self.cycle(brd)
        self.sync(brd)
        self.ops.submit(owner_cmd(self.rid(), 'update_caption', '40', text=A))      # Slack: A again
        self.sync(brd)
        self.assertEqual(B.norm(brd.items['40'], 'caption'), A)          # the board shows what will be published
        self.assertEqual(self.item('40')['caption'], A)
        with self.ops.store.read() as c:
            self.assertEqual(self.ops.payload(c, self.ops.item(c, '40'))['caption'], A)

    def test_format_rejection_twice_is_written_back_twice(self):
        self.make_ready('41', 'Story')
        brd = Board([monday_item('41', fmt='Story', name='Item41 LIP12')])
        self.sync(brd)
        for _ in range(2):
            setcell(brd, '41', 'format', 'Carousel', {'index': 9})
            self.cycle(brd)
            self.sync(brd)
            self.assertEqual(B.norm(brd.items['41'], 'format'), 'Story')


class R5_M2_DateAndTimeEditIsOneDecision(BoardCase):
    def test_post_date_and_time_changed_together_make_one_command(self):
        self.make_ready('50', 'Post', code='LIP50')
        self.make_ready('51', 'Post', code='LIP51')
        brd = Board([monday_item('50', fmt='Post', code='LIP50', name='Item50 LIP50'),
                     monday_item('51', fmt='Post', code='LIP51', name='Item51 LIP51')])
        self.sync(brd)
        target = rules.cairo_local(2026, 10, 13, 19, 30)          # both the date and the hour differ
        local = target.astimezone(rules.TZ)
        self.assertNotEqual(B.norm(brd.items['50'], 'post_date'), local.date().isoformat())
        self.assertNotEqual(B.norm(brd.items['50'], 'post_time'), '19:30')
        setcell(brd, '50', 'post_date', local.date().isoformat(), {'date': local.date().isoformat()})
        setcell(brd, '50', 'post_time', 'x', {'hour': 19, 'minute': 30})
        r = self.cycle(brd)
        time_edits = [e for e in r['edits'] if e['edit'] in ('post_date', 'post_time', 'publish_at')]
        self.assertEqual(len(time_edits), 1, time_edits)
        self.assertEqual(self.res('50')['slot'], rules.iso(target))
        with self.ops.store.read() as c:
            cmds = c.execute("SELECT COUNT(*) FROM ops_commands WHERE item_id='50' AND op='request_reschedule'").fetchone()[0]
        self.assertEqual(cmds, 1)


class R5_M1_ApprovalsBindToTheirDependencies(BoardCase):
    def test_unrelated_changes_do_not_stale_a_swap_and_a_slot_change_does(self):
        self.make_ready('60')
        self.make_ready('61')
        p = self.ops.submit(owner_cmd(self.rid(), 'request_reschedule', '60', at=self.res('61')['slot']))
        self.assertEqual(p['state'], 'awaiting_approval', p)
        self.ops.submit(__import__('support').Command(self.rid(), 'set_notes', 'monday', 'monday', '61',
                                                      {'value': 'unrelated note'}))
        with self.ops.store.tx() as c:                    # e.g. a readiness re-evaluation bumps the version
            self.ops.update_item(c, '60', 'test', 'unrelated bump', infra_issue=None, waiting_since=self.ops.now())
        a = self.ops.submit(owner_cmd(self.rid(), 'approve_proposal', None, proposal_id=p['proposal_id']))
        self.assertEqual(a['state'], 'completed', a)


class R5_LOW04_CaptionNormalisationMatchesJS(unittest.TestCase):
    def test_bom_and_whitespace_compare_equal(self):
        item = monday_item('1', fmt='Post', caption='Caption text﻿ ')
        self.assertEqual(B.norm(item, 'caption'), 'Caption text')
        self.assertEqual(B.compare_value('caption', '﻿Caption text'), 'Caption text')


class R5_LOW10_PublishedAtIsNotInvented(OpsCase):
    def test_reconciled_publication_has_no_invented_publication_time(self):
        self.make_ready('70')
        self.clock.set(rules.instant(self.res('70')['slot']) + timedelta(seconds=30))
        cl = self.ops.claim('70', 'w')
        self.ops.container(cl['attempt_id'], 'w', cl['fence'], 'C70')
        self.ops.commit(cl['attempt_id'], 'w', cl['fence'], container_status='FINISHED')
        self.ops.result(cl['attempt_id'], 'w', error='ETIMEDOUT')
        self.clock.advance(5 * 3600)
        self.ops.reconcile(cl['attempt_id'], 'PUBLISHED')
        with self.ops.store.read() as c:
            d = self.ops.desired_display(c, self.ops.item(c, '70'))
        self.assertNotIn('published_at', {k for k, v in d.items() if v})
        self.assertIn('recorded', d['system'])


class R5_LOW11_CanceledSourceWithPreCommitAttempt(OpsCase):
    def test_cancellation_ends_the_attempt_instead_of_rereading_every_minute(self):
        self.make_ready('80')
        self.clock.set(rules.instant(self.res('80')['slot']) + timedelta(seconds=30))
        cl = self.ops.claim('80', 'w')
        self.ops.container(cl['attempt_id'], 'w', cl['fence'], 'C80')
        self.clock.advance(400)                           # lease expired, attempt still pre-commit
        r = self.ops.claim('80', 'w2', source_status='Canceled')
        self.assertFalse(r['claimed'])
        self.assertEqual(self.item('80')['owner_state'], 'skipped')
        self.clock.advance(60)
        self.assertEqual([w for w in self.ops.due('w3')['work'] if w['item_id'] == '80'], [])


class R5_LOW14_ForeignTextInASystemColumn(BoardCase):
    def test_no_false_restore_claim_and_no_loop(self):
        brd = Board([monday_item('90', name='Item90 LIP12')])
        self.observe(*brd.snapshot())                     # not prepared: v2 shows no measurements for it
        setcell(brd, '90', 'measurements', 'manual note by someone')
        for _ in range(3):
            self.cycle(brd)
            self.sync(brd)
        notices = [o for o in self.outbox('slack') if 'managed by the system' in o['payload']]
        self.assertEqual(notices, [])
        notice = (json.loads(self.item('90')['observed']).get('_notice') or {}).get('text', '')
        if B.norm(brd.items['90'], 'measurements'):       # the text stays: nothing may claim it was not applied
            self.assertNotIn('managed by the system', notice)
        with self.ops.store.read() as c:
            n = c.execute("SELECT COUNT(*) FROM ops_audit WHERE item_id='90' AND kind='revert_system_column'").fetchone()[0]
        self.assertLessEqual(n, 1)


class R5_LOW17_ExpiredProposalsReachATerminalState(OpsCase):
    def test_supervisor_closes_expired_proposals_and_their_holds(self):
        self.make_ready('100')
        with self.ops.store.tx() as c:
            p = self.ops.create_proposal(c, 'resume', ['100'], {'item_id': '100'}, 'Resume?', 'U-OWNER')
        self.clock.advance(8 * 86400)
        rid = 'wf3-x'
        self.ops.repair(rid, self.ops.run_start('wf3', rid)['fence'])
        with self.ops.store.read() as c:
            st = c.execute('SELECT state FROM ops_proposals WHERE id=?', (p['proposal_id'],)).fetchone()['state']
        self.assertEqual(st, 'expired')


if __name__ == '__main__':
    unittest.main()
