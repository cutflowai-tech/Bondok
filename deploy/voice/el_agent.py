#!/usr/bin/env python3
"""Create or update Bondok's ElevenLabs agent (telephone extension 111). Stdlib only; prints ids, never secrets.

    python3 el_agent.py --env /etc/bondok-voice/bridge.env --llm-key-file /etc/bondok-voice/llm.key \
        --url https://<voice host>/v1/chat/completions --state /etc/bondok-voice/agent-state.json [--dry-run]

ElevenLabs does the listening, turn-taking, barge-in and the voice; Bondok (the custom LLM) decides every word.
Settings that matter for correctness and privacy:
* ``speculative_turn`` off and ``turn_eagerness`` normal: only final transcripts reach Bondok, so a sentence the
  owner is still saying (or correcting) is never acted on;
* no backup LLM: if Bondok cannot answer, ElevenLabs must not let a hosted model improvise in Bondok's name;
* ``record_voice`` off, audio deleted, shortest retention; zero-retention mode requested (plan permitting);
* only audio, interruption and ping events are sent to the bridge (no transcript events);
* signed URLs only (``enable_auth``), per-call ``custom_llm_extra_body`` allowed (the session token), at most two
  concurrent calls and a ten-minute cap per call (cost guard on a quota shared with Sara).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API = 'https://api.elevenlabs.io'
VOICE_ID = 'ihycSANIrpHfhWoaq1g3'          # "Ahmad - Conversational AI Voice": Egyptian, young adult male
TTS_MODEL = 'eleven_v4_turbo'              # the model Sara's agent runs live
SECRET_NAME = 'bondok-voice-llm'
KEYWORDS = ['بندق', 'Bondok', 'ستوري', 'بوست', 'ريلز', 'كابشن', 'توباز', 'Topaz', 'مونداي', 'Monday', 'انستجرام']


def env_file(path) -> dict:
    out = {}
    for line in open(path, encoding='utf-8'):
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def agent_body(url: str, secret_id: str, *, zero_retention=True) -> dict:
    return {
        'name': 'Bondok (111)',
        'conversation_config': {
            'asr': {'quality': 'high', 'provider': 'scribe_realtime', 'user_input_audio_format': 'pcm_16000',
                    'keywords': KEYWORDS},
            'turn': {'turn_timeout': 7, 'turn_eagerness': 'normal', 'speculative_turn': False,
                     'silence_end_call_timeout': 40},
            'tts': {'model_id': TTS_MODEL, 'voice_id': VOICE_ID, 'agent_output_audio_format': 'pcm_16000',
                    'stability': 0.45, 'similarity_boost': 0.8, 'speed': 1.0},
            'conversation': {'max_duration_seconds': 600,
                             'client_events': ['conversation_initiation_metadata', 'audio', 'interruption', 'ping']},
            'agent': {
                'first_message': '{{opening}}', 'language': 'ar',
                'dynamic_variables': {'dynamic_variable_placeholders': {'opening': 'أهلا، معاك بندق.'}},
                'prompt': {
                    'prompt': 'You are the voice of Bondok. Every answer comes from the custom LLM.',
                    'llm': 'custom-llm',
                    'custom_llm': {'url': url, 'model_id': 'bondok', 'api_key': {'secret_id': secret_id},
                                   'api_type': 'chat_completions'},
                    'backup_llm_config': {'preference': 'disabled'},
                    'cascade_timeout_seconds': 15,
                    'timezone': 'Africa/Cairo',
                },
            },
        },
        'platform_settings': {
            'auth': {'enable_auth': True},
            'overrides': {'custom_llm_extra_body': True},
            'call_limits': {'agent_concurrency_limit': 2, 'daily_limit': 40},
            'privacy': {'record_voice': False, 'delete_audio': True, 'delete_transcript_and_pii': True,
                        'retention_days': 1, 'zero_retention_mode': zero_retention},
        },
    }


def call(method, path, key, body=None):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={'xi-api-key': key, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b'{}')
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors='replace')[:600]
        raise SystemExit(f'{method} {path} -> {e.code}: {detail}') from None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--env', required=True)
    ap.add_argument('--llm-key-file', required=True)
    ap.add_argument('--url', required=True)
    ap.add_argument('--state', required=True)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--no-zero-retention', action='store_true', help='if the plan refuses zero-retention mode')
    a = ap.parse_args()
    if not a.url.startswith('https://'):
        raise SystemExit('the custom LLM url must be https')
    key = env_file(a.env)['ELEVENLABS_API_KEY']
    state = json.load(open(a.state)) if os.path.exists(a.state) else {}
    if a.dry_run:
        body = agent_body(a.url, state.get('secret_id') or '<secret>', zero_retention=not a.no_zero_retention)
        print(json.dumps(body, ensure_ascii=False, indent=1))
        return
    llm_key = open(a.llm_key_file).read().strip()
    if len(llm_key) < 32:
        raise SystemExit('llm key too short')
    if state.get('secret_id'):
        call('PATCH', f"/v1/convai/secrets/{state['secret_id']}", key, {'type': 'update', 'name': SECRET_NAME,
                                                                         'value': llm_key})
    else:
        state['secret_id'] = call('POST', '/v1/convai/secrets', key,
                                  {'type': 'new', 'name': SECRET_NAME, 'value': llm_key})['secret_id']
    body = agent_body(a.url, state['secret_id'], zero_retention=not a.no_zero_retention)
    if state.get('agent_id'):
        call('PATCH', f"/v1/convai/agents/{state['agent_id']}", key, body)
    else:
        state['agent_id'] = call('POST', '/v1/convai/agents/create', key, body)['agent_id']
    fd = os.open(a.state, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(state, f)
    got = call('GET', f"/v1/convai/agents/{state['agent_id']}", key)
    cc, ps = got.get('conversation_config') or {}, got.get('platform_settings') or {}
    print(json.dumps({'agent_id': state['agent_id'], 'secret_id': state['secret_id'],
                      'llm': cc.get('agent', {}).get('prompt', {}).get('llm'),
                      'voice_id': cc.get('tts', {}).get('voice_id'),
                      'speculative_turn': cc.get('turn', {}).get('speculative_turn'),
                      'backup_llm': (cc.get('agent', {}).get('prompt', {}).get('backup_llm_config') or {}),
                      'privacy': ps.get('privacy'), 'call_limits': ps.get('call_limits')}, ensure_ascii=False))


if __name__ == '__main__':
    sys.exit(main())
