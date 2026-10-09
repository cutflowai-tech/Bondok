"""Read-only Monday client for Bondok, limited to board 5105608159.

Membership is verified with a metadata-only query before any item content is
read. Bondok performs no Monday writes; display changes flow through the
handler's synchronization outbox (applied by WF2).
"""
from __future__ import annotations

import re


BOARD = '5105608159'


class MondayReader:
    def __init__(self, token):
        import httpx
        self.http = httpx.Client(timeout=35, follow_redirects=False,
                                 headers={'Authorization': token, 'API-Version': '2026-07', 'Content-Type': 'application/json'})

    def query(self, q, v=None):
        r = self.http.post('https://api.monday.com/v2', json={'query': q, 'variables': v or {}})
        if r.status_code != 200:
            raise RuntimeError(f'Monday HTTP {r.status_code}')
        data = r.json()
        if data.get('errors') or 'data' not in data:
            raise RuntimeError('Monday rejected the query')
        return data['data']

    def item(self, item_id):
        item_id = str(item_id)
        if not re.fullmatch(r'\d{1,20}', item_id):
            raise ValueError('invalid item id')
        meta = self.query('query($i:[ID!]){items(ids:$i){id board{id}}}', {'i': [item_id]})['items']
        if len(meta) != 1 or meta[0]['board']['id'] != BOARD:
            raise PermissionError('Item is not on the For Social Media board')
        row = self.query('query($i:[ID!]){items(ids:$i){id name board{id} column_values{id text}}}', {'i': [item_id]})['items'][0]
        if row['board']['id'] != BOARD:
            raise PermissionError('Item moved off the social board')
        return row
