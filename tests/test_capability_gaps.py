"""Capabilities the 2026-10-16 matrix found untested or weakly tested (tests audit): each asserts the effect the owner
asked for, through the owner's command path where one exists."""
import json
import unittest
from datetime import timedelta

from runtime_fakes import FakeDropbox, FakeInstagram, FakeMonday
from support import OpsCase, board_from_projection, owner_cmd, rules, set_cell
from test_r5_language import LangCase, Script, act
from waset_ops.core import Command

from waset_runtime.cycles import Runner


class Guards(OpsCase):
    def board(self, iid, **kw):
        return board_from_projection(self.ops, iid, code=self.item(iid)['code'], **kw)

    def test_topaz_turned_back_on_the_board_keeps_the_confirmation_until_proven(self):
        self.make_ready('900', 'Story', code='GA1')
        self.drain_monday()
        self.clock.advance(1800)
        asset = self.item('900')['topaz_asset']
        self.observe(self.board('900', topaz='Not yet'))
        self.assertEqual(self.item('900')['topaz_asset'], asset)
        self.assertEqual(len(self.outbox('board_evidence')), 1)
        self.prove_board_edits()                                     # a person did turn it back
        self.assertIsNone(self.item('900')['topaz_asset'])

    def test_emptied_approved_caption_is_kept_until_proven(self):
        self.make_ready('901', 'Post', code='GA2', caption='A valid caption, DM us 🔥\n\n#reels')
        self.drain_monday()
        self.clock.advance(1800)
        b = self.board('901', fmt='Post', caption='A valid caption, DM us 🔥\n\n#reels')
        set_cell(b, 'caption', '', None)
        self.observe(b)
        self.assertEqual(self.item('901')['caption_state'], 'approved')
        self.assertTrue(self.item('901')['caption'])

    def test_exemption_then_revoke_keeps_the_confirmation_and_the_rule(self):
        self.make_ready('902', 'Story', code='GA3')                  # Topaz confirmed by the owner
        asset = self.item('902')['topaz_asset']
        self.ops.submit(owner_cmd('w', 'waive_rule', '902', explicit=True, rule='topaz_required'))
        self.ops.submit(owner_cmd('r', 'restore_rule', '902', explicit=True, rule='topaz_required'))
        self.assertEqual(self.item('902')['topaz_asset'], asset)    # the confirmation stays
        with self.ops.store.read() as c:
            self.assertTrue(self.ops.policy(c)['topaz_required'])    # the rule itself never changed
            self.assertNotIn('topaz_required', self.ops.waived(c, self.ops.item(c, '902')))
        self.assertEqual(self.item('902')['readiness'], 'ready')
        self.assertIsNotNone(self.res('902'))
        st = self.ops.item_status('902')
        self.assertEqual(st['topaz_confirmation']['version'], asset)
        self.assertEqual(st['topaz_confirmation']['confirmed_by'], 'the owner')


class OwnerSetRuleValues(OpsCase):
    def rule(self, name, value):
        r = self.ops.submit(owner_cmd(self.rid('pol'), 'set_policy', None, explicit=True, rule=name, value=value))
        self.assertEqual(r['state'], 'completed', r)

    def test_occupied_slot_moves_directly_when_the_owner_says_so(self):
        self.make_ready('910', 'Story', code='OR1')
        self.make_ready('911', 'Story', code='OR2')
        taken = self.res('910')['slot']
        self.rule('occupied_slot_needs_approval', False)
        self.ops.submit(owner_cmd('mv', 'request_reschedule', '911', explicit=True, at=taken))
        self.assertEqual(self.res('911')['slot'], taken)
        self.assertNotEqual((self.res('910') or {}).get('slot'), taken)

    def test_caption_approval_off_schedules_a_valid_unapproved_post(self):
        self.make_ready('912', 'Post', code='OR3', approve_caption=False)
        with self.ops.store.tx() as c:
            c.execute("UPDATE ops_items SET caption='A valid caption, DM us 🔥', caption_state='pending_approval' "
                      "WHERE item_id='912'")
        self.assertIsNone(self.res('912'))
        self.rule('post_caption_approval', False)
        with self.ops.store.tx() as c:
            self.ops.try_schedule(c, '912', 'service:wf3')
        self.assertIsNotNone(self.res('912'))

    def test_late_window_and_attempt_limit_take_the_owners_values(self):
        self.rule('late_window_minutes', 30)
        self.rule('publish_attempts_per_slot', 1)
        with self.ops.store.read() as c:
            p = self.ops.policy(c)
        self.assertEqual((p['late_window_minutes'], p['publish_attempts_per_slot']), (30, 1))
        self.assertEqual(rules.late_window(p), timedelta(minutes=30))

    def test_post_slots_change_reaches_the_grid(self):
        self.rule('post_slots', {'sat': '20:00'})
        with self.ops.store.read() as c:
            p = self.ops.policy(c)
        slots = list(rules.grid(self.ops.now_dt(), 'Post', policy=p, horizon=14))
        self.assertTrue(slots)
        self.assertTrue(all(s.astimezone(rules.TZ).strftime('%a %H:%M') == 'Sat 20:00' for s in slots))


class ReadTools(LangCase):
    def tool(self, core, name, args):
        return core.agent.tool(name, args, {'thread': 't', 'actor': 'U1', 'event_id': 'e', 'text': 'x'})

    def test_list_files_instagram_recent_and_operation_status(self):
        self.make_ready('920', 'Story', code='RT1')
        core = self.core(Script())
        dbx = FakeDropbox()
        url = dbx.add_folder('/Social Media/Production/RT1/Story')
        dbx.add_file('/Social Media/Production/RT1/Story/Clip RT1 v1.mp4', content='x1')
        with self.ops.store.tx() as c:
            c.execute("UPDATE ops_items SET folder_url=? WHERE item_id='920'", (url,))
        core.agent.clients.update({'dropbox': dbx, 'instagram': FakeInstagram()})
        files = self.tool(core, 'list_files', {'item': 'RT1'})
        self.assertEqual([v['name'] for v in files['videos']], ['Clip RT1 v1.mp4'])
        recent = self.tool(core, 'instagram_recent', {})
        self.assertIn('recent', recent)
        r = self.ops.submit(owner_cmd('opx', 'pause', '920', explicit=True))
        st = self.tool(core, 'get_operation_status', {'id': 'opx'})
        self.assertEqual(st['state'], 'completed')

    def test_refresh_item_reports_a_board_change_and_why(self):
        self.make_ready('921', 'Story', code='RT2')
        self.drain_monday()
        self.clock.advance(1800)
        mon = FakeMonday()
        b = board_from_projection(self.ops, '921', code='RT2')
        set_cell(b, 'notes', 'typed on the board')
        mon.board.items['921'] = b
        core = self.core(Script())
        core.agent.clients['monday'] = mon
        out = self.tool(core, 'refresh_item', {'item': 'RT2'})
        self.assertTrue(out['board_edits'])
        self.assertEqual(self.item('921')['notes'], 'typed on the board')


class AdminEffects(LangCase):
    def test_delete_moves_and_fields_reach_the_external_services(self):
        from waset_runtime.admin import run_admin_jobs
        self.make_ready('930', 'Story', code='AE1')
        self.make_ready('931', 'Story', code='AE2')
        mon, dbx = FakeMonday(), FakeDropbox()
        for i in ('930', '931'):
            mon.board.items[i] = board_from_projection(self.ops, i, code=self.item(i)['code'])
        core = self.core(Script(
            [act('board_delete_item', 'امسح AE1 نهائي', 'imperative', 'AE1', item='AE1')], 'تمام',
            [act('set_item_field', 'خلي الكود بتاع AE2 AE22 والفارايتي Tips والكولاب @studio', 'imperative', 'AE2',
                 item='AE2', field='code', value='AE22'),
             act('set_item_field', 'خلي الكود بتاع AE2 AE22 والفارايتي Tips والكولاب @studio', 'imperative', 'AE2',
                 item='AE2', field='variety', value='Tips'),
             act('set_item_field', 'خلي الكود بتاع AE2 AE22 والفارايتي Tips والكولاب @studio', 'imperative', 'AE2',
                 item='AE2', field='collab', value='@studio')], 'تمام'))
        self.say(core, 'امسح AE1 نهائي')
        self.say(core, 'خلي الكود بتاع AE2 AE22 والفارايتي Tips والكولاب @studio')
        run_admin_jobs(self.ops, mon, dbx)
        self.drain_monday()
        self.assertIn(('delete_item', '930'), mon.writes)
        self.assertEqual((self.item('931')['code'], self.item('931')['variety'], self.item('931')['collab']),
                         ('AE22', 'Tips', '@studio'))


class IsolationThroughTheRuntime(OpsCase):
    def test_an_isolated_due_item_never_reaches_instagram_through_a_publish_cycle(self):
        self.ops.submit(Command('iso', 'set_isolation', 'service:acceptance', 'service:acceptance', None,
                                {'code_prefixes': ['ACC']}))
        self.make_ready('940', 'Story', code='ACC40')
        self.make_ready('941', 'Story', code='RL41')
        self.ops.run_finish('wf1', self.run_id, fence=self.fence)
        ig, dbx, mon = FakeInstagram(), FakeDropbox(), FakeMonday()
        dbx.files['/p/video_v1.mp4'] = {'.tag': 'file', 'id': 'id:FILE1', 'rev': 'rev1', 'path_lower': '/p/video_v1.mp4'}
        self.drain_monday()
        for i in ('940', '941'):
            mon.board.items[i] = board_from_projection(self.ops, i, code=self.item(i)['code'])
        r = Runner(self.ops, mon, dbx, ig, sleep=lambda s: None)
        slot = max(rules.instant(self.res(i)['slot']) for i in ('940', '941'))
        for n in range(3):
            self.clock.set(slot + timedelta(seconds=5 + 60 * n))
            r.publish_cycle()
        self.assertEqual(self.item('940')['publication'], 'not_started')
        self.assertEqual(self.item('941')['publication'], 'published')     # real content is not held back
        self.assertEqual(len(ig.publish_calls()), 1)


class CommandPathFixes(LangCase):
    def test_publish_now_on_an_item_still_checked_publishes_once_it_is_ready(self):
        from support import monday_item
        self.observe(monday_item('950', fmt='Story', code='PN9', name='Item950 PN9'))
        self.select('950', 1, 'c950-')
        self.duration('950', 30.0, 1, 'c950-')
        self.assertNotEqual(self.item('950')['readiness'], 'ready')
        text = 'انشر PN9 دلوقتي'
        core = self.core(Script([act('publish_now', text, 'imperative', 'PN9', item='PN9')], 'تمام'))
        self.say(core, text)
        self.assertIsNone(self.res('950'))
        self.clock.advance(3600)                                      # an hour later the check finishes
        self.topaz('950')
        self.select('950', 1, 'c950-')
        mid = self.add_media('950', 'Story', 1, duration=30.0)
        self.wf1('prep_media', '950', result={'ready': True, 'mediaId': mid})   # verified: ready
        res = self.res('950')
        self.assertIsNotNone(res)                                     # reserved about two minutes after ready
        self.assertLess(rules.instant(res['slot']) - self.ops.now_dt(), timedelta(minutes=4))
        self.assertEqual(res['origin'], 'owner')

    def test_broadcast_and_file_messages_are_accepted(self):
        import sys
        from support import ROOT
        sys.path.insert(0, str(ROOT / 'bondok'))
        from app import accept_event
        from test_bondok import OWNER
        for sub in ('thread_broadcast', 'file_share'):
            ok = {'team_id': 'T1', 'event': {'type': 'app_mention', 'channel': 'C1', 'user': OWNER, 'subtype': sub}}
            self.assertTrue(accept_event(ok, 'C1', 'T1', lambda t: False, 'UBOT'), sub)
        bad = {'team_id': 'T1', 'event': {'type': 'app_mention', 'channel': 'C1', 'user': OWNER,
                                          'subtype': 'message_changed'}}
        self.assertFalse(accept_event(bad, 'C1', 'T1', lambda t: False, 'UBOT'))

    def test_a_member_naming_items_does_not_add_them_to_the_owners_them(self):
        core = self.core(Script())
        core.store.remember('th', 'user', '[member] كمان MB1 و MB2')
        core.store.remember('th', 'user', '[owner] شوف MB3')
        self.assertNotIn('MB1', core.store.thread_text('th'))
        self.assertIn('MB3', core.store.thread_text('th'))


class ReadToolsMore(LangCase):
    def tool(self, core, name, args):
        return core.agent.tool(name, args, {'thread': 't', 'actor': 'U1', 'event_id': 'e', 'text': 'x'})

    def test_schedule_health_and_history_through_the_agent_tools(self):
        self.make_ready('960', 'Story', code='RM1')
        core = self.core(Script())
        sched = self.tool(core, 'get_schedule', {'days': 14})
        self.assertIn('960', [r['item_id'] for r in sched['reservations']])
        health = self.tool(core, 'get_system_health', {})
        self.assertIn('heartbeat_age_seconds', health)
        self.ops.submit(owner_cmd('p60', 'pause', '960', explicit=True))
        hist = self.tool(core, 'get_history', {'item': 'RM1'})
        self.assertIn('pause', [d['op'] for d in hist['decisions']])

    def test_approve_existing_caption_approves_it(self):
        self.make_ready('961', 'Post', code='RM2', approve_caption=False)
        with self.ops.store.tx() as c:
            c.execute("UPDATE ops_items SET caption='Existing caption, DM us 🔥', caption_state='legacy_unapproved' "
                      "WHERE item_id='961'")
        text = 'اعتمد الكابشن اللي على RM2'
        core = self.core(Script([act('approve_existing_caption', text, 'imperative', 'RM2', item='RM2')], 'تمام'))
        self.say(core, text)
        self.assertEqual(self.item('961')['caption_state'], 'approved')
        self.assertIsNotNone(self.res('961'))


class FileChangedDuringPreparation(OpsCase):
    def test_a_new_file_version_during_preparation_is_not_published_as_the_old_one(self):
        from support import monday_item
        self.observe(monday_item('970', fmt='Story', code='FC1', name='Item970 FC1'))
        self.select('970', 1, 'c970-')
        self.duration('970', 30.0, 1, 'c970-')
        self.topaz('970', 1)
        self.select('970', 1, 'c970-')
        self.select('970', 2, 'c970b-')                    # the editor uploads v2 while v1 is being prepared
        mid = self.add_media('970', 'Story', 1, duration=30.0)
        r = self.wf1('prep_media', '970', result={'ready': True, 'mediaId': mid})   # v1's result arrives late
        self.assertNotEqual(self.item('970')['readiness'], 'ready', r)            # never ready on the old version
        self.assertIsNone(self.res('970'))


if __name__ == '__main__':
    unittest.main()
