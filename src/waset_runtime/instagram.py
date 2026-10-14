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
        return {'Authorization': 'Bearer ' + self.token}     # never in a URL (logs)

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

    def media(self, limit=25) -> list[dict]:
        r = self._get(self.account + '/media', 'id,caption,media_type,permalink,timestamp')
        return ((r or {}).get('data') or [])[:limit]

    @staticmethod
    def unsupported(operation) -> dict:
        return {'state': 'unsupported', 'operation': operation,
                'reason': f'Instagram\'s publishing API does not offer "{operation}" for this account; do it in the '
                          'Instagram app. Nothing was changed.'}


def error_text(e: HttpError) -> str:
    j = e.json()
    return json.dumps(j.get('error', j))[:600] if isinstance(j, dict) else str(e)[:600]
