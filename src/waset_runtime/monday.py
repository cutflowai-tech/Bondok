"""Monday GraphQL client for the boards Bondok works on (social, source projects, editor subitems).

Scope is enforced here, not in prompts: item writes are refused unless the item is verified to belong to an allowed
board. Errors come back as ``MondayError`` with ``answered`` telling a refusal from "no answer" (unknown effect).
"""
from __future__ import annotations

import json
import time

from waset_ops import board as B
from waset_ops import rules

from .http import Http, HttpError

API = 'https://api.monday.com/v2'
SOURCE_COLS = ['text_mm066x8y', 'link_mm06bswn', 'color_mm1ryfcb', 'project_owner']
SOURCE_STATUS = 'color_mm1ryfcb'
ALLOWED_BOARDS = {rules.BOARD, rules.SOURCE_BOARD, rules.EDITOR_SUBITEM_BOARD}
PAGE = 500


class MondayError(Exception):
    def __init__(self, message, *, answered=True, status=None, errors=None):
        super().__init__(message)
        self.answered, self.status, self.errors = answered, status, errors


class Monday:
    def __init__(self, token, http: Http | None = None, api_version='2026-07', sleep=time.sleep, boards=None):
        self.http = http or Http(timeout=60)
        self.boards = boards        # callable -> the owner's allowed_boards rule (default: the three project boards)
        self.headers = {'Authorization': token, 'API-Version': api_version}
        self.sleep = sleep

    # ------------------------------------------------------------------ transport
    def query(self, q, variables=None, *, retries=2):
        """One GraphQL call. Rate/complexity limits and 5xx are retried with a short backoff (reads and idempotent
        writes only: callers pass retries=0 for creates)."""
        for attempt in range(retries + 1):
            try:
                data = self.http.request('POST', API, headers=self.headers,
                                         json_body={'query': q, 'variables': variables or {}})
            except HttpError as e:
                retry = (not e.answered) or e.status in (429, 500, 502, 503, 504)
                if retry and attempt < retries:
                    self.sleep(2 * (attempt + 1))
                    continue
                raise MondayError(str(e), answered=e.answered, status=e.status) from None
            errs = (data or {}).get('errors')
            if errs:
                text = json.dumps(errs, ensure_ascii=False)[:800]
                limited = any('complexity' in str(x).lower() or 'rate' in str(x).lower() for x in errs)
                if limited and attempt < retries:
                    self.sleep(5 * (attempt + 1))
                    continue
                raise MondayError('Monday rejected the request: ' + text, errors=errs)
            if not isinstance(data, dict) or not isinstance(data.get('data'), dict):
                raise MondayError('Monday answered without data (no response from Monday)')
            return data['data']
        raise MondayError('unreachable')

    # ------------------------------------------------------------------ reads
    def board_items(self, board_id, columns) -> list[dict]:
        """Every item of a board (paginated). A missing page is an error, never a partial snapshot."""
        cols = json.dumps(columns)
        fields = f'cursor items{{id name group{{id}} column_values(ids:{cols}){{id text value}}}}'
        d = self.query(f'query($b:[ID!]!){{boards(ids:$b){{items_page(limit:{PAGE}){{{fields}}}}}}}',
                       {'b': [str(board_id)]})
        boards = d.get('boards') or []
        if not boards or boards[0].get('items_page') is None:
            raise MondayError('Missing board page; refusing a partial snapshot')
        page = boards[0]['items_page']
        items = list(page.get('items') or [])
        cursor = page.get('cursor')
        while cursor:
            d = self.query(f'query($c:String!){{next_items_page(cursor:$c,limit:{PAGE}){{{fields}}}}}', {'c': cursor})
            page = d.get('next_items_page')
            if page is None:
                raise MondayError('Missing board page; refusing a partial snapshot')
            items += page.get('items') or []
            cursor = page.get('cursor')
        return items

    def social_snapshot(self) -> list[dict]:
        return self.board_items(rules.BOARD, B.SNAPSHOT_COLUMNS)

    def source_snapshot(self) -> list[dict]:
        return self.board_items(rules.SOURCE_BOARD, SOURCE_COLS)

    def items(self, ids, columns=None) -> list[dict]:
        cols = '' if columns is None else f'(ids:{json.dumps(columns)})'
        d = self.query('query($ids:[ID!]){items(ids:$ids){id name group{id} board{id} column_values' + cols +
                       '{id text value}}}', {'ids': [str(i) for i in ids]})
        return d.get('items') or []

    def fresh_item(self, item_id) -> dict:
        """The social item as it is now; refuses an item that is not on the social board."""
        rows = self.items([item_id], B.SNAPSHOT_COLUMNS)
        if len(rows) != 1:
            raise MondayError('item not found on the board')
        it = rows[0]
        if str((it.get('board') or {}).get('id')) != rules.BOARD:
            raise MondayError('Item is not on the social board')
        return it

    def source_status(self, source_item_id) -> str | None:
        if not source_item_id:
            return None
        rows = self.items([source_item_id], [SOURCE_STATUS])
        if not rows:
            return None
        return next((c.get('text') for c in rows[0].get('column_values') or [] if c['id'] == SOURCE_STATUS), None)

    def guard_values(self, item_id, column_ids) -> dict | None:
        """Current values of the guarded columns, or None when the item is gone."""
        d = self.query('query($i:[ID!],$c:[String!]){items(ids:$i){column_values(ids:$c){id text value}}}',
                       {'i': [str(item_id)], 'c': list(column_ids)})
        rows = d.get('items') or []
        if not rows:
            return None
        return {c['id']: c for c in rows[0].get('column_values') or []}

    def updates(self, item_id, limit=100) -> list[dict]:
        d = self.query('query($ids:[ID!]){items(ids:$ids){id name updates(limit:' + str(int(limit)) +
                       '){text_body created_at}}}', {'ids': [str(item_id)]})
        rows = d.get('items') or []
        return rows[0] if rows else {}

    def subitems(self, parent_id) -> list[dict]:
        d = self.query('query($id:[ID!]){items(ids:$id){id subitems{id name updates(limit:25){text_body}}}}',
                       {'id': [str(parent_id)]})
        rows = d.get('items') or []
        return (rows[0].get('subitems') or []) if rows else []

    def board_of(self, item_id) -> str | None:
        d = self.query('query($i:[ID!]){items(ids:$i){id board{id}}}', {'i': [str(item_id)]})
        rows = d.get('items') or []
        return str(rows[0]['board']['id']) if rows and rows[0].get('board') else None

    # ------------------------------------------------------------------ writes
    def _scope(self, board_id):
        allowed = set(map(str, self.boards())) if self.boards else ALLOWED_BOARDS
        if str(board_id) not in allowed:
            raise MondayError(f'Board {board_id} is outside the boards Bondok may write (owner rule allowed_boards); '
                              'nothing was written')

    def change_columns(self, item_id, columns: dict, group=None, board_id=rules.BOARD):
        self._scope(board_id)
        args = '$b:ID!,$i:ID!,$v:JSON!' + (',$g:String!' if group else '')
        q = (f'mutation({args}){{change_multiple_column_values(board_id:$b,item_id:$i,column_values:$v,'
             'create_labels_if_missing:false){id}' + (' move_item_to_group(item_id:$i,group_id:$g){id}' if group
                                                      else '') + '}')
        v = {'b': str(board_id), 'i': str(item_id), 'v': json.dumps(columns, ensure_ascii=False)}
        if group:
            v['g'] = group
        return self.query(q, v, retries=1)

    def change_column(self, board_id, item_id, column_id, value):
        self._scope(board_id)
        return self.query('mutation($b:ID!,$i:ID!,$v:JSON!){change_column_value(board_id:$b,item_id:$i,column_id:"'
                          + column_id + '",value:$v){id}}',
                          {'b': str(board_id), 'i': str(item_id), 'v': json.dumps(value, ensure_ascii=False)},
                          retries=1)

    def create_items(self, creates: list[dict], board_id=rules.BOARD) -> dict:
        """One aliased mutation for up to 20 creates; returns {alias_key: id | None} plus the raw error if any.
        Not retried: a lost answer is an uncertain create, reported as such."""
        self._scope(board_id)
        sig, body, v = ['$b:ID!'], [], {'b': str(board_id)}
        for j, cr in enumerate(creates):
            sig += [f'$v{j}:JSON!', f'$n{j}:String!', f'$g{j}:String!']
            body.append(f's{cr["key"]}:create_item(board_id:$b,group_id:$g{j},item_name:$n{j},column_values:$v{j}){{id}}')
            v.update({f'v{j}': json.dumps(cr['columns'], ensure_ascii=False), f'n{j}': cr['name'], f'g{j}': cr['group']})
        q = 'mutation(' + ','.join(sig) + '){' + ' '.join(body) + '}'
        return self.http.request('POST', API, headers=self.headers, json_body={'query': q, 'variables': v})

    def create_item(self, name, columns=None, group=None, board_id=rules.BOARD) -> str:
        self._scope(board_id)
        v = {'b': str(board_id), 'n': name, 'v': json.dumps(columns or {}, ensure_ascii=False)}
        sig, grp = '$b:ID!,$n:String!,$v:JSON!', ''
        if group:
            sig, grp, v['g'] = sig + ',$g:String!', ',group_id:$g', group
        d = self.query(f'mutation({sig}){{create_item(board_id:$b,item_name:$n,column_values:$v{grp}){{id}}}}', v,
                       retries=0)
        return d['create_item']['id']

    def create_subitem(self, parent_id, name, columns) -> str:
        d = self.query('mutation($p:ID!,$n:String!,$v:JSON!){create_subitem(parent_item_id:$p,item_name:$n,'
                       'column_values:$v){id}}', {'p': str(parent_id), 'n': name,
                                                  'v': json.dumps(columns, ensure_ascii=False)}, retries=0)
        return d['create_subitem']['id']

    def create_update(self, item_id, body) -> str:
        d = self.query('mutation($i:ID!,$b:String!){create_update(item_id:$i,body:$b){id}}',
                       {'i': str(item_id), 'b': body}, retries=0)
        return d['create_update']['id']

    def move_to_group(self, item_id, group_id):
        return self.query('mutation($i:ID!,$g:String!){move_item_to_group(item_id:$i,group_id:$g){id}}',
                          {'i': str(item_id), 'g': group_id}, retries=1)

    def archive_item(self, item_id):
        return self.query('mutation($i:ID!){archive_item(item_id:$i){id}}', {'i': str(item_id)}, retries=1)

    def delete_item(self, item_id):
        return self.query('mutation($i:ID!){delete_item(item_id:$i){id}}', {'i': str(item_id)}, retries=0)

    def rename_item(self, item_id, name, board_id=rules.BOARD):
        return self.change_columns(item_id, {'name': name}, board_id=board_id)
