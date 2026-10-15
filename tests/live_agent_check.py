"""Live agent check (not part of the offline suite): the REAL model (OpenRouter, production model and policy) drives
the real bridge and handler on a synthetic store. No Monday/Dropbox/Instagram/Slack calls; costs a few model calls.

    OPENROUTER_API_KEY=... python3 tests/live_agent_check.py

Each scenario says the owner's words and checks the effect in the store, not the model's text.
"""
import os
import sys
import unittest

from support import ROOT, owner_cmd, rules  # noqa: F401

sys.path.insert(0, str(ROOT / 'bondok'))
from agent import Model, policy_text  # noqa: E402
from test_r5_language import LangCase  # noqa: E402


class LiveAgent(LangCase):
    def setUp(self):
        super().setUp()
        from datetime import datetime, timezone
        self.clock.set(datetime.now(timezone.utc).replace(microsecond=0))   # the model is told the real time
        self.ops.run_finish('wf1', self.run_id, fence=self.fence)
        self.fence = self.ops.run_start('wf1', self.run_id)['fence']

    def core(self, model=None):
        from app import Core
        m = Model(os.environ['OPENROUTER_API_KEY'], os.environ.get('OPENROUTER_MODEL') or 'openai/gpt-6.1-sol',
                  policy_text())
        return Core(self.env, model=m, ops=self.ops, post=self._post)

    def waiting(self, iid, code):
        self.stable_file(iid, code)
        self.select(iid)
        self.assertTrue(self.item(iid)['block_key'].startswith('topaz:'))

    def thread(self, core, code, th):
        self.say(core, f'ايه وضع {code}؟', thread=th)

    def test_1_topaz_confirmation_plain_file_name(self):
        self.waiting('901', 'TA1')
        core = self.core()
        self.thread(core, 'TA1', '1800000001.000100')
        reply = self.say(core, 'أنا عملت توباز للفيديو ده', thread='1800000001.000100')
        print('\n[1]', reply)
        self.assertEqual(self.item('901')['topaz_asset'], 'id:FILE1@rev1')

    def test_2_publish_without_topaz_is_an_exemption(self):
        self.waiting('902', 'TB1')
        core = self.core()
        self.thread(core, 'TB1', '1800000002.000100')
        reply = self.say(core, 'انشره من غير توباز', thread='1800000002.000100')
        print('\n[2]', reply)
        with self.ops.store.read() as c:
            self.assertIn('topaz_required', self.ops.waived(c, self.ops.item(c, '902')))
        self.assertIsNone(self.item('902')['topaz_asset'])

    def test_3_lasting_rule_change(self):
        self.waiting('903', 'TC1')
        core = self.core()
        reply = self.say(core, 'من النهارده توباز مش شرط لأي فيديو', thread='1800000003.000100')
        print('\n[3]', reply)
        with self.ops.store.read() as c:
            self.assertFalse(self.ops.policy(c)['topaz_required'])

    def test_4_question_does_nothing(self):
        self.waiting('904', 'TD1')
        core = self.core()
        before = self.snap()
        reply = self.say(core, 'هو ينفع ننشر TD1 من غير توباز؟', thread='1800000004.000100')
        print('\n[4]', reply)
        self.assertEqual(self.changed(before), set())
        with self.ops.store.read() as c:
            self.assertTrue(self.ops.policy(c)['topaz_required'])

    def test_5_pause_named_item(self):
        self.make_ready('905', 'Story', code='TE1')
        core = self.core()
        reply = self.say(core, 'وقف TE1', thread='1800000005.000100')
        print('\n[5]', reply)
        self.assertEqual(self.item('905')['owner_state'], 'paused')

    def test_6_reschedule_cairo_time(self):
        self.make_ready('906', 'Story', code='TF1')
        core = self.core()
        reply = self.say(core, 'غير معاد TF1 لبكرة الساعة 9 بالليل', thread='1800000006.000100')
        print('\n[6]', reply)
        res = self.res('906')
        local = rules.instant(res['slot']).astimezone(rules.TZ) if res else None
        self.assertIsNotNone(local)
        from datetime import timedelta
        tomorrow = (self.ops.now_dt().astimezone(rules.TZ) + timedelta(days=1)).date().isoformat()
        self.assertEqual((local.date().isoformat(), local.hour, local.minute), (tomorrow, 21, 0))

    def test_7_revoke_exemption(self):
        self.waiting('907', 'TG1')
        core = self.core()
        self.thread(core, 'TG1', '1800000007.000100')
        self.say(core, 'انشره من غير توباز', thread='1800000007.000100')
        with self.ops.store.read() as c:
            self.assertIn('topaz_required', self.ops.waived(c, self.ops.item(c, '907')))
        reply = self.say(core, 'لا رجع شرط توباز على TG1، ماينزلش من غير توباز', thread='1800000007.000100')
        print('\n[7]', reply)
        with self.ops.store.read() as c:
            self.assertNotIn('topaz_required', self.ops.waived(c, self.ops.item(c, '907')))

    def test_8_delete_is_not_archive(self):
        import json
        self.make_ready('908', 'Story', code='TH1')
        self.make_ready('909', 'Story', code='TH2')
        core = self.core()
        r1 = self.say(core, 'امسح TH1 من البورد نهائي', thread='1800000008.000100')
        r2 = self.say(core, 'أرشف TH2', thread='1800000009.000100')
        print('\n[8]', r1, '|', r2)
        acts = {json.loads(j['payload'])['item_id']: json.loads(j['payload'])['action']
                for j in self.outbox('monday_admin')}
        self.assertEqual(acts, {'908': 'delete', '909': 'archive'})

    def test_9_twelve_items_no_confirmation(self):
        codes = [f'TJ{n}' for n in range(12)]
        for n, code in enumerate(codes):
            self.make_ready(str(910 + n), 'Story', code=code)
        core = self.core()
        reply = self.say(core, 'وقف ' + ' و '.join(codes), thread='1800000010.000100')
        print('\n[9]', reply)
        self.assertEqual({self.item(str(910 + n))['owner_state'] for n in range(12)}, {'paused'})



    def test_10_why_is_it_held_says_the_true_source(self):
        """A board read emptied the time (not the owner): the real model must say so, not "you cancelled it"."""
        self.make_ready('950', 'Story', code='TK1')
        from waset_ops.core import Command
        self.ops.submit(Command('monday:950:post_date:1:0:x', 'request_reschedule', 'monday', 'monday', '950',
                                {'at': None}, auth={'board': {'source': 'observation', 'column': 'post_date'}}))
        core = self.core()
        self.say(core, 'ايه وضع TK1؟', thread='1800000011.000100')
        reply = self.say(core, 'ليه ده واقف؟', thread='1800000011.000100')
        print('\n[10]', reply)
        low = reply.lower()
        self.assertTrue(any(w in reply for w in ('البورد', 'Monday', 'مونداي', 'board')), reply)
        for claim in ('انت لغيت', 'إنت لغيت', 'أنت ألغيت', 'انت ألغيت', 'لغيته انت', 'you cancelled', 'you canceled'):
            self.assertNotIn(claim, low)

    # ---- meaning (owner review 2026-10-16): negation, politeness, plans, opposites, ambiguity, several commands
    def tomorrow_at(self, hour):
        from datetime import timedelta
        d = (self.ops.now_dt().astimezone(rules.TZ) + timedelta(days=1)).date()
        return d.isoformat(), hour

    def local(self, iid):
        res = self.res(iid)
        if res:
            return rules.instant(res['slot']).astimezone(rules.TZ)
        req = self.item(iid)['requested_at']
        return rules.instant(req).astimezone(rules.TZ) if req else None

    def test_11_two_commands_in_one_message(self):
        self.make_ready('961', 'Story', code='TL1')
        self.make_ready('962', 'Story', code='TL2')
        reply = self.say(self.core(), 'وقف TL1 وانشر TL2 بكرة الساعة 9 بالليل', thread='1800000021.000100')
        print('\n[11]', reply)
        self.assertEqual(self.item('961')['owner_state'], 'paused')
        at = self.local('962')
        self.assertEqual((at.date().isoformat(), at.hour), self.tomorrow_at(21))

    def test_12_publish_without_topaz(self):
        self.waiting('963', 'TM1')
        reply = self.say(self.core(), 'انشر TM1 من غير توباز', thread='1800000022.000100')
        print('\n[12]', reply)
        with self.ops.store.read() as c:
            self.assertIn('topaz_required', self.ops.waived(c, self.ops.item(c, '963')))
        self.assertIsNone(self.item('963')['topaz_asset'])

    def test_13_negated_publish_is_a_stop(self):
        self.make_ready('964', 'Story', code='TN1')
        reply = self.say(self.core(), 'متنشرش TN1', thread='1800000023.000100')
        print('\n[13]', reply)
        with self.ops.store.read() as c:
            stopped = self.ops.item(c, '964')['owner_state'] == 'paused' or self.ops.publish_block(c, '964')
        self.assertTrue(stopped)

    def test_14_polite_request(self):
        self.make_ready('965', 'Story', code='TO1')
        reply = self.say(self.core(), 'ممكن توقف TO1 لو سمحت', thread='1800000024.000100')
        print('\n[14]', reply)
        self.assertEqual(self.item('965')['owner_state'], 'paused')

    def test_15_plan_records_nothing(self):
        self.waiting('966', 'TP1')
        reply = self.say(self.core(), 'هعمل توباز لـ TP1 بكرة', thread='1800000025.000100')
        print('\n[15]', reply)
        self.assertIsNone(self.item('966')['topaz_asset'])

    def test_16_opposite_words_do_not_exempt(self):
        self.waiting('967', 'TQ1')
        reply = self.say(self.core(), 'لازم توباز لـ TQ1 قبل ما ينزل', thread='1800000026.000100')
        print('\n[16]', reply)
        with self.ops.store.read() as c:
            self.assertNotIn('topaz_required', self.ops.waived(c, self.ops.item(c, '967')))

    def test_17_real_ambiguity_asks_one_question(self):
        self.make_ready('968', 'Story', code='TR1')
        reply = self.say(self.core(), 'شيل TR1', thread='1800000027.000100')
        print('\n[17]', reply)
        self.assertEqual(self.outbox('monday_admin'), [])
        self.assertEqual(self.item('968')['owner_state'], 'active')
        self.assertLessEqual(reply.count('❓'), 1)

    def test_18_concessive_directive(self):
        self.make_ready('969', 'Story', code='TS1')
        reply = self.say(self.core(), 'متنشرش TS1 فعليًا حتى لو اتحجزله معاد، ده عنصر تجربة', thread='1800000028.000100')
        print('\n[18]', reply)
        with self.ops.store.read() as c:
            self.assertTrue(self.ops.publish_block(c, '969') or self.ops.item(c, '969')['owner_state'] == 'paused')

    def test_19_tomorrow_9(self):
        self.make_ready('970', 'Story', code='TT1')
        reply = self.say(self.core(), 'غير معاد TT1 لبكرة 9', thread='1800000029.000100')
        print('\n[19]', reply)
        at = self.local('970')
        self.assertIsNotNone(at)
        self.assertEqual((at.date().isoformat(), at.hour), self.tomorrow_at(21))


if __name__ == '__main__':
    unittest.main(verbosity=2)
