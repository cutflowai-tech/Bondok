"""Bondok on the telephone (extension 111): the same agent, tools, owner model and records as Slack.

Path: office phone → Asterisk 111 → AudioSocket → voice bridge on the PBX host (Sara's ElevenLabs bridge, second
instance) → ElevenLabs agent (speech recognition, turn-taking, barge-in, Egyptian male voice) → THIS service as the
agent's "custom LLM" → Bondok's Core (verifier, tools, waset_ops) → text back → speech.

Identity is never chosen by the model or by what the caller says. The bridge, on the PBX host, confirms the calling
PJSIP endpoint through ARI and registers a call session here over Tailscale (HMAC-signed); the session token rides in
the ElevenLabs conversation's ``elevenlabs_extra_body`` and binds every turn to that verified extension. The role
comes from an explicit mapping file (``VOICE_ACCESS_FILE``): ``owner`` acts as the Bondok owner, ``member`` as a
non-owner (no changes), ``test`` as the owner on isolated acceptance items only; any other extension gets no access.

Turns are idempotent: a turn key is derived from the call and the position and text of the caller's message, its
result is stored; a provider retry or a dropped call never executes twice, and an interrupted answer is kept for the
next turn. Speculative (partial) turns must be disabled on the ElevenLabs agent: only final transcripts arrive here.
No audio and no full transcript is stored by this service; Bondok keeps its usual command and result records.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bridge import COMMIT_GATE

LOG = logging.getLogger('bondok.voice')
SESSION_TTL = 2 * 3600
SETTLE = 1.5            # seconds after a turn arrives before any change is committed (a correction may follow)
SPEECH_MARGIN = 0.35    # caller speech this long after the turn arrived is new speech, not the turn's own tail
ROLES = ('owner', 'member', 'test')

# ----------------------------------------------------------------------------------------------- spoken output
STYLE = (
    '\nTHIS TURN IS A PHONE CALL. Answer in short spoken Egyptian Arabic, like a friendly young Egyptian colleague: '
    'one to three short sentences, no lists, no markdown, no emojis, no links, no ids unless asked. Say item codes '
    'letter by letter only when needed. Say dates as weekday plus date and times as Cairo time ("الخميس 20 نوفمبر '
    'الساعة 4 العصر"). If the caller corrects himself ("الخميس... لا، الجمعة"), act on the final meaning only. Never '
    'say something was done unless a tool result says so; say "اتقبل ولسه بيتنفذ" for accepted work. Interrupting you '
    'is not a cancellation: cancel only when he asks to cancel. A tool result "superseded" means it was NOT done '
    'because he kept talking: read his newer words together with the older ones and act on what he finally means, '
    'without asking him to confirm again. If he corrects something a tool result says was already done, say it was '
    'already done, and undo it (the opposite tool) when he asks for that or his words clearly mean it.')

TOOL_AR = {
    'pause_item': 'إيقاف', 'resume_item': 'استئناف', 'skip_item': 'تخطي', 'request_recheck': 'إعادة الفحص',
    'request_reschedule': 'تحديد الموعد', 'request_publish': 'طلب النشر', 'set_window': 'تحديد الشرط',
    'change_format': 'تغيير النوع', 'replace_source': 'تغيير الملف', 'update_caption': 'تعديل الكابشن',
    'approve_existing_caption': 'اعتماد الكابشن', 'approve_caption_draft': 'اعتماد الكابشن المقترح',
    'draft_caption': 'كتابة كابشن مقترح', 'confirm_topaz': 'تسجيل تأكيد توباز', 'resolve_publication': 'تسجيل نتيجة النشر',
    'report_published': 'تسجيل إنك نشرته', 'request_rework': 'رجوعه للمونتير', 'waive_rule': 'الاستثناء من القاعدة',
    'restore_rule': 'إلغاء الاستثناء', 'set_rule': 'تغيير القاعدة', 'publish_now': 'النشر دلوقتي',
    'cancel_schedule': 'إلغاء الموعد', 'board_create_item': 'إنشاء العنصر', 'board_archive_item': 'الأرشفة',
    'board_delete_item': 'المسح', 'set_item_field': 'تعديل البيانات', 'file_action': 'تعديل الملف',
    'block_publishing': 'منع النشر', 'allow_publishing': 'السماح بالنشر',
}


def clean(text: str) -> str:
    """Text a voice can say: no markdown, emoji, links or code marks."""
    t = re.sub(r'https?://\S+', '', text or '')
    t = re.sub(r'[*_`#>|]+', ' ', t)
    t = re.sub(r'[\U0001F000-\U0001FAFF☀-➿️]', '', t)
    return re.sub(r'\s+', ' ', t).strip()


def spoken_outcomes(outcomes) -> str:
    """Trusted, short spoken summary of what the tools actually did (the model's words never stand in for it)."""
    parts = []
    for o in outcomes or []:
        r, name = o.get('result') or {}, TOOL_AR.get(o.get('tool'), 'الطلب')
        st = r.get('state')
        if st == 'completed' and r.get('unchanged'):
            parts.append(f'{name}: مكانش محتاج تغيير.')
        elif st == 'completed':
            parts.append(f'{name}: اتنفذ.')
        elif st == 'accepted':
            parts.append(f'{name}: اتقبل ولسه بيتنفذ.')
        elif st == 'awaiting_approval':
            parts.append(f'{name}: محتاج موافقتك الأول.')
        elif st == 'superseded':
            parts.append(f'{name}: لسه منفذتهوش، مستنيك تكمل.')
        elif st == 'needs_confirmation':
            parts.append(clean(r.get('question') or '') + ' قول اه عشان أنفذ، أو لا.')
        elif st == 'needs_clarification':
            q = r.get('question') or r.get('error')
            if q:
                parts.append(clean(q))
            elif r.get('ambiguous'):
                parts.append('تقصد أنهي واحد: ' + '، '.join(str(a.get('name')) for a in r['ambiguous'][:4]) + '؟')
        else:
            parts.append(f'{name}: ماتنفذش.')
    return ' '.join(parts)


# ----------------------------------------------------------------------------------------------- sessions
class Access:
    """Explicit extension → role mapping (no default: an unknown extension has no access)."""

    def __init__(self, path):
        self.path = Path(path) if path else None

    def role(self, endpoint: str) -> str | None:
        try:
            data = json.loads(self.path.read_text()) if self.path else {}
        except (OSError, ValueError):
            return None
        role = ((data.get('extensions') or {}).get(str(endpoint)) or {}).get('role')
        return role if role in ROLES else None


def sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class Voice:
    def __init__(self, core, env: dict):
        self.core, self.env = core, env
        self.access = Access(env.get('VOICE_ACCESS_FILE'))
        self.llm_key = env.get('VOICE_LLM_KEY') or ''
        self.session_secret = env.get('VOICE_SESSION_SECRET') or ''
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._speech: dict[str, float] = {}       # call id -> when the bridge last heard the caller (monotonic)

    # sessions -------------------------------------------------------------------------------------
    def open_session(self, body: dict) -> dict:
        endpoint, call_id = str(body.get('endpoint') or ''), str(body.get('call_id') or '')
        if not re.fullmatch(r'[0-9]{2,6}', endpoint) or not re.fullmatch(r'[A-Za-z0-9-]{8,64}', call_id):
            return {'ok': False, 'error': 'bad endpoint or call id'}
        role = self.access.role(endpoint)
        token = secrets.token_urlsafe(32)
        actor = (self.core.owner if role in ('owner', 'test') else f'voice:{endpoint}') if role else None
        s = {'call_id': call_id, 'endpoint': endpoint, 'role': role, 'actor': actor, 'verified_by':
             body.get('verified_by'), 'created': time.time(), 'expires': time.time() + SESSION_TTL}
        self.core.store.kv('voice-session:' + hashlib.sha256(token.encode()).hexdigest(), s)
        with self.core.store.db() as c:              # minimal audit: who called and with what role, never words
            c.execute('INSERT INTO audit(kind,detail,created) VALUES(?,?,?)', ('voice_session', json.dumps(
                {k: s[k] for k in ('call_id', 'endpoint', 'role', 'verified_by')}), time.time()))
        LOG.info('voice_session call=%s endpoint=%s role=%s', call_id, endpoint, role or 'none')
        return {'ok': True, 'token': token, 'role': role or 'none',
                'greeting': GREETING[role or 'none']}

    def session(self, token: str) -> dict | None:
        if not token:
            return None
        s = self.core.store.kv('voice-session:' + hashlib.sha256(token.encode()).hexdigest())
        if not s or s.get('expires', 0) < time.time():
            return None
        return s

    def end_session(self, body: dict) -> dict:
        call_id = str(body.get('call_id') or '')
        self.core.store.forget('voice:' + call_id)          # the call's words are dropped
        with self._guard:
            self._locks.pop(call_id, None)
        LOG.info('voice_session_end call=%s', call_id)
        return {'ok': True}

    def speech(self, body: dict) -> dict:
        """The bridge heard the caller speaking (at least 200 ms within 600 ms). Kept in memory only."""
        self._speech[str(body.get('call_id') or '')] = time.monotonic()
        return {'ok': True}

    def gate(self, call_id: str, ctx: dict) -> bool:
        """May a change from this turn be committed now? Waits until SETTLE after the turn arrived; refuses if the
        caller spoke again meanwhile (a correction may be coming) or the provider dropped the turn."""
        while True:
            if ctx['cancelled'].is_set():
                ctx['held'] = 'turn_cancelled'
            elif self._speech.get(call_id, 0) > ctx['received'] + SPEECH_MARGIN:
                ctx['held'] = 'caller_spoke'
            elif time.monotonic() >= ctx['received'] + SETTLE:
                return True
            else:
                time.sleep(0.05)
                continue
            LOG.info('voice_commit_held call=%s reason=%s', call_id, ctx['held'])
            return False

    # turns ----------------------------------------------------------------------------------------
    def turn(self, body: dict, ctx: dict | None = None) -> tuple[str, str | None]:
        """(what to say, turn key or None). The key marks the answer delivered once it was streamed."""
        ctx = ctx if ctx is not None else new_ctx()
        extra = body.get('elevenlabs_extra_body') or {}
        s = self.session(str(extra.get('session') or ''))
        if not s:
            return 'آسف، مش قادر أتأكد من الخط ده. اقفل وكلمني تاني.', None
        if not s.get('role'):
            return 'الخط ده لسه مش متسجل عندي، فمش هقدر أساعد في حاجة على البورد.', None
        msgs = [m for m in body.get('messages') or [] if m.get('role') == 'user']
        if not msgs:
            return GREETING[s['role']], None
        text = _text(msgs[-1].get('content')).strip()
        if not text:
            return 'معلش، مسمعتش كويس. ممكن تعيد؟', None
        key = hashlib.sha256(f"{s['call_id']}|{len(msgs)}|{text}".encode()).hexdigest()[:24]
        with self._lock(s['call_id']):                       # one turn of a call at a time, in order
            done = self.core.store.kv('voice-turn:' + key)
            if done:
                return done['spoken'], key                   # a retried request: same answer, nothing re-run
            missed = self._undelivered(s, text)
            if missed:
                spoken = 'الطلب ده وصلني في المكالمة اللي فاتت قبل ما تقطع، ومش هكرره. ' + missed
            else:
                spoken = self._run(s, text, key, ctx)
            self.core.store.kv('voice-turn:' + key, {'spoken': spoken, 'at': time.time()})
            self.core.store.kv('voice-last:' + s['endpoint'], {
                'call_id': s['call_id'], 'key': key, 'text': hashlib.sha256(_norm(text).encode()).hexdigest(),
                'spoken': spoken, 'at': time.time(), 'delivered': False})
            return spoken, key

    def delivered(self, key, endpoint):
        last = self.core.store.kv('voice-last:' + endpoint) or {}
        if last.get('key') == key:
            self.core.store.kv('voice-last:' + endpoint, {**last, 'delivered': True})

    def _undelivered(self, s, text) -> str | None:
        """A dropped call: the same request, from the same line, in a new call within 10 minutes, whose answer
        never reached the caller, is answered from the record instead of being executed again."""
        last = self.core.store.kv('voice-last:' + s['endpoint']) or {}
        if (last and not last.get('delivered') and last.get('call_id') != s['call_id'] and
                time.time() - last.get('at', 0) < 600 and
                last.get('text') == hashlib.sha256(_norm(text).encode()).hexdigest()):
            self.core.store.kv('voice-last:' + s['endpoint'], {**last, 'delivered': True})
            return last['spoken']
        return None

    def _lock(self, call_id):
        with self._guard:
            return self._locks.setdefault(call_id, threading.Lock())

    def _run(self, s, text, key, ctx) -> str:
        e = {'user': s['actor'], 'text': text, 'ts': f'{time.time():.6f}', 'channel': 'voice',
             'thread_ts': 'voice:' + s['call_id'], 'restrict': 'isolated' if s['role'] == 'test' else None,
             'event_key': f"voice:{s['call_id']}:{key}"}
        COMMIT_GATE.check = lambda: self.gate(s['call_id'], ctx)
        try:
            reply, outcomes = self.core.handle(e, voice=True)
        except Exception as ex:  # noqa: BLE001 - never a silent failure on the phone
            LOG.error('voice_turn_failed type=%s', type(ex).__name__)
            return 'حصلت مشكلة عندي ومقدرتش أكمل الطلب ده، ومأكدتش أي تغيير. جرب تاني بعد شوية.'
        finally:
            COMMIT_GATE.check = None
        said, check = speakable(reply), spoken_outcomes(outcomes)
        LOG.info('voice_turn call=%s outcomes=%s', s['call_id'],
                 ','.join(f"{o.get('tool')}:{(o.get('result') or {}).get('state')}:{(o.get('args') or {}).get('item', '')}"
                          for o in outcomes or []) or '-')
        return (said + (' ' + check if check else '')).strip() or 'تمام.'


def new_ctx() -> dict:
    return {'received': time.monotonic(), 'received_at': time.time(), 'cancelled': threading.Event()}


def _norm(text) -> str:
    return re.sub(r'[\s.,!?؟،]+', ' ', text or '').strip().lower()


_NAMES = '(' + '|'.join(sorted((k.replace('_', ' ') for k in TOOL_AR), key=len, reverse=True)) + '|[a-z]+(?: [a-z]+)?)'
HEADS = [(r'✅ Done: ' + _NAMES, 'اتنفذ: {}'), (r'ℹ️ No change needed: ' + _NAMES, 'مكانش محتاج تغيير: {}'),
         (r'↩️ Already handled earlier: ' + _NAMES, 'اتعمل قبل كده: {}'),
         (r'🕒 Accepted \(not yet performed\): ' + _NAMES, 'اتقبل ولسه بيتنفذ: {}'),
         (r'⛔ Not done: ' + _NAMES, 'ماتنفذش: {}'), (r'✅ Executed (B-\w+)', 'اتنفذ المقترح {}'),
         (r'❌ Rejected (B-\w+)', 'اترفض المقترح {}')]


def speakable(text: str) -> str:
    """A reply made for Slack, said on the phone: Bondok's English status heads in Arabic, no marks."""
    t = text or ''
    for pat, ar in HEADS:
        t = re.sub(pat, lambda m: ar.format(TOOL_AR.get(m.group(1).strip().replace(' ', '_'), m.group(1))), t)
    t = t.replace('(رد بـ «اه» عشان أنفذ، أو «لا»)', 'قول اه عشان أنفذ، أو لا.')
    return clean(t)


GREETING = {
    'owner': 'أهلا يا باشا، معاك بندق. أقدر أساعدك في إيه؟',
    'member': 'أهلا، معاك بندق. أقدر أقولك حالة الفيديوهات؛ التعديلات بتبقى من المالك.',
    'test': 'أهلا، معاك بندق، خط الاختبار. أقدر أشتغل على عناصر الاختبار بس.',
    'none': 'أهلا، معاك بندق. الخط ده لسه مش متسجل عندي.',
}


def _text(content) -> str:
    if isinstance(content, list):
        return ' '.join(str(p.get('text') or '') for p in content if isinstance(p, dict))
    return str(content or '')


# ----------------------------------------------------------------------------------------------- HTTP
FILLERS = ('ثانية واحدة... ', 'لحظة أشوفلك... ', 'تمام، ثانية... ')
FIRST_WAIT = 1.2          # seconds before a buffer phrase (ElevenLabs cascades on a silent custom LLM after 2-15 s)


class Stream:
    """OpenAI chat-completions streaming, the format ElevenLabs' custom LLM expects."""

    def __init__(self, handler, model='bondok'):
        self.h, self.model = handler, model
        self.chunk({'role': 'assistant', 'content': ''})

    def chunk(self, delta, finish=None):
        d = {'id': 'bondok', 'object': 'chat.completion.chunk', 'created': int(time.time()), 'model': self.model,
             'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
        self.h.wfile.write(b'data: ' + json.dumps(d, ensure_ascii=False).encode() + b'\n\n')
        self.h.wfile.flush()

    def keepalive(self):
        self.h.wfile.write(b': working\n\n')
        self.h.wfile.flush()

    def say(self, text):
        for part in re.split(r'(?<=[.!؟?،])\s+', text):
            if part:
                self.chunk({'content': part + ' '})

    def end(self):
        self.chunk({}, 'stop')
        self.h.wfile.write(b'data: [DONE]\n\n')
        self.h.wfile.flush()


def make_llm_handler(voice: Voice):
    class H(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *a):            # no request logs with bodies
            return

        def do_GET(self):
            if self.path.rstrip('/') in ('/health', '/v1/health'):
                return self._json(200, {'ok': True})
            self._json(404, {'error': 'not found'})

        def do_POST(self):
            if not self.path.rstrip('/').endswith('/chat/completions'):
                return self._json(404, {'error': 'not found'})
            auth = self.headers.get('Authorization', '')
            if not voice.llm_key or not hmac.compare_digest(auth, 'Bearer ' + voice.llm_key):
                return self._json(401, {'error': 'unauthorized'})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
            except ValueError:
                return self._json(400, {'error': 'bad json'})
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'close')
            self.end_headers()
            result, done, ctx = {}, threading.Event(), new_ctx()

            def work():
                try:
                    result['text'], result['key'] = voice.turn(body, ctx)
                except Exception as ex:  # noqa: BLE001
                    LOG.error('voice_turn_crashed type=%s', type(ex).__name__)
                    result['text'], result['key'] = 'حصلت مشكلة عندي، ومأكدتش أي تغيير.', None
                done.set()
            threading.Thread(target=work, daemon=True).start()
            timing = {}
            try:
                out = Stream(self)
                while not done.wait(0.4):                 # a dropped request is noticed within 0.4 s
                    out.keepalive()
                    if 'filler' not in timing and time.monotonic() - ctx['received'] >= FIRST_WAIT:
                        out.say(secrets.choice(FILLERS))  # a buffer phrase: the caller hears Bondok at once
                        timing['filler'] = time.time()
                timing['answer'] = time.time()
                out.say(result['text'])
                out.end()
                if result.get('key'):
                    s = voice.session(str((body.get('elevenlabs_extra_body') or {}).get('session') or ''))
                    if s:
                        voice.delivered(result['key'], s['endpoint'])
            except (BrokenPipeError, ConnectionResetError):
                ctx['cancelled'].set()                   # nothing from this turn is committed after this
                timing['dropped'] = time.time()
                LOG.info('voice_turn_interrupted (the answer is kept for the next turn)')
            t0 = ctx['received_at']
            LOG.info('voice_turn_timing received=%.3f filler_s=%s answer_s=%s dropped_s=%s held=%s', t0,
                     *(f'{timing[k] - t0:.2f}' if k in timing else '-' for k in ('filler', 'answer', 'dropped')),
                     ctx.get('held', '-'))

        def _json(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    return H


def make_session_handler(voice: Voice):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            return

        def do_GET(self):
            if self.path.rstrip('/') == '/health':
                return self._json(200, {'ok': True})
            self._json(404, {'error': 'not found'})

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get('Content-Length') or 0))
            sig = self.headers.get('X-Bondok-Signature', '')
            if not voice.session_secret or not hmac.compare_digest(sig, sign(voice.session_secret, raw)):
                return self._json(401, {'ok': False, 'error': 'unauthorized'})
            try:
                body = json.loads(raw or b'{}')
            except ValueError:
                return self._json(400, {'ok': False})
            if abs(time.time() - float(body.get('ts') or 0)) > 60:
                return self._json(401, {'ok': False, 'error': 'stale'})
            if self.path.rstrip('/') == '/session':
                return self._json(200, voice.open_session(body))
            if self.path.rstrip('/') == '/session/end':
                return self._json(200, voice.end_session(body))
            if self.path.rstrip('/') == '/speech':
                return self._json(200, voice.speech(body))
            self._json(404, {'ok': False})

        def _json(self, code, obj):
            data = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    return H


def main():
    """Its own restricted configuration file only (VOICE_ENV, mode 0600): the Slack tokens are not in it."""
    import sys
    from dotenv import dotenv_values
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from app import Core, runtime_clients
    from monday_read import MondayReader
    env = dict(dotenv_values(os.environ.get('BONDOK_VOICE_ENV', '/opt/waset-bondok/voice.env')))
    if env.get('VOICE_ENABLED', '1') != '1':
        LOG.warning('voice disabled (VOICE_ENABLED=0): not serving')
        return
    core = Core(env, monday=MondayReader(env['MONDAY_API_TOKEN']), post=None, clients=runtime_clients(env))
    voice = Voice(core, env)
    llm = ThreadingHTTPServer((env.get('VOICE_LLM_BIND') or '127.0.0.1', int(env.get('VOICE_LLM_PORT') or 8790)),
                              make_llm_handler(voice))
    ses = ThreadingHTTPServer((env['VOICE_SESSION_BIND'], int(env.get('VOICE_SESSION_PORT') or 8791)),
                              make_session_handler(voice))
    threading.Thread(target=ses.serve_forever, daemon=True, name='voice-session').start()
    LOG.info('voice ready llm=%s:%s session=%s:%s', *llm.server_address, *ses.server_address)
    llm.serve_forever()


if __name__ == '__main__':
    main()
