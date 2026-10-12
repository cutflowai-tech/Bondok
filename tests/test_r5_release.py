"""Release review 2026-10-12: blockers found by the standards/spec review of bondok/r5-monday-authority.

* RR1 Monday commands carry and are checked for board provenance (who/what asked for a publication).
* RR2 ADR decision 4: the publication intent is acknowledged on Monday before media_publish; a failed or refused
  acknowledgement publishes nothing.
* RR3 The empty-Status-cell Pause race (R5 B7) end to end: board sync, publisher, intent guard, observation.
* RR4 R5 M4 completed: both halves of the register entry are exercised (the cited test covered neither).
* RR5 Monday to publisher end to end on a simulated board: an owner's Scheduled order with a passed time is
  published late once, through the intent write; duplicates and forged Monday commands publish nothing.
Synthetic data only.
"""
import json
import unittest
from datetime import timedelta

import board_sim
from support import OpsCase, board_cmd, board_from_projection, monday_item, rules, set_cell
from test_workflows import NODE, WF, run_js

from waset_ops import MONDAY, Command, board


class RR1_MondayCommandProvenance(OpsCase):
    """A command in the Monday actor kind is the owner's board decision (contract §1). It is accepted only with the
    provenance the board observation attaches (the column that changed and its value), and only for an operation
    that column can request. Before: any caller could submit e.g. ``request_publish`` as ``monday`` with nothing
    tying it to a board edit."""

    def test_monday_command_without_board_provenance_is_refused(self):
        self.make_ready('1')
        r = self.ops.submit(Command('x1', 'request_publish', 'monday', MONDAY, '1', {}))
        self.assertEqual((r['state'], r['code']), ('rejected', 'forbidden'))
        r = self.ops.submit(Command('x2', 'pause', 'monday', MONDAY, '1', {}))
        self.assertEqual((r['state'], r['code']), ('rejected', 'forbidden'))
        self.assertEqual(self.item('1')['owner_state'], 'active')

    def test_publish_order_only_from_the_status_column_set_to_scheduled(self):
        self.make_ready('2')
        r = self.ops.submit(board_cmd('x3', 'request_publish', '2', 'notes', seen='Scheduled'))
        self.assertEqual(r['code'], 'forbidden')
        r = self.ops.submit(board_cmd('x4', 'request_publish', '2', 'status', seen='Paused'))
        self.assertEqual(r['code'], 'forbidden')
        r = self.ops.submit(board_cmd('x5', 'request_publish', '2', 'status', seen='Scheduled'))
        self.assertNotEqual(r.get('code'), 'forbidden', r)

    def test_board_observation_records_its_provenance(self):
        self.make_ready('3')
        self.drain_monday()
        self.clock.advance(180)
        b = board_from_projection(self.ops, '3')
        set_cell(b, 'status', 'Paused')
        self.observe(b)
        self.assertEqual(self.item('3')['owner_state'], 'paused')
        with self.ops.store.read() as c:
            row = c.execute("SELECT auth_ref FROM ops_commands WHERE op='pause' AND item_id='3'").fetchone()
        auth = json.loads(row['auth_ref'])
        self.assertEqual((auth['board']['source'], auth['board']['column'], auth['board']['value']),
                         ('observation', 'status', 'Paused'))


class RR2_IntentAcknowledgedBeforePublish(OpsCase):
    """Before: commit went straight to 'committed' and WF2 called media_publish; nothing waited for Monday."""

    def due(self, iid):
        self.make_ready(iid)
        self.drain_monday()
        self.clock.set(rules.instant(self.res(iid)['slot']) + timedelta(seconds=10))
        cl = self.ops.claim(iid, 'w')
        self.assertTrue(cl['claimed'], cl)
        self.ops.container(cl['attempt_id'], 'w', cl['fence'], 'C-' + iid)
        return cl

    def stage(self, aid):
        with self.ops.store.read() as c:
            return c.execute('SELECT stage FROM ops_attempts WHERE id=?', (aid,)).fetchone()['stage']

    def receipts(self, iid):
        with self.ops.store.read() as c:
            return c.execute("SELECT COUNT(*) FROM publications WHERE item=?", (iid,)).fetchone()[0]

    def test_commit_asks_for_a_guarded_intent_write_and_does_not_commit_yet(self):
        cl = self.due('1')
        r = self.ops.commit(cl['attempt_id'], 'w', cl['fence'], container_status='FINISHED')
        self.assertFalse(r['committed'])
        i = r['intent']
        self.assertEqual(i['item_id'], '1')
        self.assertEqual(i['columns'][board.COL['status']], {'label': 'Publishing'})
        self.assertIn(cl['attempt_id'], i['columns'][board.COL['system']]['text'])
        self.assertIn(board.COL['status'], i['guard'])
        self.assertEqual(self.stage(cl['attempt_id']), 'container_created')    # still before the commitment point
        self.assertEqual(self.receipts('1'), 0)
        # the intent write is never taken by the display sync (it belongs to this publication run)
        self.assertEqual([j for j in self.ops.outbox_take(['monday', 'source_monday'], 'other', 50)
                          if j['payload'].get('attempt')], [])

    def test_acknowledged_intent_commits(self):
        cl = self.due('2')
        i = self.ops.commit(cl['attempt_id'], 'w', cl['fence'], container_status='FINISHED')['intent']
        r = self.ops.intent_ack(cl['attempt_id'], 'w', cl['fence'], i['job'], ok=True)
        self.assertTrue(r['committed'], r)
        self.assertEqual(self.stage(cl['attempt_id']), 'committed')
        self.assertEqual(self.receipts('2'), 1)
        self.assertEqual(json.loads(self.item('2')['projected'])['status'], 'Publishing')   # what the board shows

    def test_failed_intent_write_publishes_nothing_and_the_slot_is_retried(self):
        cl = self.due('3')
        i = self.ops.commit(cl['attempt_id'], 'w', cl['fence'], container_status='FINISHED')['intent']
        r = self.ops.intent_ack(cl['attempt_id'], 'w', cl['fence'], i['job'], ok=False, error='HTTP 503')
        self.assertFalse(r['committed'])
        self.assertIn('not acknowledged on Monday', ' '.join(r['reasons']))
        self.assertEqual(self.stage(cl['attempt_id']), 'abandoned')
        self.assertEqual(self.receipts('3'), 0)
        self.assertIsNotNone(self.res('3'))                                   # same slot, next run
        self.assertTrue(self.ops.claim('3', 'w2')['claimed'])

    def test_item_paused_between_intent_and_ack_is_not_committed(self):
        cl = self.due('4')
        i = self.ops.commit(cl['attempt_id'], 'w', cl['fence'], container_status='FINISHED')['intent']
        from support import owner_cmd
        self.ops.submit(owner_cmd('p4', 'pause', '4'))
        r = self.ops.intent_ack(cl['attempt_id'], 'w', cl['fence'], i['job'], ok=True)
        self.assertFalse(r['committed'])
        self.assertEqual(self.receipts('4'), 0)

    def test_stale_or_foreign_acknowledgement_is_refused(self):
        cl = self.due('5')
        i = self.ops.commit(cl['attempt_id'], 'w', cl['fence'], container_status='FINISHED')['intent']
        r = self.ops.intent_ack(cl['attempt_id'], 'w', cl['fence'], i['job'] + 999, ok=True)
        self.assertFalse(r.get('committed'))
        r = self.ops.intent_ack(cl['attempt_id'], 'intruder', cl['fence'], i['job'], ok=True)
        self.assertFalse(r.get('committed'))
        self.assertEqual(self.stage(cl['attempt_id']), 'container_created')

    def test_publisher_stopping_before_the_ack_publishes_nothing_and_leaves_no_intent(self):
        cl = self.due('6')
        i = self.ops.commit(cl['attempt_id'], 'w', cl['fence'], container_status='FINISHED')['intent']
        self.clock.advance(3 * 3600)                                        # worker died; the window passed
        self.ops.due('w2')
        self.assertEqual(self.stage(cl['attempt_id']), 'abandoned')
        self.assertEqual(self.receipts('6'), 0)
        with self.ops.store.read() as c:
            self.assertEqual(c.execute('SELECT state FROM ops_outbox WHERE id=?', (i['job'],)).fetchone()[0],
                             'superseded')
        self.assertNotIn('Publishing now', json.dumps(json.loads(self.item('6')['pending_projection'] or '{}')))
        r = self.ops.intent_ack(cl['attempt_id'], 'w', cl['fence'], i['job'], ok=True)   # a late ack changes nothing
        self.assertFalse(r['committed'])


@unittest.skipUnless(NODE, 'node is required to execute the generated workflow code')
class RR2_WorkflowWritesIntentBeforePublishing(unittest.TestCase):
    W = WF['pUIshuf16zIYoYRz']

    def node(self, name):
        return next(n for n in self.W['nodes'] if n['name'] == name)

    def succ(self, name):
        return [d['node'] for out in self.W['connections'].get(name, {}).get('main', []) for d in out]

    def paths_to(self, target, start):
        """Every node from which ``target`` is reachable without passing ``start``... simple DFS over the graph."""
        seen, stack = set(), [start]
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            stack.extend(self.succ(n))
        return target in seen

    def test_publish_is_reachable_only_through_the_intent_acknowledgement(self):
        self.assertIn('Publication Intent?', self.succ('Commit Publication'))
        self.assertNotIn('Committed?', self.succ('Commit Publication'))
        preds = [src for src, c in self.W['connections'].items() for out in c.get('main', []) for d in out
                 if d['node'] == 'Committed?']
        self.assertEqual(preds, ['Acknowledge Publication Intent'])
        preds = [src for src, c in self.W['connections'].items() for out in c.get('main', []) for d in out
                 if d['node'] == 'Publish To Instagram']
        self.assertEqual(preds, ['Committed?'])
        self.assertEqual(self.succ('Read Board Before Intent'), ['Check Intent Guard'])

    def test_guard_conflict_reaches_the_ack_as_a_refusal(self):
        m = {'job': 7, 'item': '1', 'guard': {'status': {'kind': 'status', 'was': [None, 'Scheduled'],
                                                           'new': 'Publishing'}}, 'gql': {}}
        out = run_js(self.node('Check Intent Guard')['parameters']['jsCode'],
                     {'data': {'items': [{'column_values': [{'id': 'status', 'text': 'Paused', 'value': None}]}]}},
                     refs={'Build Intent Write': m}, each=True)['json']
        self.assertTrue(out['conflict'].startswith('conflict'))
        out = run_js(self.node('Check Intent Guard')['parameters']['jsCode'],
                     {'data': {'items': [{'column_values': []}]}}, refs={'Build Intent Write': m}, each=True)['json']
        self.assertIsNone(out['conflict'])                                   # empty cell: as expected


class RR3_EmptyStatusPauseRace(OpsCase):
    """R5 B7 end to end. The status cell is empty (first sight / cleared); v2's first status write has not landed.
    The owner types Paused into the empty cell after the publisher's fresh read and claim, while Instagram is
    processing the container. Board sync, the intent guard and the next observation must keep the Pause and
    publish nothing."""

    def intent_write(self, b, intent):
        """WF2's intent branch (Python mirror of Check Intent Guard + Write Publication Intent)."""
        p = {'item_id': intent['item_id'], 'columns': intent['columns'], 'guard': intent['guard']}
        conflict = b.guard_conflict(p)
        if conflict:
            return False, 'conflict: changed on the board by a person (' + ','.join(conflict) + ')'
        return b.apply(p) is None, None

    def test_pause_in_the_empty_cell_during_processing_is_kept_and_nothing_is_published(self):
        self.observe(monday_item('70', status=''))
        self.make_ready('70')
        b = board_sim.Board([board_from_projection(self.ops, '70')])
        b.set_raw('70', board.COL['status'], None)                 # the status cell is empty on the board
        self.clock.set(rules.instant(self.res('70')['slot']) + timedelta(seconds=10))
        self.observe(*b.snapshot(), actor='service:wf2')          # the publisher's fresh read
        cl = self.ops.claim('70', 'wf2')
        self.assertTrue(cl['claimed'], cl)
        self.ops.container(cl['attempt_id'], 'wf2', cl['fence'], 'C-70')
        b.set_raw('70', board.COL['status'], {'id': 'status', 'text': 'Paused', 'value': '{"index":5}'})   # owner
        board_sim.wf2_sync(self.ops, b)                           # display sync of the same WF2 run
        r = self.ops.commit(cl['attempt_id'], 'wf2', cl['fence'], container_status='FINISHED')
        ok, err = self.intent_write(b, r['intent'])
        self.assertFalse(ok)
        a = self.ops.intent_ack(cl['attempt_id'], 'wf2', cl['fence'], r['intent']['job'], ok=ok, error=err)
        self.assertFalse(a['committed'])                          # media_publish is never called
        self.assertEqual(board.norm(b.items['70'], 'status'), 'Paused')
        # WF1's next observation records the Pause; later syncs and publisher runs keep it.
        self.clock.advance(60)
        self.observe(*b.snapshot())
        self.assertEqual(self.item('70')['owner_state'], 'paused')
        for _ in range(3):
            board_sim.wf2_sync(self.ops, b, clock=self.clock, step=120)
            self.assertFalse(self.ops.claim('70', 'wf2').get('claimed'))
        self.assertEqual(board.norm(b.items['70'], 'status'), 'Paused')
        with self.ops.store.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM ops_attempts WHERE item_id='70' AND stage IN "
                                       "('committed','published','outcome_unknown')").fetchone()[0], 0)

    def test_pause_typed_before_the_claim_stops_the_claim(self):
        self.observe(monday_item('71', status=''))
        self.make_ready('71')
        b = board_sim.Board([board_from_projection(self.ops, '71')])
        b.set_raw('71', board.COL['status'], {'id': 'status', 'text': 'Paused', 'value': '{"index":5}'})
        board_sim.wf2_sync(self.ops, b)
        self.assertEqual(board.norm(b.items['71'], 'status'), 'Paused')    # never overwritten by the sync
        self.clock.set(rules.instant(self.res('71')['slot']) + timedelta(seconds=10))
        self.observe(*b.snapshot(), actor='service:wf2')
        self.assertFalse(self.ops.claim('71', 'wf2').get('claimed'))
        self.assertEqual(self.item('71')['owner_state'], 'paused')


class RR4_M4_TimeFeedbackStaysVisible(OpsCase):
    """R5 M4: 'Rejected time hidden when an Action is already shown; accepted-but-unreservable time vanishes from
    the board'."""

    def action(self, iid):
        with self.ops.store.read() as c:
            return self.ops.desired_display(c, self.ops.item(c, iid))['action'] or ''

    def test_rejected_time_is_explained_even_when_an_action_is_already_shown(self):
        self.make_ready('80', 'Post', approve_caption=False)        # Action: caption needs approval
        self.assertIn('Caption needs owner approval', self.action('80'))
        self.drain_monday()
        self.clock.advance(180)
        b = board_from_projection(self.ops, '80', fmt='Post')
        far = (self.ops.now_dt() + timedelta(days=120)).replace(hour=19, minute=0, second=0, microsecond=0)
        set_cell(b, 'publish_at', 'x', rules.monday_publish_at_value(far))      # beyond the 84-day horizon
        self.observe(b)
        text = self.action('80')
        self.assertIn('not applied', text)                          # the rejection is visible ...
        self.assertIn('Caption needs owner approval', text)         # ... next to the existing Action

    def test_accepted_but_unreservable_time_stays_on_the_board(self):
        self.observe(monday_item('81', fmt='Story'))               # still checking: cannot be reserved yet
        self.drain_monday()
        self.clock.advance(180)
        at = (self.ops.now_dt() + timedelta(days=1)).replace(hour=19, minute=7, second=0, microsecond=0)
        b = board_from_projection(self.ops, '81', topaz='Not yet')
        set_cell(b, 'publish_at', 'x', rules.monday_publish_at_value(at))
        self.observe(b)
        self.assertIsNone(self.res('81'))
        self.drain_monday()
        self.assertEqual(json.loads(self.item('81')['projected'])['publish_at'], rules.iso(at))
        self.clock.advance(180)
        self.assertEqual(self.observe(board_from_projection(self.ops, '81', topaz='Not yet'))['edits'], [])   # stable, no churn


class RR5_MondayToPublisher(OpsCase):
    def wf2_publish(self, b, iid, media):
        """One WF2 publication run for ``iid`` as the generated workflow does it (fresh read, claim, container,
        commit, guarded intent write, acknowledgement, media_publish, result)."""
        self.observe(*[x for x in b.snapshot() if x['id'] == iid], actor='service:wf2')
        cl = self.ops.claim(iid, 'wf2')
        if not cl.get('claimed'):
            return cl
        self.ops.container(cl['attempt_id'], 'wf2', cl['fence'], 'C-' + iid)
        r = self.ops.commit(cl['attempt_id'], 'wf2', cl['fence'], container_status='FINISHED')
        if not r.get('intent'):
            return r
        p = {'item_id': r['intent']['item_id'], 'columns': r['intent']['columns'], 'guard': r['intent']['guard']}
        conflict = b.guard_conflict(p)
        ok = not conflict and b.apply(p) is None
        a = self.ops.intent_ack(cl['attempt_id'], 'wf2', cl['fence'], r['intent']['job'], ok=ok,
                                error='conflict' if conflict else None)
        if a.get('committed'):
            self.published.append(iid)                                 # Instagram publishes here
            self.ops.result(cl['attempt_id'], 'wf2', media_id=media)
        return a

    def test_owner_scheduled_order_reaches_instagram_once(self):
        self.published = []
        self.make_ready('90')
        self.make_ready('91', content='c90-')                          # same video on a duplicated item
        self.assertIsNone(self.res('91'))
        self.drain_monday()
        self.clock.advance(180)
        b = board_sim.Board([board_from_projection(self.ops, '90'), board_from_projection(self.ops, '91')])
        # The owner clears 90's time, then orders it published at a time that passed 20 minutes ago.
        for k in ('publish_at', 'post_date', 'post_time'):
            b.set_raw('90', board.COL[k], None)
        self.observe(*b.snapshot())
        board_sim.wf2_sync(self.ops, b)
        self.clock.advance(600)                                           # the next WF1 cycle
        at = (self.ops.now_dt() - timedelta(minutes=20)).replace(second=0, microsecond=0)
        b.set_raw('90', board.COL['status'], {'id': 'status', 'text': 'Scheduled', 'value': '{"index":1}'})
        b.set_raw('90', board.COL['publish_at'], {'id': board.COL['publish_at'], 'text': 'x',
                                                  'value': json.dumps(rules.monday_publish_at_value(at))})
        b.set_raw('91', board.COL['status'], {'id': 'status', 'text': 'Scheduled', 'value': '{"index":1}'})
        self.observe(*b.snapshot())
        self.assertEqual(self.res('90')['slot'], rules.iso(at))           # the owner's time, published late
        self.assertIsNone(self.res('91'))                                 # the duplicate is still blocked
        board_sim.wf2_sync(self.ops, b)
        # A forged Monday-kind order (no board provenance) is refused.
        self.assertEqual(self.ops.submit(Command('forged', 'request_publish', 'monday', MONDAY, '91', {}))['code'],
                         'forbidden')
        for _ in range(3):                                                # three WF2 runs, a minute apart
            for iid in ('90', '91'):
                self.wf2_publish(b, iid, 'IG-' + iid)
            board_sim.wf2_sync(self.ops, b, clock=self.clock, step=60)
        self.assertEqual(self.published, ['90'])
        self.assertEqual(self.item('90')['publication'], 'published')
        self.assertEqual(board.norm(b.items['90'], 'status'), 'Posted')
        self.assertNotEqual(self.item('91')['publication'], 'published')


if __name__ == '__main__':
    unittest.main()
