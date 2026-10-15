"""Synthetic Monday / Dropbox / Instagram / media worker for the Bondok runtime tests (agent migration P2/P4).

The fakes implement the client interfaces of src/waset_runtime with in-memory state and scriptable failures. They
never touch the network. Shapes follow what the APIs return (Monday column values via board_sim, Dropbox metadata
fields, Graph API container statuses).
"""
from __future__ import annotations

import copy
import itertools
import json

import board_sim
from support import rules
from waset_ops import board as B
from waset_ops import media
from waset_ops.db import dumps

from waset_runtime.dropbox import DropboxError
from waset_runtime.http import HttpError
from waset_runtime.monday import MondayError


class FakeMonday:
    def __init__(self, social=(), sources=()):
        self.board = board_sim.Board(list(social))
        self.sources = {str(s['id']): copy.deepcopy(s) for s in sources}
        self.subs = {}                     # parent id -> [subitem]
        self.writes = []                   # (op, args)
        self.fail = {}                     # method -> exception to raise (once per entry in a list)
        self.ids = itertools.count(7000)

    def _maybe_fail(self, name):
        f = self.fail.get(name)
        if f:
            e = f.pop(0) if isinstance(f, list) else f
            if isinstance(f, list) and not f:
                self.fail.pop(name)
            if e:
                raise e

    # reads
    def source_snapshot(self):
        self._maybe_fail('source_snapshot')
        return [copy.deepcopy(s) for s in self.sources.values()]

    def social_snapshot(self):
        self._maybe_fail('social_snapshot')
        return self.board.snapshot()

    def fresh_item(self, item_id):
        self._maybe_fail('fresh_item')
        it = self.board.items.get(str(item_id))
        if it is None:
            raise MondayError('item not found on the board')
        return {**copy.deepcopy(it), 'board': {'id': rules.BOARD}}

    def source_status(self, sid):
        s = self.sources.get(str(sid)) if sid else None
        return next((c.get('text') for c in s['column_values'] if c['id'] == 'color_mm1ryfcb'), None) if s else None

    def guard_values(self, item_id, cols):
        self._maybe_fail('guard_values')
        it = self.board.items.get(str(item_id))
        if it is None:
            return None
        return {c['id']: c for c in it['column_values'] if c['id'] in cols}

    def updates(self, item_id, limit=100):
        s = self.sources.get(str(item_id)) or {}
        return {'id': item_id, 'name': s.get('name'), 'updates': s.get('updates') or []}

    def subitems(self, parent):
        return copy.deepcopy(self.subs.get(str(parent), []))

    # writes
    def change_columns(self, item_id, columns, group=None, board_id=rules.BOARD):
        self._maybe_fail('change_columns')
        self.writes.append(('change_columns', str(item_id), columns, group))
        err = self.board.apply({'item_id': str(item_id), 'columns': columns, 'group': group})
        if err:
            raise MondayError(err)
        return {'change_multiple_column_values': {'id': str(item_id)}}

    def change_column(self, board_id, item_id, column_id, value):
        self._maybe_fail('change_column')
        self.writes.append(('change_column', str(board_id), str(item_id), column_id, value))
        if str(board_id) == rules.SOURCE_BOARD:
            s = self.sources[str(item_id)]
            s['column_values'] = [c for c in s['column_values'] if c['id'] != column_id] + \
                [{'id': column_id, 'text': value.get('label'), 'value': json.dumps(value)}]
        return {}

    def create_items(self, creates, board_id=rules.BOARD):
        self._maybe_fail('create_items')
        data = {}
        for cr in creates:
            iid = str(next(self.ids))
            cells = []
            for colid, mv in cr['columns'].items():
                if B.KIND.get(board_sim.INV.get(colid)) in (None, 'people'):
                    if isinstance(mv, str):
                        cells.append({'id': colid, 'text': mv, 'value': json.dumps(mv)})
                    continue
                c = board_sim.cell_after_write(colid, mv)
                if c:
                    cells.append(c)
            self.board.items[iid] = {'id': iid, 'name': cr['name'], 'group': {'id': cr['group']}, 'column_values': cells}
            data['s' + str(cr['key'])] = {'id': iid}
            self.writes.append(('create_item', iid, cr['name']))
        return {'data': data}

    def create_subitem(self, parent, name, columns):
        sid = str(next(self.ids))
        self.subs.setdefault(str(parent), []).append({'id': sid, 'name': name, 'updates': []})
        self.writes.append(('create_subitem', str(parent), name))
        return sid

    def create_update(self, item_id, body):
        for subs in self.subs.values():
            for s in subs:
                if s['id'] == str(item_id):
                    s['updates'].append({'text_body': body})
        self.writes.append(('create_update', str(item_id)))
        return str(next(self.ids))

    def create_item(self, name, columns=None, group=None, board_id=rules.BOARD):
        r = self.create_items([{'key': 'x', 'name': name, 'columns': columns or {}, 'group': group}])
        return r['data']['sx']['id']

    def board_of(self, item_id):
        return rules.BOARD if str(item_id) in self.board.items else None

    def archive_item(self, item_id):
        self.writes.append(('archive_item', str(item_id)))
        self.board.items.pop(str(item_id), None)

    def delete_item(self, item_id):
        self.writes.append(('delete_item', str(item_id)))
        self.board.items.pop(str(item_id), None)

    def text(self, iid, key):
        return B.norm(self.board.items[str(iid)], key)


def source_item(sid, *, code='LIP12', fmt='Story', link=None, name=None, updates=None):
    cells = [{'id': 'text_mm066x8y', 'text': code, 'value': json.dumps(code)},
             {'id': 'color_mm1ryfcb', 'text': fmt, 'value': json.dumps({'index': 1})}]
    if link:
        cells.append({'id': 'link_mm06bswn', 'text': link, 'value': json.dumps({'url': link, 'text': 'Dropbox'})})
    return {'id': str(sid), 'name': name or f'Project {code}', 'column_values': cells, 'updates': updates or []}


class FakeDropbox:
    def __init__(self):
        self.files = {}      # path_lower -> metadata
        self.links = {}      # url -> path_lower
        self.n = itertools.count(1)
        self.uploads = []
        self.fail = {}

    def _maybe_fail(self, name):
        e = self.fail.get(name)
        if e:
            raise e

    def add_folder(self, path):
        url = f'https://www.dropbox.com/scl/fo/{abs(hash(path)) % 10**8}/x?dl=0'
        self.links[url] = path.lower()
        self.files[path.lower()] = {'.tag': 'folder', 'name': path.split('/')[-1], 'path_lower': path.lower(),
                                    'id': f'id:DIR{next(self.n)}'}
        return url

    def add_file(self, path, *, content='hash', rev='r1', modified='2026-10-01T10:00:00Z'):
        n = next(self.n)
        meta = {'.tag': 'file', 'name': path.split('/')[-1], 'path_lower': path.lower(), 'id': f'id:F{n}',
                'rev': rev, 'content_hash': content, 'size': 50_000_000, 'client_modified': modified}
        self.files[path.lower()] = meta
        return meta

    def link_metadata(self, url):
        self._maybe_fail('link_metadata')
        base = url.split('?')[0]
        for u, p in self.links.items():
            if u.split('?')[0] == base:
                return copy.deepcopy(self.files[p])
        raise DropboxError('The Dropbox link on the board no longer exists', kind='config',
                           tag='shared_link_not_found/')

    def metadata(self, path):
        self._maybe_fail('metadata')
        for m in self.files.values():
            if m['path_lower'] == str(path).lower() or m.get('id') == path:
                return copy.deepcopy(m)
        raise DropboxError('path/not_found', kind='config', tag='path/not_found/')

    def list_folder(self, path, recursive=True, limit=2000):
        p = path.lower()
        return [copy.deepcopy(m) for k, m in self.files.items()
                if k.startswith(p + '/') and (recursive or '/' not in k[len(p) + 1:])]

    def shared_links(self, path):
        return [{'url': u} for u, p in self.links.items() if p == path.lower()]

    def share(self, path):
        self._maybe_fail('share')
        for u, p in self.links.items():
            if p == path.lower():
                return u
        url = f'https://www.dropbox.com/s/{next(self.n)}/{path.split("/")[-1]}?dl=0'
        self.links[url] = path.lower()
        return url

    def ensure_folder(self, path):
        self.files.setdefault(path.lower(), {'.tag': 'folder', 'name': path.split('/')[-1], 'path_lower': path.lower()})

    def move(self, src, dst):
        m = self.files.pop(src.lower())
        m = {**m, 'path_lower': dst.lower(), 'name': dst.split('/')[-1]}
        self.files[dst.lower()] = m
        return {'metadata': {**m, 'path_display': dst}}

    def delete(self, path):
        return {'metadata': self.files.pop(path.lower())}

    def upload(self, local, path):
        self._maybe_fail('upload')
        meta = {'.tag': 'file', 'name': path.split('/')[-1], 'path_lower': path.lower(), 'id': f'id:U{next(self.n)}',
                'rev': 'u1', 'content_hash': media.dropbox_hash(local), 'size': 1}
        self.files[path.lower()] = meta
        self.uploads.append(path)
        return copy.deepcopy(meta)


class FakeInstagram:
    """Containers finish immediately unless scripted. ``publish_mode``: ok | lost | refused | server_error."""

    def __init__(self):
        self.containers = {}
        self.statuses = {}         # container -> list of status codes to return (last repeats)
        self.publish_mode = 'ok'
        self.published = []        # container ids that really went live
        self.calls = []
        self.n = itertools.count(1)

    def create_container(self, payload):
        self.calls.append(('create', payload['media_type']))
        cid = f'C{next(self.n)}'
        self.containers[cid] = payload
        return cid

    def container_status(self, cid):
        self.calls.append(('status', cid))
        if cid in self.published:
            return {'status_code': 'PUBLISHED'}
        seq = self.statuses.get(cid) or ['FINISHED']
        code = seq.pop(0) if len(seq) > 1 else seq[0]
        return {'status_code': code}

    def publish(self, cid, timeout=120):
        self.calls.append(('publish', cid))
        mode = self.publish_mode
        if mode == 'ok':
            self.published.append(cid)
            return 200, {'id': 'IG' + cid}
        if mode == 'lost':                 # Instagram accepted and published, the answer never arrived
            self.published.append(cid)
            raise HttpError('no answer: timeout', None, None)
        if mode == 'refused':
            return 400, {'error': {'message': 'Invalid parameter', 'code': 100}}
        return 500, {'error': {'message': 'An unknown error occurred', 'code': 1}}

    def media(self, limit=25):
        return ig_media(self)[:limit]

    def permalink(self, media_id):
        return f'https://www.instagram.com/reel/{media_id}/'

    def probe(self):
        return True, None

    def publish_calls(self):
        return [c for c in self.calls if c[0] == 'publish']


class FakeMediaWorker:
    """media.preflight / media.prepare replaced by immediate results: the measured duration, and a prepared file
    written into the test directory with its media row (the real worker runs ffprobe/ffmpeg detached)."""

    def __init__(self, ops, folder, duration=30.0, width=1080, height=1920, size=90_000_000):
        self.ops, self.folder = ops, folder
        self.duration, self.width, self.height, self.size = duration, width, height, size
        self.prepared = []

    def preflight(self, b):
        return {'ready': True, 'duration': self.duration, 'reason': None, 'replaceRequired': False,
                'failureClass': None, 'trimTo': None, 'assetKey': b['assetKey'], 'contentHash': b['contentHash'],
                'checkedAt': 0}

    def prepare(self, b):
        if min(self.width, self.height) < b.get('minShortEdge', 1080):
            return {'ready': False, 'reason': f"Source short edge is below {b.get('minShortEdge', 1080)} pixels",
                    'failureClass': 'verdict', 'assetKey': b['assetKey'], 'format': b['format']}
        mid = media.media_key(b)
        path = self.folder / (mid + '.mp4')
        path.write_bytes(b'prepared ' + mid.encode())
        info = {'width': self.width, 'height': self.height, 'bytes': self.size, 'duration': self.duration,
                'assetKey': b['assetKey'], 'fileId': b['fileId'], 'revision': b['revision'],
                'contentHash': b['contentHash'], 'format': b['format'], 'qaPolicy': rules.QA_POLICY,
                'sha256': 'sha-' + mid[:12], 'dropboxHash': media.dropbox_hash(path), 'cacheKey': media.cache_key(mid)}
        with self.ops.store.tx() as c:
            c.execute('INSERT OR REPLACE INTO media VALUES(?,?,?,?,?)', (mid, str(b['itemId']), str(path), 'src',
                                                                         dumps(info)))
        self.prepared.append(b['itemId'])
        return {'ready': True, 'mediaId': mid, 'filePath': str(path), **info}


def ig_media(ig):
    """Recent media as Instagram lists them (newest first) for the restore-gap check."""
    return [{'id': 'IG' + c, 'permalink': f'https://www.instagram.com/reel/IG{c}/', 'timestamp': '2099-01-01T00:00:00+0000'}
            for c in reversed(ig.published)]
