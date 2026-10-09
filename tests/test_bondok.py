"""Bondok integration contract: Slack identity, approvals, cost-aware routing.

Uses the real handler (waset_ops) and the real Bondok Core with a fake model
and a fake Slack poster; no network, no paid model calls.
"""
import json
import sys
import unittest

from support import ROOT, OpsCase, monday_item, rules

sys.path.insert(0, str(ROOT / 'bondok'))
from app import Core, accept_event  # noqa: E402
from bridge import explicit  # noqa: E402

OWNER = 'U-OWNER'


class FakeModel:
    """Scripted Responses-API outputs; counts calls."""
    model = 'fake'

    def __init__(self, script=None, fail=False):
        self.script, self.calls, self.fail = list(script or []), 0, fail

    def __call__(self, history, tools):
        from agent import ServiceError
        self.calls += 1
        if self.fail:
            raise ServiceError('model_unreachable')
        step = self.script.pop(0) if self.script else {'say': 'تمام'}
        if 'tool' in step:
            return {'output': [{'type': 'function_call', 'name': step['tool'], 'call_id': 'c%d' % self.calls,
                                'arguments': json.dumps(step['args'])}]}
        return {'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': step['say']}]}]}


class BondokCase(OpsCase):
    def setUp(self):
        super().setUp()
        self.posts = []
        self.env = {'SLACK_OWNER_ID': OWNER, 'SLACK_TEAM_ID': 'T1', 'PIPELINE_DB': str(self.dir / 'state.sqlite'),
                    'BONDOK_STATE_DIR': str(self.dir / 'bondok')}

    def core(self, model):
        return Core(self.env, model=model, ops=self.ops, post=lambda t, th=None: self.posts.append((t, th)))

    def msg(self, core, text, user=OWNER, ts='1700000000.000100', thread=None):
        return core.handle({'user': user, 'text': text, 'ts': ts, 'channel': 'C1', 'thread_ts': thread})


class Scenario14SlackAuthentication(unittest.TestCase):
    def test_workspace_channel_bot_filters(self):
        ok = {'team_id': 'T1', 'event': {'type': 'app_mention', 'channel': 'C1', 'user': OWNER}}
        self.assertTrue(accept_event(ok, 'C1', 'T1', lambda t: False, 'UBOT'))
        for bad in ({**ok, 'team_id': 'T2'}, {**ok, 'event': {**ok['event'], 'channel': 'C2'}},
                    {**ok, 'event': {**ok['event'], 'bot_id': 'B1'}}, {**ok, 'event': {**ok['event'], 'user': 'UBOT'}},
                    {**ok, 'event': {'type': 'message', 'channel': 'C1', 'user': OWNER}}):
            self.assertFalse(accept_event(bad, 'C1', 'T1', lambda t: False, 'UBOT'))


class ExplicitIntent(unittest.TestCase):
    def test_owner_words_decide(self):
        self.assertTrue(explicit('change_format', {'format': 'Post'}, 'حوّل الستوري دي لبوست'))
        self.assertTrue(explicit('change_format', {'format': 'Post'}, 'Change this Story to Post'))
        self.assertFalse(explicit('change_format', {'format': 'Post'}, 'ايه رأيك نخليها أقصر؟'))
        self.assertFalse(explicit('change_format', {'format': 'Story'}, 'Change this Story to Post'))
        self.assertTrue(explicit('confirm_topaz', {}, 'توباز خلص للفيديو ده'))
        self.assertFalse(explicit('confirm_topaz', {}, 'توباز لسه مش خلص'))


class Scenario17ModelCannotChangeFormat(BondokCase):
    def test_model_initiated_conversion_becomes_proposal(self):
        self.observe(monday_item('300', fmt='Story', code='LIP3'))
        model = FakeModel([{'tool': 'change_format', 'args': {'item': '300', 'format': 'Post'}},
                           {'say': 'اقترحت تحويلها لبوست'}])
        reply = self.msg(self.core(model), 'الستوري دي طويلة، تقترح ايه؟')
        self.assertEqual(self.item('300')['format'], 'Story')
        self.assertIn('Proposal B-', reply)
        pid = reply.split('Proposal ')[1].split(' ')[0]
        # The approval is deterministic: no model call.
        core = self.core(FakeModel(fail=True))
        out = self.msg(core, f'اعتمد {pid}', ts='1700000000.000200', thread='1700000000.000100')
        self.assertIn('Executed', out)
        self.assertEqual(self.item('300')['format'], 'Post')

    def test_explicit_owner_instruction_executes_once(self):
        self.observe(monday_item('301', fmt='Story', code='KE4'))
        model = FakeModel([{'tool': 'change_format', 'args': {'item': 'KE4', 'format': 'Post'}}, {'say': 'تم'}])
        reply = self.msg(self.core(model), 'حوّل KE4 لبوست')
        self.assertIn('Done: change format', reply)
        self.assertEqual(self.item('301')['format'], 'Post')

    def test_member_cannot_change_or_approve(self):
        self.observe(monday_item('302', fmt='Story', code='MA5'))
        model = FakeModel([{'tool': 'pause_item', 'args': {'item': '302', 'reason': 'x'}}, {'say': 'ok'}])
        self.msg(self.core(model), 'pause MA5', user='U-OTHER')
        self.assertEqual(self.item('302')['owner_state'], 'active')


class Scenario07SlackDuplicates(BondokCase):
    def test_same_event_twice_runs_effect_once(self):
        self.make_ready('310', 'Story')
        script = [{'tool': 'pause_item', 'args': {'item': '310', 'reason': 'hold'}}, {'say': 'وقفته'}]
        core = self.core(FakeModel(script * 2))
        first = self.msg(core, 'وقف 310')
        second = self.msg(core, 'وقف 310')            # Slack redelivery: same ts
        self.assertIn('Done', first)
        self.assertIn('Already handled', second)
        with self.ops.store.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM ops_commands WHERE op='pause'").fetchone()[0], 1)

    def test_event_queue_dedupes_before_model(self):
        core = self.core(FakeModel())
        e = {'user': OWNER, 'text': 'hi', 'ts': '1.1', 'channel': 'C1'}
        self.assertTrue(core.store.enqueue_event('T1:C1:1.1', e))
        self.assertFalse(core.store.enqueue_event('T1:C1:1.1', e))


class Scenario19ZeroModelRoutine(BondokCase):
    def test_routine_paths_make_no_model_calls(self):
        model = FakeModel()
        core = self.core(model)
        self.make_ready('320', 'Story')
        self.ops.repair()
        self.ops.due('w')
        self.ops.submit(__import__('support').owner_cmd('p', 'pause', '320'))
        core.deliver_notifications()
        core.watchdog()
        self.msg(core, 'اعتمد B-DEADBEEF')
        self.assertEqual(model.calls, 0)


class Scenario29ModelOutage(BondokCase):
    def test_outage_guesses_nothing_and_publishing_continues(self):
        self.make_ready('330', 'Story')
        core = self.core(FakeModel(fail=True))
        reply = self.msg(core, 'وقف 330 وغيّره لبوست')
        self.assertIn('did not act', reply)
        it = self.item('330')
        self.assertEqual((it['owner_state'], it['format']), ('active', 'Story'))
        from datetime import timedelta
        self.clock.set(rules.instant(self.res('330')['slot']) + timedelta(seconds=10))
        self.assertTrue(self.ops.claim('330', 'wf2')['claimed'])


class AcceptedIsNotCompleted(BondokCase):
    def test_recheck_reports_accepted(self):
        self.make_ready('340', 'Story')
        model = FakeModel([{'tool': 'request_recheck', 'args': {'item': '340'}}, {'say': 'هراجعه'}])
        reply = self.msg(self.core(model), 'افحص 340 تاني')
        self.assertIn('Accepted (not yet performed)', reply)
        self.assertNotIn('Done', reply)


class Notifications(BondokCase):
    def test_outcome_unknown_notified_once_and_delivered(self):
        self.make_ready('350', 'Story')
        from datetime import timedelta
        self.clock.set(rules.instant(self.res('350')['slot']) + timedelta(seconds=10))
        cl = self.ops.claim('350', 'w')
        self.ops.container(cl['attempt_id'], 'w', cl['fence'], 'C')
        self.ops.commit(cl['attempt_id'], 'w', cl['fence'], container_status='FINISHED')
        self.ops.result(cl['attempt_id'], 'w', error='timeout')
        core = self.core(FakeModel())
        core.deliver_notifications(50)
        core.deliver_notifications(50)
        unknown = [p for p, _ in self.posts if 'outcome unknown' in p]
        self.assertEqual(len(unknown), 1)

    def test_helper_error_log_alert_without_database(self):
        (self.dir / 'errors.log').write_text(json.dumps({'path': '/v2/publish/due', 'kind': 'OperationalError',
                                                         'error': 'attempt to write a readonly database'}) + '\n')
        core = self.core(FakeModel())
        alerts = core.watchdog()
        self.assertTrue(any('readonly database' in a for a in alerts))
        self.assertEqual(core.watchdog(), [])     # unchanged: quiet


if __name__ == '__main__':
    unittest.main()
