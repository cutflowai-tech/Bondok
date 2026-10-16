"""Live telephone check (not part of the offline suite): the REAL model and verifier, through the voice path
(Voice.turn: phone style, commit gate, call memory), on a synthetic store. No Monday/Dropbox/Instagram/Slack, no
audio; costs a few model calls.

    OPENROUTER_API_KEY=... python3 tests/live_voice_check.py -k test_voice_

Natural Egyptian follow-ups with their preceding conversation, checked by their effect in the store:
* «لا رجّعه» after Bondok paused an item: resume it (a reversible execution state);
* «آه وقّفه» after a held pause, and after the owner's own question about pausing: pause it;
* «لا رجّعه» when two items were just paused together: one narrow question, nothing changed;
* «لا رجّعه» after a publication was recorded: never described as undone on Instagram.
"""
import os
import sys
import threading
import unittest

from support import ROOT, monday_item

sys.path.insert(0, str(ROOT / 'bondok'))
import voice  # noqa: E402
from live_agent_check import LiveAgent  # noqa: E402


class LiveVoice(LiveAgent):
    def setUp(self):
        super().setUp()
        self.env['VOICE_ACCESS_FILE'] = str(self.dir / 'access.json')
        (self.dir / 'access.json').write_text('{"extensions": {"102": {"role": "owner"}}}')

    def line(self, call):
        self.v = voice.Voice(self.core(), self.env)
        self.token = self.v.open_session({'endpoint': '102', 'call_id': call, 'verified_by': 'test'})['token']
        self.msgs = []

    def talk(self, text, speech_after=None):
        """One caller turn; speech_after=s simulates the caller speaking again s seconds after the turn arrived."""
        self.msgs.append({'role': 'user', 'content': text})
        ctx = voice.new_ctx()
        if speech_after is not None:
            call = self.v.session(self.token)['call_id']
            threading.Timer(speech_after, lambda: self.v.speech({'call_id': call})).start()
        said, _ = self.v.turn({'messages': list(self.msgs), 'elevenlabs_extra_body': {'session': self.token}}, ctx)
        self.msgs.append({'role': 'assistant', 'content': said})
        print(f'\n> {text}\n< {said}' + (f"  [held: {ctx['held']}]" if ctx.get('held') else ''))
        return said

    def state(self, iid):
        return self.item(iid)['owner_state']

    def test_voice_1_la_ragga3o_after_a_pause_resumes(self):
        self.make_ready('951', 'Story', code='VA1')
        self.line('call-live-0001')
        self.talk('وقف VA1')
        self.assertEqual(self.state('951'), 'paused')
        self.talk('لا رجّعه')
        self.assertEqual(self.state('951'), 'active')

    def test_voice_2_ah_wa22afo_after_a_held_pause_pauses(self):
        self.make_ready('952', 'Story', code='VB1')
        self.line('call-live-0002')
        self.talk('وقف VB1', speech_after=0.6)              # he went on talking: nothing is committed
        self.assertEqual(self.state('952'), 'active')
        said = self.talk('آه وقّفه')
        self.assertEqual(self.state('952'), 'paused')
        self.assertNotIn('قول اه', said)

    def test_voice_3_ah_wa22afo_after_his_own_question_pauses(self):
        self.make_ready('953', 'Story', code='VC1')
        self.line('call-live-0003')
        self.talk('إيه وضع VC1؟')
        self.talk('طب لو وقفته دلوقتي هيحصل إيه؟')
        self.assertEqual(self.state('953'), 'active')        # a question changes nothing
        self.talk('آه وقّفه')
        self.assertEqual(self.state('953'), 'paused')

    def test_voice_4_la_ragga3o_with_two_candidates_asks_one_question(self):
        self.make_ready('954', 'Story', code='VD1')
        self.make_ready('955', 'Story', code='VD2')
        self.line('call-live-0004')
        self.talk('وقف VD1 و VD2')
        self.assertEqual((self.state('954'), self.state('955')), ('paused', 'paused'))
        said = self.talk('لا رجّعه')
        self.assertEqual((self.state('954'), self.state('955')), ('paused', 'paused'))
        self.assertIn('؟', said)

    def test_voice_5_a_publication_is_not_undone_by_words(self):
        self.make_ready('956', 'Story', code='VE1')
        self.line('call-live-0005')
        self.talk('أنا نزلت VE1 بنفسي على حساب الاستوديو')
        published = self.item('956')['publication']
        said = self.talk('لا رجّعه')
        print('   publication before/after:', published, self.item('956')['publication'])
        for claim in ('اتشال من انستجرام', 'اتمسح من انستجرام', 'لغيت النشر', 'اتلغى النشر'):
            self.assertNotIn(claim, said)
        if self.item('956')['publication'] != published:
            self.assertIn('انستجرام', said)             # only the record changed: he must be told Instagram did not


if __name__ == '__main__':
    unittest.main()
