"""Agent migration P5: the runtime under failures, restores, clock changes and load (06-acceptance.md A19, A25-A28).

The invariant of every crash test: Instagram receives at most one publication for the item, and the store ends
saying "published" exactly when Instagram has it (after reconciliation), never "published" without it.
"""
import shutil
import sqlite3
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from runtime_fakes import source_item
from support import owner_cmd, rules
from test_runtime import RuntimeCase
from waset_ops import Ops

from waset_runtime.cycles import Runner


class Crash(BaseException):
    """A process death at one point (not an Exception: nothing in the runtime may swallow it)."""


STEPS = [
    ('monday', 'fresh_item', 'before'), ('ops', 'claim', 'after'), ('ig', 'create_container', 'before'),
    ('ig', 'create_container', 'after'), ('ops', 'container', 'before'), ('ops', 'renew', 'before'),
    ('ig', 'container_status', 'before'), ('dbx', 'metadata', 'before'), ('ops', 'observe', 'before'),
    ('ops', 'commit', 'before'), ('ops', 'commit', 'after'), ('ig', 'publish', 'before'),
    ('ig', 'publish', 'after'), ('ops', 'result', 'before'), ('ops', 'evidence', 'before'),
]


UNKNOWN_AFTER_COMMIT = {('ops', 'commit', 'after'), ('ig', 'publish', 'before')}


class A19_CrashAtEveryStep(RuntimeCase):
    def run_crash(self, target, method, when):
        iid = self.ready_item()
        self.runner.publish_cycle()
        self.at_slot(iid)
        obj = {'ig': self.ig, 'ops': self.ops, 'dbx': self.dbx, 'monday': self.monday}[target]
        real = getattr(obj, method)
        state = {'armed': True}

        def crashing(*a, **kw):
            if state['armed'] and when == 'before':
                state['armed'] = False
                raise Crash(f'{method} {when}')
            out = real(*a, **kw)
            if state['armed'] and when == 'after':
                state['armed'] = False
                raise Crash(f'{method} {when}')
            return out
        with mock.patch.object(obj, method, crashing):
            try:
                self.runner.publish_one({'item_id': iid}, 'crashing-worker')
            except Crash:
                pass
        for _ in range(30):                     # the service restarts; minutes pass
            self.clock.advance(300)
            self.runner.publish_cycle()
        return iid

    def test_every_step(self):
        for n, (target, method, when) in enumerate(STEPS):
            with self.subTest(step=f'{target}.{method} {when}'):
                if n:
                    self.doCleanups()
                    self.tearDown()
                    self.setUp()
                iid = self.run_crash(target, method, when)
                live = len(self.ig.published)
                self.assertLessEqual(live, 1)
                self.assertEqual(self.item(iid)['publication'] == 'published', live == 1,
                                 (self.item(iid)['publication'], live))
                if (target, method, when) in UNKNOWN_AFTER_COMMIT:
                    # Past the commitment point and Instagram never got the request: the store cannot know that.
                    # It stays "outcome unknown" (never re-sent, never assumed failed) and the owner is asked.
                    self.assertEqual((self.item(iid)['publication'], live), ('outcome_unknown', 0))
                    self.assertTrue(any('Check' in j['payload'] or 'check' in j['payload']
                                        for j in self.outbox('slack')))
                else:
                    self.assertEqual(live, 1, 'published once in the end (same or next slot)')


class A28_RestoreOlderThanTheLastPost(RuntimeCase):
    def backup(self):
        dst = self.dir / 'backup.sqlite'
        src = sqlite3.connect(self.dir / 'state.sqlite')
        out = sqlite3.connect(dst)
        with out:
            src.backup(out)
        src.close()
        out.close()
        return dst, self.ops.now()

    def restore(self, path):
        for suffix in ('', '-wal', '-shm'):
            p = self.dir / ('state.sqlite' + suffix)
            if p.exists():
                p.unlink()
        shutil.copy(path, self.dir / 'state.sqlite')

    def test_gap_pauses_publishing_until_reconciled(self):
        a = self.ready_item()
        self.monday.sources['5002'] = source_item('5002', code='LIP13', link=self.dbx.add_folder(
            '/Social Media/Production/LIP13/Story'), name='Calli 4')
        self.dbx.add_file('/Social Media/Production/LIP13/Story/Calli 4 v1.mp4', content='srchash2')
        self.cycle(2)
        b = next(i for i in self.monday.board.items if i != a)
        self.ops.submit(owner_cmd('tz2', 'confirm_topaz', b, explicit=True, confirmed=True))
        self.runner.prepare_cycle()
        path, at = self.backup()
        self.at_slot(a)
        self.runner.publish_cycle()
        self.assertEqual(len(self.ig.published), 1)
        self.restore(path)                                        # the store goes back to before that post
        self.assertEqual(self.item(a)['publication'], 'not_started')
        self.ops.mark_restored(at)
        for _ in range(3):
            self.clock.advance(60)
            out = self.runner.publish_cycle()
        self.assertTrue(out['restore_gap']['open'])
        self.assertEqual(len(self.ig.published), 1)              # item a is not published again
        self.at_slot(b)
        self.runner.publish_cycle()
        self.assertEqual(len(self.ig.published), 1)              # nothing new either while the gap is open
        # The owner says which item the unknown post is, then closes the check.
        self.ops.submit(owner_cmd('rp', 'report_published', a, explicit=True, source='slack', value='posted'))
        self.ops.submit(owner_cmd('rg', 'resolve_restore_gap', None, explicit=True))
        self.clock.advance(30)
        self.runner.publish_cycle()
        self.assertEqual(len(self.ig.published), 2)
        self.assertEqual(self.item(b)['publication'], 'published')
        self.assertNotEqual(self.item(a)['publication'], 'not_started')

    def test_empty_gap_closes_itself(self):
        iid = self.ready_item()
        self.ops.mark_restored(self.ops.now())
        self.at_slot(iid)
        out = self.runner.publish_cycle()
        self.assertTrue(out['restore_gap'].get('closed'))
        self.clock.advance(30)
        self.runner.publish_cycle()
        self.assertEqual(self.item(iid)['publication'], 'published')


class A26_CairoClockChange(unittest.TestCase):
    def test_story_slots_follow_the_october_change(self):
        before = datetime(2026, 10, 29, 6, 0, tzinfo=timezone.utc)
        slots = [rules.iso(x) for x in rules.grid(before, 'Story', horizon=2)]
        self.assertIn('2026-10-29T19:00:00Z', slots)      # 22:00 Cairo summer time (UTC+3)
        self.assertIn('2026-10-30T20:00:00Z', slots)      # 22:00 Cairo winter time (UTC+2)
        self.assertEqual(rules.display(rules.instant('2026-10-30T20:00:00Z')), '2026-10-30 22:00 (Cairo)')

    def test_owner_time_on_the_change_day(self):
        at = rules.cairo_local(2026, 10, 30, 21, 0)
        self.assertEqual(rules.iso(at), '2026-10-30T19:00:00Z')


class P5_04_RepresentativeLoad(RuntimeCase):
    def test_two_hundred_items_and_twenty_due(self):
        for n in range(200):
            code = f'LD{n}'
            folder = f'/Social Media/Production/{code}/Story'
            self.monday.sources[str(6000 + n)] = source_item(str(6000 + n), code=code, name=f'Load {n}',
                                                             link=self.dbx.add_folder(folder))
            self.dbx.add_file(f'{folder}/Load {n} v1.mp4', content=f'ld{n}')
        t0 = time.monotonic()
        self.cycle(2)
        took = time.monotonic() - t0
        self.assertEqual(len(self.monday.board.items), 201)
        self.assertLess(took, 60, f'two preparation cycles over 200 items took {took:.1f} s')
        with self.ops.store.read() as c:
            topaz = [r[0] for r in c.execute("SELECT item_id FROM ops_items WHERE block_key LIKE 'topaz:%'")]
        self.assertGreaterEqual(len(topaz), 20)
        self.ops.submit(owner_cmd('pol', 'set_policy', None, explicit=True, rule='topaz_required', value=False))
        for _ in range(12):
            self.runner.prepare_cycle()
            self.clock.advance(60)
        with self.ops.store.read() as c:
            slots = c.execute('SELECT COUNT(*), COUNT(DISTINCT slot) FROM ops_reservations').fetchone()
        self.assertGreaterEqual(slots[0], 20)
        self.assertEqual(slots[0], slots[1])                     # one Story per slot, as the grid says
        t0 = time.monotonic()
        published = 0
        for _ in range(24 * 6):
            self.clock.advance(3600)
            out = self.runner.publish_cycle()
            published += len([p for p in out.get('published') or [] if p.get('stage') == 'published'])
        self.assertGreaterEqual(published, 20)
        self.assertEqual(len(self.ig.published), len(set(self.ig.published)))
        self.assertLess(time.monotonic() - t0, 120)


if __name__ == '__main__':
    unittest.main()
