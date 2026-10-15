"""Minimal JSON-over-HTTPS client (urllib) with explicit timeouts and a transport seam for tests.

A transport is ``callable(method, url, headers, body: bytes | None, timeout) -> (status, headers, body: bytes)``
and raises ``OSError`` (or ``TimeoutError``) when no answer arrived. Callers must treat "no answer" as unknown.
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from urllib.parse import urlencode


class HttpError(Exception):
    """An HTTP answer with a non-2xx status (``status`` set) or no answer at all (``status`` None)."""

    def __init__(self, message, status=None, body=None):
        super().__init__(message)
        self.status, self.body = status, body

    @property
    def answered(self) -> bool:
        return self.status is not None

    def json(self):
        try:
            return json.loads(self.body) if self.body else None
        except (TypeError, ValueError):
            return None


def urllib_transport(method, url, headers, body, timeout):
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:     # noqa: S310 - fixed https hosts only
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()
    except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
        raise OSError(f'{type(e).__name__}: {getattr(e, "reason", e)}') from None


class Http:
    def __init__(self, transport=None, timeout=60):
        self.transport = transport or urllib_transport
        self.timeout = timeout

    def request(self, method, url, *, headers=None, json_body=None, data=None, params=None, timeout=None,
                raw=False):
        """JSON response (or bytes with ``raw``). Raises HttpError for non-2xx and for no answer."""
        if params:
            url += ('&' if '?' in url else '?') + urlencode(params)
        h = dict(headers or {})
        body = data
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode()
            h.setdefault('Content-Type', 'application/json')
        try:
            status, _, out = self.transport(method, url, h, body, timeout or self.timeout)
        except (OSError, TimeoutError) as e:
            raise HttpError(f'no answer: {e}', None, None) from None
        if not 200 <= status < 300:
            text = out.decode('utf-8', 'replace') if isinstance(out, (bytes, bytearray)) else str(out)
            raise HttpError(f'HTTP {status}: {text[:500]}', status, text)
        if raw:
            return out
        if not out:
            return None
        try:
            return json.loads(out)
        except ValueError:
            raise HttpError('response is not JSON', status, out[:500]) from None
