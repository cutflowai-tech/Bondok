"""Bondok's telephone bridge on the PBX host: Sara's proven ElevenLabs AudioSocket bridge, second instance.

Reused unchanged from voice-monday-mvp (``voice_agent.telephony.el_bridge``): AudioSocket framing, resampling,
ringback, the 20 ms pacer, barge-in (``interruption`` clears playback), ping/pong, signed-URL prefetch, hangup.
Changed here only:

* identity: the dialplan puts the *authenticated PJSIP endpoint* (``${CHANNEL(endpoint)}``, digits only) in the
  last 12 digits of the AudioSocket UUID; caller ID is never used. The bridge registers the call with Bondok over
  Tailscale (HMAC-signed, with a timestamp) and receives a session token and the greeting for that line's role;
* the token rides to Bondok in ``custom_llm_extra_body`` (the model never sees or chooses it);
* no Sara features (typing sound, outbound calls, pranks, Redis); its own port, agent id and env file;
* caller speech is reported to Bondok (signed, throttled: >= 200 ms of speech within 600 ms) so a change is not
  committed while the caller is still talking (Bondok's commit gate);
* a usage budget (``Budget``): measured call seconds against the approved call time (enforced per call), billed
  credits (and fixture TTS) against a credit ceiling (checked between calls); no call starts when either is spent
  or when the account's remaining credits would fall below the reserve kept for Sara;
* a disable switch: ``/etc/bondok-voice/switch/disabled`` present (or ``BONDOK_VOICE_ENABLED=0``) refuses calls at once,
  and the dialplan plays the busy tone.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import hashlib
import hmac
import json
import os
import sys
import time
import uuid

import httpx
from voice_agent.logging_setup import configure_logging, get_logger
from voice_agent.telephony import el_bridge
from voice_agent.telephony.el_bridge import KIND_HANGUP, BridgeCall, ElevenLabsBridge, caller_from_uuid, frame

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


EST_CREDITS_PER_MINUTE = 400    # estimate only (one measured Sara call: 171 credits / 0.43 min); billed values win
MAX_CALL_SECONDS = 600
MIN_CALL_SECONDS = 20           # less than this left: the call is not started


class Budget:
    """Bondok's own ElevenLabs usage, in a durable ledger, with two separate limits and separate measurements:

    * call time: measured seconds per call (bridge clock, from the call's start to its end) against the approved
      call-time budget; enforced exactly: each call is granted only what remains and is cut off there;
    * credits: what ElevenLabs actually billed (per conversation, when it reports it), plus other billed usage
      (fixture TTS, from the account counter), against a credit ceiling. A call whose bill is not (yet) known counts
      at an *estimate* until it is; the credit limit is checked between calls, so a single call can overshoot it
      by its own cost: it is not a precise spending cap.
    """

    def __init__(self, path, total_seconds, total_credits, other_credits=0, other_note=''):
        self.path, self.total, self.total_credits = path, float(total_seconds), float(total_credits)
        self.active: dict[str, float] = {}
        if not os.path.exists(path):
            self._write({'calls': [], 'other': [{'what': other_note, 'credits': int(other_credits)}]
                         if other_credits else []})

    def _read(self) -> dict:
        with open(self.path) as f:
            d = json.load(f)
        d.setdefault('other', [])
        return d

    def _write(self, data):
        tmp = self.path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def seconds_used(self) -> float:
        return sum(float(c.get('seconds') or 0) for c in self._read()['calls'])

    def credits(self) -> dict:
        d = self._read()
        billed = sum(int(c['credits']) for c in d['calls'] if c.get('credits') is not None)
        est = sum(float(c.get('seconds') or 0) * EST_CREDITS_PER_MINUTE / 60 for c in d['calls']
                  if c.get('credits') is None)
        other = sum(int(o['credits']) for o in d['other'])
        return {'billed_calls': billed, 'estimated_unbilled_calls': round(est), 'other_billed': other,
                'total_counted': round(billed + est + other)}

    def remaining(self) -> float:
        by_time = self.total - self.seconds_used() - sum(self.active.values())
        left_credits = self.total_credits - self.credits()['total_counted']
        by_credits = left_credits * 60 / EST_CREDITS_PER_MINUTE   # estimated seconds those credits may cover
        return max(0.0, min(by_time, by_credits))

    def reserve(self, call_id) -> float:
        grant = min(MAX_CALL_SECONDS, self.remaining())
        if grant < MIN_CALL_SECONDS:
            return 0.0
        self.active[call_id] = grant
        return grant

    def finish(self, call_id, seconds, conversation_id=None, account_delta=None):
        self.active.pop(call_id, None)
        d = self._read()
        d['calls'].append({'call_id': call_id, 'seconds': round(seconds, 1), 'conversation_id': conversation_id,
                           'credits': None, 'account_delta': account_delta, 'at': time.time()})
        self._write(d)

    def set_credits(self, call_id, credits):
        d = self._read()
        for c in d['calls']:
            if c['call_id'] == call_id:
                c['credits'] = int(credits)
        self._write(d)

    def add_other(self, what, credits):
        d = self._read()
        d['other'].append({'what': what, 'credits': int(credits)})
        self._write(d)

    def summary(self) -> dict:
        d = self._read()
        return {'call_seconds_limit': self.total, 'call_seconds_measured': round(self.seconds_used(), 1),
                'credit_limit': self.total_credits, 'credits': self.credits(), 'calls': len(d['calls']),
                'grantable_s': round(self.remaining(), 1)}


class BondokBridge(ElevenLabsBridge):
    def __init__(self, *, bondok_url: str, session_secret: str, budget: Budget, sara_reserve_credits: int, **kw):
        super().__init__(**kw)
        self.bondok_url = bondok_url.rstrip('/')
        self.session_secret = session_secret
        self.budget, self.sara_reserve = budget, int(sara_reserve_credits)
        self._bondok = httpx.AsyncClient(timeout=5)
        self._held: dict[asyncio.Task, dict] = {}       # handle task -> this call's state

    def enabled(self) -> bool:
        return os.environ.get('BONDOK_VOICE_ENABLED', '1') == '1' and not os.path.exists(DISABLED_FLAG)

    async def _signed(self, path: str, body: dict) -> dict:
        raw = json.dumps({**body, 'ts': time.time()}).encode()
        sig = hmac.new(self.session_secret.encode(), raw, hashlib.sha256).hexdigest()
        r = await self._bondok.post(self.bondok_url + path, content=raw,
                                    headers={'X-Bondok-Signature': sig, 'Content-Type': 'application/json'})
        r.raise_for_status()
        return r.json()

    async def account_remaining(self) -> int:
        r = await self._client.get('/v1/user/subscription', headers={'xi-api-key': self.api_key})
        r.raise_for_status()
        d = r.json()
        return int(d['character_limit']) - int(d['character_count'])

    # budget: limit_for runs first (BridgeCall.run), in this call's handle task ------------------------
    def limit_for(self, call_uuid):
        st = self._held.get(asyncio.current_task())
        if st is None:
            return MIN_CALL_SECONDS
        st['grant'] = self.budget.reserve(st['id'])
        return st['grant'] or MIN_CALL_SECONDS

    async def open_agent(self, caller, call_uuid=None):
        from websockets.asyncio.client import connect
        st = self._held.get(asyncio.current_task()) or {}
        if not self.enabled():
            raise RuntimeError('bondok voice disabled')
        if not st.get('grant'):
            log.warning('bondok_bridge.budget_spent', **self.budget.summary())
            raise RuntimeError('bondok voice budget spent')
        endpoint = caller_from_uuid(call_uuid) if call_uuid else None
        if not endpoint:
            raise RuntimeError('no authenticated endpoint on this call')
        call_id = st['id']
        t0 = time.monotonic()
        session, url, left = await asyncio.gather(
            self._signed('/session', {'endpoint': endpoint, 'call_id': call_id,
                                      'verified_by': 'dialplan:CHANNEL(endpoint)'}),
            self.signed_url(), self.account_remaining())
        st['session'] = bool(session.get('ok'))          # ended at hangup whatever happens next
        st['account_before'] = left
        need = st['grant'] * EST_CREDITS_PER_MINUTE / 60
        if left - need < self.sara_reserve:
            log.warning('bondok_bridge.sara_reserve', account_remaining=left, reserve=self.sara_reserve)
            raise RuntimeError('account credits at the reserve kept for Sara')
        if not session.get('ok'):
            raise RuntimeError('bondok refused the session')
        t1 = time.monotonic()
        ws = await connect(url, max_size=2**22)
        log.info('bondok_bridge.opening', endpoint=endpoint, role=session.get('role'), grant_s=round(st['grant']),
                 session_ms=round((t1 - t0) * 1000), connect_ms=round((time.monotonic() - t1) * 1000))
        await ws.send(json.dumps({
            'type': 'conversation_initiation_client_data',
            'dynamic_variables': {'opening': session.get('greeting') or 'أهلا، معاك بندق.'},
            'custom_llm_extra_body': {'session': session['token']},
        }))
        st['connected'] = time.monotonic()
        return ws

    # caller speech -> Bondok (commit gate) --------------------------------------------------------
    def _speech_reporter(self, st):
        frames: collections.deque = collections.deque()

        async def on_speech(conversation_id):
            now = time.monotonic()
            frames.append(now)
            while frames and now - frames[0] > 0.6:
                frames.popleft()
            if len(frames) >= 10 and now - st.get('reported', 0) >= 0.25 and st.get('session'):
                st['reported'] = now
                with contextlib.suppress(Exception):
                    await self._signed('/speech', {'call_id': st['id']})
        return on_speech

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        st = {'id': uuid.uuid4().hex[:24], 'started': time.monotonic()}
        self._held[task] = st
        self.calls.add(task)
        call = None
        try:
            call = BridgeCall(reader, writer, open_agent=self.open_agent, limit_for=self.limit_for,
                              on_speech=self._speech_reporter(st), typing_volume=0)
            await call.run()
        except Exception as exc:  # noqa: BLE001 - the dialplan plays the busy tone
            log.warning('bondok_bridge.call_failed', error=type(exc).__name__, detail=str(exc)[:120])
            with contextlib.suppress(Exception):
                writer.write(frame(KIND_HANGUP))
                await writer.drain()
                writer.close()
        finally:
            self.calls.discard(task)
            self._held.pop(task, None)
            if st.get('session'):
                with contextlib.suppress(Exception):
                    await self._signed('/session/end', {'call_id': st['id']})
            if st.get('grant'):
                seconds = time.monotonic() - st['started'] if st.get('connected') else 0.0
                conv = getattr(call, 'conversation_id', None)
                delta = None
                if st.get('account_before') is not None:
                    with contextlib.suppress(Exception):     # all account usage in the window (Sara's too)
                        delta = st['account_before'] - await self.account_remaining()
                self.budget.finish(st['id'], seconds, conv, delta)
                log.info('bondok_bridge.usage', call_seconds=round(seconds, 1), **self.budget.summary())
                if conv:
                    asyncio.create_task(self._billed(st['id'], conv))

    async def _billed(self, call_id, conversation_id):
        """ElevenLabs' billed credits for the call, when it still reports them (retention may delete it first)."""
        await asyncio.sleep(8)
        with contextlib.suppress(Exception):
            r = await self._client.get(f'/v1/convai/conversations/{conversation_id}',
                                       headers={'xi-api-key': self.api_key})
            if r.status_code == 200:
                cost = (r.json().get('metadata') or {}).get('cost')
                if cost is not None:
                    self.budget.set_credits(call_id, int(cost))
                    log.info('bondok_bridge.billed', credits=int(cost), **self.budget.summary())


async def main():
    from voice_agent.cli import _install_stop_handlers
    env = os.environ
    configure_logging(env.get('LOG_LEVEL', 'INFO'), env.get('LOG_FORMAT', 'json'))
    budget = Budget(env.get('BONDOK_LEDGER', '/var/lib/bondok-voice/ledger.json'),
                    float(env.get('BONDOK_BUDGET_SECONDS', '600')), float(env.get('BONDOK_BUDGET_CREDITS', '4000')),
                    int(env.get('BONDOK_OTHER_CREDITS', '0')), env.get('BONDOK_OTHER_NOTE', ''))
    if '--budget' in sys.argv:
        print(json.dumps(budget.summary()))
        return
    if '--add-other' in sys.argv:                  # --add-other <credits> <what>: other billed usage (fixture TTS)
        i = sys.argv.index('--add-other')
        budget.add_other(' '.join(sys.argv[i + 2:]), int(sys.argv[i + 1]))
        print(json.dumps(budget.summary()))
        return
    bridge = BondokBridge(
        api_key=env['ELEVENLABS_API_KEY'], agent_id=env['BONDOK_ELEVENLABS_AGENT_ID'],
        base_url=env.get('ELEVENLABS_BASE_URL', 'https://api.elevenlabs.io'),
        host=env.get('BONDOK_BRIDGE_HOST', '127.0.0.1'), port=int(env.get('BONDOK_BRIDGE_PORT', '9020')),
        typing_volume=0, bondok_url=env['BONDOK_SESSION_URL'], session_secret=env['VOICE_SESSION_SECRET'],
        budget=budget, sara_reserve_credits=int(env.get('BONDOK_SARA_RESERVE_CREDITS', '15000')))
    stop = asyncio.Event()
    _install_stop_handlers(stop)
    await bridge.serve(stop)


if __name__ == '__main__':
    asyncio.run(main())
