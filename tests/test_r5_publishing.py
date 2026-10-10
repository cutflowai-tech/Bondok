"""Round 5 publisher traces (A2, B8, B9, A18, M6, M7). Provider semantics follow Meta's Instagram Graph API error
reference (2026-10-10): 9007/2207027 not ready; 2207008/2207020 container missing/expired; 4/17/32/613 throttled;
9 daily limit; 25 restricted account; 190 invalid token; 9004 media URI not fetchable; 352/2207026 unsupported
format; 1/2/-1 generic or unknown. Synthetic data only."""
import json
import unittest
from datetime import timedelta

from support import OpsCase, owner_cmd, rules


def err(code, sub=None, msg='x', typ=None):
    e = {'code': code, 'message': msg}
    if sub:
        e['error_subcode'] = sub
    if typ:
        e['type'] = typ
    return json.dumps({'error': e})


AUTH = err(190, msg='Error validating access token: Session has expired', typ='OAuthException')
SERVER = err(-1, 2207001, 'An unexpected error has occurred')


class Base(OpsCase):
    def slack(self, needle):
        return [json.loads(o['payload'])['text'] for o in self.outbox('slack') if needle in o['payload']]

    def wf2_minute(self, worker, create_error=None, container_error_status=None, stats=None):
        """One WF2 run as built in workflows/build.py: due -> claim -> create container (or fail) -> record."""
        out = self.ops.due(worker)
        for w in out['work']:
            if w['kind'] != 'publish':
                continue
            cl = self.ops.claim(w['item_id'], worker)
            if not cl.get('claimed'):
                continue
            if stats is not None and not cl.get('container_id'):
                stats['creates'] += 1
            if create_error:
                self.ops.precommit_failure(cl['attempt_id'], worker, cl['fence'], 'create_container',
                                           error=create_error, http_status=400)
                continue
            if not cl.get('container_id'):
                self.ops.container(cl['attempt_id'], worker, cl['fence'], 'C-' + worker)
            if container_error_status:
                self.ops.precommit_failure(cl['attempt_id'], worker, cl['fence'], 'container_status',
                                           error=container_error_status, status_code='ERROR')
        return out


class R5_A2_PreCommitFailuresAreBounded(Base):
    """A2: create/save/check failures went to 'Item Finished': no abandon, no limit, 246 creates over 30 h and the
    item cycled through 7 slots with a false 'missed without a publication attempt'."""

    def run_hours(self, hours, **kw):
        stats = {'creates': 0, 'slots': set()}
        for minute in range(hours * 60):
            res = self.res('1')
            if res:
                stats['slots'].add(res['slot'])
            self.wf2_minute(f'wf2-{minute}', stats=stats, **kw)
            if minute % 30 == 5:
                rid = f'wf3-{minute}'
                self.ops.repair(rid, self.ops.run_start('wf3', rid)['fence'])
                self.ops.run_finish('wf3', rid)
            self.clock.advance(60)
        return stats

    def test_expired_token_opens_the_account_breaker_once(self):
        self.make_ready('1')
        self.make_ready('2')
        self.clock.set(rules.instant(self.res('1')['slot']) - timedelta(minutes=1))
        stats = self.run_hours(30, create_error=AUTH)
        self.assertLessEqual(stats['creates'], 2)                  # no hourly container churn
        self.assertLessEqual(len(stats['slots']), 1)               # no slot-to-slot churn
        alerts = self.slack('Instagram access')
        self.assertEqual(len(alerts), 1, alerts)
        self.assertIn('Error validating access token', alerts[0])
        self.assertEqual(self.slack('without a publication attempt'), [])
        # Access restored and demonstrated (a successful provider read): publishing resumes for both items.
        self.assertTrue(self.ops.due('probe').get('probe'))
        self.ops.provider_probe(ok=True)
        rid = 'wf3-after'
        self.ops.repair(rid, self.ops.run_start('wf3', rid)['fence'])
        for iid in ('1', '2'):
            res = self.res(iid)
            self.assertIsNotNone(res, iid)
            self.assertGreater(rules.instant(res['slot']), self.ops.now_dt())
        self.assertEqual(len(self.slack('Instagram access is working again')), 1)

    def test_transient_create_failures_stop_after_a_bounded_budget(self):
        self.make_ready('1')
        self.clock.set(rules.instant(self.res('1')['slot']) - timedelta(minutes=1))
        stats = self.run_hours(30, create_error=SERVER)
        self.assertLessEqual(stats['creates'], 6)
        self.assertLessEqual(len(stats['slots']), 2)
        hold = json.loads(self.item('1')['hold'] or '{}')
        self.assertEqual(hold.get('kind'), 'publish_retry_limit')
        self.assertIn('unexpected error', hold['reason'])
        self.assertEqual(len(self.slack('Publication failed')), 1)

    def test_expired_lease_without_container_counts_as_a_failed_try(self):
        self.make_ready('1')
        self.clock.set(rules.instant(self.res('1')['slot']) + timedelta(seconds=30))
        creates = 0
        for i in range(12):                  # the worker dies after claim every time (no container saved)
            for w in self.ops.due(f'w{i}')['work']:
                cl = self.ops.claim(w['item_id'], f'w{i}')
                creates += bool(cl.get('claimed'))
            self.clock.advance(200)          # > LEASE
        self.assertLessEqual(creates, 3)


class R5_M6_ContainerContentErrors(Base):
    """M6: container ERROR text dropped; content errors retried then an opaque owner hold; editor never told."""

    def test_unsupported_format_blocks_the_item_and_tells_the_editor_once(self):
        self.make_ready('3')
        with self.ops.store.tx() as c:
            c.execute("UPDATE ops_items SET source_item_id='903' WHERE item_id='3'")
        self.clock.set(rules.instant(self.res('3')['slot']) + timedelta(seconds=30))
        self.wf2_minute('w1', container_error_status='Error: unsupported video format (2207026)')
        it = self.item('3')
        self.assertEqual(it['readiness'], 'blocked')
        self.assertIn('2207026', it['block_reason'])
        self.assertIsNone(self.res('3'))
        self.assertEqual(sum('2207026' in o['payload'] for o in self.outbox('editor')), 1)
        self.clock.advance(120)
        self.wf2_minute('w2')
        self.assertEqual(sum('2207026' in o['payload'] for o in self.outbox('editor')), 1)


class PublishOnce(Base):
    def committed(self, iid):
        self.make_ready(iid)
        self.clock.set(rules.instant(self.res(iid)['slot']) + timedelta(seconds=30))
        cl = self.ops.claim(iid, 'w')
        self.ops.container(cl['attempt_id'], 'w', cl['fence'], 'C' + iid)
        self.assertTrue(self.ops.commit(cl['attempt_id'], 'w', cl['fence'], container_status='FINISHED')['committed'])
        return cl


class R5_B8_GenericProviderErrorsAreNotDefinitive(PublishOnce):
    """B8: a 4xx with Meta generic code 1/2 was 'definitive': attempt failed, rollback receipt deleted."""

    def test_generic_codes_keep_the_attempt_unknown_and_reconcilable(self):
        for iid, code in (('10', 1), ('11', 2), ('12', -1)):
            with self.subTest(code=code):
                cl = self.committed(iid)
                r = self.ops.result(cl['attempt_id'], 'w', error=err(code, msg='An unknown error occurred'),
                                    http_status=400, definitive=True)
                self.assertEqual(r['stage'], 'outcome_unknown')
                self.assertEqual(self.item(iid)['publication'], 'outcome_unknown')
                with self.ops.store.read() as c:
                    self.assertIsNotNone(c.execute('SELECT 1 FROM publications WHERE item=?', (iid,)).fetchone())
                self.clock.advance(301)
                self.assertTrue(any(w['kind'] == 'reconcile' and w['item_id'] == iid
                                    for w in self.ops.due('x', limit=50)['work']))

    def test_documented_refusals_are_not_published(self):
        cl = self.committed('13')
        r = self.ops.result(cl['attempt_id'], 'w', error=err(9, 2207042, 'daily limit'), http_status=400,
                            definitive=True)
        self.assertEqual(r['stage'], 'failed')
        self.assertNotEqual(self.item('13')['publication'], 'outcome_unknown')


class R5_A18_LateRefusalOnUnknownAttempt(PublishOnce):
    """A18: a late throttling answer on an outcome_unknown attempt left the item stuck and claimed a retry."""

    def test_late_throttling_answer_reschedules_truthfully(self):
        cl = self.committed('20')
        self.clock.advance(1000)
        self.ops.due('w2')
        self.assertEqual(self.item('20')['publication'], 'outcome_unknown')
        self.ops.result(cl['attempt_id'], 'w', error=err(4, msg='Application request limit reached'),
                        http_status=400, definitive=True)
        self.assertEqual(self.item('20')['publication'], 'not_started')
        res = self.res('20')
        self.assertIsNotNone(res)
        msgs = self.slack('temporary error')
        self.assertEqual(len(msgs), 1)
        self.assertIn(rules.display(rules.instant(res['slot'])), msgs[0])


class R5_B9_OwnerNotPublishedVersusLaterEvidence(PublishOnce):
    """B9: the owner's 'not published' was accepted without checking the container; a later PUBLISHED reconcile
    was rejected and the item was rescheduled (duplicate)."""

    def test_later_published_evidence_wins_and_blocks_a_second_publication(self):
        cl = self.committed('30')
        self.clock.advance(1000)
        self.ops.due('w2')                                           # unknown
        r = self.ops.submit(owner_cmd(self.rid(), 'resolve_outcome', '30', explicit=True, outcome='not_published'))
        self.assertEqual(r['state'], 'completed', r)
        # The new attempt cannot commit before the old container was re-checked after the owner's statement.
        res = self.res('30')
        self.assertIsNotNone(res)
        self.clock.set(rules.instant(res['slot']) + timedelta(seconds=30))
        cl2 = self.ops.claim('30', 'w3')
        self.assertTrue(cl2['claimed'], cl2)
        self.ops.container(cl2['attempt_id'], 'w3', cl2['fence'], 'C30b')
        c2 = self.ops.commit(cl2['attempt_id'], 'w3', cl2['fence'], container_status='FINISHED')
        self.assertFalse(c2['committed'])
        self.assertTrue(any('re-checked' in x for x in c2['reasons']), c2)
        # Reconciliation of the old container now finds it PUBLISHED: that evidence is recorded.
        rec = [w for w in self.ops.due('w4', limit=50)['work'] if w['kind'] == 'reconcile']
        self.assertTrue(any(w['attempt_id'] == cl['attempt_id'] for w in rec), rec)
        self.ops.reconcile(cl['attempt_id'], 'PUBLISHED')
        self.assertEqual(self.item('30')['publication'], 'published')
        self.assertTrue(self.slack('was published after all'))

    def test_old_container_not_published_lets_the_new_attempt_commit(self):
        cl = self.committed('31')
        self.clock.advance(1000)
        self.ops.due('w2')
        self.ops.submit(owner_cmd(self.rid(), 'resolve_outcome', '31', explicit=True, outcome='not_published'))
        self.ops.reconcile(cl['attempt_id'], 'FINISHED')
        res = self.res('31')
        self.clock.set(rules.instant(res['slot']) + timedelta(seconds=30))
        cl2 = self.ops.claim('31', 'w3')
        self.ops.container(cl2['attempt_id'], 'w3', cl2['fence'], 'C31b')
        self.assertTrue(self.ops.commit(cl2['attempt_id'], 'w3', cl2['fence'], container_status='FINISHED')['committed'])


class R5_CaptionRefusalGoesToTheOwner(PublishOnce):
    def test_too_many_mentions_is_a_caption_problem_not_an_editor_task(self):
        cl = self.committed('41')
        self.ops.result(cl['attempt_id'], 'w', error=err(100, 2207040, 'Too many @ tags'), http_status=400,
                        definitive=True)
        self.assertEqual(self.item('41')['publication'], 'failed')
        self.assertEqual(self.outbox('editor'), [])
        self.assertIsNotNone(rules.validate_caption('Hi ' + ' '.join(f'@user{i}' for i in range(21))))
        self.assertIsNone(rules.validate_caption('Hi ' + ' '.join(f'@user{i}' for i in range(20))))


class R5_M7_FailedPublicationDisplayAndRetry(PublishOnce):
    """M7: a definitively rejected item showed 'Redy For Scheduled' and had no retry primitive."""

    def test_rejection_is_visible_and_owner_can_retry(self):
        cl = self.committed('40')
        self.ops.result(cl['attempt_id'], 'w', error=err(100, msg='Invalid parameter'), http_status=400,
                        definitive=True)
        with self.ops.store.read() as c:
            d = self.ops.desired_display(c, self.ops.item(c, '40'))
        self.assertNotEqual(d['status'], 'Redy For Scheduled')
        self.assertIn('Invalid parameter', d['action'])
        r = self.ops.submit(owner_cmd(self.rid(), 'request_publish', '40'))
        self.assertEqual(r['state'], 'completed', r)
        self.assertEqual(self.item('40')['publication'], 'not_started')
        self.assertIsNotNone(self.res('40'))


class R5_A2_WorkflowRoutesEveryPreCommitFailure(unittest.TestCase):
    """The generated WF2 sends every pre-commit error output to the handler (not to 'Item Finished')."""

    def setUp(self):
        from test_workflows import WF
        self.w = WF['pUIshuf16zIYoYRz']
        self.nodes = {n['name']: n for n in self.w['nodes']}
        self.conns = self.w['connections']

    def targets(self, name, out=1):
        lists = self.conns.get(name, {}).get('main', [])
        return [c['node'] for c in (lists[out] if len(lists) > out else [])]

    def test_error_outputs_reach_the_failure_recorders(self):
        for src in ('Create Container', 'Container Created', 'Save Container', 'Renew Publication Lease',
                    'Check Container', 'Commit Publication'):
            with self.subTest(node=src):
                t = self.targets(src)
                self.assertEqual(t, ['Failure — ' + src])
                self.assertEqual(self.targets(t[0], 0), ['Record Pre-commit Failure — Input'])
                self.assertNotIn('Publication Item Finished', t)
        for src in ('Read Fresh Item', 'Source Status — Query', 'Read Source Status', 'Claim Publication'):
            with self.subTest(node=src):
                self.assertEqual(self.targets(src), ['Read Failure — ' + src])

    def test_failure_extraction_keeps_the_provider_body(self):
        from test_workflows import NODE, run_js
        if not NODE:
            self.skipTest('node not installed')
        js = self.nodes['Failure — Create Container']['parameters']['jsCode']
        out = run_js(js, {'error': {'message': '400 - {"error":{"message":"Error validating access token",'
                                               '"type":"OAuthException","code":190}}', 'httpCode': '400'}})
        self.assertEqual(out[0]['json']['stage'], 'create_container')
        self.assertEqual(out[0]['json']['httpStatus'], 400)
        self.assertIn('"code":190', out[0]['json']['error'])

    def test_container_status_text_reaches_the_handler(self):
        body = self.nodes['Abandon Attempt — Input']['parameters']['jsCode']
        self.assertIn('/v2/publish/failure', body)
        self.assertIn('$json.status', body)
        self.assertIn('statusCode', body)

    def test_breaker_probe_branch(self):
        self.assertIn('Probe Instagram Access?', self.targets('Due Work', 0))
        self.assertEqual(self.targets('Probe Instagram Access?', 0), ['Probe Instagram Access'])
        self.assertTrue(self.nodes['Probe Instagram Access']['parameters']['url'].endswith('?fields=id'))


class R5_LOW12_OneOwnerReportResolvesAnUnknownOutcome(PublishOnce):
    """LOW-12: an unknown outcome plus the owner's board Posted needed two owner answers."""

    def test_board_posted_on_an_unknown_attempt_needs_no_second_answer(self):
        cl = self.committed('30')
        self.ops.result(cl['attempt_id'], 'w', error=err(1, msg='An unknown error occurred'), http_status=500,
                        definitive=False)
        self.assertEqual(self.item('30')['publication'], 'outcome_unknown')
        r = self.ops.submit(owner_cmd(self.rid(), 'report_published', '30', source='board status', value='Posted'))
        self.assertEqual(r['state'], 'completed', r)
        self.assertEqual(self.item('30')['publication'], 'published')
        self.assertIsNone(self.item('30')['hold'])
        self.assertNotIn('outcome_unknown', [f['kind'] for f in self.ops.inspect()['findings']
                                             if f.get('item_id') == '30'])
        with self.ops.store.read() as c:                                 # no open owner question remains
            self.assertEqual(c.execute("SELECT COUNT(*) FROM ops_proposals WHERE state='pending' AND "
                                       "bindings LIKE '%30%'").fetchone()[0], 0)
        self.clock.advance(3 * 86400)
        self.ops.repair()
        self.assertEqual(self.item('30')['publication'], 'published')
        self.assertFalse([w for w in self.ops.due('w9', limit=50)['work'] if w.get('item_id') == '30'
                          and w['kind'] == 'publish'])


if __name__ == '__main__':
    unittest.main()
