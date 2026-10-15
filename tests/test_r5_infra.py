"""Round 5 monitoring/outbox traces (M14, M19, M28, LOW-03, LOW-05). Synthetic data only."""
import json
import sqlite3
import threading
import time
import unittest
from unittest import mock

from support import OpsCase, monday_item, owner_cmd

from waset_ops import monitor


class R5_M14_LowDiskAlertLifecycle(OpsCase):
    """M14: no hysteresis (re-alert on every crossing), frozen detail (5.9 GB shown at 0.3 GB), '0 waiting' while
    items wait, and no recovery notice."""

    def cycle(self, gb, k):
        with mock.patch.object(monitor.shutil, 'disk_usage', return_value=mock.Mock(free=int(gb * 1e9))):
            lease = self.ops.run_start('wf3', f'w{k}')
            self.ops.repair(f'w{k}', lease['fence'])
            self.ops.run_finish('wf3', f'w{k}')
            self.ops.heartbeat('wf1')
            self.ops.heartbeat('wf2')
        self.clock.advance(1800)

    def disk_msgs(self):
        return [json.loads(o['payload'])['text'] for o in self.outbox('slack') if 'disk' in o['payload'].lower()]

    def test_flapping_escalation_and_recovery(self):
        self.observe(monday_item('1'))
        self.wf1('prep_media', '1', result={'ready': False, 'retryable': True,
                                            'reason': 'Insufficient free space for safe media preparation',
                                            'assetKey': None, 'format': 'Story'})
        for k, gb in enumerate((5.9, 6.1, 5.95, 6.05, 5.9)):
            self.cycle(gb, k)
        self.assertEqual(len(self.disk_msgs()), 1)                    # hysteresis: one alert while low
        self.assertIn('1 item', self.disk_msgs()[0])                  # fresh count of waiting items
        for k, gb in enumerate((3.0, 1.0, 0.3), start=10):
            self.cycle(gb, k)
        msgs = self.disk_msgs()
        self.assertEqual(len(msgs), 2)                                # one escalation when it becomes critical
        self.assertTrue('1.0 GB' in msgs[-1] or '0.3 GB' in msgs[-1], msgs[-1])
        for k, gb in enumerate((6.5, 8.0, 8.0), start=20):
            self.cycle(gb, k)
        msgs = self.disk_msgs()
        self.assertEqual(len(msgs), 3)
        self.assertIn('recovered', msgs[-1].lower())


class R5_M19_SlackOutageKeepsNotices(OpsCase):
    """M19: notices escalated after ~1 h of Slack failures were never retried and nothing said so."""

    def test_notice_survives_a_three_hour_outage(self):
        with self.ops.store.tx() as c:
            self.ops.notify(c, 'k1', 'Important notice for the owner')
        delivered = False
        for minute in range(0, 6 * 60, 1):
            for j in self.ops.outbox_take(['slack'], 'bondok', 10, lease=120):
                ok = minute >= 180                                    # Slack is down for three hours
                self.ops.outbox_ack(j['id'], 'bondok', ok, None if ok else 'SlackApiError')
                delivered = delivered or ok
            self.clock.advance(60)
        self.assertTrue(delivered)
        with self.ops.store.read() as c:
            self.assertEqual(c.execute("SELECT state FROM ops_outbox WHERE dedupe_key='slack:k1'").fetchone()[0], 'done')


class R5_M28_SlowDisplayBatchKeepsItsLease(OpsCase):
    """M28 (REASONED, reproduced here): a display batch slower than the 300 s lease was taken again by the next
    WF2 run (duplicate writes) and counted as a crashed worker (wrong escalation reason)."""

    def test_next_run_does_not_take_a_slow_batch(self):
        for i in range(10):
            self.make_ready(str(100 + i))
        a = self.ops.outbox_take(['monday'], 'wf2-a', 10)
        self.assertEqual(len(a), 10)
        mine, taken_by_others = {j['id'] for j in a}, set()
        t0 = self.clock.t
        # WF2 run A applies its batch slowly (40 s per job, ~400 s in total), acknowledging each job as it goes;
        # the next WF2 runs start every minute meanwhile.
        for i, j in enumerate(a):
            self.clock.t = t0 + 40 * (i + 1)
            self.assertTrue(self.ops.outbox_ack(j['id'], 'wf2-a', True)['ok'])
            if (i + 1) % 2 == 0:
                for k in self.ops.outbox_take(['monday'], f'wf2-b{i}', 10):
                    taken_by_others.add(k['id'])
                    self.ops.outbox_ack(k['id'], f'wf2-b{i}', True)
        self.assertEqual(mine & taken_by_others, set())
        with self.ops.store.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM ops_outbox WHERE state='escalated'").fetchone()[0], 0)


class R5_LOW03_HeldLockIsBoundedAndTruthful(OpsCase):
    def test_submit_under_a_held_write_lock(self):
        self.ops.store.busy_ms = 300
        self.observe(monday_item('1'))
        holder = sqlite3.connect(self.ops.store.path, isolation_level=None)
        holder.execute('BEGIN IMMEDIATE')
        try:
            t = time.time()
            r = self.ops.submit(owner_cmd('x1', 'pause', '1'))
            self.assertLess(time.time() - t, 5)
            self.assertEqual(r['state'], 'failed')
            self.assertEqual(r['code'], 'storage')
            self.assertFalse(r['recorded'])
        finally:
            holder.execute('ROLLBACK')
            holder.close()
        r = self.ops.submit(owner_cmd('x1', 'pause', '1'))            # retried later with the same id: runs once
        self.assertEqual(r['state'], 'completed')


class R5_LOW05_OneWriterPerColumnPerBatch(OpsCase):
    def test_a_batch_never_writes_the_same_item_column_twice(self):
        self.make_ready('1', 'Post', code='LIP1')
        for i in range(3):
            self.ops.submit(owner_cmd(self.rid(), 'update_caption', '1', text=f'Caption {i}, DM us. 🔥\n\n#reels'))
            self.ops.submit(owner_cmd(self.rid(), 'pause' if i % 2 == 0 else 'resume', '1', explicit=True))
        jobs = self.ops.outbox_take(['monday'], 'w', 50)
        seen = set()
        for j in jobs:
            for col in j['payload'].get('columns', {}):
                self.assertNotIn((j['item_id'], col), seen, col)
                seen.add((j['item_id'], col))


if __name__ == '__main__':
    unittest.main()
