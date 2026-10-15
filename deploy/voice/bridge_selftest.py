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
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import bondok_bridge as bb
from voice_agent.telephony.el_bridge import KIND_HANGUP, KIND_UUID, frame

SECRET = 'x' * 48
seen = {'session': [], 'end': [], 'init': None, 'audio_in': 0}


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
        else:
            seen['end'].append((ok, body))
            out = {'ok': True}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


async def fake_agent(ws):
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
    got, voiced = 0, []
    for i in range(frames):
        w.write(frame(0x10, b'\x10\x00' * 160))                 # 20 ms at 8 kHz
        await w.drain()
        await asyncio.sleep(0.02)
        while True:
            try:
                head = await asyncio.wait_for(r.readexactly(3), 0.001)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                break
            n = struct.unpack('>H', head[1:])[0]
            data = await r.readexactly(n)
            if head[0] == 0x10:
                got += 1
                if any(data):
                    voiced.append(i)
    w.write(frame(KIND_HANGUP))
    await w.drain()
    await asyncio.sleep(0.3)
    w.close()
    asterisk_call.voiced = voiced
    return got


async def main():
    from websockets.asyncio.server import serve
    log_buf = io.StringIO()
    logging.basicConfig(stream=log_buf, level=logging.INFO)
    bb.configure_logging('INFO', 'json')
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), FakeBondok)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    async with serve(fake_agent, '127.0.0.1', 0) as ws_server:
        ws_port = ws_server.sockets[0].getsockname()[1]
        bridge = bb.BondokBridge(api_key='k', agent_id='a', base_url='http://127.0.0.1:1', host='127.0.0.1', port=0,
                                 typing_volume=0, bondok_url=f'http://127.0.0.1:{httpd.server_address[1]}',
                                 session_secret=SECRET)

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
            refused_sessions = len(seen['session'])
            try:
                await asyncio.wait_for(asterisk_call(port, frames=3), 20)
            except (ConnectionError, asyncio.IncompleteReadError):
                pass                                            # refused: the bridge hung up at once
            os.environ['BONDOK_VOICE_ENABLED'] = '1'
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
        'disable_switch_refuses': len(seen['session']) == refused_sessions,
    }
    print(json.dumps({'checks': checks, 'agent_frames_heard': got, 'last_voiced_frame': max(voiced) if voiced else None, 'caller_frames_sent': seen['audio_in']}))
    return 0 if all(checks.values()) else 1


if __name__ == '__main__':
    code = asyncio.run(main())
    sys.exit(code)
