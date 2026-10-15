"""Live account check (not part of the offline suite): the runtime clients against the real accounts.

    python3 tests/live_accounts_check.py <runtime.env> [--writes]

Reads: Monday boards (paginated), one reserved item fresh, Dropbox link/listing/metadata of that item's file,
Instagram account probe, recent media, publishing quota, a past container's status.
--writes: reversible changes only: a test item created on the social board and deleted at once; a scratch Dropbox
folder with a small file uploaded, shared, renamed and deleted. Nothing is posted to Instagram.
Prints counts and states only (never tokens or file contents).
"""
import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from waset_ops import board as B  # noqa: E402
from waset_ops import rules  # noqa: E402
from waset_runtime.dropbox import Dropbox  # noqa: E402
from waset_runtime.instagram import Instagram  # noqa: E402
from waset_runtime.monday import Monday  # noqa: E402
from waset_runtime.service import read_env  # noqa: E402


def main():
    env = read_env(sys.argv[1])
    writes = '--writes' in sys.argv
    out = {}
    m = Monday(env['MONDAY_API_TOKEN'])
    social, sources = m.social_snapshot(), m.source_snapshot()
    out['monday'] = {'social_items': len(social), 'source_items': len(sources)}
    db = sqlite3.connect(f"file:{env['PIPELINE_DB']}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT r.item_id, i.file_id, i.file_url, i.folder_url, i.asset_key FROM ops_reservations r JOIN "
                     "ops_items i USING(item_id) WHERE i.publication='not_started' ORDER BY r.slot LIMIT 1").fetchone()
    it = m.fresh_item(row['item_id'])
    out['monday']['fresh_item_status'] = B.norm(it, 'status')
    d = Dropbox(refresh_token=env['DROPBOX_REFRESH_TOKEN'], app_key=env['DROPBOX_APP_KEY'],
                app_secret=env['DROPBOX_APP_SECRET'])
    meta = d.metadata(row['file_id'])
    out['dropbox'] = {'selected_file_matches_store': f"{meta['id']}@{meta['rev']}" == row['asset_key']}
    if row['folder_url']:
        lm = d.link_metadata(row['folder_url'])
        out['dropbox']['folder_listing'] = len(d.list_folder(lm['path_lower'], recursive=True))
    ig = Instagram(env['INSTAGRAM_TOKEN'], env['INSTAGRAM_ACCOUNT_ID'])
    ok, err = ig.probe()
    out['instagram'] = {'probe': ok, 'error': err, 'recent_media': len(ig.media(5))}
    try:
        out['instagram']['quota'] = ig.publishing_limit()
    except Exception as e:  # noqa: BLE001
        out['instagram']['quota_error'] = str(e)[:200]
    c = db.execute("SELECT container_id, media_id FROM ops_attempts WHERE stage='published' AND container_id IS NOT "
                   'NULL ORDER BY updated DESC LIMIT 1').fetchone()
    if c:
        out['instagram']['last_container_status'] = ig.container_status(c['container_id']).get('status_code')
        out['instagram']['last_permalink'] = bool(ig.permalink(c['media_id']))
    if writes:
        name = f'Bondok runtime test {int(time.time())} (deleted automatically)'
        iid = m.create_item(name, {B.COL['status']: {'label': 'Skipped'}, B.COL['notes']: 'runtime write check'},
                            B.GROUPS['Skipped'])
        m.change_columns(iid, {B.COL['notes']: 'runtime write check: updated'})
        seen = B.norm(m.fresh_item(iid), 'notes')
        m.delete_item(iid)
        gone = all(x.get('state') == 'deleted' for x in m.items([iid]))
        out['monday_writes'] = {'created': bool(iid), 'updated_read_back': seen == 'runtime write check: updated',
                                'deleted': gone}
        folder = '/Social Media/Bondok runtime test'
        d.ensure_folder(folder)
        tmp = Path(tempfile.mkdtemp()) / 'probe.txt'
        tmp.write_text('bondok runtime write check')
        up = d.upload(str(tmp), folder + '/probe.txt')
        url = d.share(folder + '/probe.txt')
        mv = d.move(folder + '/probe.txt', folder + '/probe-renamed.txt')
        d.delete(folder + '/probe-renamed.txt')
        d.delete(folder)
        out['dropbox_writes'] = {'uploaded': bool(up.get('rev')), 'shared': url.startswith('https://'),
                                 'renamed': (mv.get('metadata') or {}).get('name') == 'probe-renamed.txt',
                                 'deleted': True}
    print(json.dumps(out, indent=1, default=str))


if __name__ == '__main__':
    main()
