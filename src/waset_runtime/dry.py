"""Shadow mode (P5-05): real reads, no external writes. Intended writes are logged and answered with neutral
placeholders so the cycles can be compared with what n8n does, against a copy of the store."""
from __future__ import annotations

import json
import logging
import secrets

LOG = logging.getLogger('waset_runtime.dry')
WRITES = {
    'Monday': ('change_columns', 'change_column', 'create_items', 'create_item', 'create_subitem', 'create_update',
               'move_to_group', 'archive_item', 'delete_item', 'rename_item'),
    'Dropbox': ('ensure_folder', 'share', 'upload', 'move', 'delete'),
    'Instagram': ('create_container', 'publish'),
}


class Dry:
    def __init__(self, inner):
        self._inner = inner
        self._writes = WRITES.get(type(inner).__name__, ())
        self.log = []

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if name not in self._writes:
            return attr

        def fake(*args, **kw):
            self.log.append((name, args, kw))
            LOG.info('dry_write %s.%s %s', type(self._inner).__name__, name,
                     json.dumps([str(a)[:120] for a in args], ensure_ascii=False))
            if name == 'share':
                links = self._inner.shared_links(args[0])
                return links[0]['url'] if links else 'https://www.dropbox.com/s/dry-run/' + secrets.token_hex(4)
            if name == 'create_items':
                return {'data': {}}             # nothing created: recorded as uncertain in the copy
            if name == 'create_container':
                return 'dry-' + secrets.token_hex(6)
            if name == 'publish':
                return 0, {'error': {'message': 'dry run: not sent'}}
            if name in ('create_subitem', 'create_item', 'create_update'):
                return 'dry-' + secrets.token_hex(4)
            if name == 'upload':
                return {}
            return {}
        return fake


def dry(inner):
    return Dry(inner)
