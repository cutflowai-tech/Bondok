"""Owner-approved acceptance run (bondok/acceptance.py): one authenticated owner message starts an exact, hash-bound
script whose steps go through Bondok's real queue, dedupe, model, tools, handler and reply."""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from support import ROOT
from test_bondok import OWNER
from test_r5_language import LangCase, Script, act

import sys
sys.path.insert(0, str(ROOT / 'bondok'))
import acceptance  # noqa: E402


class AcceptanceCase(LangCase):
    def setUp(self):
        super().setUp()
        self.adir = Path(tempfile.mkdtemp())
        self.env.update({'SLACK_CHANNEL_ID': 'C1', 'ACCEPTANCE_DIR': str(self.adir)})

    def script(self, steps, sid='ACC-T1', **extra):
        raw = json.dumps({'id': sid, 'steps': steps, **extra}, ensure_ascii=False).encode()
        (self.adir / f'{sid}.json').write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()[:12]

    def trusted_core(self, model):
        core = self.core(model)
        core.acceptance_trust = lambda p: None        # test files are not root-owned
        return core


class Trigger(AcceptanceCase):
    def test_only_the_owner_with_the_exact_hash_starts_a_run(self):
        digest = self.script([{'thread': 'A', 'text': 'وقف PZ1'}])
        core = self.trusted_core(Script())
        self.assertIn('المالك فقط', self.say(core, f'اختبار قبول ACC-T1 {digest}', user='U_MEMBER'))
        self.assertIn('hash', self.say(core, 'اختبار قبول ACC-T1 000000000000'))
        self.assertIn('no approved script', self.say(core, f'اختبار قبول ACC-NOPE {digest}'))
        self.assertIsNone(getattr(core, '_acceptance', None))

    def test_script_not_owned_by_root_is_refused(self):
        digest = self.script([{'thread': 'A', 'text': 'وقف PZ1'}])
        core = self.core(Script())                     # real trust check: the temp file belongs to this user
        if os.geteuid() == 0:
            self.skipTest('running as root')
        self.assertIn('owned by root', self.say(core, f'اختبار قبول ACC-T1 {digest}'))

    def test_step_text_cannot_start_another_run(self):
        digest = self.script([{'thread': 'A', 'text': 'x'}])
        core = self.trusted_core(Script())
        e = {'user': OWNER, 'text': f'اختبار قبول ACC-T1 {digest}', 'ts': '1.1', 'channel': 'C1',
             'acceptance': {'step': 1}}
        core.handle(e)
        self.assertIsNone(getattr(core, '_acceptance', None))


class Steps(AcceptanceCase):
    def test_steps_run_as_owner_messages_through_the_queue_and_redelivery_collapses(self):
        self.make_ready('820', 'Story', code='PZ1')
        steps = [{'thread': 'A', 'text': 'ايه وضع PZ1؟'},
                 {'thread': 'A', 'text': 'وقف PZ1', 'redeliver': True}]
        digest = self.script(steps)
        model = Script('PZ1 جاهز.', [act('pause_item', 'وقف PZ1', 'imperative', 'PZ1', item='PZ1', reason=None)],
                       'وقفته.')
        core = self.trusted_core(model)
        spec = acceptance.load(self.adir, 'ACC-T1', digest, trust=lambda p: None)
        run = acceptance.Run(core, spec, pump=core.process_one, sleep=lambda s: None)
        run.run()
        self.assertEqual(self.item('820')['owner_state'], 'paused')
        lines = [json.loads(x) for x in run.log_path.read_text().splitlines()]
        done = [x for x in lines if 'step' in x and 'state' in x]
        self.assertEqual([x['state'] for x in done], ['done', 'done'])
        self.assertFalse(done[1]['redelivered_accepted'])
        root = done[0]['event'].split(':')[-1]
        self.assertTrue(all(th == root for t, th, _ in self.sent if 'وقف PZ1' in t and '🧪' in t))
        # the step went through the owner's command path once (one owner pause command)
        with self.ops.store.read() as c:
            n = c.execute("SELECT COUNT(*) FROM ops_commands WHERE op='pause' AND actor=?", (OWNER,)).fetchone()[0]
        self.assertEqual(n, 1)
        self.assertEqual(model.calls, 3)                 # no extra model call for the redelivered event

    def test_stop_message_ends_the_run(self):
        core = self.trusted_core(Script())
        self.assertIn('هوقف', self.say(core, 'اوقف اختبار القبول'))
        digest = self.script([{'thread': 'A', 'text': 'x'}])
        spec = acceptance.load(self.adir, 'ACC-T1', digest, trust=lambda p: None)
        run = acceptance.Run(core, spec, pump=core.process_one, sleep=lambda s: None)
        core.store.kv('acceptance:stop', True)
        run.run = run.run                                 # run() clears the flag at start; set it after
        orig = run._quiet

        def stop_then_quiet():
            core.store.kv('acceptance:stop', True)
            orig()
        run._quiet = stop_then_quiet
        run.run()
        self.assertFalse([t for t, _, _ in self.sent if t.startswith('🧪 ACC-T1 · 1/')])

    def test_waits_away_from_a_live_publication(self):
        self.make_ready('821', 'Story', code='PZ2')
        from waset_ops import rules
        slot = rules.instant(self.res('821')['slot']).timestamp()
        clock = {'t': slot - 600}
        waits = []
        core = self.trusted_core(Script('تمام'))
        digest = self.script([{'thread': 'A', 'text': 'ايه الأخبار؟'}], quiet_minutes=30)
        spec = acceptance.load(self.adir, 'ACC-T1', digest, trust=lambda p: None)

        def sleep(s):
            waits.append(s)
            clock['t'] += s
        run = acceptance.Run(core, spec, pump=core.process_one, sleep=sleep, clock=lambda: clock['t'])
        run.run()
        self.assertTrue(waits)
        self.assertGreaterEqual(clock['t'], slot + 1800 - 1)


class TriggerChecksNow(AcceptanceCase):
    def test_refused_without_isolation_and_reports_the_window_at_trigger_time(self):
        digest = self.script([{'thread': 'A', 'text': 'x'}])
        core = self.trusted_core(Script())
        self.assertIn('مش معزولة', self.say(core, f'اختبار قبول ACC-T1 {digest}'))
        from waset_ops.core import Command
        self.ops.submit(Command('iso', 'set_isolation', 'service:acceptance', 'service:acceptance', None,
                                {'code_prefixes': ['ACC']}))
        self.make_ready('860', 'Story', code='RL9')
        acceptance.Run.run = lambda self: None                 # do not run the steps in this test
        reply = self.say(core, f'اختبار قبول ACC-T1 {digest}')
        self.assertIn('بدأ', reply)
        self.assertIn('أقرب نشر حقيقي', reply)



class Resume(AcceptanceCase):
    def test_a_restarted_run_continues_without_repeating_done_steps(self):
        self.make_ready('870', 'Story', code='PZ7')
        self.make_ready('871', 'Story', code='PZ8')
        digest = self.script([{'thread': 'A', 'text': 'وقف PZ7'}, {'thread': 'A', 'text': 'وقف PZ8'}])
        spec = acceptance.load(self.adir, 'ACC-T1', digest, trust=lambda p: None)
        model = Script([act('pause_item', 'وقف PZ7', 'imperative', 'PZ7', item='PZ7', reason=None)], 'تمام',
                       [act('pause_item', 'وقف PZ8', 'imperative', 'PZ8', item='PZ8', reason=None)], 'تمام')
        core = self.trusted_core(model)
        run = acceptance.Run(core, spec, pump=core.process_one, sleep=lambda s: None)
        orig = run._deliver
        calls = {'n': 0}

        def crash_after_first(key, event, timeout=600):
            calls['n'] += 1
            out = orig(key, event, timeout)
            if calls['n'] == 1:
                raise SystemExit('service restarted')
            return out
        run._deliver = crash_after_first
        with self.assertRaises(SystemExit):
            run.run()
        self.assertEqual(self.item('870')['owner_state'], 'paused')
        posted = len([t for t, _, _ in self.sent if t.startswith('🧪 ACC-T1 · 1/')])
        run2 = acceptance.Run(core, spec, pump=core.process_one, sleep=lambda s: None)   # what resume() starts
        run2.run()
        self.assertEqual(self.item('871')['owner_state'], 'paused')
        self.assertEqual(len([t for t, _, _ in self.sent if t.startswith('🧪 ACC-T1 · 1/')]), posted)  # not reposted
        self.assertEqual(model.calls, 4)                        # step 1 not sent to the model again
        self.assertFalse(core.store.kv('acceptance:active'))


if __name__ == '__main__':
    unittest.main()
