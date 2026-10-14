"""Preparation steps shared by every runner (n8n helper and the Bondok runtime): one implementation.

Network and media work happen between handler transactions, never inside one.
"""
from __future__ import annotations

import json
import time

from . import media
from .core import Command


def prep_step(o, b, actor='service:wf1'):
    """Per-item preparation step: prep_source, then the Story duration preflight and/or media preparation the
    handler asks for, each result submitted as a command with a stable id derived from ``b['requestId']``."""
    base = {'run_id': b['runId'], 'fence': b.get('fence'), 'expected_format': b.get('expectedFormat')}
    args = {**base, 'file': b.get('file') or {}, 'url': b.get('url'), 'folder_url': b.get('folderUrl'),
            'error': b.get('error'), 'error_kind': b.get('errorKind'), 'selection': b.get('selection')}
    rid = str(b['requestId'])
    r = o.submit(Command(rid + ':source', 'prep_source', actor, actor, b['itemId'], args))
    for step in range(3):
        if r.get('state') != 'completed' or r.get('next') not in ('preflight', 'prepare'):
            return r
        body = r['media']
        if body.get('recheck'):
            with o.store.read() as c:
                chk = c.execute("SELECT requested FROM ops_checks WHERE item_id=? AND kind='media'",
                                (str(b['itemId']),)).fetchone()
            body['recheckRequestedAt'] = chk['requested'] if chk else time.time()
        if r['next'] == 'preflight':
            res = media.preflight(body)
            r = o.submit(Command(f'{rid}:preflight:{step}', 'prep_preflight', actor, actor,
                                 b['itemId'], {**base, 'result': res}))
            if r.get('next') == 'continue':
                r = o.submit(Command(f'{rid}:source:{step}', 'prep_source', actor, actor, b['itemId'], args))
            continue
        res = media.prepare(body)
        return o.submit(Command(rid + ':media', 'prep_media', actor, actor, b['itemId'], {**base, 'result': res}))
    return r


def delivered_args(o, b):
    """Delivery stage report: an upload/identification with its Dropbox metadata, the registered share link, or a
    failure at one stage (R5 M9/M17). The local file's Dropbox content hash is computed here, outside any handler
    transaction, when the prepared file's record does not carry it yet."""
    args = {'run_id': b['runId'], 'fence': b.get('fence'), 'mediaId': b['mediaId'], 'url': b.get('url'),
            'stage': b.get('stage'), 'error': b.get('error'), 'error_kind': b.get('errorKind'),
            'upload': b.get('upload')}
    if b.get('upload') and b.get('stage') in ('uploaded', 'identified'):
        with o.store.read() as c:
            m = o.media_row(c, b['mediaId'])
        info = json.loads(m['metadata'] or '{}') if m else {}
        if m and not info.get('dropboxHash'):
            local = o.local_media(m)
            args['local_hash'] = media.dropbox_hash(local) if local else None
    return {k: v for k, v in args.items() if v is not None}
