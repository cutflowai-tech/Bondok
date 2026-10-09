"""Migration + rollback rehearsal on ISOLATED copies (never the live database).

usage: python3 deploy/rehearse.py <state.sqlite snapshot> <board snapshot json> <old helper.py>
Prints a JSON report. The inputs are copied into a temporary directory.
"""
import base64
import collections
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from waset_ops import Command, Ops, rules  # noqa: E402


def old_helper(helper, data_dir, path, body):
    arg = base64.b64encode(json.dumps({'path': path, 'body': body}).encode()).decode()
    p = subprocess.run([sys.executable, str(helper), arg], capture_output=True, text=True, timeout=60,
                       env={'WASET_SOCIAL_DATA_DIR': str(data_dir), 'PATH': '/usr/bin:/bin'})
    return p.returncode, (json.loads(p.stdout) if p.stdout.strip() else None)


def main(db_snapshot, board_snapshot, helper):
    report = {}
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        db = d / 'state.sqlite'
        shutil.copy(db_snapshot, db)
        with sqlite3.connect(db) as c:
            before = {t: c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
                      for t in ('reservations', 'media', 'publications', 'jobs', 'item_state', 'audit_log', 'assets')}
        report['legacy_counts_before'] = before
        o = Ops(db)
        report['schema'] = o.store.migrate()
        with sqlite3.connect(db) as c:
            after = {t: c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in before}
        report['legacy_rows_preserved'] = before == after

        # Rollback compatibility: the deployed helper must still run on the migrated DB.
        rc1, snap = old_helper(helper, d, '/v1/publish/snapshot', {})
        rc2, lock = old_helper(helper, d, '/v1/lock', {'name': 'rehearsal', 'owner': 'r'})
        rc3, unlock = old_helper(helper, d, '/v1/unlock', {'name': 'rehearsal', 'owner': 'r'})
        report['old_helper_on_migrated_db'] = {'snapshot_ok': rc1 == 0 and 'receipts' in (snap or {}),
                                               'lock_ok': rc2 == 0 and (lock or {}).get('acquired') is True,
                                               'unlock_ok': rc3 == 0}

        # Bootstrap from the real board snapshot (conservative import, no writes planned).
        items = json.loads(Path(board_snapshot).read_text())['boards'][0]['items_page']['items']
        t0 = time.time()
        obs = o.observe(items, complete=True)
        report['bootstrap'] = {'items': len(items), 'imported': len(obs['imported']), 'edits': len(obs['edits']),
                               'seconds': round(time.time() - t0, 2)}
        with o.store.read() as c:
            q = lambda s: [dict(r) for r in c.execute(s)]
            report['states'] = {
                'publication': {r['publication']: r['n'] for r in q('SELECT publication, COUNT(*) n FROM ops_items GROUP BY 1')},
                'owner_state': {r['owner_state']: r['n'] for r in q('SELECT owner_state, COUNT(*) n FROM ops_items GROUP BY 1')},
                'format': {str(r['format']): r['n'] for r in q('SELECT format, COUNT(*) n FROM ops_items GROUP BY 1')},
                'caption_state': {str(r['caption_state']): r['n'] for r in q('SELECT caption_state, COUNT(*) n FROM ops_items GROUP BY 1')},
                'holds': {r['h']: r['n'] for r in q("SELECT json_extract(hold,'$.kind') h, COUNT(*) n FROM ops_items WHERE hold IS NOT NULL GROUP BY 1")},
                'requested_future_times': q("SELECT COUNT(*) n FROM ops_items WHERE requested_at IS NOT NULL")[0]['n'],
                'topaz_bound': q("SELECT COUNT(*) n FROM ops_items WHERE topaz_asset IS NOT NULL")[0]['n'],
                'reservations': q('SELECT COUNT(*) n FROM ops_reservations')[0]['n'],
                'attempts': q('SELECT COUNT(*) n FROM ops_attempts')[0]['n'],
                'monday_writes_queued': q("SELECT COUNT(*) n FROM ops_outbox WHERE kind='monday'")[0]['n'],
                'slack_notifications': q("SELECT COUNT(*) n FROM ops_outbox WHERE kind='slack'")[0]['n'],
            }
        report['second_observation_edits'] = len(o.observe(items)['edits'])     # must be 0: no loop
        report['due_now'] = o.due('rehearsal')['work']
        report['work_queue_first_cycle'] = len(o.work_queue(limit=20))
        styles = collections.Counter(w['rotation'].split(':')[0] for w in o.work_queue(limit=20))
        report['work_queue_styles'] = dict(styles)
        report['monitor'] = o.repair()

        # Rollback receipt guard: a v2 publication blocks the old helper's claim.
        legacy_posted = [i['id'] for i in items if any(c['id'] == 'status' and c['text'] == 'Posted' for c in i['column_values'])]
        with o.store.tx() as c:
            iid = next(r['item_id'] for r in c.execute("SELECT item_id FROM ops_items WHERE publication='not_started' LIMIT 1"))
            c.execute("INSERT INTO ops_attempts(id,item_id,content_rev,payload_fp,payload,slot,worker,fence,lease_until,stage,"
                      "created,updated) VALUES('A-rehearsal',?,1,'fp','{}','2026-10-10T18:00:00Z','w',1,0,'committed',0,0)", (iid,))
            a = dict(c.execute("SELECT * FROM ops_attempts WHERE id='A-rehearsal'").fetchone())
            o._legacy_receipt(c, a, 'published', {'publishedMediaId': 'synthetic'})
            # Give the old helper everything else it checks, so only the receipt can stop it.
            (d / 'media').mkdir(exist_ok=True)
            (d / 'media' / 'r.mp4').write_bytes(b'x')
            info = {'width': 1080, 'height': 1920, 'bytes': 1000, 'duration': 10, 'format': 'Story', 'qaPolicy': 3,
                    'topazed': True, 'assetKey': 'x', 'url': 'x', 'contentHash': 'h'}
            c.execute('INSERT INTO media VALUES(?,?,?,?,?)', ('x', iid, str(d / 'media' / 'r.mp4'), 'x', json.dumps(info)))
            now_slot = time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime(time.time() - 60))
            c.execute('INSERT INTO reservations VALUES(?,?,?,?,1)', (iid, 'Story', 'X', now_slot))
        rc, claim = old_helper(helper, d, '/v1/publish/claim', {'itemId': iid, 'format': 'Story', 'at': now_slot,
                                                                 'mediaId': 'x', 'topazed': True, 'assetKey': 'x',
                                                                 'expectedUrl': 'x', 'sourceProjectId': '1',
                                                                 'owner': 'old', 'account': rules.ACCOUNT})
        report['rollback_old_claim_on_v2_receipt'] = {'exit': rc, 'claimed': (claim or {}).get('claimed'),
                                                       'stage': (claim or {}).get('stage'),
                                                       'refused_by_receipt': (claim or {}).get('stage') == 'published'}
        report['legacy_posted_on_board'] = len(legacy_posted)
    print(json.dumps(report, ensure_ascii=False, indent=1, default=str))


if __name__ == '__main__':
    main(*sys.argv[1:4])
