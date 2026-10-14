"""Dropbox file selection for one item: a line-by-line port of WF1's Item Context, Plan Listing, Candidates,
Owner File Link and Owner Selected File code nodes (workflows/build.py, 452da98..8675c93). The rules are unchanged:
which file is "the final video" is the same decision whichever runner makes it.

Problems are raised as ``SelectionProblem`` with the kind the handler expects (``config`` | ``editor``).
"""
from __future__ import annotations

import functools
import json
import re

VID = re.compile(r'\.(mp4|mov|m4v)$', re.I)
NOCAP = re.compile(r'(before|without|no|w\/o|pre)[\s_.-]*cap', re.I)
CAPNAME = re.compile(r'(^|[^a-z])(cap|caps|captions?|captioned|subs|subtitled)([^a-z]|$)', re.I)
JUNK = re.compile(r'(^|[^a-z])(test|testing|tmp|temp|proxy|preview|sample|draft|copy|old|backup|bkp|scratch|'
                  r'untitled)([^a-z]|$)', re.I)
TOPAZ = re.compile(r'(?:^|[^a-z])topaz(?:[^a-z]|$)', re.I)
CODE = re.compile(r'^\s*[a-z]{2,3}\s*[#_\- ]?\s*\d+', re.I)
FOLDER_ROOT = '/Social Media/Production'
MISSING_CODE = ('Code is missing or invalid on the social board, so no project folder can be created. Add the Code '
                '(for example LIP12) or a Dropbox link to the item')


class SelectionProblem(Exception):
    def __init__(self, message, kind='config'):
        super().__init__(message)
        self.kind = kind


def _val(item, col):
    c = next((c for c in (item or {}).get('column_values') or [] if c.get('id') == col), None)
    try:
        return json.loads(c.get('value') or 'null') if c else None
    except ValueError:
        return None


def context(work: dict, source: dict | None) -> dict:
    """Item Context: owner-selected file, or the folder to list (the item's, else a folder link)."""
    src_link = (_val(source, 'link_mm06bswn') or {}).get('url') if source else None
    owner = work.get('selection_mode') == 'owner_selected_file' and bool(work.get('selected_url'))
    links = [work.get('file_url'), src_link]
    folder = '' if owner else (work.get('folder_url') or next((u for u in links if u and '/scl/fo/' in u), ''))
    if not owner and not folder:
        folder = next((u for u in links if u and 'dropbox' in u), '')
    return {**work, 'itemId': work['item_id'], 'ownerSelected': owner,
            'selectedUrl': work.get('selected_url') if owner else None, 'folderUrl': folder}


def new_folder_path(ctx) -> str:
    if not CODE.match(str(ctx.get('code') or '')):
        raise SelectionProblem(MISSING_CODE, 'config')
    return FOLDER_ROOT + '/' + re.sub(r'[^A-Za-z0-9#_-]', '_', str(ctx['code'])) + '/' + ctx['format']


def plan_listing(meta: dict) -> dict:
    if not meta.get('path_lower'):
        raise SelectionProblem('The Dropbox link on the board is not in the connected Dropbox account or is no longer '
                               'shared; replace the link or share it from that account', 'config')
    direct = meta.get('.tag') == 'file' or bool(VID.search(meta.get('name') or ''))
    path = meta['path_lower'][:meta['path_lower'].rfind('/')] if direct else meta['path_lower']
    parts = path.split('/')
    parts.pop()
    parent = '/'.join(parts)
    use_parent = not direct and bool(re.search(r'#\d+', parts[-1] if parts else ''))
    return {'directPath': meta['path_lower'] if direct else None, 'folderPath': path, 'parent': parent,
            'useParent': use_parent, 'list': {'path': parent if use_parent else path, 'recursive': not direct}}


def _ver(n):
    m = re.search(r'(?:^|[^a-z])v\s?(\d+)(?:[._-](\d+))?', n, re.I) or re.search(r'V(\d+)(?:[._-](\d+))?', n)
    return (int(m[1]), int(m[2] or '0')) if m else None


def _key(s):
    m = re.match(r'^\s*([a-z]+)\s*(\d+)', (s or '').lower())
    return f'{m[1]} {m[2]}' if m else None


def _cmp(a, b):
    both = a['v'] and b['v']
    if both and a['v'][0] != b['v'][0]:
        return b['v'][0] - a['v'][0]
    if both and a['v'][1] != b['v'][1]:
        return b['v'][1] - a['v'][1]
    if bool(a['v']) != bool(b['v']):
        return -1 if a['v'] else 1
    if a['topaz'] != b['topaz']:
        return -1 if a['topaz'] else 1
    return (b['modified'] > a['modified']) - (b['modified'] < a['modified'])


def candidates(item_name, plan, entries) -> dict:
    """Candidates: the chosen final video (and the ordered pool), or SelectionProblem."""
    fp = plan['folderPath']
    root = plan['parent'] if plan['useParent'] else fp
    folder_name = fp.split('/')[-1]
    ki, kf = _key(item_name), _key(folder_name)
    if ki and kf and ki != kf:
        raise SelectionProblem(f'Folder "{folder_name}" does not match this item', 'config')
    pool_all = []
    for e in entries:
        if e.get('.tag') != 'file' or not VID.search(e.get('name') or ''):
            continue
        pl = e['path_lower']
        if plan.get('directPath') and pl != plan['directPath']:
            continue
        if re.search(r'\.fcp\w*bundle/', pl):
            continue
        d = pl[:pl.rfind('/')]
        if d != root and not d.startswith(root + '/'):
            continue
        segs = [] if d == root else d[len(root) + 1:].split('/')
        archived = any(s in ('archive', 'old') for s in segs)
        in_captions = not archived and any('caption' in s for s in segs)
        in_project = d == fp
        in_project_archive = archived and d.startswith(fp + '/')
        if not in_project and not in_captions and not in_project_archive:
            continue
        stem = re.sub(r'\.[^.]+$', '', e['name'])
        nocap = bool(NOCAP.search(stem))
        pool_all.append({'name': e['name'], 'path': pl, 'size': e.get('size'), 'id': e.get('id'), 'rev': e.get('rev'),
                         'content_hash': e.get('content_hash'),
                         'modified': e.get('client_modified') or e.get('server_modified') or '',
                         'archived': archived, 'cap': (in_captions or bool(CAPNAME.search(stem))) and not nocap,
                         'nocap': nocap, 'junk': bool(JUNK.search(stem)), 'topaz': bool(TOPAZ.search(stem)),
                         'v': _ver(stem)})
    live = [f for f in pool_all if not f['archived']]
    pool = [f for f in live if not f['junk'] and not f['nocap']] or [f for f in live if not f['junk']]
    if not pool:
        raise SelectionProblem('No final video found in the project folder', 'editor')
    key = functools.cmp_to_key(_cmp)
    mains = sorted([f for f in pool if not f['cap']], key=key)
    caps = sorted([f for f in pool if f['cap']], key=key)
    chosen = (mains or caps)[0]
    return {'file': chosen, 'files': [chosen] + [f for f in pool if f is not chosen]}


def owner_file(link_meta: dict, meta: dict) -> dict:
    """Owner File Link + Owner Selected File: the exact file the owner chose, with its version."""
    if link_meta.get('.tag') != 'file':
        raise SelectionProblem('The Dropbox link selected for this item is not a file; select the exact video file')
    if not link_meta.get('path_lower'):
        raise SelectionProblem('The Dropbox file selected for this item is not in the connected Dropbox account; '
                               'share it from that account or select another file')
    if meta.get('.tag') and meta['.tag'] != 'file':
        raise SelectionProblem('The Dropbox link selected for this item is not a file; select the exact video file')
    if not (meta.get('id') and meta.get('rev') and meta.get('content_hash')):
        raise SelectionProblem('Dropbox did not report the exact version of the selected file; select it again')
    if link_meta.get('id') and link_meta['id'] != meta['id']:
        raise SelectionProblem('The selected Dropbox link no longer points to the same file; select the file again')
    if not VID.search(meta.get('name') or ''):
        raise SelectionProblem('The selected Dropbox file is not an MP4/MOV video (' +
                               str(meta.get('name') or '')[:80] + ')')
    return {'id': meta['id'], 'rev': meta['rev'], 'content_hash': meta['content_hash'], 'name': meta['name'],
            'path': meta['path_lower']}
