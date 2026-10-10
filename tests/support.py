"""Synthetic fixtures. No production data, no network, no model calls."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from waset_ops import Command, Ops, rules  # noqa: E402
from waset_ops.board import COL  # noqa: E402
from waset_ops.db import dumps  # noqa: E402

T0 = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)   # Saturday 15:00 Cairo


class Clock:
    def __init__(self, start=T0):
        self.t = start.timestamp()

    def __call__(self):
        return self.t

    def set(self, d):
        self.t = d.timestamp()

    def advance(self, seconds):
        self.t += seconds


OWNER_ID = 'U-OWNER'


def owner_cmd(cid, op, item=None, explicit=False, **args):
    return Command(cid, op, OWNER_ID, 'owner', item, args, auth={'explicit': explicit})


def cell(key, text=None, value=None):
    return {'id': COL[key], 'text': text or '', 'value': None if value is None else json.dumps(value)}


def monday_item(iid, *, name='Calli 3', fmt='Story', code='LIP12', status='جاري فحص الفيديو', caption='',
                topaz='Not yet', asset='', publish_at=None, extra=None, group=None):
    cells = [cell('format', fmt, {'index': 0}), cell('code', code), cell('status', status, {'index': 0}),
             cell('topaz', topaz, {'index': 0}), cell('asset', asset)]
    if caption:
        cells.append(cell('caption', caption, {'text': caption}))
    if publish_at:
        cells.append(cell('publish_at', 'x', rules.monday_publish_at_value(rules.instant(publish_at))))
    for k, (text, value) in (extra or {}).items():
        cells.append(cell(k, text, value))
    return {'id': str(iid), 'name': name, 'column_values': cells, 'group': {'id': group} if group else None}


def set_cell(item, key, text, value=None):
    item['column_values'] = [c for c in item['column_values'] if c['id'] != COL[key]]
    item['column_values'].append(cell(key, text, value))
    return item


class OpsCase(unittest.TestCase):
    def setUp(self):
        from unittest import mock
        from waset_ops import monitor
        ample = mock.Mock(free=100_000_000_000, total=200_000_000_000, used=100_000_000_000)
        if hasattr(monitor, 'shutil'):     # tests never depend on this machine's free disk space
            patcher = mock.patch.object(monitor.shutil, 'disk_usage', return_value=ample)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.clock = Clock()
        self.ops = Ops(self.dir / 'state.sqlite', clock=self.clock)
        self.ops.store.migrate()
        self.run_id = 'run-1'
        self.fence = self.ops.run_start('wf1', self.run_id)['fence']
        self.seq = 0

    def tearDown(self):
        self.tmp.cleanup()

    def rid(self, prefix='r'):
        self.seq += 1
        return f'{prefix}-{self.seq}'

    # -------------------------------------------------- pipeline helpers
    def observe(self, *items, **kw):
        return self.ops.observe(list(items), **kw)

    def wf1(self, op, item_id, **args):
        self.ops.run_check_ok = True
        return self.ops.submit(Command(self.rid('wf1'), op, 'service:wf1', 'service:wf1', str(item_id),
                                       {'run_id': self.run_id, 'fence': self.fence, **args}))

    def file(self, n=1, content='hash'):
        return {'id': f'id:FILE{n}', 'rev': f'rev{n}', 'content_hash': f'{content}{n}', 'name': f'video_v{n}.mp4'}

    def select(self, item_id, n=1, content='hash'):
        return self.wf1('prep_source', item_id, file=self.file(n, content), url=f'https://www.dropbox.com/s/f{n}/v.mp4')

    def duration(self, item_id, seconds, n=1, content='hash'):
        return self.wf1('prep_preflight', item_id, result={'ready': True, 'duration': seconds,
                                                          'assetKey': f'id:FILE{n}@rev{n}',
                                                          'contentHash': f'{content}{n}'})

    def add_media(self, item_id, fmt, n=1, *, width=1080, height=1920, size=90_000_000, duration=30.0, url=True):
        path = self.dir / f'{item_id}-{n}-{fmt}.mp4'
        path.write_bytes(b'x')
        mid = f'media-{item_id}-{n}-{fmt}'
        info = {'width': width, 'height': height, 'bytes': size, 'duration': duration, 'assetKey': f'id:FILE{n}@rev{n}',
                'format': fmt, 'qaPolicy': rules.QA_POLICY, 'sha256': f'sha-{item_id}-{n}', 'topazed': True}
        if url:
            info['url'] = f'https://www.dropbox.com/s/prepared-{item_id}-{n}.mp4?raw=1'
        with self.ops.store.tx() as c:
            c.execute('INSERT OR REPLACE INTO media VALUES(?,?,?,?,?)', (mid, str(item_id), str(path), 'src', dumps(info)))
        return mid

    def topaz(self, item_id, n=1):
        return self.ops.submit(owner_cmd(self.rid('topaz'), 'confirm_topaz', str(item_id), explicit=True,
                                         confirmed=True, asset_key=f'id:FILE{n}@rev{n}'))

    def make_ready(self, iid, fmt='Story', *, code='LIP12', caption='', n=1, duration=30.0, approve_caption=True,
                   name=None):
        self.observe(monday_item(iid, fmt=fmt, code=code, name=name or f'Item{iid} {code}'))
        self.select(iid, n)
        if fmt == 'Story':
            self.duration(iid, duration, n)
        self.topaz(iid, n)
        r = self.select(iid, n)          # proceeds to prepare once Topaz is bound
        assert r.get('next') == 'prepare', r
        mid = self.add_media(iid, fmt, n, duration=duration)
        if fmt == 'Post' and approve_caption:
            self.ops.submit(owner_cmd(self.rid('cap'), 'update_caption', str(iid),
                                      text=caption or 'Sunlight sets the pace, DM us for edits. 🔥\n\n#reels'))
        r = self.wf1('prep_media', iid, result={'ready': True, 'mediaId': mid})
        return r

    def item(self, iid):
        with self.ops.store.read() as c:
            return self.ops.item(c, str(iid))

    def res(self, iid):
        with self.ops.store.read() as c:
            return self.ops.reservation(c, str(iid))

    def outbox(self, kind=None):
        with self.ops.store.read() as c:
            q = 'SELECT * FROM ops_outbox' + (' WHERE kind=?' if kind else '')
            return [dict(r) for r in c.execute(q, (kind,) if kind else ())]

    def drain_monday(self):
        """Simulate WF2's display sync succeeding for all pending Monday jobs."""
        for j in self.ops.outbox_take(['monday'], 'sync-test', 100):
            self.ops.outbox_ack(j['id'], 'sync-test', True)


def board_from_projection(ops, iid, fmt='Story', code='LIP12', caption='', topaz='Topazed'):
    """The board item as it looks after every v2 display write so far has landed."""
    import json as _json
    from waset_ops import board as _board
    with ops.store.read() as c:
        it = ops.item(c, iid)
    proj = _json.loads(it['projected'] or '{}')
    bi = monday_item(iid, fmt=fmt, code=code, status=proj.get('status') or '', caption=caption, topaz=topaz,
                     asset=proj.get('asset') or '', group=proj.get('_group'), name=it['name'])
    for k in _board.SYSTEM:
        v = proj.get(k)
        if v is None or k in ('status', 'asset'):
            continue
        kind = _board.KIND.get(k)
        if kind == 'link':
            set_cell(bi, k, v, {'url': v})
        elif kind == 'long':
            set_cell(bi, k, v, {'text': v})
        elif kind == 'datetime':
            set_cell(bi, k, 'x', rules.monday_publish_at_value(rules.instant(v)))
        elif kind == 'date':
            set_cell(bi, k, v, {'date': v})
        elif kind == 'hour':
            h, m = v.split(':')
            set_cell(bi, k, v, {'hour': int(h), 'minute': int(m)})
        else:
            set_cell(bi, k, v)
    return bi
