"""R2 authority migration (ADR decision 1-2, Gate 2 / T-AUTH): Monday keeps the business facts.

Rebuild test from the ADR: delete the operational cache, read the board, and owner decisions and publication
protection come back unchanged; owner edits made on the board meanwhile still act. Synthetic data only.
"""
import json
import unittest
from datetime import timedelta

from support import Clock, OpsCase, board_from_projection, monday_item, owner_cmd, rules, set_cell

from waset_ops import Ops, board, record


def with_record_col(item, text=''):
    """The board has the Bondok record column (created by the authorized migration step)."""
    if not any(c['id'] == board.COL['record'] for c in item['column_values']):
        set_cell(item, 'record', text, {'text': text} if text else None)
    return item


class AuthorityCase(OpsCase):
    def observe(self, *items, **kw):
        return self.ops.observe([with_record_col(i) for i in items], **kw)

    def proj(self, iid, ops=None):
        with (ops or self.ops).store.read() as c:
            return json.loads((ops or self.ops).item(c, str(iid))['projected'] or '{}')

    def record(self, iid, ops=None):
        return json.loads(self.proj(iid, ops)['record'])

    def board(self, iid, fmt='Story', **kw):
        it = self.item(iid)
        b = board_from_projection(self.ops, iid, fmt=fmt, code=it['code'], caption=it['caption'] or '', **kw)
        return set_cell(b, 'record', self.proj(iid)['record'], {'text': self.proj(iid)['record']})

    def fresh_cache(self):
        """The operational store is gone (deleted or corrupt): a new, empty one on the same clock."""
        ops = Ops(self.dir / 'rebuilt.sqlite', clock=self.clock)
        ops.store.migrate()
        return ops


class R2_RecordIsWrittenWithTheDisplay(AuthorityCase):
    def test_record_holds_the_business_facts_and_stays_within_the_column_limit(self):
        self.make_ready('1', 'Story')
        self.drain_monday()
        rec = self.record('1')
        self.assertEqual(rec['s'], record.RECORD_SCHEMA)
        self.assertEqual(rec['id'], '1')
        self.assertEqual(rec['tz'], 'id:FILE1@rev1')                  # Topaz binding
        self.assertEqual(rec['res']['slot'], self.res('1')['slot'])
        self.assertEqual(rec['st'], 'Scheduled')
        self.assertEqual(rec['ch'], 'c1-1')                           # content identity (duplicate protection)
        self.ops.submit(owner_cmd('p', 'pause', '1'))
        self.drain_monday()
        self.assertEqual(self.record('1')['os'], 'paused')
        with self.ops.store.tx() as c:                                  # extreme values: still below 2,000 chars
            c.execute('UPDATE ops_items SET source_override_url=?, owner_state_reason=?, hold=? WHERE item_id=?',
                      ('https://www.dropbox.com/s/' + 'x' * 1500, 'r' * 500,
                       json.dumps({'kind': 'rework', 'reason': 'ج' * 1500}), '1'))
            self.ops.project(c, '1')
        self.drain_monday()
        self.assertLessEqual(len(self.proj('1')['record']), record.RECORD_MAX)

    def test_no_write_to_a_board_without_the_column(self):
        self.ops.observe([monday_item('2')])                          # no record column on this board
        self.make_ready_without_column = None
        with self.ops.store.read() as c:
            jobs = [json.loads(r['payload']) for r in c.execute("SELECT payload FROM ops_outbox WHERE kind='monday'")]
        self.assertFalse([j for j in jobs if board.COL['record'] in j.get('columns', {})])

    def test_a_board_that_gains_the_column_gets_its_record(self):
        self.ops.observe([monday_item('3')])
        self.drain_monday()
        self.ops.observe([with_record_col(monday_item('3'))])
        jobs = [json.loads(o['payload']) for o in self.outbox('monday') if o['state'] == 'pending']
        self.assertTrue([j for j in jobs if board.COL['record'] in j['columns']])

    def test_an_edited_record_is_restored_not_obeyed(self):
        self.make_ready('4', 'Story')
        self.drain_monday()
        b = self.board('4')
        forged = dict(self.record('4'), os='skipped', pub='published')
        set_cell(b, 'record', record.encode(forged), {'text': record.encode(forged)})
        self.clock.advance(120)
        self.observe(b)
        self.assertEqual(self.item('4')['owner_state'], 'active')
        self.assertEqual(self.item('4')['publication'], 'not_started')
        jobs = [json.loads(o['payload']) for o in self.outbox('monday') if o['state'] == 'pending']
        self.assertTrue([j for j in jobs if json.loads(j['columns'][board.COL['record']]['text'])['os'] == 'active'])


class R2_CacheRebuildFromTheBoard(AuthorityCase):
    def build_states(self):
        self.make_ready('10', 'Story')                                # Topaz bound, scheduled automatically
        self.make_ready('11', 'Post')                                 # approved caption
        self.make_ready('12', 'Story')
        self.ops.submit(owner_cmd('p12', 'pause', '12'))              # owner pause
        self.make_ready('13', 'Story')
        at = rules.instant(self.res('13')['slot']) + timedelta(days=1)
        r = self.ops.submit(owner_cmd('t13', 'request_reschedule', '13', at=rules.iso(at)))
        self.assertEqual(r['state'], 'completed', r)                  # owner time
        self.make_ready('14', 'Story')
        r = self.ops.submit(owner_cmd('pub14', 'report_published', '14', explicit=True, source='owner'))
        self.assertEqual(self.item('14')['publication'], 'published', r)
        self.drain_monday()
        return rules.iso(at)

    def snapshot(self, ops, iid):
        with ops.store.read() as c:
            it = ops.item(c, iid)
            res = ops.reservation(c, iid)
        pinned = res['slot'] if res and res['owner_pinned'] else None
        return {k: it[k] for k in ('format', 'topaz_asset', 'owner_state', 'publication', 'hold', 'content_hash')} | \
            {'caption_ok': it['caption_state'] in (None, 'approved'), 'owner_time': it['requested_at'] or pinned}

    def test_owner_decisions_and_publication_protection_survive_losing_the_store(self):
        owner_time = self.build_states()
        boards = [self.board(i, fmt='Post' if i == '11' else 'Story') for i in ('10', '11', '12', '13', '14')]
        before = {i: self.snapshot(self.ops, i) for i in ('10', '11', '12', '13', '14')}
        rebuilt = self.fresh_cache()
        rebuilt.observe(boards)
        after = {i: self.snapshot(rebuilt, i) for i in ('10', '11', '12', '13', '14')}
        self.assertEqual(after, before)
        self.assertEqual(after['13']['owner_time'], owner_time)
        self.assertEqual(after['12']['owner_state'], 'paused')
        # The rebuild itself issued no owner command and no publication.
        with rebuilt.store.read() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM ops_commands').fetchone()[0], 0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM ops_attempts').fetchone()[0], 0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM ops_reservations').fetchone()[0], 0)

    def test_published_content_still_blocks_a_duplicate_after_the_rebuild(self):
        self.build_states()
        boards = [self.board('14')]
        rebuilt = self.fresh_cache()
        rebuilt.observe(boards)
        # A copy of item 14 (same Dropbox content) appears on the board.
        self.ops = rebuilt
        self.fence = rebuilt.run_start('wf1', 'run-dup')['fence']
        self.run_id = 'run-dup'
        self.make_ready('15', 'Story', content='c14-')
        it = self.item('15')
        self.assertEqual(it['readiness'], 'blocked')
        self.assertIn('14', it['block_reason'])
        self.assertIsNone(self.res('15'))

    def test_owner_edits_made_while_the_store_was_gone_still_act(self):
        self.build_states()
        b10 = self.board('10')
        set_cell(b10, 'status', 'Paused')                              # owner paused it on the board meanwhile
        b11 = self.board('11', fmt='Post')
        b12 = self.board('12')
        new_time = rules.instant(self.res('11')['slot']) + timedelta(days=2)
        set_cell(b12, 'publish_at', 'x', rules.monday_publish_at_value(new_time))   # owner time on a paused item
        rebuilt = self.fresh_cache()
        rebuilt.observe([b10, b11, b12])
        with rebuilt.store.read() as c:
            self.assertEqual(rebuilt.item(c, '10')['owner_state'], 'paused')
            self.assertEqual(rebuilt.item(c, '11')['owner_state'], 'active')
            self.assertEqual(rebuilt.item(c, '12')['requested_at'], rules.iso(new_time))
            self.assertEqual(rebuilt.item(c, '12')['owner_state'], 'paused')

    def test_owner_time_is_honoured_once_the_media_is_verified_again(self):
        owner_time = self.build_states()
        b13 = self.board('13')
        self.ops = self.fresh_cache()
        self.fence = self.ops.run_start('wf1', 'run-v')['fence']
        self.run_id = 'run-v'
        self.ops.observe([b13])
        self.contents['13'] = 'c13-'
        self.select('13')
        self.duration('13', 30.0)
        r = self.select('13')
        self.assertEqual(r.get('next'), 'prepare', r)                 # Topaz binding came back: no new confirmation
        mid = self.add_media('13', 'Story')
        self.wf1('prep_media', '13', result={'ready': True, 'mediaId': mid})
        self.assertEqual(self.res('13')['slot'], owner_time)


if __name__ == '__main__':
    unittest.main()
