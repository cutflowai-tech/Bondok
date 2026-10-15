"""Instagram Graph API client (content publishing), the calls WF2 made.

Only documented operations are offered (IG User Media / Container / media_publish / content_publishing_limit,
read 2026-10-14). Deleting or editing a published post is not offered: the content-publishing documentation does
not provide it, and ``unsupported()`` says so instead of inventing an endpoint (acceptance A32).
"""
from __future__ import annotations

import json

from .http import Http, HttpError

GRAPH = 'https://graph.facebook.com/'


class Instagram:
    def __init__(self, token, account, http: Http | None = None, version='v26.0', base=GRAPH):
        self.http = http or Http(timeout=120)
        self.token, self.account, self.base = token, str(account), base + version + '/'
        if not self.account or self.account.startswith('<'):
            raise ValueError('Instagram account id is not configured')

    def _get(self, path, fields, retries=2):
        last = None
        for _ in range(retries + 1):
            try:
                return self.http.request('GET', self.base + path, params={'fields': fields},
                                         headers=self._auth())
            except HttpError as e:
                last = e
                if e.answered and e.status < 500:
                    break
        raise last

    def _post(self, path, body, timeout=None):
        return self.http.request('POST', self.base + path, json_body=body, headers=self._auth(), timeout=timeout)

    def _auth(self):
        token = self.token.token() if hasattr(self.token, 'token') else self.token
        return {'Authorization': 'Bearer ' + token}     # never in a URL (logs)

    # ------------------------------------------------------------------ publishing
    def create_container(self, payload) -> str:
        """POST /{ig}/media with exactly the payload the handler bound (REELS: caption, share_to_feed)."""
        body = {'media_type': payload['media_type'], 'video_url': payload['video_url']}
        if payload['media_type'] == 'REELS':
            body['caption'] = payload.get('caption') or ''
            body['share_to_feed'] = payload.get('share_to_feed') is True
            if payload.get('collaborators'):
                body['collaborators'] = list(payload['collaborators'])[:3]
        r = self._post(self.account + '/media', body)
        if not r or not r.get('id'):
            raise HttpError('No Instagram container ID; attempt left pre-commit', 200, json.dumps(r))
        return str(r['id'])

    def container_status(self, container_id) -> dict:
        return self._get(str(container_id), 'status_code,status') or {}

    def publish(self, container_id, timeout=120):
        """(http_status, body) of media_publish, never raising for an answered error: the handler decides what an
        answer means. No answer raises HttpError (answered False): the outcome is unknown."""
        try:
            return 200, self._post(self.account + '/media_publish', {'creation_id': str(container_id)},
                                   timeout=timeout)
        except HttpError as e:
            if not e.answered:
                raise
            return e.status, (e.json() or {'error': {'message': str(e.body)[:500]}})

    def permalink(self, media_id) -> str | None:
        try:
            return (self._get(str(media_id), 'permalink') or {}).get('permalink')
        except HttpError:
            return None

    def probe(self):
        """(ok, error) of a cheap authenticated read of the account."""
        try:
            r = self._get(self.account, 'id')
            return bool(r and r.get('id')), None
        except HttpError as e:
            return False, (json.dumps(e.json())[:600] if e.json() else str(e)[:600])

    def publishing_limit(self) -> dict:
        r = self._get(self.account + '/content_publishing_limit', 'config,quota_usage')
        data = (r or {}).get('data') or [{}]
        return data[0]

    def media(self, limit=25, since=None) -> list[dict]:
        """Recent feed posts, newest first; with ``since`` (epoch s) every page back to that moment."""
        out, path, params = [], self.account + '/media', {'fields': 'id,caption,media_type,permalink,timestamp',
                                                          'limit': 50}
        while path:
            r = self._page(path, params)
            data = (r or {}).get('data') or []
            out += data
            oldest = data[-1].get('timestamp') if data else None
            if since is None and len(out) >= limit:
                break
            if since is not None and (not oldest or _ts(oldest) < float(since)):
                break
            nxt = ((r or {}).get('paging') or {}).get('next')
            path, params = (nxt, None) if nxt else (None, None)
        return out if since is not None else out[:limit]

    def stories(self) -> list[dict]:
        """Stories of the last 24 hours (Instagram lists no older ones)."""
        r = self._get(self.account + '/stories', 'id,media_type,permalink,timestamp')
        return (r or {}).get('data') or []

    def _page(self, path, params):
        url = path if str(path).startswith('http') else self.base + path
        return self.http.request('GET', url, params=params, headers=self._auth())

    @staticmethod
    def unsupported(operation) -> dict:
        return {'state': 'unsupported', 'operation': operation,
                'reason': f'Instagram\'s publishing API does not offer "{operation}" for this account; do it in the '
                          'Instagram app. Nothing was changed.'}


def _ts(stamp) -> float:
    from datetime import datetime
    try:
        return datetime.strptime(str(stamp).replace('+0000', '+00:00'), '%Y-%m-%dT%H:%M:%S%z').timestamp()
    except ValueError:
        return 0.0


def error_text(e: HttpError) -> str:
    j = e.json()
    return json.dumps(j.get('error', j))[:600] if isinstance(j, dict) else str(e)[:600]


class TokenStore:
    """Long-lived Instagram Login tokens last 60 days and can be refreshed once they are at least 24 hours old
    (GET graph.instagram.com/refresh_access_token, grant_type=ig_refresh_token). A Facebook Page token (Facebook
    Login) does not use this. The refreshed token is kept in a 0600 file next to the runtime env, never logged."""

    REFRESH_AFTER = 30 * 86400

    def __init__(self, path, initial, http: Http | None = None, clock=None):
        import time as _t
        self.path, self.http, self.clock = path, http or Http(timeout=30), clock or _t.time
        self.state = self._load() or {'token': initial, 'refreshed': self.clock()}

    def _load(self):
        import os
        try:
            with open(self.path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None
        finally:
            if os.path.exists(self.path):
                os.chmod(self.path, 0o600)

    def token(self) -> str:
        if self.clock() - float(self.state.get('refreshed') or 0) >= self.REFRESH_AFTER:
            self.refresh()
        return self.state['token']

    def refresh(self):
        import os
        try:
            r = self.http.request('GET', 'https://graph.instagram.com/refresh_access_token',
                                  params={'grant_type': 'ig_refresh_token'},
                                  headers={'Authorization': 'Bearer ' + self.state['token']})
        except HttpError:
            r = None                # the current token still works until it expires; auth errors open the breaker
        if not r or not r.get('access_token'):
            self.state['refreshed'] = self.clock() - self.REFRESH_AFTER + 3600     # try again in an hour
            return False
        self.state = {'token': r['access_token'], 'refreshed': self.clock()}
        tmp = str(self.path) + '.tmp'
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(self.state, f)
        os.replace(tmp, self.path)
        return True
