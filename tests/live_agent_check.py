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


if __name__ == '__main__':
    unittest.main(verbosity=2)
