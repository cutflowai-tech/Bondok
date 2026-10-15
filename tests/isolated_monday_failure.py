"""Isolated proof (not part of the offline suite): the deployed runtime code publishes an approved item while Monday
status writes fail, records the attempt, and updates the display later without publishing twice.

    PYTHONPATH=<deployed code dir>:tests python3 tests/isolated_monday_failure.py <runtime.env> <production store>

Isolation: a consistent copy of the production store; Monday is a simulator loaded from a real read-only snapshot of
both boards (nothing is written to the real Monday); Dropbox is the real account for reads only (writes refused);
Instagram is a fake that records publish calls (nothing reaches Instagram). The clock is set to the item's slot.
Only Monday writes that change the item's status fail, until the "recovery" step.
"""
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from runtime_fakes import FakeInstagram, FakeMonday
from waset_ops import Ops
from waset_ops import board as B
from waset_ops import rules
from waset_runtime.cycles import Runner
from waset_runtime.dropbox import Dropbox
from waset_runtime.monday import Monday, MondayError
from waset_runtime.service import read_env


class StatusWritesFail(FakeMonday):
    failing = True
    status_failures = 0

    def change_columns(self, item_id, columns, group=None, board_id=rules.BOARD):
        if self.failing and B.COL['status'] in (columns or {}):
            self.status_failures += 1
            raise MondayError('isolated test: Monday status write refused (HTTP 500)')
        return super().change_columns(item_id, columns, group, board_id)


class ReadOnlyDropbox:
    WRITES = ('ensure_folder', 'share', 'upload', 'move', 'delete')

    def __init__(self, real):
        self.real, self.refused = real, []

    def __getattr__(self, name):
        if name in self.WRITES:
            def refuse(*a, **kw):
                self.refused.append(name)
                raise RuntimeError('isolated test: Dropbox writes are disabled')
            return refuse
        return getattr(self.real, name)


def main():
    env = read_env(sys.argv[1])
    work = Path(tempfile.mkdtemp(prefix='iso-monday-'))
    src = sqlite3.connect(f'file:{sys.argv[2]}?mode=ro', uri=True)
    dst = sqlite3.connect(work / 'state.sqlite')
    src.backup(dst)
    src.close()
    dst.close()
    T = [0.0]
    ops = Ops(str(work / 'state.sqlite'), clock=lambda: T[0])
    with ops.store.read() as c:
        r = c.execute("SELECT r.item_id, r.slot FROM ops_reservations r JOIN ops_items i USING(item_id) WHERE "
                      "i.publication='not_started' AND i.owner_state='active' ORDER BY r.slot LIMIT 1").fetchone()
    item, slot = r['item_id'], r['slot']
    real = Monday(env['MONDAY_API_TOKEN'])
    monday = StatusWritesFail(social=real.social_snapshot(), sources=real.source_snapshot())
    dbx = ReadOnlyDropbox(Dropbox(refresh_token=env['DROPBOX_REFRESH_TOKEN'], app_key=env['DROPBOX_APP_KEY'],
                                  app_secret=env['DROPBOX_APP_SECRET']))
    ig = FakeInstagram()
    runner = Runner(ops, monday, dbx, ig, name='isolated', sleep=lambda s: None, maintenance=lambda: {})
    T[0] = rules.instant(slot).timestamp() + 5

    def state():
        with ops.store.read() as c:
            it = c.execute('SELECT publication FROM ops_items WHERE item_id=?', (item,)).fetchone()
            att = [dict(x) for x in c.execute('SELECT id, stage, media_id, container_id FROM ops_attempts WHERE '
                                              'item_id=? ORDER BY created', (item,))]
            jobs = [dict(x) for x in c.execute(
                "SELECT id, state, attempts, substr(last_error,1,90) err, substr(payload,1,70) p FROM ops_outbox "
                "WHERE item_id=? AND kind='monday' AND created>=? ORDER BY id", (item, T[0] - 3600))]
        return {'publication': it['publication'], 'attempts': att, 'monday_jobs': jobs,
                'instagram_publish_calls': len(ig.publish_calls()), 'board_status': B.norm(monday.board.items[item],
                                                                                         'status')}

    out = {'item': item, 'slot': slot, 'steps': []}
    out['steps'].append({'step': 'publish cycle with Monday status writes failing', 'cycle': str(runner.publish_cycle())[:300],
                         **state()})
    for n in range(3):
        T[0] += 60
        runner.publish_cycle()
    out['steps'].append({'step': '3 more cycles, still failing', 'status_failures': monday.status_failures, **state()})
    monday.failing = False
    for n in range(20):
        T[0] += 120
        runner.sync_cycle()
        runner.publish_cycle()
    out['steps'].append({'step': 'Monday recovered; 20 sync+publish cycles', **state()})
    out['dropbox_writes_refused'] = dbx.refused
    out['verdict'] = {
        'published_once': len(ig.publish_calls()) == 1,
        'attempt_recorded': any(a['stage'] == 'published' and a['media_id'] for a in state()['attempts']),
        'status_failed_before_recovery': monday.status_failures > 0,
        'display_posted_after_recovery': state()['board_status'] == 'Posted',
    }
    print(json.dumps(out, indent=1, ensure_ascii=False, default=str))
    shutil.rmtree(work)


if __name__ == '__main__':
    main()
