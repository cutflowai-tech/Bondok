"""Self-test of Bondok's bridge inside Sara's image, with no PBX call, no ElevenLabs credit and no Bondok host:
a fake Asterisk (AudioSocket client), a fake ElevenLabs agent (WebSocket server) and a fake Bondok session API.

    docker run --rm --network none -v $PWD:/bondok:ro -e PYTHONPATH=/bondok --entrypoint python <image> \
        /bondok/bridge_selftest.py

Checks: the authenticated endpoint from the UUID reaches Bondok signed; the session token travels only in
custom_llm_extra_body; the greeting is the role's; caller audio reaches the agent; agent audio reaches the caller;
an interruption clears queued agent audio (barge-in); hangup ends the Bondok session; the disable switch refuses
the call; no transcript text is logged.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import json
import logging
import os
import struct
import sys
import tempfile
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import bondok_bridge as bb
from voice_agent.telephony.el_bridge import KIND_HANGUP, KIND_UUID, frame

SECRET = 'x' * 48
seen = {'session': [], 'end': [], 'speech': [], 'init': None, 'audio_in': 0, 'conversations': 0}


class FakeBondok(BaseHTTPRequestHandler):
    def log_message(self, *a):
        return

    def do_POST(self):
        raw = self.rfile.read(int(self.headers['Content-Length']))
        ok = hmac.compare_digest(self.headers.get('X-Bondok-Signature', ''),
                                 hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest())
        body = json.loads(raw)
        if self.path == '/session':
            seen['session'].append((ok, body))
            out = {'ok': True, 'token': 'TOKEN-123', 'role': 'test', 'greeting': 'أهلا، خط الاختبار.'}
        elif self.path == '/speech':
            seen['speech'].append((ok, body))
            out = {'ok': True}
        else:
            seen['end'].append((ok, body))
            out = {'ok': True}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


async def fake_agent(ws):
    seen['conversations'] += 1                                 # what ElevenLabs bills: a started conversation
    seen['init'] = json.loads(await ws.recv())
    await ws.send(json.dumps({'type': 'conversation_initiation_metadata', 'conversation_initiation_metadata_event': {
        'conversation_id': 'conv-1', 'agent_output_audio_format': 'pcm_16000', 'user_input_audio_format': 'pcm_16000'}}))
    tone = struct.pack('<320h', *([3000, -3000] * 160))          # 20 ms at 16 kHz
    await ws.send(json.dumps({'type': 'audio', 'audio_event': {'audio_base_64': base64.b64encode(tone * 50).decode()}}))
    await ws.send(json.dumps({'type': 'user_transcript', 'user_transcription_event': {'user_transcript': 'SECRET-WORDS'}}))
    async for msg in ws:
        m = json.loads(msg)
        if 'user_audio_chunk' in m:
            seen['audio_in'] += 1
            if seen['audio_in'] == 10:
                await ws.send(json.dumps({'type': 'interruption', 'interruption_event': {}}))


async def asterisk_call(port, endpoint='99019', frames=25):
    r, w = await asyncio.open_connection('127.0.0.1', port)
    u = uuid.UUID(str(uuid.uuid4())[:24] + endpoint.zfill(12))
    w.write(frame(KIND_UUID, u.bytes))
    got, voiced, t0 = 0, [], time.monotonic()
    asterisk_call.hangup_after = None
    for i in range(frames):
        try:
            w.write(frame(0x10, struct.pack('<160h', *([2500, -2500] * 80))))   # 20 ms of "speech" at 8 kHz
            await w.drain()
        except ConnectionError:
            if i < 5:
                raise                                           # refused at once
            asterisk_call.hangup_after = time.monotonic() - t0   # the bridge closed the call
            break
        await asyncio.sleep(0.02)
        while True:
            try:
                head = await asyncio.wait_for(r.readexactly(3), 0.001)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                break
            n = struct.unpack('>H', head[1:])[0]
            data = await r.readexactly(n)
            if head[0] == KIND_HANGUP:
                asterisk_call.hangup_after = time.monotonic() - t0
                break
            if head[0] == 0x10:
                got += 1
                if any(data):
                    voiced.append(i)
        if asterisk_call.hangup_after is not None:
            break
    try:
        w.write(frame(KIND_HANGUP))
        await w.drain()
    except ConnectionError:
        pass
    await asyncio.sleep(0.3)
    w.close()
    asterisk_call.voiced = voiced
    return got


async def main():
    from websockets.asyncio.server import serve
    log_buf = io.StringIO()
    logging.basicConfig(stream=log_buf, level=logging.INFO)
    bb.configure_logging('INFO', 'json')

    class Words(logging.Handler):                               # any log record carrying the call's words
        hits = 0

        def emit(self, record):
            if 'SECRET-WORDS' in str(record.__dict__):
                Words.hits += 1
    logging.getLogger().addHandler(Words())
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), FakeBondok)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    async with serve(fake_agent, '127.0.0.1', 0) as ws_server:
        ws_port = ws_server.sockets[0].getsockname()[1]
        ledger = tempfile.mkdtemp() + '/ledger.json'
        budget = bb.Budget(ledger, 600, 4000, other_credits=11, other_note='fixture TTS')
        bridge = bb.BondokBridge(api_key='k', agent_id='a', base_url='http://127.0.0.1:1', host='127.0.0.1', port=0,
                                 typing_volume=0, bondok_url=f'http://127.0.0.1:{httpd.server_address[1]}',
                                 session_secret=SECRET, budget=budget, sara_reserve_credits=15000)
        account = {'left': 25000}

        async def remaining():
            return account['left']
        bridge.account_remaining = remaining

        async def url():
            return f'ws://127.0.0.1:{ws_port}'
        bridge.signed_url = url
        server = await asyncio.start_server(bridge.handle, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            got = await asyncio.wait_for(asterisk_call(port, frames=60), 20)
            voiced = asterisk_call.voiced
            await asyncio.sleep(0.5)
            os.environ['BONDOK_VOICE_ENABLED'] = '0'
            refused_sessions = seen['conversations']
            try:
                await asyncio.wait_for(asterisk_call(port, frames=3), 20)
            except (ConnectionError, asyncio.IncompleteReadError):
                pass                                            # refused: the bridge hung up at once
            refused_conversations = seen['conversations']
            os.environ['BONDOK_VOICE_ENABLED'] = '1'

            async def refused(frames=3):
                n, ends, sessions = seen['conversations'], len(seen['end']), len(seen['session'])
                try:
                    await asyncio.wait_for(asterisk_call(port, frames=frames), 20)
                except (ConnectionError, asyncio.IncompleteReadError):
                    pass
                await asyncio.sleep(0.3)
                opened = len(seen['session']) - sessions
                # no conversation started; a Bondok session opened in parallel is ended again
                return seen['conversations'] == n and len(seen['end']) - ends == opened
            first = json.load(open(ledger))['calls'][0]
            # Sara's reserve: the account would fall below it -> no call
            account['left'] = 15500
            sara_protected = await refused()
            account['left'] = 25000
            # the budget cuts a call off where it runs out: 3 s left -> ended at ~3 s although the caller stays on
            bb.MIN_CALL_SECONDS = 1
            budget.total = budget.seconds_used() + 3
            await asyncio.wait_for(asterisk_call(port, frames=400), 30)
            cut_after = asterisk_call.hangup_after
            # spent: nothing starts
            budget.total = budget.seconds_used()
            spent_refused = await refused()
            # credits spent (billed bill recorded) while call time is left: refused too
            budget.total = 600
            budget.set_credits(first['call_id'], 3990)
            credits_refused = await refused()
    checks = {
        'session_signed': bool(seen['session']) and seen['session'][0][0],
        'endpoint_from_uuid': bool(seen['session']) and seen['session'][0][1]['endpoint'] == '99019',
        'token_in_extra_body_only': (seen['init'] or {}).get('custom_llm_extra_body') == {'session': 'TOKEN-123'}
                                    and 'TOKEN-123' not in json.dumps((seen['init'] or {}).get('dynamic_variables')),
        'greeting_from_role': (seen['init'] or {}).get('dynamic_variables', {}).get('opening') == 'أهلا، خط الاختبار.',
        'caller_audio_to_agent': seen['audio_in'] >= 10,
        'agent_audio_to_caller': got >= 5,
        # 1 s of agent speech was queued; the interruption comes at caller frame 10: speech must stop soon after.
        'barge_in_cleared_queue': bool(voiced) and max(voiced) <= 13,
        'session_ended_signed': bool(seen['end']) and seen['end'][0][0],
        'disable_switch_refuses': refused_conversations == refused_sessions,
        'speech_reported_signed': bool(seen['speech']) and all(ok for ok, _ in seen['speech']),
        'speech_reports_throttled': len(seen['speech']) <= seen['audio_in'] * 0.02 / 0.25 + 4,
        'ledger_recorded_call': first['seconds'] > 0 and first['conversation_id'] == 'conv-1',
        'budget_cuts_call_off': cut_after is not None and 2.0 <= cut_after <= 6.0,
        'budget_spent_refuses': spent_refused,
        'sara_reserve_refuses': sara_protected,
        'credit_limit_refuses': credits_refused,
        'no_words_in_logs': Words.hits == 0,
        'seconds_and_credits_separate': all('credits' in c and 'seconds' in c for c in json.load(open(ledger))['calls'])
                                        and json.load(open(ledger))['other'][0]['credits'] == 11,
    }
    print(json.dumps({'checks': checks, 'agent_frames_heard': got, 'last_voiced_frame': max(voiced) if voiced else None, 'caller_frames_sent': seen['audio_in'],
                      'cut_after_s': cut_after and round(cut_after, 1), 'speech_reports': len(seen['speech']),
                      'ledger': budget.summary()}))
    return 0 if all(checks.values()) else 1


if __name__ == '__main__':
    code = asyncio.run(main())
    sys.exit(code)
