"""R5 language contract for Bondok (T-LANG, T-APPROVAL, T-COST, Bondok part of T-INFRA).

Contract: docs/OWNER_AUTHORITY_CONTRACT.md §1 (Slack identity), §3 (owner decisions), §4 (Slack language), §7
(approvals). One class per Round 5 register entry (R5_<ID>_...). Every row of the directive's §9 language fixture
table is an executable test that asserts the exact allowed effect and that nothing else changed.

The model is scripted (no network, no paid calls). Where a fixture is a false positive in R5, the script plays the
*worst plausible* model: it calls the protected tool and claims an executable speech act. Trusted code must still
refuse: the model proposes meaning, it never supplies authorization. A passing mocked test does not prove that the
live model or live Slack behave this way (see the report for what only an authorized live check can confirm).
"""
import ast
import json
import sys
import time
import unittest
from datetime import timedelta
from unittest import mock

from support import ROOT, Command, monday_item, owner_cmd, rules
from test_bondok import OWNER, BondokCase

sys.path.insert(0, str(ROOT / 'bondok'))
from app import Core, accept_event  # noqa: E402

TH = '1700000000.000100'
TOMORROW = '2026-10-11'                       # T0 is Sat 2026-10-10 15:00 Cairo


def act(tool, quote, speech='imperative', targets='', polarity='positive', **args):
    """One model tool call carrying the structured meaning the model proposes."""
    return (tool, {**args, 'meaning': {'speech_act': speech, 'polarity': polarity, 'targets_as_said': targets,
                                       'evidence_quote': quote}})


class Script:
    """Scripted Responses-API model: each step is a list of tool calls or a text answer. Counts calls."""
    model = 'fake'

    def __init__(self, *steps, fail=False):
        self.steps, self.calls, self.fail, self.inputs = list(steps), 0, fail, []

    def __call__(self, history, tools):
        from agent import ServiceError
        self.calls += 1
        self.inputs.append(history)
        if self.fail:
            raise ServiceError('model_unreachable')
        step = self.steps.pop(0) if self.steps else 'تمام'
        if isinstance(step, str):
            return {'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': step}]}]}
        return {'output': [{'type': 'function_call', 'name': t, 'call_id': f'c{self.calls}-{i}',
                            'arguments': json.dumps(a)} for i, (t, a) in enumerate(step)]}


class LangCase(BondokCase):
    def setUp(self):
        super().setUp()
        self.n, self.sent = 0, []

    def core(self, model=None):
        return Core(self.env, model=model or Script(), ops=self.ops, post=self._post)

    def _post(self, text, thread=None):
        self.n += 1
        ts = f'1700009{self.n:03d}.000100'
        self.posts.append((text, thread))
        self.sent.append((text, thread, ts))
        return {'ts': ts}

    def root_of(self, needle):
        """Slack ts of the last notification whose text contains `needle` (its thread root)."""
        hits = [ts for t, th, ts in self.sent if needle in t and th is None]
        self.assertTrue(hits, [t for t, _, _ in self.sent])
        return hits[-1]

    def say(self, core, text, thread=TH, user=OWNER):
        self.n += 1
        return core.handle({'user': user, 'text': text, 'ts': f'1700000{self.n:03d}.000100', 'channel': 'C1',
                            'thread_ts': thread})

    def seed(self, core, *codes, thread=TH):
        """A thread in which Bondok listed these items (the thread's named set)."""
        core.model.steps.insert(0, 'دول اللي اتكلمنا عنهم: ' + '، '.join(codes))
        return self.say(core, 'ايه الأخبار؟', thread=thread)

    def snap(self):
        out = {}
        with self.ops.store.read() as c:
            for r in c.execute('SELECT * FROM ops_items ORDER BY item_id'):
                res = self.ops.reservation(c, r['item_id'])
                out[r['item_id']] = {
                    'owner_state': r['owner_state'], 'format': r['format'], 'topaz': r['topaz_asset'],
                    'publication': r['publication'], 'caption': r['caption'], 'caption_state': r['caption_state'],
                    'hold': (json.loads(r['hold'] or 'null') or {}).get('kind'), 'slot': res['slot'] if res else None,
                    'requested': r['requested_at'], 'window': (json.loads(r['observed'] or '{}') or {}).get('_window')}
        return out

    def changed(self, before):
        after = self.snap()
        return {k for k in after if after[k] != before.get(k)}

    def owner_ops(self):
        """Owner commands caused by Slack messages (fixture setup such as make_ready's Topaz confirmation and
        self.pause() is issued as the owner too, but is not something the conversation did)."""
        with self.ops.store.read() as c:
            return [(r['op'], r['item_id'], r['state']) for r in c.execute(
                "SELECT op,item_id,state FROM ops_commands WHERE actor=? AND id LIKE 'slack:%' "
                "ORDER BY created, rowid", (OWNER,))]

    def executed(self):
        return [(op, i) for op, i, st in self.owner_ops() if st in ('completed', 'accepted')]

    def cairo_day(self, iid):
        res = self.res(iid)
        return rules.instant(res['slot']).astimezone(rules.TZ).date().isoformat() if res else None

    def bystander(self):
        self.make_ready('999', 'Story', code='BY99', name='Bystander BY99')

    def pause(self, *ids):
        for i in ids:
            self.ops.submit(owner_cmd(self.rid('p'), 'pause', i, reason='owner'))

    def stable_file(self, iid, code, n=1):
        self.observe(monday_item(iid, fmt='Story', code=code, name=f'Clip {code}'))
        self.select(iid, n)
        self.duration(iid, 30.0, n)
        self.clock.advance(7 * 3600)

    def unknown_outcome(self, iid, code):
        self.make_ready(iid, 'Story', code=code)
        self.clock.set(rules.instant(self.res(iid)['slot']) + timedelta(seconds=10))
        cl = self.ops.claim(iid, 'w')
        self.ops.container(cl['attempt_id'], 'w', cl['fence'], 'C')
        self.ops.commit(cl['attempt_id'], 'w', cl['fence'], container_status='FINISHED')
        self.ops.result(cl['attempt_id'], 'w', error='timeout')
        return cl['attempt_id']


# ====================================================================== B5: false positives never act
class R5_B5_FalsePositivesNeverAct(LangCase):
    """B5: future/question/conditional/missed-expectation/other-destination statements executed protected actions."""

    def topaz_case(self, text, speech='completed_statement'):
        self.stable_file('500', 'LIP50')
        self.bystander()
        core = self.core(Script([act('confirm_topaz', text, speech, item='LIP50')], 'تمام'))
        before = self.snap()
        reply = self.say(core, text + ' LIP50' if 'LIP50' not in text else text)
        self.assertIsNone(self.item('500')['topaz_asset'], text)
        self.assertEqual(self.changed(before), set(), text)
        self.assertEqual(self.executed(), [], text)
        return reply

    def test_topaz_will_be_ready_tomorrow_arabic(self):
        self.topaz_case('توباز هيبقى جاهز بكره')

    def test_topaz_will_be_done_tomorrow_english(self):
        self.topaz_case('Topaz will be done tomorrow')

    def test_is_topaz_done_with_and_without_question_mark(self):
        self.topaz_case('هو التوباز خلص')

    def test_is_topaz_done_question_mark(self):
        self.topaz_case('هو التوباز خلص؟')

    def test_editor_is_doing_topaz_tonight(self):
        self.topaz_case('المونتير بيعمل توباز دلوقتي لـ LIP50 وهيبقى جاهز بالليل')

    def test_conditional_topaz_is_not_proof(self):
        self.topaz_case('لو خلص توباز ابقى جهزه')

    def test_editor_said_topaz_done_is_a_quotation(self):
        self.topaz_case('المونتير قال توباز خلص بس انا مش متأكد')

    def test_missed_expectation_is_an_inquiry(self):
        self.unknown_outcome('510', 'LIP51')
        self.bystander()
        text = 'كان المفروض يبقى منشور امبارح'
        core = self.core(Script([act('report_published', text, 'completed_statement', item='LIP51',
                                     destination='studio_account')],
                                [act('resolve_publication', text, 'completed_statement', item='LIP51',
                                     published=True, media_id=None)], 'هتأكد'))
        before = self.snap()
        self.say(core, 'LIP51 ' + text)
        self.assertEqual(self.item('510')['publication'], 'outcome_unknown')
        self.assertEqual(self.changed(before), set())

    def test_supposed_to_be_posted_yesterday_english(self):
        self.unknown_outcome('511', 'LIP52')
        text = 'LIP52 was supposed to be posted yesterday, check Instagram'
        core = self.core(Script([act('resolve_publication', text, 'completed_statement', item='LIP52',
                                     published=True, media_id=None)], 'ok'))
        self.say(core, text)
        self.assertEqual(self.item('511')['publication'], 'outcome_unknown')

    def test_published_on_another_account_is_not_a_studio_record(self):
        self.unknown_outcome('512', 'LIP53')
        text = 'اتنشرت على الحساب التاني'
        core = self.core(Script([act('report_published', text, 'completed_statement', item='LIP53',
                                     destination='studio_account')],
                                [act('resolve_publication', text, 'completed_statement', item='LIP53',
                                     published=True, media_id=None)], 'تمام'))
        before = self.snap()
        reply = self.say(core, 'LIP53 ' + text)
        self.assertEqual(self.item('512')['publication'], 'outcome_unknown')
        self.assertEqual(self.changed(before), set())
        self.assertIn('❓', reply)                       # clarify the destination, nothing recorded

    def test_can_we_publish_tomorrow_or_wait_is_a_question(self):
        self.make_ready('520', 'Story', code='LIP54')
        self.pause('520')
        self.bystander()
        text = 'ينفع انشرهم بكره ولا نستنى'
        core = self.core(Script([act('resume_item', text, 'imperative', 'انشرهم', item='LIP54', on_date=TOMORROW,
                                     not_before=None)], 'ممكن'))
        self.seed(core, 'LIP54')
        before = self.snap()
        self.say(core, text)
        self.assertEqual(self.changed(before), set())
        self.assertEqual(self.item('520')['owner_state'], 'paused')

    def test_okay_thanks_never_approves_a_caption(self):
        self.make_ready('530', 'Post', code='LIP55', approve_caption=False)
        self.ops.submit(Command(self.rid(), 'caption_draft', 'service:wf1', 'service:wf1', '530',
                                {'input_hash': 'h530', 'text': R5_A8_DraftApprovalInPlainWords.TEXT}))
        model = Script([act('approve_existing_caption', 'تمام شكرا', 'imperative', item='LIP55')],
                       [act('approve_caption_draft', 'تمام شكرا', 'imperative', item='LIP55')], 'العفو')
        core = self.core(model)
        core.deliver_notifications(50)
        root = self.root_of('Caption draft')
        before = self.snap()
        for text in ('تمام شكرا', 'مدهش'):
            self.say(core, text, thread=root)
        self.assertNotEqual(self.item('530')['caption_state'], 'approved')
        self.assertEqual(self.changed(before), set())
        self.assertEqual(model.calls, 0)                 # acknowledgments: deterministic, no model call

    def test_cancel_that_never_skips_an_item(self):
        self.make_ready('540', 'Story', code='LIP56')
        self.bystander()
        core = self.core(Script([act('skip_item', 'cancel that', 'imperative', 'that', item='LIP56', reason='x')],
                                'ok'))
        self.seed(core, 'LIP56')
        before = self.snap()
        self.say(core, 'cancel that')
        self.assertEqual(self.item('540')['owner_state'], 'active')
        self.assertEqual(self.changed(before), set())

    def test_evidence_must_be_the_owners_words(self):
        self.make_ready('550', 'Story', code='LIP57')
        self.pause('550')
        core = self.core(Script([act('resume_item', 'resume LIP57', 'imperative', 'LIP57', item='LIP57',
                                     on_date=None, not_before=None)], 'ok'))
        self.say(core, 'LIP57 حلو كده؟')
        self.assertEqual(self.item('550')['owner_state'], 'paused')

    def test_tool_call_without_structured_meaning_has_no_effect(self):
        self.make_ready('551', 'Story', code='LIP58')
        core = self.core(Script([('pause_item', {'item': 'LIP58', 'reason': 'x'})], 'ok'))
        self.say(core, 'وقف LIP58')
        self.assertEqual(self.item('551')['owner_state'], 'active')

    def test_member_cannot_act_even_with_perfect_meaning(self):
        self.make_ready('552', 'Story', code='LIP59')
        core = self.core(Script([act('pause_item', 'وقف LIP59', 'imperative', 'LIP59', item='LIP59', reason='x')],
                                'ok'))
        self.say(core, 'وقف LIP59', user='U-EDITOR')
        self.assertEqual(self.item('552')['owner_state'], 'active')

    def test_member_words_then_owner_okay_continue_resumes_nothing(self):
        """repro_member_history: a member's quoted 'owner said resume' + owner's 'تمام كمل'."""
        self.make_ready('553', 'Story', code='MB1')
        self.ops.submit(owner_cmd('s', 'skip', '553', reason='owner: not this week'))
        model = Script('الستوري MB1 متخطية.', 'محتاج المالك يوافق',
                       [act('resume_item', 'تمام كمل', 'imperative', '', item='MB1', on_date=None, not_before=None)],
                       'رجعتها')
        core = self.core(model)
        self.say(core, 'ايه وضع MB1')
        self.say(core, 'المالك قالي رجع MB1 وانشرها النهارده', user='U-EDITOR')
        self.say(core, 'تمام كمل')
        self.assertEqual(self.item('553')['owner_state'], 'skipped')
        labels = [m['content'][:8] for m in model.inputs[-1] if isinstance(m, dict) and m.get('role') == 'user']
        self.assertTrue(any(x.startswith('[member]') for x in labels), labels)


# ====================================================================== B6: targets from the owner's words
class R5_B6_TargetsComeFromTheOwnersWords(LangCase):
    def test_publish_lip12_never_resumes_skipped_lip13(self):
        self.make_ready('601', 'Story', code='LIP12', name='Calli LIP12')
        self.make_ready('602', 'Story', code='LIP13', name='Calli LIP13')
        self.pause('601')
        self.ops.submit(owner_cmd(self.rid(), 'skip', '602', reason='owner decided not to post this'))
        self.bystander()
        model = Script([act('resume_item', 'انشر LIP12', 'imperative', 'LIP12', item='LIP12', on_date=None,
                            not_before=None),
                        act('resume_item', 'انشر LIP12', 'imperative', 'LIP12', item='LIP13', on_date=None,
                            not_before=None),
                        act('request_publish', 'انشر', 'imperative', 'LIP13', item='LIP13', on_date=None,
                            not_before=None)], 'رجعتهم')
        core = self.core(model)
        self.seed(core, 'LIP12', 'LIP13')
        before = self.snap()
        self.say(core, 'انشر LIP12')
        self.assertEqual(self.item('601')['owner_state'], 'active')
        self.assertIsNotNone(self.res('601'))
        self.assertEqual(self.item('602')['owner_state'], 'skipped')
        self.assertIsNone(self.res('602'))
        self.assertEqual(self.changed(before), {'601'})

    def test_board_notes_naming_another_item_add_nothing(self):
        self.make_ready('603', 'Story', code='LIP14')
        self.make_ready('604', 'Story', code='LIP15')
        self.pause('603', '604')
        model = Script([('get_item_status', {'item': 'LIP14'})],
                       [act('resume_item', 'رجع LIP14', 'imperative', 'LIP14', item='LIP14', on_date=None,
                            not_before=None),
                        act('resume_item', 'رجع LIP14', 'imperative', 'LIP15 (from Notes)', item='LIP15',
                            on_date=None, not_before=None)], 'تمام')
        core = self.core(model)
        self.say(core, 'رجع LIP14')
        self.assertEqual(self.item('603')['owner_state'], 'active')
        self.assertEqual(self.item('604')['owner_state'], 'paused')

    def test_them_means_the_items_named_in_the_thread(self):
        for i, code in enumerate(('ST1', 'ST2')):
            self.make_ready(str(610 + i), 'Story', code=code)
        self.make_ready('612', 'Story', code='ST3')
        self.pause('610', '611', '612')
        model = Script(*[[act('resume_item', 'رجعهم', 'imperative', 'رجعهم', item=c, on_date=None, not_before=None)
                          for c in ('ST1', 'ST2', 'ST3')]], 'تمام')
        core = self.core(model)
        self.seed(core, 'ST1', 'ST2')            # ST3 was never named in this thread
        self.say(core, 'رجعهم')
        self.assertEqual([self.item(i)['owner_state'] for i in ('610', '611', '612')], ['active', 'active', 'paused'])


# ====================================================================== A14: no broad pause from vague words
class R5_A14_PauseNeedsResolvedScope(LangCase):
    def five(self):
        ids = [str(1000 + i) for i in range(5)]
        for i, iid in enumerate(ids):
            self.make_ready(iid, 'Story', code=f'ST{i}')
        return ids

    def test_friday_vague_stop_pauses_nothing(self):
        ids = self.five()
        model = Script('تمام هنشرهم بكرة',
                       [act('pause_item', 'وقف كل ده خلاص', 'imperative', 'كل ده', item=i, reason='owner said stop')
                        for i in ids], 'وقفتهم')
        core = self.core(model)
        before = self.snap()
        self.say(core, 'انا عاوزك تنشرهم في موعدهم بكرا بس مش اكثر')
        reply = self.say(core, 'وقف كل ده خلاص، ده تضييع وقت وموارد')
        self.assertEqual([self.item(i)['owner_state'] for i in ids], ['active'] * 5)
        self.assertEqual(self.changed(before), set())
        self.assertIn('❓', reply)

    def test_stop_everything_running_asks_for_scope_then_yes_pauses_exactly_the_discussed_set(self):
        ids = self.five()
        model = Script([act('pause_item', 'وقف كل اللي شغال ده', 'imperative', 'كل اللي شغال ده', item=f'ST{i}',
                            reason='stop') for i in range(3)], 'تمام')
        core = self.core(model)
        self.seed(core, 'ST0', 'ST1', 'ST2')
        before = self.snap()
        reply = self.say(core, 'وقف كل اللي شغال ده')
        self.assertIn('❓', reply)
        self.assertEqual(self.changed(before), set())
        calls = model.calls
        self.say(core, 'اه')
        self.assertEqual(model.calls, calls)              # the answer is deterministic
        self.assertEqual([self.item(i)['owner_state'] for i in ids], ['paused'] * 3 + ['active'] * 2)

    def test_clear_stop_of_named_items_still_works(self):
        ids = self.five()
        model = Script([act('pause_item', 'بطل تنشرهم', 'imperative', 'تنشرهم', item=f'ST{i}', reason='stop')
                        for i in range(2)] +
                       [act('resume_item', 'بطل تنشرهم', 'imperative', 'تنشرهم', item='ST2', on_date=None,
                            not_before=None)], 'وقفتهم')
        core = self.core(model)
        self.seed(core, 'ST0', 'ST1')
        before = self.snap()
        self.say(core, 'بطل تنشرهم')
        self.assertEqual([self.item(i)['owner_state'] for i in ids], ['paused', 'paused'] + ['active'] * 3)
        self.assertEqual(self.changed(before), {ids[0], ids[1]})

    def test_wait_before_publishing_them_pauses_never_resumes(self):
        ids = self.five()
        self.pause(ids[2])
        model = Script([act('pause_item', 'استنى قبل ما تنشرهم', 'imperative', 'تنشرهم', item=f'ST{i}', reason='wait')
                        for i in range(2)] +
                       [act('resume_item', 'استنى قبل ما تنشرهم', 'imperative', 'تنشرهم', item='ST2',
                            on_date=None, not_before=None)], 'تمام')
        core = self.core(model)
        self.seed(core, 'ST0', 'ST1', 'ST2')
        self.say(core, 'استنى قبل ما تنشرهم')
        self.assertEqual([self.item(i)['owner_state'] for i in ids[:3]], ['paused', 'paused', 'paused'])


# ====================================================================== A15: constraints travel
class R5_A15_ConstraintsTravel(LangCase):
    def paused_set(self):
        ids = ['700', '701']
        for i, iid in enumerate(ids):
            self.make_ready(iid, 'Story', code=f'TM{i}')
        self.pause(*ids)
        return ids

    def test_publish_them_at_their_time_tomorrow_nothing_more(self):
        ids = self.paused_set()
        self.bystander()
        text = 'انشرهم في موعدهم بكرا بس مش اكثر'
        core = self.core(Script([act('resume_item', text, 'imperative', 'انشرهم', item=f'TM{i}', on_date=TOMORROW,
                                     not_before=None) for i in range(2)], 'تمام'))
        self.seed(core, 'TM0', 'TM1')
        before = self.snap()
        self.say(core, text)
        self.assertEqual([self.cairo_day(i) for i in ids], [TOMORROW, TOMORROW])
        self.assertEqual(self.changed(before), set(ids))

    def test_constraint_the_model_dropped_is_not_lost(self):
        ids = self.paused_set()
        text = 'انشرهم في موعدهم بكرا بس مش اكثر'
        core = self.core(Script([act('resume_item', text, 'imperative', 'انشرهم', item=f'TM{i}', on_date=None,
                                     not_before=None) for i in range(2)], 'تمام'))
        self.seed(core, 'TM0', 'TM1')
        self.say(core, text)
        for i in ids:
            self.assertIn(self.cairo_day(i), (None, TOMORROW))       # never today
        self.assertEqual(self.cairo_day(ids[0]), TOMORROW)

    def test_let_them_go_up_tomorrow_requests_tomorrow(self):
        ids = ['710', '711']
        for i, iid in enumerate(ids):
            self.make_ready(iid, 'Story', code=f'TP{i}')
        self.assertEqual([self.cairo_day(i) for i in ids], ['2026-10-10'] * 2)
        for n, text in enumerate(('خليهم ينزلوا بكرة', 'نزلهم بكره')):
            with self.subTest(text=text):
                core = self.core(Script([act('request_publish', text, 'imperative', text.split()[0], item=f'TP{i}',
                                             on_date=TOMORROW, not_before=None) for i in range(2)], 'تمام'))
                th = f'17000005{n}0.000100'
                self.seed(core, 'TP0', 'TP1', thread=th)
                self.say(core, text, thread=th)
                self.assertEqual([self.cairo_day(i) for i in ids], [TOMORROW, TOMORROW])

    def test_not_before_tomorrow_is_a_window_not_a_publish(self):
        self.make_ready('720', 'Story', code='NB1')
        self.assertEqual(self.cairo_day('720'), '2026-10-10')
        text = 'متنشرش قبل بكرة'
        nb = rules.iso(rules.cairo_local(2026, 10, 11, 0, 0))
        core = self.core(Script([act('set_window', text, 'imperative', '', item='NB1', on_date=None,
                                     not_before='2026-10-11 00:00')], 'تمام'))
        self.seed(core, 'NB1')
        self.say(core, text)
        self.assertGreaterEqual(self.res('720')['slot'], nb)
        self.assertEqual(self.item('720')['owner_state'], 'active')

    def test_publish_it_at_four_seventeen_is_an_exact_off_grid_owner_time(self):
        self.make_ready('730', 'Story', code='OT1')
        text = 'انشره الساعة ٤:١٧'
        core = self.core(Script([act('request_reschedule', text, 'imperative', '', item='OT1',
                                     cairo_time='2026-10-10 16:17')], 'تمام'))
        self.seed(core, 'OT1')
        self.say(core, text)
        res = self.res('730')
        self.assertEqual(rules.display(rules.instant(res['slot'])), '2026-10-10 16:17 (Cairo)')
        self.assertEqual(res['owner_pinned'], 1)

    def test_a_time_the_owner_did_not_say_is_not_used(self):
        self.make_ready('731', 'Story', code='OT2')
        slot = self.res('731')['slot']
        text = 'انشره الساعة ٤:١٧'
        core = self.core(Script([act('request_reschedule', text, 'imperative', '', item='OT2',
                                     cairo_time='2026-10-10 16:30')], 'تمام'))
        self.seed(core, 'OT2')
        reply = self.say(core, text)
        self.assertEqual(self.res('731')['slot'], slot)
        self.assertIn('❓', reply)


# ====================================================================== M25: clear instructions work first time
class R5_M25_ClearInstructionsExecute(LangCase):
    def shown_topaz(self, text):
        self.stable_file('800', 'TZ1')
        self.bystander()
        core = self.core(Script([('get_item_status', {'item': 'TZ1'})], 'TZ1 مستني توباز للفايل video_v1.mp4',
                                [act('confirm_topaz', text, 'completed_statement', '', item='TZ1')], 'تمام'))
        self.say(core, 'ايه وضع TZ1؟')
        before = self.snap()
        reply = self.say(core, text)
        self.assertEqual(self.item('800')['topaz_asset'], 'id:FILE1@rev1', text)
        self.assertEqual(self.changed(before), {'800'})
        self.assertNotIn('Proposal', reply)
        return core

    def test_we_finished_topaz(self):
        self.shown_topaz('خلصنا التوباز')

    def test_we_did_topaz(self):
        self.shown_topaz('عملنا توباز')

    def test_topaz_binds_to_the_shown_version_not_a_newer_file(self):
        self.stable_file('801', 'TZ2')
        core = self.core(Script([('get_item_status', {'item': 'TZ2'})], 'TZ2 على الفايل video_v1.mp4',
                                [act('confirm_topaz', 'خلصنا التوباز', 'completed_statement', '', item='TZ2')], 'ok'))
        self.say(core, 'ايه وضع TZ2؟')
        self.select('801', 2)                      # the editor uploads v2 after the owner looked
        self.duration('801', 30.0, 2)
        reply = self.say(core, 'خلصنا التوباز')
        self.assertIsNone(self.item('801')['topaz_asset'])
        self.assertIn('video_v2.mp4', reply)
        self.assertIn('❓', reply)
        self.say(core, 'اه')                        # the owner confirms the exact file named in the question
        self.assertEqual(self.item('801')['topaz_asset'], 'id:FILE2@rev2')

    def test_yes_answers_the_single_open_question(self):
        self.make_ready('810', 'Story', code='YS1')
        self.pause('810')
        model = Script([act('resume_item', '', 'bondok_offer', '', item='YS1', on_date=None, not_before=None)],
                       'YS1 متوقفة. تحب أرجعها؟')
        core = self.core(model)
        self.say(core, 'ايه وضع YS1')
        self.assertEqual(self.item('810')['owner_state'], 'paused')
        calls = model.calls
        reply = self.say(core, 'اه')
        self.assertEqual(model.calls, calls)
        self.assertEqual(self.item('810')['owner_state'], 'active')
        self.assertIn(rules.display(rules.instant(self.res('810')['slot'])), reply)   # M22: the new slot is shown
        self.say(core, 'اه')                        # an old "yes" is never reused
        self.assertEqual(len([x for x in self.executed() if x[0] == 'resume']), 1)

    def test_okay_continue_continues_only_the_agreed_operation(self):
        self.make_ready('811', 'Story', code='YS2')
        self.make_ready('812', 'Story', code='YS3')
        self.pause('811', '812')
        model = Script([act('resume_item', '', 'bondok_offer', '', item='YS2', on_date=None, not_before=None)],
                       'أرجع YS2؟')
        core = self.core(model)
        self.say(core, 'ايه وضع YS2 و YS3')
        self.say(core, 'تمام كمل')
        self.assertEqual((self.item('811')['owner_state'], self.item('812')['owner_state']), ('active', 'paused'))

    def test_okay_continue_without_an_agreed_operation_resumes_nothing(self):
        self.make_ready('813', 'Story', code='YS4')
        self.pause('813')
        core = self.core(Script([act('resume_item', 'تمام كمل', 'imperative', '', item='YS4', on_date=None,
                                     not_before=None)], 'ok'))
        self.seed(core, 'YS4')
        reply = self.say(core, 'تمام كمل')
        self.assertEqual(self.item('813')['owner_state'], 'paused')
        self.assertIn('❓', reply)

    def test_go_ahead_with_them(self):
        for i in range(2):
            self.make_ready(str(820 + i), 'Story', code=f'GA{i}')
        self.pause('820', '821')
        core = self.core(Script([act('resume_item', 'go ahead with them', 'imperative', 'them', item=f'GA{i}',
                                     on_date=None, not_before=None) for i in range(2)], 'done'))
        self.seed(core, 'GA0', 'GA1')
        self.say(core, 'go ahead with them')
        self.assertEqual([self.item(i)['owner_state'] for i in ('820', '821')], ['active', 'active'])

    def test_amazing_and_blurry_are_not_negations(self):
        self.make_ready('830', 'Story', code='AM1')
        self.make_ready('831', 'Story', code='AM2')
        self.pause('830', '831')
        core = self.core(Script([act('resume_item', 'مدهش انشره', 'imperative', 'انشره', item='AM1', on_date=None,
                                     not_before=None)], 'تمام',
                                [act('resume_item', 'انشره', 'imperative', 'انشره', item='AM2', on_date=None,
                                     not_before=None)], 'تمام'))
        self.say(core, 'الفيديو ده AM1 مدهش انشره', thread='1700000801.000100')
        self.say(core, 'AM2 مشوش شوية بس انشره', thread='1700000802.000100')
        self.assertEqual((self.item('830')['owner_state'], self.item('831')['owner_state']), ('active', 'active'))

    def test_owner_posted_it_on_our_account(self):
        self.make_ready('840', 'Story', code='PO1')
        self.bystander()
        text = 'نزلته خلاص على حسابنا'
        core = self.core(Script([act('report_published', text, 'completed_statement', 'نزلته', item='PO1',
                                     destination='studio_account')], 'تمام'))
        self.seed(core, 'PO1')
        before = self.snap()
        self.say(core, text)
        it = self.item('840')
        self.assertEqual(it['publication'], 'published')
        self.assertEqual(json.loads(it['observed'])['_owner_report']['by'], OWNER)
        self.assertEqual(self.changed(before), {'840'})

    def test_send_it_back_to_the_editor_is_rework_never_resume(self):
        self.make_ready('850', 'Story', code='RW1')
        self.pause('850')
        text = 'رجعها للمونتير'
        core = self.core(Script([act('resume_item', text, 'imperative', 'رجعها', item='RW1', on_date=None,
                                     not_before=None),
                                 act('request_rework', text, 'imperative', 'رجعها', item='RW1',
                                     reason='owner asked for a new edit')], 'تمام'))
        self.seed(core, 'RW1')
        self.say(core, text)
        it = self.item('850')
        self.assertEqual(json.loads(it['hold'])['kind'], 'rework')
        self.assertEqual(it['owner_state'], 'paused')
        self.assertIsNone(self.res('850'))
        self.assertEqual([op for op, _ in self.executed()], ['request_rework'])   # the pause is the fixture's

    def test_cancel_that_cancels_the_open_question_only(self):
        self.make_ready('860', 'Story', code='CN1')
        self.pause('860')
        model = Script([act('resume_item', '', 'bondok_offer', '', item='CN1', on_date=None, not_before=None)],
                       'أرجعها؟')
        core = self.core(model)
        self.say(core, 'ايه وضع CN1')
        before = self.snap()
        self.say(core, 'cancel that')
        self.say(core, 'اه')
        self.assertEqual(self.changed(before), set())
        self.assertEqual(self.item('860')['owner_state'], 'paused')


# ====================================================================== M21: alert-thread replies
class R5_M21_AlertThreadReplies(LangCase):
    def alert(self, needle):
        core = self.core(Script())
        core.deliver_notifications(50)
        return core, self.root_of(needle)

    def test_published_reply_under_unknown_outcome_records_an_owner_report(self):
        att = self.unknown_outcome('900', 'UK1')
        core, ts = self.alert('outcome unknown')
        body = {'team_id': 'T1', 'event': {'type': 'message', 'channel': 'C1', 'user': OWNER, 'ts': '1700001000.1',
                                           'thread_ts': ts, 'text': 'published'}}
        self.assertTrue(accept_event(body, 'C1', 'T1', lambda t: bool(core.store.kv('thread:' + t)), 'UBOT'))
        reply = self.say(core, 'published', thread=ts)
        it = self.item('900')
        self.assertEqual(it['publication'], 'published')
        with self.ops.store.read() as c:
            a = c.execute('SELECT * FROM ops_attempts WHERE id=?', (att,)).fetchone()
        self.assertIsNone(a['media_id'])                      # no invented proof
        self.assertEqual(core.model.calls, 0)
        self.assertIn('UK1', reply + it['name'])

    def test_not_published_reply_asks_once_then_yes_records_it(self):
        self.unknown_outcome('901', 'UK2')
        core, ts = self.alert('outcome unknown')
        reply = self.say(core, 'not published', thread=ts)
        self.assertIn('❓', reply)
        self.assertEqual(self.item('901')['publication'], 'outcome_unknown')
        self.say(core, 'اه', thread=ts)
        self.assertEqual(self.item('901')['publication'], 'not_started')

    def test_reply_under_a_missed_time_notice_binds_to_that_item(self):
        self.make_ready('902', 'Story', code='MS1')
        self.bystander()
        at = rules.cairo_local(2026, 10, 11, 14, 0)
        self.ops.submit(owner_cmd(self.rid(), 'request_reschedule', '902', at=rules.iso(at)))
        self.clock.set(at + timedelta(hours=3))
        rid = self.rid('wf3')
        self.ops.repair(rid, self.ops.run_start('wf3', rid)['fence'])
        self.ops.run_finish('wf3', rid)
        core, ts = self.alert('passed without publication')
        core.model = core.agent.model = Script([act('request_publish', 'انشرها', 'imperative', 'انشرها', item='902',
                                                    on_date=None, not_before=None)], 'تمام')
        before = self.snap()
        self.say(core, 'انشرها', thread=ts)
        self.assertIsNotNone(self.res('902'))
        self.assertEqual(self.changed(before), {'902'})


# ====================================================================== M22: truthful approval replies
class R5_M22_ApprovalRepliesAreTruthful(LangCase):
    def test_legacy_resume_proposal_approval_shows_the_new_slot(self):
        self.make_ready('950', 'Story', code='AP1')
        self.pause('950')
        with self.ops.store.tx() as c:
            pid = self.ops.propose_command(c, 'resume', '950', {}, 'Resume AP1', OWNER, TH)['proposal_id']
        core = self.core()
        out = self.say(core, f'اعتمد {pid}')
        self.assertIn(rules.display(rules.instant(self.res('950')['slot'])), out)

    def test_pause_confirmed_after_the_commitment_point_says_may_already_be_in_progress(self):
        self.make_ready('951', 'Story', code='AP2')
        model = Script([act('pause_item', '', 'bondok_offer', '', item='AP2', reason='owner')], 'أوقفها؟')
        core = self.core(model)
        self.say(core, 'ايه وضع AP2')
        self.clock.set(rules.instant(self.res('951')['slot']) + timedelta(seconds=10))
        cl = self.ops.claim('951', 'w')
        self.ops.container(cl['attempt_id'], 'w', cl['fence'], 'C')
        self.ops.commit(cl['attempt_id'], 'w', cl['fence'], container_status='FINISHED')
        reply = self.say(core, 'اه')
        self.assertIn('may already be in progress', reply)
        self.assertNotIn('stopped', reply.lower())

    def test_swap_approval_names_both_new_times(self):
        self.make_ready('952', 'Story', code='SW1')
        self.make_ready('953', 'Story', code='SW2')
        a, b = self.res('952')['slot'], self.res('953')['slot']
        r = self.ops.submit(Command(self.rid(), 'request_reschedule', OWNER, 'owner', '952', {'at': b},
                                    auth={'thread': TH}))
        self.assertEqual(r['state'], 'awaiting_approval', r)
        core = self.core()
        out = self.say(core, 'اعتمد ' + r['proposal_id'])
        for slot in (a, b):
            self.assertIn(rules.display(rules.instant(slot)), out)


# ====================================================================== M23: asking again for the same draft
class R5_M23_SameDraftAgain(LangCase):
    GOOD = 'Golden hour on the water 🌅\n\n' + ' '.join('#tag%d' % i for i in range(13))

    def test_same_draft_twice_returns_the_existing_draft(self):
        self.observe(monday_item('1200', fmt='Post', code='CAP1'))
        model = Script([('draft_caption', {'item': 'CAP1', 'text': self.GOOD})], 'ok',
                       [('draft_caption', {'item': 'CAP1', 'text': self.GOOD})], 'ok')
        core = self.core(model)
        self.say(core, 'اكتب كابشن ل CAP1')
        reply = self.say(core, 'اكتبه تاني كده زي ما هو')
        self.assertNotIn('error', reply.lower())
        self.assertIn('Golden hour on the water', reply)
        with self.ops.store.read() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM ops_caption_drafts').fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM ops_proposals WHERE kind='approve_caption'").fetchone()[0], 1)

    def test_show_it_again_needs_no_model_call(self):
        self.observe(monday_item('1201', fmt='Post', code='CAP2'))
        model = Script([('draft_caption', {'item': 'CAP2', 'text': self.GOOD})], 'ok')
        core = self.core(model)
        self.say(core, 'اكتب كابشن ل CAP2')
        calls = model.calls
        reply = self.say(core, 'ابعتهولي تاني عشان اعتمده')
        self.assertEqual(model.calls, calls)
        self.assertIn('Golden hour on the water', reply)
        self.assertNotEqual(self.item('1201')['caption_state'], 'approved')


# ====================================================================== M24: captions typed in Slack
class R5_M24_SlackMarkupIsDecodedInCaptions(LangCase):
    def test_entities_and_links_are_decoded_before_storing(self):
        self.observe(monday_item('1300', fmt='Post', code='CP1'))
        slack_text = 'Sun &amp; sea :fire: visit <http://www.waset.co|www.waset.co>\n\n#reels #travel'
        msg = 'غير الكابشن لـ: ' + slack_text
        core = self.core(Script([act('update_caption', 'غير الكابشن', 'imperative', 'CP1', item='CP1',
                                     text=slack_text)], 'تم'))
        self.say(core, msg + ' (CP1)')
        it = self.item('1300')
        self.assertEqual(it['caption'], 'Sun & sea 🔥 visit www.waset.co\n\n#reels #travel')
        self.assertEqual(it['caption_state'], 'approved')

    def test_unresolvable_slack_mention_is_not_published(self):
        self.observe(monday_item('1301', fmt='Post', code='CP2'))
        slack_text = 'Thanks <@U12345> for the shoot 📸\n\n#reels'
        core = self.core(Script([act('update_caption', 'غير الكابشن', 'imperative', 'CP2', item='CP2',
                                     text=slack_text)], 'تم'))
        self.say(core, 'CP2 غير الكابشن لـ: ' + slack_text)
        self.assertNotIn('<@', self.item('1301')['caption'] or '')
        self.assertNotEqual(self.item('1301')['caption_state'], 'approved')

    def test_invalid_caption_is_refused_like_a_board_caption(self):
        self.observe(monday_item('1302', fmt='Post', code='CP3'))
        bad = 'Too many ' + ' '.join(f'#t{i}' for i in range(31))
        core = self.core(Script([act('update_caption', 'غير الكابشن', 'imperative', 'CP3', item='CP3', text=bad)],
                                'تم'))
        reply = self.say(core, 'CP3 غير الكابشن لـ: ' + bad)
        self.assertNotEqual(self.item('1302')['caption_state'], 'approved')
        self.assertIn('30 hashtags', reply)


# ====================================================================== M26: approval near-misses
class R5_M26_ApprovalNearMisses(LangCase):
    def two_proposals(self):
        for iid, code in (('1400', 'NM1'), ('1401', 'NM2')):
            self.make_ready(iid, 'Story', code=code)
            self.pause(iid)
        with self.ops.store.tx() as c:
            p1 = self.ops.propose_command(c, 'resume', '1400', {}, 'Resume NM1', OWNER, TH)['proposal_id']
            p2 = self.ops.propose_command(c, 'resume', '1401', {}, 'Resume NM2', OWNER, TH)['proposal_id']
        return p1, p2

    def proposals(self):
        with self.ops.store.read() as c:
            return {r['id']: r['state'] for r in c.execute('SELECT id,state FROM ops_proposals')}

    def test_decorated_approvals_approve_exactly_that_proposal(self):
        p1, p2 = self.two_proposals()
        model = Script(fail=True)
        core = self.core(model)
        for text in (f'*اعتمد {p1}* :+1:', ):
            self.say(core, text)
        self.assertEqual(self.proposals(), {p1: 'executed', p2: 'pending'})
        self.say(core, f'‏اعتمد​ {p2.lower()} 👍')
        self.assertEqual(self.proposals(), {p1: 'executed', p2: 'executed'})
        self.assertEqual(model.calls, 0)

    def test_approve_it_with_two_open_proposals_asks_once_and_creates_nothing(self):
        p1, p2 = self.two_proposals()
        model = Script(fail=True)
        core = self.core(model)
        r1 = self.say(core, 'اعتمدها')
        r2 = self.say(core, 'اعتمدها')
        self.assertIn('❓', r1)
        self.assertIn(p1, r1)
        self.assertIn(p2, r1)
        self.assertEqual(self.proposals(), {p1: 'pending', p2: 'pending'})
        self.assertEqual(model.calls, 0)
        del r2

    def test_quoted_approval_is_confirmed_before_it_runs(self):
        p1, p2 = self.two_proposals()
        core = self.core(Script(fail=True))
        reply = self.say(core, f'&gt; اعتمد {p1}')
        self.assertIn('❓', reply)
        self.assertEqual(self.proposals()[p1], 'pending')
        self.say(core, 'اه')
        self.assertEqual(self.proposals(), {p1: 'executed', p2: 'pending'})

    def test_several_ids_approve_each_named_proposal_only(self):
        p1, p2 = self.two_proposals()
        core = self.core(Script(fail=True))
        self.say(core, f'اعتمد {p1} و {p2}')
        self.assertEqual(self.proposals(), {p1: 'executed', p2: 'executed'})

    def test_thumbs_up_alone_answers_the_single_open_proposal(self):
        self.make_ready('1402', 'Story', code='NM3')
        self.pause('1402')
        with self.ops.store.tx() as c:
            pid = self.ops.propose_command(c, 'resume', '1402', {}, 'Resume NM3', OWNER, TH)['proposal_id']
        core = self.core(Script(fail=True))
        self.say(core, ':+1:')
        self.assertEqual(self.proposals()[pid], 'executed')


# ====================================================================== M27: names and normalisation
class R5_M27_NamesResolveWithoutGuessing(LangCase):
    def bridge(self):
        from bridge import Bridge
        return Bridge(self.ops, OWNER)

    def test_arabic_indic_digits_in_names_and_codes(self):
        self.observe(monday_item('990', fmt='Story', name='ستوري الهرم ٣', code='PYR3'))
        self.observe(monday_item('991', fmt='Story', name='ريل سيوة ١٢', code='SIW12'))
        br = self.bridge()
        for ref, want in (('ستوري الهرم ٣', '990'), ('ستوري الهرم 3', '990'), ('ريل سيوة ١٢', '991'),
                          ('ريل سيوه 12', '991'), ('SIW١٢', '991')):
            self.assertEqual(br.resolve(ref), {'item_id': want}, ref)

    def test_ya_and_ta_marbuta_spellings(self):
        self.observe(monday_item('992', fmt='Story', name='ستورى رحلة سيوة', code='SW1'))
        br = self.bridge()
        for ref in ('ستوري رحله سيوه', 'ستورى رحلة سيوة', 'رحله سيوه'):
            self.assertEqual(br.resolve(ref), {'item_id': '992'}, ref)

    def test_normalisation_collision_asks_instead_of_choosing(self):
        self.observe(monday_item('993', fmt='Story', name='رحلة سيوة', code='SW2'))
        self.observe(monday_item('994', fmt='Story', name='رحله سيوه', code='SW3'))
        r = self.bridge().resolve('رحلة سيوة')
        self.assertEqual(sorted(a['item_id'] for a in r.get('ambiguous', [])), ['993', '994'])

    def test_slack_instruction_with_an_arabic_indic_name(self):
        self.make_ready('995', 'Story', code='PYR5', name='ستوري الهرم ٥')
        core = self.core(Script([act('pause_item', 'وقف ستوري الهرم ٥', 'imperative', 'ستوري الهرم ٥',
                                     item='ستوري الهرم ٥', reason='owner')], 'وقفتها'))
        self.say(core, 'وقف ستوري الهرم ٥')
        self.assertEqual(self.item('995')['owner_state'], 'paused')


# ====================================================================== A8 (Bondok part)
class R5_A8_DraftApprovalInPlainWords(LangCase):
    TEXT = 'Golden hour on the bay, DM us for the tour today. ✨\n\n' + ' '.join(f'#tag{i}' for i in range(13))

    def draft(self, iid='1500', code='DR1'):
        self.make_ready(iid, 'Post', code=code, approve_caption=False)
        r = self.ops.submit(Command(self.rid(), 'caption_draft', 'service:wf1', 'service:wf1', iid,
                                    {'input_hash': 'h' + iid, 'text': self.TEXT}))
        self.assertEqual(r['draft_state'], 'pending_approval', r)

    def test_plain_approve_in_the_draft_thread_after_the_interaction_expired(self):
        self.draft()
        model = Script(fail=True)
        core = self.core(model)
        core.deliver_notifications(50)
        root = self.root_of('Caption draft')
        self.clock.advance(3 * 3600)
        self.say(core, 'اعتمدها', thread=root)
        self.assertEqual(self.item('1500')['caption_state'], 'approved')
        self.assertEqual(self.item('1500')['caption'], self.TEXT)
        self.assertIsNotNone(self.res('1500'))
        self.assertEqual(model.calls, 0)

    def test_approve_the_caption_of_an_item_in_a_new_thread(self):
        self.draft('1501', 'DR2')
        core = self.core(Script([act('approve_caption_draft', 'اعتمد الكابشن بتاع DR2', 'imperative', 'DR2',
                                     item='DR2')], 'تمام'))
        core.deliver_notifications(50)            # the owner was shown the draft in its notification
        self.clock.advance(3 * 3600)
        self.say(core, 'اعتمد الكابشن بتاع DR2', thread='1700000990.000100')
        self.assertEqual(self.item('1501')['caption_state'], 'approved')

    def test_asking_for_the_draft_again_shows_it(self):
        self.draft('1502', 'DR3')
        model = Script(fail=True)
        core = self.core(model)
        core.deliver_notifications(50)
        root = self.root_of('Caption draft')
        self.clock.advance(3 * 3600)
        reply = self.say(core, 'ابعتهولي تاني', thread=root)
        self.assertIn('Golden hour on the bay', reply)
        self.assertEqual(model.calls, 0)


# ====================================================================== M20: watchdog keeps every distinct error
def _line(kind, err, path='/v2/prep/step'):
    return json.dumps({'at': 0, 'path': path, 'kind': kind, 'error': err}) + '\n'


class R5_M20_WatchdogKeepsDistinctErrors(LangCase):
    HOUR = 1_800_000_000 - (1_800_000_000 % 3600)

    def wd(self):
        core = self.core()
        posted = []
        core.post = lambda t, th=None: posted.append(t)
        return core, posted, self.dir / 'errors.log'

    def test_second_error_class_in_the_same_hour_is_reported(self):
        core, posted, log = self.wd()
        with mock.patch('app.time.time', return_value=self.HOUR + 60):
            log.write_text(_line('KeyError', "'title'", '/v2/caption/needed'))
            core.run_watchdog()
        with mock.patch('app.time.time', return_value=self.HOUR + 600):
            with log.open('a') as f:
                for _ in range(30):
                    f.write(_line('storage', 'database is locked', '/v2/publish/claim'))
            core.run_watchdog()
        self.assertEqual(len(posted), 2, posted)
        self.assertIn('/v2/publish/claim', posted[1])
        self.assertIn('30', posted[1])

    def test_alert_survives_a_failed_slack_post(self):
        core, posted, log = self.wd()
        log.write_text(_line('OperationalError', 'attempt to write a readonly database'))

        def broken(t, th=None):
            raise OSError('slack down')
        core.post = broken
        with self.assertRaises(OSError):
            core.run_watchdog()
        core.post = lambda t, th=None: posted.append(t)
        core.run_watchdog()
        core.run_watchdog()
        self.assertEqual(len([p for p in posted if 'readonly database' in p]), 1, posted)

    def test_rotation_keeps_the_unread_tail(self):
        core, posted, log = self.wd()
        log.write_text(_line('x', 'a') * 3)
        with mock.patch('app.time.time', return_value=self.HOUR * 2):
            core.run_watchdog()
        with log.open('a') as f:                       # written after Bondok's read, before the helper rotates
            f.write(_line('storage', 'readonly database (tail)', '/v2/publish/commit'))
        log.replace(self.dir / 'errors.log.1')
        log.write_text(_line('y', 'b', '/v2/other'))
        with mock.patch('app.time.time', return_value=self.HOUR * 2 + 7200):
            core.run_watchdog()
        text = ' '.join(posted[1:])
        self.assertIn('readonly database (tail)', text)
        self.assertIn('/v2/other', text)

    def test_repeats_are_coalesced_but_counted(self):
        core, posted, log = self.wd()
        with mock.patch('app.time.time', return_value=self.HOUR + 60):
            log.write_text(_line('storage', 'locked', '/v2/a'))
            core.run_watchdog()
        with mock.patch('app.time.time', return_value=self.HOUR + 120):
            with log.open('a') as f:
                f.write(_line('storage', 'locked', '/v2/a') * 5)
            core.run_watchdog()
        self.assertEqual(len(posted), 1)                 # same failure within the hour: coalesced
        with mock.patch('app.time.time', return_value=self.HOUR + 3700):
            core.run_watchdog()
        self.assertEqual(len(posted), 2)                 # ... but never silently dropped
        self.assertIn('5', posted[1])

    def test_partial_last_line_is_not_consumed(self):
        core, posted, log = self.wd()
        log.write_text(_line('storage', 'first', '/v2/a') + '{"path": "/v2/b", "kind": "storage", "err')
        core.run_watchdog()
        with log.open('a') as f:
            f.write('or": "second"}\n')
        with mock.patch('app.time.time', return_value=time.time() + 4000):
            core.run_watchdog()
        self.assertTrue(any('/v2/b' in p for p in posted), posted)


# ====================================================================== T-COST and outage
class R5_TCOST_DeterministicPathsUseNoModel(LangCase):
    def test_pending_question_survives_a_model_outage_and_yes_still_works(self):
        self.make_ready('1600', 'Story', code='OU1')
        self.pause('1600')
        model = Script([act('resume_item', '', 'bondok_offer', '', item='OU1', on_date=None, not_before=None)],
                       'أرجعها؟')
        core = self.core(model)
        self.say(core, 'ايه وضع OU1')
        model.fail = True
        reply = self.say(core, 'وقف كل حاجة وغير الفورمات')          # needs the model: nothing guessed
        self.assertIn('معملتش أي حاجة', reply)
        self.assertEqual(self.item('1600')['owner_state'], 'paused')
        calls = model.calls
        self.say(core, 'اه')
        self.assertEqual(model.calls, calls)
        self.assertEqual(self.item('1600')['owner_state'], 'active')

    def test_notifications_watchdog_answers_and_acknowledgments_make_no_model_calls(self):
        model = Script(fail=True)
        core = self.core(model)
        self.make_ready('1601', 'Story', code='OU2')
        self.ops.submit(owner_cmd('p1601', 'pause', '1601'))
        core.deliver_notifications(50)
        core.watchdog()
        self.say(core, 'اعتمد B-DEADBEEF')
        self.say(core, 'تمام شكرا')
        self.say(core, 'اه')
        self.say(core, 'cancel that')
        self.assertEqual(model.calls, 0)


# ====================================================================== R5-LOW-16 (Bondok part)
class R5_LOW16_PolicyAndTestCollection(unittest.TestCase):
    def test_policy_states_the_approved_story_trim(self):
        text = (ROOT / 'bondok' / 'policy.txt').read_text()
        self.assertNotIn('60.000 fails', text)
        self.assertIn('65', text)
        self.assertIn('trim', text.lower())

    def test_test_bondok_main_guard_is_the_last_statement(self):
        tree = ast.parse((ROOT / 'tests' / 'test_bondok.py').read_text())
        guards = [i for i, n in enumerate(tree.body) if isinstance(n, ast.If) and '__main__' in ast.unparse(n.test)]
        self.assertEqual(guards, [len(tree.body) - 1])


if __name__ == '__main__':
    unittest.main()
