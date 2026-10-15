"""Dropbox API client: the calls WF1/WF2 made, plus the file operations the owner can ask Bondok for.

Errors are classified exactly like the n8n workflow did (``classify``): ``config`` (permanent for the current
evidence: missing/unshared link, bad path), ``editor`` (content problem), ``infra`` (temporary), so the handler
receives the same facts it received from n8n.
"""
from __future__ import annotations

import json
import re
import time

from .http import Http, HttpError

API = 'https://api.dropboxapi.com/2/'
CONTENT = 'https://content.dropboxapi.com/2/'
TOKEN_URL = 'https://api.dropboxapi.com/oauth2/token'
SINGLE_UPLOAD_MAX = 150 * 1024 * 1024
SESSION_CHUNK = 32 * 1024 * 1024

PERMANENT = re.compile(r'^(path/(not_found|malformed_path|no_write_permission|insufficient_space|conflict|'
                       r'disallowed_name)|path_lookup/(not_found|malformed_path)|shared_link_(not_found|access_denied|'
                       r'is_directory)|unsupported_link_type|email_not_verified)')
FRIENDLY = {
    'shared_link_not_found': 'The Dropbox link on the board no longer exists; put a working link on the item',
    'shared_link_access_denied': 'The connected Dropbox account cannot open this link; share it with the account',
    'path/not_found': 'The Dropbox folder or file was not found; check the link on the item',
    'path/malformed_path': 'The Dropbox path is not valid; check the link on the item',
    'path/no_write_permission': 'The connected Dropbox account cannot write to this folder',
    'path/insufficient_space': 'The Dropbox account is out of space',
}


class DropboxError(Exception):
    def __init__(self, message, *, kind='infra', status=None, tag=None, answered=True):
        super().__init__(message)
        self.kind, self.status, self.tag, self.answered = kind, status, tag, answered


def classify(status, summary, message='') -> tuple[str, str]:
    """(kind, owner text) for a Dropbox failure, the same rules as the n8n DROPBOX_CLASSIFY code."""
    msg = re.sub(r' \[line \d+\]$', '', str(message or summary or ''))
    m = re.match(r'^\[(config|editor)\]\s*(.*)', msg)
    if m:
        return m[1], m[2][:400]
    tag = re.sub(r'[/.]+$', '', str(summary or '')).strip()
    bare = bool(re.fullmatch(r'[a-z_]+(/[a-z_]+)*/?\.*', msg.strip()))
    credential = status in (401, 403) and not tag.startswith('shared_link')
    if not credential and ((status is not None and 400 <= status < 500 and status not in (408, 429)) or
                           (status is None and bare and PERMANENT.match(tag))):
        key = next((k for k in FRIENDLY if tag.startswith(k)), None)
        text = (FRIENDLY[key] + f' (Dropbox: {tag})') if key else f'Dropbox refused the request ({tag or status})'
        return 'config', text[:400]
    code = status if status is not None else (tag or 'no answer')
    return 'infra', f'Temporary Dropbox problem ({code}): {msg[:200]}'[:400]


class Dropbox:
    def __init__(self, *, token=None, refresh_token=None, app_key=None, app_secret=None, http: Http | None = None,
                 clock=time.time):
        self.http = http or Http(timeout=60)
        self._token, self._expires = token, float('inf') if token else 0.0
        self.refresh_token, self.app_key, self.app_secret = refresh_token, app_key, app_secret
        self.clock = clock

    # ------------------------------------------------------------------ auth
    def token(self) -> str:
        if self._token and self.clock() < self._expires - 60:
            return self._token
        if not self.refresh_token:
            # A credential problem is the account's, not the item's: infra (one alert, items wait), as in n8n.
            raise DropboxError('Dropbox credential missing or expired; reconnect Dropbox', kind='infra')
        try:
            r = self.http.request('POST', TOKEN_URL, data=(
                f'grant_type=refresh_token&refresh_token={self.refresh_token}&client_id={self.app_key}'
                f'&client_secret={self.app_secret}').encode(),
                headers={'Content-Type': 'application/x-www-form-urlencoded'})
        except HttpError as e:
            raise DropboxError('Dropbox credential refresh failed (' + str(e.status) + '); reconnect Dropbox',
                               kind='infra', status=e.status, answered=e.answered) from None
        self._token, self._expires = r['access_token'], self.clock() + float(r.get('expires_in') or 14400)
        return self._token

    def _call(self, endpoint, body, *, retries=2, content=False, arg=None, data=None, raw=False, timeout=None):
        url = (CONTENT if content else API) + endpoint
        for attempt in range(retries + 1):
            headers = {'Authorization': 'Bearer ' + self.token()}
            try:
                if content:
                    headers['Dropbox-API-Arg'] = json.dumps(arg or {}, ensure_ascii=True)
                    headers['Content-Type'] = 'application/octet-stream'
                    return self.http.request('POST', url, headers=headers, data=data or b'', raw=raw, timeout=timeout)
                return self.http.request('POST', url, headers=headers, json_body=body, timeout=timeout)
            except HttpError as e:
                j = e.json() or {}
                summary = j.get('error_summary') if isinstance(j, dict) else None
                if e.status == 401 and self.refresh_token and attempt < retries:
                    self._expires = 0                      # expired access token: refresh once and repeat
                    continue
                if (not e.answered or e.status in (429, 500, 502, 503)) and attempt < retries:
                    time.sleep(2)
                    continue
                kind, text = classify(e.status, summary, summary or str(e))
                raise DropboxError(text, kind=kind, status=e.status, tag=summary, answered=e.answered) from None
        raise DropboxError('unreachable')

    # ------------------------------------------------------------------ reads
    def link_metadata(self, url):
        return self._call('sharing/get_shared_link_metadata', {'url': url})

    def metadata(self, path):
        return self._call('files/get_metadata', {'path': path})

    def list_folder(self, path, recursive=True, limit=2000) -> list[dict]:
        r = self._call('files/list_folder', {'path': path, 'recursive': recursive, 'limit': limit})
        entries = list(r.get('entries') or [])
        while r.get('has_more'):
            r = self._call('files/list_folder/continue', {'cursor': r['cursor']})
            entries += r.get('entries') or []
        return entries

    def shared_links(self, path) -> list[dict]:
        return (self._call('sharing/list_shared_links', {'path': path, 'direct_only': True}) or {}).get('links') or []

    # ------------------------------------------------------------------ writes
    def ensure_folder(self, path):
        try:
            return self._call('files/create_folder_v2', {'path': path, 'autorename': False}, retries=0)
        except DropboxError as e:
            if (e.tag or '').startswith('path/conflict/folder'):
                return None                     # already there
            raise

    def share(self, path) -> str:
        """The public link of a path: created, or the existing one."""
        try:
            r = self._call('sharing/create_shared_link_with_settings',
                           {'path': path, 'settings': {'requested_visibility': 'public'}}, retries=0)
            if r and r.get('url'):
                return r['url']
        except DropboxError as e:
            if not (e.tag or '').startswith('shared_link_already_exists'):
                raise
        links = self.shared_links(path)
        if not links:
            raise DropboxError('Could not get a share link', kind='infra')
        return links[0]['url']

    def upload(self, local_path, path) -> dict:
        """Upload a file (overwrite). Single request up to 150 MiB, upload session above. Returns file metadata."""
        import os
        size = os.path.getsize(local_path)
        arg = {'path': path, 'mode': 'overwrite', 'autorename': False, 'mute': True}
        with open(local_path, 'rb') as f:
            if size <= SINGLE_UPLOAD_MAX:
                return self._call('files/upload', None, content=True, arg=arg, data=f.read(), retries=0,
                                  timeout=300)
            first = f.read(SESSION_CHUNK)
            sid = self._call('upload_session/start', None, content=True, arg={'close': False}, data=first,
                             retries=0, timeout=300)['session_id']
            offset = len(first)
            while True:
                chunk = f.read(SESSION_CHUNK)
                if offset + len(chunk) >= size:
                    return self._call('upload_session/finish', None, content=True,
                                      arg={'cursor': {'session_id': sid, 'offset': offset}, 'commit': arg},
                                      data=chunk, retries=0, timeout=300)
                self._call('upload_session/append_v2', None, content=True,
                           arg={'cursor': {'session_id': sid, 'offset': offset}, 'close': False}, data=chunk,
                           retries=0, timeout=300)
                offset += len(chunk)

    def move(self, src, dst):
        return self._call('files/move_v2', {'from_path': src, 'to_path': dst, 'autorename': False}, retries=0)

    def delete(self, path):
        """Moves to Dropbox's deleted files (recoverable there); never a permanent delete."""
        return self._call('files/delete_v2', {'path': path}, retries=0)


def raw_link(url: str) -> str:
    """The delivery form the publisher hands Instagram: dl/raw parameters replaced by raw=1."""
    u = re.sub(r'[?&](dl|raw)=\d+', '', url)
    u = u.replace('?&', '?')
    return u + ('&' if '?' in u else '?') + 'raw=1'


def strip_raw(url: str) -> str:
    return re.sub(r'([?&])raw=1(&|$)', lambda m: m[1] if m[2] else '', url).rstrip('?&')
