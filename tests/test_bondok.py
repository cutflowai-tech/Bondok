"""Bondok integration contract: Slack identity, approvals, cost-aware routing.

Uses the real handler (waset_ops) and the real Bondok Core with a fake model
and a fake Slack poster; no network, no paid model calls.
"""
import json
import sqlite3
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
        self.assertIn('معملتش أي حاجة', reply)
        self.assertNotIn('B-X', reply)
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


class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakeHttp:
    """Records request budgets; answers from a list of (status, body)."""

    def __init__(self, answers):
        self.answers, self.budgets, self.inputs = list(answers), [], []

    def post(self, url, json=None, headers=None):
        self.budgets.append(json['max_output_tokens'])
        self.inputs.append(json['input'])
        return FakeResponse(*self.answers.pop(0))


def real_model(answers):
    """The production Model class with a fake transport (no network, no httpx needed)."""
    from agent import Model
    m = Model.__new__(Model)
    m.key, m.model, m.policy, m.calls, m.last_budget = 'k', 'openai/gpt-6.1-sol', 'policy', 0, None
    m._httpx = type('X', (), {'HTTPError': OSError})
    m.http = FakeHttp(answers)
    return m


def say(text):
    return (200, {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': text}]}]})


REFUSAL = {'error': {'code': 402, 'message': 'This request requires more credits, or fewer max_tokens. You requested '
                                            'up to 3000 tokens, but can only afford 2614. To increase, visit '
                                            'https://openrouter.ai/workspaces/default/keys/abc'}}
BROKE = {'error': {'code': 402, 'message': 'This request requires more credits, or fewer max_tokens. You requested '
                                          'up to 3000 tokens, but can only afford 120.'}}


class ModelCreditsAndConversation(BondokCase):
    """Production incident 2026-10-09 20:22: OpenRouter 402 (credit) shown as a generic English outage."""

    def test_affordable_budget_is_retried_once_and_answers(self):
        model = real_model([(402, REFUSAL), say('تمام، البوستين متخطيين عشان اتعمل لهم Skip.')])
        core = self.core(model)
        reply = self.msg(core, 'ليه معمولهم تخطي؟')
        self.assertIn('متخطيين', reply)
        self.assertEqual(model.http.budgets, [3000, 2514])

    def test_credit_exhausted_is_named_in_egyptian_arabic_once_per_thread(self):
        model = real_model([(402, BROKE), (402, BROKE), (402, BROKE)])
        core = self.core(model)
        first = self.msg(core, 'ليه معمولهم تخطي؟', ts='1700000000.000200', thread='1700000000.000100')
        self.assertIn('رصيد OpenRouter', first)
        self.assertIn('معملتش أي حاجة', first)
        self.assertNotIn('B-X', first)
        self.assertNotIn('language model', first)
        self.assertEqual(model.http.budgets, [3000])          # no retry when the affordable budget is too small
        second = self.msg(core, 'ايه المشكلة؟', ts='1700000000.000300', thread='1700000000.000100')
        self.assertLess(len(second), len(first))               # not the same full notice again
        self.assertIn('لسه', second)
        other = self.msg(core, 'ايه الأخبار؟', ts='1700000000.000400')
        self.assertIn('محتاجين نشحن', other)                  # a different thread gets the full notice

    def test_other_failures_are_not_reported_as_credit(self):
        core = self.core(real_model([(500, {'error': {'message': 'upstream'}})]))
        reply = self.msg(core, 'ايه الأخبار؟')
        self.assertIn('الموديل مش متاح', reply)
        self.assertNotIn('رصيد', reply)

    def test_arabic_follow_ups_keep_thread_context(self):
        model = real_model([say('مفيش حاجة محجوزة الأسبوع الجاي.'), say('البوستين معمول لهم Skip.'),
                            say('عشان المالك عمل لهم تخطي من البورد.')])
        core = self.core(model)
        th = '1700000000.000100'
        self.msg(core, 'ايه اللي هينزل الفترة الجاية ع البيدج', ts=th)
        self.msg(core, 'ايه المشكلة عشان يبقى فيه بوستات جاهزة؟', ts='1700000000.000200', thread=th)
        r = self.msg(core, 'ليه معمولهم تخطي؟', ts='1700000000.000300', thread=th)
        self.assertIn('تخطي', r)
        last = model.http.inputs[-1]
        texts = [m.get('content') for m in last if isinstance(m, dict) and m.get('role')]
        self.assertEqual([m['role'] for m in last if isinstance(m, dict) and m.get('role')],
                         ['user', 'assistant', 'user', 'assistant', 'user'])
        self.assertIn('مفيش حاجة محجوزة الأسبوع الجاي.', texts)
        self.assertTrue(texts[-1].endswith('ليه معمولهم تخطي؟'))

    def test_placeholder_approval_id_is_never_shown(self):
        core = self.core(real_model([say('لو موافق اكتب اعتمد B-XXXXXXXX')]))
        reply = self.msg(core, 'ينفع نغير الميعاد؟')
        self.assertNotIn('B-XXXXXXXX', reply)


if __name__ == '__main__':
    unittest.main()


class ExplicitIntentFailsClosed(unittest.TestCase):
    """Audit C1/C2: questions, negations and words that merely contain a keyword are not owner instructions."""

    def test_not_instructions(self):
        cases = [
            ('confirm_topaz', {}, 'Topaz is not done for LIP12'), ('confirm_topaz', {}, 'is topaz done?'),
            ('confirm_topaz', {}, "topaz isn't finished"), ('confirm_topaz', {}, 'هل توباز اتعمل؟'),
            ('change_format', {'format': 'Post'}, 'postpone LIP12'),
            ('change_format', {'format': 'Post'}, 'مش عايز احوله لبوست'),
            ('change_format', {'format': 'Post'}, 'غير الميعاد لابريل'),
            ('change_format', {'format': 'Story'}, 'change the storyboard note'),
            ('resume', {}, 'ايه الشغل النهارده؟'), ('resume', {}, 'متشغلوش'), ('resume', {}, 'do not resume LIP12'),
            ('skip', {}, 'cancel the pause on LIP12'),
            ('resolve_outcome', {'outcome': 'not_published'}, 'هو 350 مش منشور؟'),
            ('resolve_outcome', {'outcome': 'not_published'}, '350 not published'),
            ('resolve_outcome', {'outcome': 'published'}, 'هل نزل LIP12؟'),
            ('resolve_outcome', {'outcome': 'published'}, 'has LIP12 posted?'),
            ('approve_caption', {}, 'مش موافق على الكابشن'), ('approve_caption', {}, 'look at the caption'),
            ('update_caption', {'text': 'the LIP12 caption'}, 'what do you think of the LIP12 caption?'),
        ]
        for op, args, text in cases:
            self.assertFalse(explicit(op, args, text), (op, text))

    def test_plain_instructions_still_work(self):
        cases = [
            ('change_format', {'format': 'Post'}, 'حوّل الستوري دي لبوست'),
            ('change_format', {'format': 'Post'}, 'Change this Story to Post'),
            ('change_format', {'format': 'Story'}, 'خليها ستوري'),
            ('confirm_topaz', {}, 'توباز خلص للفيديو ده'), ('confirm_topaz', {}, 'Topaz done for LIP12'),
            ('resume', {}, 'كمل LIP12'), ('resume', {}, 'resume LIP12'), ('resume', {}, 'شغله تاني'),
            ('skip', {}, 'تخطى LIP12'), ('skip', {}, 'skip LIP12'),
            ('resolve_outcome', {'outcome': 'published'}, 'LIP12 اتنشر خلاص'),
            ('approve_caption', {}, 'اعتمد الكابشن'), ('approve_caption', {}, 'تمام'),
            ('update_caption', {'text': 'Sunlight sets the pace 🔥 #reels'},
             'غير الكابشن لـ: Sunlight sets the pace 🔥 #reels'),
        ]
        for op, args, text in cases:
            self.assertTrue(explicit(op, args, text), (op, text))


class UncertainOutcomeNeedsApproval(BondokCase):
    def test_not_published_is_always_a_proposal(self):
        self.make_ready('350', 'Story')
        with self.ops.store.tx() as c:
            c.execute("UPDATE ops_items SET publication='outcome_unknown' WHERE item_id='350'")
        core = self.core(FakeModel([{'tool': 'resolve_publication', 'args': {'item': '350', 'published': False}},
                                    {'say': 'تمام'}]))
        reply = self.msg(core, '350 مش منشور، رجعه للجدول')
        self.assertIn('Proposal', reply)
        self.assertEqual(self.item('350')['publication'], 'outcome_unknown')


class FailAfter:
    """Scripted model that fails with a ServiceError on call number `fail_at`."""
    model = 'fake'

    def __init__(self, script, fail_at):
        self.script, self.fail_at, self.calls = list(script), fail_at, 0

    def __call__(self, history, tools):
        from agent import ServiceError
        self.calls += 1
        if self.calls == self.fail_at:
            raise ServiceError('model_http_502')
        step = self.script.pop(0)
        if 'tools' in step:
            return {'output': [{'type': 'function_call', 'name': t, 'call_id': f'c{self.calls}-{i}',
                                'arguments': json.dumps(a)} for i, (t, a) in enumerate(step['tools'])]}
        return {'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': step['say']}]}]}


class SlackAuditHigh(BondokCase):
    """Audit H1-H4, M1 (Bondok service)."""

    def test_actions_taken_before_model_failure_are_reported(self):              # H1
        self.make_ready('700', 'Story')
        core = self.core(FailAfter([{'tools': [('pause_item', {'item': '700', 'reason': 'x'})]}], fail_at=2))
        reply = self.msg(core, 'وقف 700')
        self.assertEqual(self.item('700')['owner_state'], 'paused')
        self.assertNotIn('معملتش أي حاجة', reply)
        self.assertIn('pause item', reply)

    def test_more_than_six_calls_get_outputs(self):                               # H2
        for i in range(7):
            self.make_ready(str(710 + i), 'Story', code='LIP%d' % (10 + i))
        seen = []

        class Strict(FailAfter):
            def __call__(s, history, tools):
                calls = {x['call_id'] for x in history if isinstance(x, dict) and x.get('type') == 'function_call'}
                outs = {x['call_id'] for x in history if isinstance(x, dict) and x.get('type') == 'function_call_output'}
                seen.append(calls - outs)
                return FailAfter.__call__(s, history, tools)
        core = self.core(Strict([{'tools': [('pause_item', {'item': str(710 + i)}) for i in range(7)]},
                                 {'say': 'وقفت ٦ والسابع محتاج طلب تاني'}], fail_at=99))
        reply = self.msg(core, 'وقف كل دول')
        self.assertEqual(seen[-1], set())                     # no unanswered function_call is sent back
        self.assertIn('وقفت', reply)

    def test_replies_in_proposal_notification_thread_are_accepted(self):          # H3
        posted = []
        core = Core(self.env, model=FakeModel(), ops=self.ops, post=lambda t, th=None: posted.append(t) or {'ts': '1700000009.000100'})
        with self.ops.store.tx() as c:
            self.ops.notify(c, 'proposal:B-1', 'x', None, proposal_id='B-1')
        core.deliver_notifications()
        known = lambda ts: bool(core.store.kv('thread:' + ts))
        body = {'team_id': 'T1', 'event': {'type': 'message', 'channel': 'C1', 'user': OWNER,
                                           'thread_ts': '1700000009.000100', 'text': 'اعتمد B-1'}}
        self.assertTrue(accept_event(body, 'C1', 'T1', known, 'UBOT'))

    def test_watchdog_runs_when_notifications_fail(self):                         # H4
        core = self.core(FakeModel())
        core.deliver_notifications = lambda: (_ for _ in ()).throw(sqlite3.DatabaseError('unreadable'))
        core.ops.health = lambda: (_ for _ in ()).throw(sqlite3.DatabaseError('unreadable'))
        posted = []
        core.post = lambda t, th=None: posted.append(t)
        try:
            core.deliver_notifications()
        except sqlite3.DatabaseError:
            pass
        core.run_watchdog()
        self.assertTrue(any('not readable' in p for p in posted), posted)

    def test_bad_error_log_line_does_not_disable_watchdog(self):                  # M1
        core = self.core(FakeModel())
        log = self.dir / 'errors.log'
        log.write_text('{"path": "/v2/prep/step", "kind": "storage", "error": "x"}\n{"path": "/v2/pr\n')
        self.assertTrue(core.watchdog())
        self.assertEqual(core.watchdog(), [])

    def test_undelivered_alert_is_reported_again(self):
        core = self.core(FakeModel())
        core.ops.health = lambda: (_ for _ in ()).throw(sqlite3.DatabaseError('unreadable'))
        def broken(t, th=None):
            raise OSError('slack down')
        core.post = broken
        with self.assertRaises(OSError):
            core.run_watchdog()
        posted = []
        core.post = lambda t, th=None: posted.append(t)
        core.run_watchdog()
        self.assertTrue(any('not readable' in p for p in posted))


class TopazFromSlackBinding(BondokCase):
    """Audit MP5: an owner's "Topaz done" must not attach to a file version selected moments ago."""

    def test_recent_file_change_needs_approval_naming_the_file(self):
        self.observe(monday_item('720', fmt='Story'))
        self.select('720', 1)
        self.duration('720', 30.0, 1)
        self.select('720', 2)                                  # editor uploaded v2, WF1 selected it
        self.duration('720', 30.0, 2)
        core = self.core(FakeModel([{'tool': 'confirm_topaz', 'args': {'item': '720'}}, {'say': 'تمام'}]))
        reply = self.msg(core, 'توباز خلص للفيديو 720')
        self.assertIn('Proposal', reply)
        self.assertIn('video_v2.mp4', reply)
        self.assertIsNone(self.item('720')['topaz_asset'])

    def test_stable_selection_executes_bound_to_that_version(self):
        self.observe(monday_item('721', fmt='Story'))
        self.select('721', 1)
        self.duration('721', 30.0, 1)
        self.clock.advance(7 * 3600)
        core = self.core(FakeModel([{'tool': 'confirm_topaz', 'args': {'item': '721'}}, {'say': 'تمام'}]))
        self.msg(core, 'توباز خلص للفيديو 721')
        self.assertEqual(self.item('721')['topaz_asset'], 'id:FILE1@rev1')
