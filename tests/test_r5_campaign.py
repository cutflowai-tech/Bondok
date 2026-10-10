"""Seeded multi-cycle campaigns (directive §19: multi-cycle, not isolated happy paths). Synthetic data only.

A simulated week of 10-minute cycles drives the real handler the way WF1/WF2/WF3 and the owner do: new items
(some carrying the same video as another item), owner pause / resume / skip / Posted / time changes, file
replacement mid-job, Instagram answers (published, lost response, unknown, documented refusal), worker crashes,
WF2 and Monday/Slack outages, and restarts on the same store (replay after restart). A world model records what
Instagram actually published. The invariants are stated against that world and the owner's decisions, not against
the handler's own state:

  I1  the same video (format + content) is published automatically at most once, also after an owner report;
  I2  an automatic publication is claimed only for an item the owner has left active and not reported posted;
  I3  every Slack notice is delivered once Slack is back;
  I4  liveness: after a quiet settling period every active video is published (exactly once);
  I5  every real publication ends as `published` in the store (unknown outcomes are reconciled).
"""
import json
import random
import unittest
from datetime import timedelta

from support import OpsCase, monday_item, owner_cmd, rules

from waset_ops import Ops

SEEDS = (11, 23, 37, 41)


class World:
    def __init__(self):
        self.published = {}         # identity -> ('auto'|'owner', item)
        self.containers = {}        # container id -> identity, published?
        self.n = 0


class Campaign(OpsCase):
    def setUp(self):
        super().setUp()
        self.world = World()
        self.oracle = {}            # item -> active | paused | skipped | posted (the owner's decisions)
        self.video = {}             # item -> [format, content key, file version]
        self.violations = []

    # ------------------------------------------------------------------ owner / WF1
    def identity(self, iid):
        f, k, n = self.video[iid]
        return f'{f}|{k}{n}'

    def new_item(self, rnd):
        iid = str(100 + len(self.video))
        fmt = rnd.choice(('Story', 'Story', 'Post'))
        same = [i for i in self.video if self.video[i][0] == fmt]
        key = self.video[rnd.choice(same)][1] if same and rnd.random() < 0.25 else f'v{iid}-'
        self.video[iid] = [fmt, key, 1]
        self.oracle[iid] = 'active'
        self.observe(monday_item(iid, fmt=fmt, code='LIP12', name=f'Item{iid} LIP12'))
        self.prepare(iid)

    def prepare(self, iid):
        fmt, key, n = self.video[iid]
        self.contents[iid] = key
        self.select(iid, n, key)
        if fmt == 'Story':
            self.duration(iid, 30.0, n, key)
        self.topaz(iid, n)
        self.select(iid, n, key)
        mid = self.add_media(iid, fmt, n)
        with self.ops.store.tx() as c:               # the prepared file carries this video's bytes (sha)
            m = c.execute('SELECT metadata FROM media WHERE id=?', (mid,)).fetchone()
            info = json.loads(m['metadata'])
            info['sha256'] = f'sha-{key}{n}'
            c.execute('UPDATE media SET metadata=? WHERE id=?', (json.dumps(info), mid))
        if fmt == 'Post':
            self.ops.submit(owner_cmd(self.rid('cap'), 'update_caption', iid,
                                      text='Sunlight sets the pace, DM us for edits. 🔥\n\n#reels'))
        self.wf1('prep_media', iid, result={'ready': True, 'mediaId': mid})

    def owner_event(self, rnd):
        iid = rnd.choice(list(self.video))
        state = self.oracle[iid]
        op = rnd.choice(('pause', 'resume', 'skip', 'report_published', 'request_reschedule', 'replace', 'replace'))
        if op == 'replace':
            if state == 'posted':
                return
            n = self.video[iid][2] + 1                 # a new file version mid-job; prepared again later
            self.select(iid, n, self.video[iid][1])
            if self.item(iid)['content_hash'] == f'{self.video[iid][1]}{n}':
                self.video[iid][2] = n                 # WF1 took it (a paused or skipped item is not visited)
            return
        args = {}
        if op == 'request_reschedule':
            at = self.ops.now_dt() + timedelta(hours=rnd.randint(2, 60))
            at = at.replace(minute=0, second=0, microsecond=0)
            args = {'at': rules.iso(at)}
        if op == 'report_published':
            args = {'source': 'slack'}
        r = self.ops.submit(owner_cmd(self.rid('o'), op, iid, explicit=True, **args))
        if r['state'] != 'completed' or state == 'posted':
            return
        if op == 'pause':
            self.oracle[iid] = 'paused'
        elif op == 'skip':
            self.oracle[iid] = 'skipped'
        elif op == 'resume':
            self.oracle[iid] = 'active'
        elif op == 'report_published':
            self.oracle[iid] = 'posted'
            self.world.published.setdefault(self.identity(iid), ('owner', iid))

    # ------------------------------------------------------------------ WF2 / Instagram
    def wf2(self, rnd, worker, outcomes=True):
        for w in self.ops.due(worker, limit=10)['work']:
            if w['kind'] == 'reconcile':
                ident, done = self.world.containers.get(w['container_id'], (None, False))
                self.ops.reconcile(w['attempt_id'], 'PUBLISHED' if done else 'EXPIRED')
                continue
            cl = self.ops.claim(w['item_id'], worker)
            if not cl.get('claimed'):
                continue
            iid = w['item_id']
            if self.oracle[iid] != 'active':                                         # I2
                self.violations.append(f'I2: claimed {iid} while the owner had it {self.oracle[iid]}')
            self.world.n += 1
            cid = f'C{self.world.n}'
            self.ops.container(cl['attempt_id'], worker, cl['fence'], cid)
            if not self.ops.commit(cl['attempt_id'], worker, cl['fence'], container_status='FINISHED').get('committed'):
                continue
            ident = self.identity(iid)
            kind = rnd.choice(('ok', 'ok', 'ok', 'lost', 'unknown', 'refused', 'crash')) if outcomes else 'ok'
            reached = kind in ('ok', 'lost', 'crash') or (kind == 'unknown' and rnd.random() < 0.5)
            if reached:
                if ident in self.world.published:                                    # I1
                    self.violations.append(f'I1: {ident} published again by {iid} '
                                           f'(first: {self.world.published[ident]})')
                self.world.published.setdefault(ident, ('auto', iid))
            self.world.containers[cid] = (ident, reached)
            if kind == 'ok':
                self.ops.result(cl['attempt_id'], worker, media_id=f'IG{self.world.n}')
            elif kind in ('lost', 'unknown'):
                self.ops.result(cl['attempt_id'], worker, error=json.dumps({'error': {'code': 2, 'message': 'x'}}),
                                http_status=500)
            elif kind == 'refused':
                self.ops.result(cl['attempt_id'], worker, error=json.dumps(
                    {'error': {'code': 9, 'error_subcode': 2207042, 'message': 'limit'}}), http_status=400,
                    definitive=True)
            # 'crash': no answer; the lease expires and the attempt becomes outcome unknown

    def sync(self, monday_up, slack_up):
        for j in self.ops.outbox_take(['monday'], 'wf2-sync', 100):
            self.ops.outbox_ack(j['id'], 'wf2-sync', monday_up, None if monday_up else 'Monday 503')
        for j in self.ops.outbox_take(['slack'], 'bondok', 100):
            self.ops.outbox_ack(j['id'], 'bondok', slack_up, None if slack_up else 'SlackApiError')

    def auto_items(self):
        return {iid for how, iid in self.world.published.values() if how == 'auto'}

    # ------------------------------------------------------------------ campaign
    def run_campaign(self, seed, days=4):
        rnd = random.Random(seed)
        for _ in range(6):
            self.new_item(rnd)
        wf2_down = monday_down = slack_down = 0
        for cycle in range(days * 144):
            if rnd.random() < 0.04 and len(self.video) < 18:
                self.new_item(rnd)
            if rnd.random() < 0.10:
                self.owner_event(rnd)
            if rnd.random() < 0.03:                    # a replaced file is confirmed and prepared again
                pend = [i for i in self.video if self.oracle[i] != 'posted']
                if pend:
                    self.prepare(rnd.choice(pend))
            if rnd.random() < 0.01:
                wf2_down = rnd.randint(6, 18)          # 1-3 hours without WF2: missed slots
            if rnd.random() < 0.01:
                monday_down = rnd.randint(3, 12)
            if rnd.random() < 0.01:
                slack_down = rnd.randint(6, 24)
            if rnd.random() < 0.01:                    # restart: the same store, a new process
                self.ops = Ops(self.dir / 'state.sqlite', clock=self.clock)
            for minute in range(0, 10, 2):
                if not wf2_down:
                    self.wf2(rnd, f'wf2-{cycle}-{minute}')
                self.clock.advance(120)
            self.sync(not monday_down, not slack_down)
            if cycle % 3 == 0:
                self.ops.repair()
            wf2_down, monday_down, slack_down = max(0, wf2_down - 1), max(0, monday_down - 1), max(0, slack_down - 1)
        # Settling: everything healthy, every remaining file confirmed and prepared, a quiet week.
        for iid in self.video:
            if self.oracle[iid] == 'active' and iid not in self.auto_items():
                self.prepare(iid)
                hold = json.loads(self.item(iid)['hold'] or 'null')
                if hold and hold.get('party') == 'owner':   # the owner answers the open question: "publish it"
                    r = self.ops.submit(owner_cmd(self.rid('ans'), 'request_publish', iid, explicit=True))
                    self.assertIn(r['state'], ('completed', 'accepted'), (iid, hold, r))
        for cycle in range(7 * 144):
            for minute in range(0, 10, 2):
                self.wf2(rnd, f'settle-{cycle}-{minute}', outcomes=False)
                self.clock.advance(120)
            self.sync(True, True)
            if cycle % 3 == 0:
                self.ops.repair()
        return rnd

    def check(self):
        self.assertEqual(self.violations, [])
        # I4: each active item went out (with whichever file version it had then), or its current video already did.
        missing = [i for i in self.video if self.oracle[i] == 'active' and i not in self.auto_items()
                   and self.identity(i) not in self.world.published]
        self.assertEqual(missing, [], {i: (self.identity(i), self.item(i)['readiness'], self.item(i)['block_reason'],
                                           self.item(i)['publication'], self.item(i)['hold']) for i in missing})
        for ident, (how, iid) in self.world.published.items():                      # I5
            if how == 'auto':
                self.assertEqual(self.item(iid)['publication'], 'published', (ident, iid))
        with self.ops.store.read() as c:                                              # I3
            self.assertEqual(c.execute("SELECT COUNT(*) FROM ops_outbox WHERE kind='slack' AND state NOT IN "
                                       "('done','superseded')").fetchone()[0], 0)


def _make(seed):
    def test(self):
        self.run_campaign(seed)
        self.check()
    test.__name__ = f'test_campaign_seed_{seed}'
    return test


for _s in SEEDS:
    setattr(Campaign, f'test_campaign_seed_{_s}', _make(_s))


if __name__ == '__main__':
    unittest.main()
