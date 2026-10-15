"""Bondok's telephone bridge on the PBX host: Sara's proven ElevenLabs AudioSocket bridge, second instance.

Reused unchanged from voice-monday-mvp (``voice_agent.telephony.el_bridge``): AudioSocket framing, resampling,
ringback, the 20 ms pacer, barge-in (``interruption`` clears playback), ping/pong, signed-URL prefetch, hangup.
Changed here only:

* identity: the dialplan puts the *authenticated PJSIP endpoint* (``${CHANNEL(endpoint)}``, digits only) in the
  last 12 digits of the AudioSocket UUID; caller ID is never used. The bridge registers the call with Bondok over
  Tailscale (HMAC-signed, with a timestamp) and receives a session token and the greeting for that line's role;
* the token rides to Bondok in ``custom_llm_extra_body`` (the model never sees or chooses it);
* no Sara features (typing sound, outbound calls, pranks, Redis); its own port, agent id and env file;
* a disable switch: ``/etc/bondok-voice/switch/disabled`` present (or ``BONDOK_VOICE_ENABLED=0``) refuses calls at once,
  and the dialplan plays the busy tone.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import time
import uuid

import httpx
from voice_agent.logging_setup import configure_logging, get_logger
from voice_agent.telephony import el_bridge
from voice_agent.telephony.el_bridge import ElevenLabsBridge, caller_from_uuid

log = get_logger('bondok_bridge')
DISABLED_FLAG = '/etc/bondok-voice/switch/disabled'
WORDS = ('el_bridge.heard', 'el_bridge.said')     # Sara's bridge logs what was said: never for Bondok's calls


class _NoWords:
    """Sara's module logger, without the two events that carry the caller's and the agent's words."""

    def __init__(self, inner):
        self._inner = inner

    def info(self, event, *a, **kw):
        if event not in WORDS:
            self._inner.info(event, *a, **kw)

    def __getattr__(self, name):
        return getattr(self._inner, name)


el_bridge.log = _NoWords(el_bridge.log)


class BondokBridge(ElevenLabsBridge):
    def __init__(self, *, bondok_url: str, session_secret: str, **kw):
        super().__init__(**kw)
        self.bondok_url = bondok_url.rstrip('/')
        self.session_secret = session_secret
        self._bondok = httpx.AsyncClient(timeout=5)
        self._calls: dict[asyncio.Task, str] = {}

    def enabled(self) -> bool:
        return os.environ.get('BONDOK_VOICE_ENABLED', '1') == '1' and not os.path.exists(DISABLED_FLAG)

    async def _signed(self, path: str, body: dict) -> dict:
        raw = json.dumps({**body, 'ts': time.time()}).encode()
        sig = hmac.new(self.session_secret.encode(), raw, hashlib.sha256).hexdigest()
        r = await self._bondok.post(self.bondok_url + path, content=raw,
                                    headers={'X-Bondok-Signature': sig, 'Content-Type': 'application/json'})
        r.raise_for_status()
        return r.json()

    async def open_agent(self, caller, call_uuid=None):
        from websockets.asyncio.client import connect
        if not self.enabled():
            raise RuntimeError('bondok voice disabled')
        endpoint = caller_from_uuid(call_uuid) if call_uuid else None
        if not endpoint:
            raise RuntimeError('no authenticated endpoint on this call')
        call_id = uuid.UUID(bytes=call_uuid).hex[:24] + '-' + uuid.uuid4().hex[:8]
        t0 = time.monotonic()
        session, url = await asyncio.gather(
            self._signed('/session', {'endpoint': endpoint, 'call_id': call_id,
                                      'verified_by': 'dialplan:CHANNEL(endpoint)'}),
            self.signed_url())
        if not session.get('ok'):
            raise RuntimeError('bondok refused the session')
        task = asyncio.current_task()
        if task:
            self._calls[task] = call_id
        t1 = time.monotonic()
        ws = await connect(url, max_size=2**22)
        log.info('bondok_bridge.opening', endpoint=endpoint, role=session.get('role'),
                 session_ms=round((t1 - t0) * 1000), connect_ms=round((time.monotonic() - t1) * 1000))
        await ws.send(json.dumps({
            'type': 'conversation_initiation_client_data',
            'dynamic_variables': {'opening': session.get('greeting') or 'أهلا، معاك بندق.'},
            'custom_llm_extra_body': {'session': session['token']},
        }))
        return ws

    async def handle(self, reader, writer):
        try:
            await super().handle(reader, writer)
        finally:
            call_id = self._calls.pop(asyncio.current_task(), None)
            if call_id:
                with contextlib.suppress(Exception):
                    await self._signed('/session/end', {'call_id': call_id})


async def main():
    from voice_agent.cli import _install_stop_handlers
    env = os.environ
    configure_logging(env.get('LOG_LEVEL', 'INFO'), env.get('LOG_FORMAT', 'json'))
    bridge = BondokBridge(
        api_key=env['ELEVENLABS_API_KEY'], agent_id=env['BONDOK_ELEVENLABS_AGENT_ID'],
        base_url=env.get('ELEVENLABS_BASE_URL', 'https://api.elevenlabs.io'),
        host=env.get('BONDOK_BRIDGE_HOST', '127.0.0.1'), port=int(env.get('BONDOK_BRIDGE_PORT', '9020')),
        typing_volume=0, bondok_url=env['BONDOK_SESSION_URL'], session_secret=env['VOICE_SESSION_SECRET'])
    stop = asyncio.Event()
    _install_stop_handlers(stop)
    await bridge.serve(stop)


if __name__ == '__main__':
    asyncio.run(main())
