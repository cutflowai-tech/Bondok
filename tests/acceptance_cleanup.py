"""Resumable cleanup of the live acceptance test (ON THE SERVER as root; not part of the offline suite).

    PYTHONPATH=/opt/waset-bondok python3 tests/acceptance_cleanup.py <runtime.env> <fixtures.json> <cleanup-log.json>

Only test resources are touched: items whose name starts with "Acceptance " or code with "ACC" (plus the ids in
the fixtures manifest), the test folder of the manifest, and prepared copies whose file name starts with a test
item id. Every step is recorded in the log with its result; a rerun skips the steps already done, so an interrupted
cleanup resumes where it stopped. Real content is never touched (release_isolated only acts on isolated items).
"""
import json
import subprocess
import sys
import time
from pathlib import Path

from waset_ops import board as B
from waset_runtime.dropbox import Dropbox
from waset_runtime.monday import Monday
from waset_runtime.service import read_env

PREPARED = '/Social Media/Prepared'


def is_test(it) -> bool:
    code = (B.norm(it, 'code') or '').upper()
    return str(it.get('name') or '').startswith('Acceptance ') or code.startswith('ACC')


def main():
    env, fx, logp = read_env(sys.argv[1]), json.loads(Path(sys.argv[2]).read_text()), Path(sys.argv[3])
    log = json.loads(logp.read_text()) if logp.exists() else {}

    def step(name, fn):
        if log.get(name, {}).get('done'):
            print('skip', name)
            return log[name].get('result')
        res = fn()
        log[name] = {'done': True, 'at': time.time(), 'result': res}
        logp.write_text(json.dumps(log, indent=1, ensure_ascii=False, default=str))
        print('done', name, json.dumps(res, ensure_ascii=False, default=str)[:300])
        return res

    def iso(action):
        p = subprocess.run(['runuser', '-u', 'bondok', '--', 'env', f"WASET_RUNTIME_ENV={sys.argv[1]}",
                            'PYTHONPATH=/opt/waset-bondok', 'python3', '-m', 'waset_runtime.isolation', action],
                           capture_output=True, text=True)
        return {'rc': p.returncode, 'out': p.stdout[-800:], 'err': p.stderr[-300:]}

    m = Monday(env['MONDAY_API_TOKEN'])
    d = Dropbox(refresh_token=env['DROPBOX_REFRESH_TOKEN'], app_key=env['DROPBOX_APP_KEY'],
                app_secret=env['DROPBOX_APP_SECRET'])
    step('1_release_and_retire_test_items_in_store', lambda: iso('finalize'))

    def board():
        ids = {str(e['item_id']) for e in fx['items'].values() if e.get('item_id')}
        found = [i for i in m.social_snapshot() if is_test(i)]
        ids |= {str(i['id']) for i in found}
        out = []
        for x in m.items(sorted(ids)):
            if not is_test(x):
                out.append({'id': x['id'], 'skipped': 'not a test item'})
                continue
            if x.get('state') in ('active', 'archived'):
                m.delete_item(x['id'])
                out.append({'id': x['id'], 'name': x.get('name'), 'deleted': True})
        return out
    board_res = step('2_delete_test_items_on_monday', board)

    def prepared():
        ids = {str(e['item_id']) for e in fx['items'].values() if e.get('item_id')} | \
            {str(x['id']) for x in board_res or [] if x.get('deleted')}
        out = []
        for e in d.list_folder(PREPARED.lower(), recursive=False):
            if e.get('.tag') == 'file' and any(e['name'].startswith(i + '-') for i in ids):
                d.delete(e['path_lower'])
                out.append(e['name'])
        return out
    step('3_delete_prepared_copies_of_test_items', prepared)
    step('4_delete_test_folder', lambda: (d.delete(fx['root']), fx['root'])[1])
    step('5_clear_isolation', lambda: iso('clear'))
    log['complete'] = time.time()
    logp.write_text(json.dumps(log, indent=1, ensure_ascii=False, default=str))


if __name__ == '__main__':
    main()
