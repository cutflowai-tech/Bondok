"""Bondok service (extends the deployed /opt/waset-bondok/app.py of 2026-10-09).

Kept: Socket Mode app identity checks (workspace, bot, app token, private
channel), event deduplication before any model call, crash-safe reply states.
Changed: all operational changes go through the shared handler (waset_ops);
Bondok no longer writes Monday or the pipeline tables directly; the 30-minute
LLM board audit is replaced by deterministic notifications and a watchdog.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

from agent import Agent, Model, ServiceError, policy_text, render_outcome
from bridge import Bridge
from monday_read import MondayReader
from store import Store
from waset_ops import Ops

LOG = logging.getLogger('bondok')
logging.basicConfig(level=logging.WARNING, format='%(asctime)s %(levelname)s %(name)s %(message)s')
LOG.setLevel(logging.INFO)
ENV_PATH = Path(os.environ.get('BONDOK_ENV_FILE', '/opt/waset-bondok/.env'))
# SLACK_BOT_USER_ID / SLACK_APP_ID pin the existing Bondok app identity (kept in the server .env, not in Git).
REQUIRED = ('SLACK_BOT_TOKEN', 'SLACK_APP_TOKEN', 'SLACK_CHANNEL_ID', 'SLACK_TEAM_ID', 'SLACK_OWNER_ID',
            'SLACK_BOT_USER_ID', 'SLACK_APP_ID', 'MONDAY_API_TOKEN', 'OPENROUTER_API_KEY', 'PIPELINE_DB')
# Copy-paste from Bondok's own reply brings backticks; a trailing period/"!" is common (audit M5: these went
# to the model).
APPROVE = re.compile(r'^\s*`?\s*(?:اعتمد|approve)\s+(B-[0-9A-F]{8})\s*`?\s*[.!۔]?\s*$', re.I)
REJECT = re.compile(r'^\s*`?\s*(?:ارفض|reject)\s+(B-[0-9A-F]{8})\s*`?\s*[.!۔]?\s*$', re.I)
HEARTBEAT_LIMITS = {'wf2': 300, 'wf1': 1800, 'wf3': 4200}
FALLBACK_REPEAT_SECONDS = 30 * 60
PLACEHOLDER_ID = re.compile(r'B-X{4,}', re.I)


def accept_event(body, channel, team, known_thread, bot) -> bool:
    e = body.get('event', {})
    if body.get('team_id') != team or e.get('channel') != channel or e.get('bot_id') or e.get('subtype') \
            or e.get('user') == bot:
        return False
    if e.get('type') == 'app_mention':
        return True
    return e.get('type') == 'message' and bool(e.get('thread_ts')) and known_thread(e['thread_ts'])


class Core:
    """Everything except the Slack transport, so it can be tested offline."""

    def __init__(self, env: dict, *, model=None, monday=None, post=None, ops=None):
        self.env = env
        self._undo, self._new_offset, self._defer_offset = [], None, False
        self.store = Store(Path(env.get('BONDOK_STATE_DIR', '/var/lib/bondok')) / 'bondok.sqlite')
        self.ops = ops or Ops(env['PIPELINE_DB'])
        self.owner = env['SLACK_OWNER_ID']
        self.bridge = Bridge(self.ops, self.owner)
        self.model = model or Model(env['OPENROUTER_API_KEY'], env.get('OPENROUTER_MODEL') or 'openai/gpt-6.1-sol',
                                    policy_text())
        self.agent = Agent(self.bridge, self.model, monday)
        self.post = post
        self.wake = threading.Event()
        self.stop = threading.Event()

    # ------------------------------------------------------------------ requests
    def handle(self, e: dict) -> str:
        """One Slack message -> reply text. Deterministic paths first; the model
        only for language understanding."""
        text = re.sub(r'<@[A-Z0-9]+>', '', e.get('text') or '').strip()
        thread = e.get('thread_ts') or e['ts']
        actor, event_id = e['user'], f"{self.env['SLACK_TEAM_ID']}:{e['channel']}:{e['ts']}"
        m = APPROVE.match(text) or REJECT.match(text)
        if m:
            r = self.bridge.approve(m[1].upper(), actor, thread, event_id, reject=bool(REJECT.match(text)))
            if r.get('state') == 'completed':
                return ('❌ Rejected ' if REJECT.match(text) else '✅ Executed ') + m[1].upper() + \
                    ('' if REJECT.match(text) else self._summary(r))
            return f"⛔ {m[1].upper()} not executed: {r.get('reason', r.get('state'))}"
        history = self.store.history(thread)
        self.store.remember(thread, 'user', f'{actor}: {text}')
        try:
            reply, outcomes = self.agent.answer(text, actor=actor, thread=thread, event_id=event_id, history=history)
        except ServiceError as e:
            # Model unavailable: nothing more is guessed or executed; automation continues.
            LOG.warning('model_error code=%s detail=%s', e.code, e.detail)
            done = getattr(e, 'outcomes', None)
            if done:
                # Actions already ran before the model failed: report them, never "nothing was done" (audit H1).
                reply = '⚠️ الموديل وقف في النص، بس الخطوات دي اتنفذت:' + ''.join('\n\n' + render_outcome(o) for o in done)
                self.store.remember(thread, 'assistant', reply)
                return reply
            return self._fallback(thread, e.code)
        if getattr(self.model, 'last_budget', None):
            LOG.warning('model_low_credit reduced_output_budget=%s', self.model.last_budget)
        self.store.kv('fallback:' + thread, {'code': None, 'at': 0})
        reply = PLACEHOLDER_ID.sub('رقم المقترح', (reply or '').strip())   # never show a made-up approval id
        for o in outcomes:
            reply += '\n\n' + render_outcome(o)
        self.store.remember(thread, 'assistant', reply)
        return reply or 'No reply.'

    def _fallback(self, thread, code) -> str:
        """Short Egyptian Arabic notice; the same cause in the same thread is not repeated in full."""
        credits = code == 'model_credits'
        last = self.store.kv('fallback:' + thread) or {}
        now = time.time()
        self.store.kv('fallback:' + thread, {'code': code, 'at': now})
        if last.get('code') == code and now - last.get('at', 0) < FALLBACK_REPEAT_SECONDS:
            return ('⚠️ لسه رصيد OpenRouter مخلص، فمعملتش حاجة في الرسالة دي.' if credits else
                    '⚠️ الموديل لسه مش متاح، فمعملتش حاجة في الرسالة دي.')
        if credits:
            return ('⚠️ رصيد OpenRouter بتاع الموديل خلص، فمش هقدر أرد على الكلام دلوقتي ومعملتش أي حاجة في '
                    'الرسالة دي. النشر المجدول والفحوصات شغالين عادي، والموافقات بكلمة «اعتمد» ورقم المقترح شغالة. '
                    'محتاجين نشحن رصيد OpenRouter.')
        return ('⚠️ الموديل مش متاح دلوقتي، فمعملتش أي حاجة في الرسالة دي. النشر المجدول والفحوصات شغالين عادي. '
                'جرّب تاني بعد شوية.')

    @staticmethod
    def _summary(r):
        if r.get('moved'):
            from waset_ops import rules
            return ': ' + '; '.join(f"{m['item_id']} → {rules.display(rules.instant(m['slot']))}" for m in r['moved'])
        if r.get('format'):
            return f": format is now {r['format']}"
        return ''

    # ------------------------------------------------------------------ notifications (no model)
    def deliver_notifications(self, limit=10) -> int:
        n = 0
        for job in self.ops.outbox_take(['slack'], 'bondok', limit, lease=120):
            p = job['payload']
            try:
                sent = self.post(p['text'], p.get('thread'))
                try:
                    ts = sent['ts'] if sent is not None else None
                except (KeyError, TypeError):
                    ts = None
                if ts and p.get('proposal_id'):
                    # Replies in a proposal notification's thread reach Bondok (audit H3).
                    self.store.kv('thread:' + ts, True)
                self.ops.outbox_ack(job['id'], 'bondok', True)
                n += 1
            except Exception as ex:  # noqa: BLE001 - delivery failure is retried with backoff
                self.ops.outbox_ack(job['id'], 'bondok', False, type(ex).__name__)
        return n

    # ------------------------------------------------------------------ watchdog (independent of the helper)
    def watchdog(self, defer_offset=False) -> list[str]:
        """Alerts on transitions only; unchanged states stay quiet."""
        alerts = []
        self._undo, self._new_offset, self._defer_offset = [], None, defer_offset
        try:
            ages = self.ops.health()['heartbeat_age_seconds']
            db_ok = True
        except (sqlite3.Error, OSError) as ex:
            ages, db_ok = {}, False
            if self._flip('db_down', True):
                alerts.append(f'⚠️ Operational database is not readable by Bondok ({type(ex).__name__}). '
                              'n8n automation may also be failing.')
        if db_ok and self._flip('db_down', False):
            alerts.append('✅ Operational database is readable again.')
        for name, limit in HEARTBEAT_LIMITS.items():
            if name not in ages:
                continue
            stale = ages[name] > limit
            if self._flip('stale:' + name, stale):
                alerts.append(f'⚠️ {name.upper()} has not reported for {ages[name] // 60} min. Check n8n executions; '
                              'publishing safety checks remain in place.' if stale else f'✅ {name.upper()} is reporting again.')
        alerts += self._helper_errors()
        return alerts

    def run_watchdog(self):
        alerts = self.watchdog(defer_offset=True)
        try:
            for a in alerts:
                self.post(a)
        except Exception:
            for key, prev in reversed(self._undo):     # not delivered: report the transition next time
                self.store.kv('wd:' + key, prev if prev is not None else False)
            raise
        if self._new_offset is not None:               # helper errors reported: advance only now (#13)
            self.store.kv('errors_offset', self._new_offset)

    def _flip(self, key, value) -> bool:
        """Record a boolean state; True when it changed (first observation of a
        healthy state is not a change worth reporting)."""
        prev = self.store.kv('wd:' + key)
        self.store.kv('wd:' + key, value)
        changed = prev != value and not (prev is None and value is False)
        if changed:
            self._undo.append((key, prev))      # restored if the alert cannot be posted (audit M1)
        return changed

    def _helper_errors(self) -> list[str]:
        """errors.log is written by helper.py without SQLite, so failures stay
        visible even when the database is the problem."""
        log = Path(self.env['PIPELINE_DB']).with_name('errors.log')
        try:
            size = log.stat().st_size
        except OSError:
            return []
        offset = self.store.kv('errors_offset') or 0
        if size < offset:
            offset = 0
        if size == offset:
            return []
        lines = []
        with log.open() as f:
            f.seek(offset)
            for x in f.read().splitlines():
                try:
                    lines.append(json.loads(x)) if x.strip().startswith('{') else None
                except ValueError:
                    lines.append({'kind': 'unreadable', 'path': 'errors.log', 'error': x[:200]})  # audit M1
        if self._defer_offset:
            self._new_offset = size
        else:
            self.store.kv('errors_offset', size)
        infra = [x for x in lines if x.get('kind') not in ('rejected', 'stale', 'fenced', 'slot_taken', 'retired')]
        if not infra:
            return []
        key = 'errhour:' + str(int(time.time() // 3600))
        if self.store.kv(key):
            return []
        self.store.kv(key, True)
        by = {}
        for x in infra:
            by[(x.get('path'), x.get('kind'))] = by.get((x.get('path'), x.get('kind')), 0) + 1
        return ['⚠️ Automation helper errors: ' + '; '.join(f'{p} {k} ×{n}' for (p, k), n in sorted(by.items())[:6])
                + f". Latest: {str(infra[-1].get('error'))[:200]}"]


class Runtime(Core):
    def __init__(self, env):
        from slack_bolt import App
        super().__init__(env, monday=MondayReader(env['MONDAY_API_TOKEN']), post=self._post)
        self.channel, self.team = env['SLACK_CHANNEL_ID'], env['SLACK_TEAM_ID']
        self.app = App(token=env['SLACK_BOT_TOKEN'], ignoring_self_events_enabled=True)
        auth = self.app.client.auth_test()
        if auth['team_id'] != self.team or auth['user_id'] != env['SLACK_BOT_USER_ID'] or \
                f"-{env['SLACK_APP_ID']}-" not in env['SLACK_APP_TOKEN']:
            raise RuntimeError('Slack credentials do not belong to the Bondok app in this workspace')
        info = self.app.client.conversations_info(channel=self.channel)['channel']
        if not info.get('is_private') or not info.get('is_member'):
            raise RuntimeError('Bot must be a member of the configured private channel')
        self.bot = auth['user_id']
        self.app.event('app_mention')(self.receive)
        self.app.event('message')(self.receive)

    def _post(self, text, thread=None):
        text = re.sub(r'<![^>]+>', '[alert]', text)
        return self.app.client.chat_postMessage(channel=self.channel, thread_ts=thread, text=text[:38000],
                                                unfurl_links=False, unfurl_media=False, parse='none', link_names=False)

    def receive(self, body, logger):
        if not accept_event(body, self.channel, self.team, lambda ts: bool(self.store.kv('thread:' + ts)), self.bot):
            return
        e = body['event']
        if not e.get('user') or not e.get('text'):
            return
        self.store.kv('thread:' + (e.get('thread_ts') or e['ts']), True)
        self.store.enqueue_event(self.team + ':' + self.channel + ':' + e['ts'], e)   # dedupe before any model call
        self.wake.set()

    def worker(self):
        self.store.recover_working()
        while not self.stop.is_set():
            r = self.store.next_event()
            if not r:
                self.wake.wait(3)
                self.wake.clear()
                continue
            e = json.loads(r['payload'])
            thread = e.get('thread_ts') or e['ts']
            if r['state'] == 'reply_ready':
                reply = r['reply']
            else:
                try:
                    if r['created'] + 1800 < time.time():
                        reply = 'This message is older than 30 minutes (service was down); send it again if still needed.'
                    else:
                        reply = self.handle(e)
                except Exception as ex:  # noqa: BLE001
                    LOG.error('request_failed type=%s', type(ex).__name__)
                    reply = 'An error stopped this request. I did not confirm any change; check status before retrying.'
                self.store.set_event(r['id'], 'reply_ready', reply)
            self.store.set_event(r['id'], 'sending')
            try:
                self._post(reply, thread)
                self.store.set_event(r['id'], 'done')
            except Exception as ex:  # noqa: BLE001
                LOG.error('reply_failed type=%s', type(ex).__name__)
                self.store.set_event(r['id'], 'send_uncertain')

    def background(self):
        while not self.stop.wait(20):
            try:
                self.deliver_notifications()
            except Exception as ex:  # noqa: BLE001 - must not stop the watchdog (audit H4)
                LOG.error('background_failed part=notifications type=%s', type(ex).__name__)
            if int(time.time()) % 60 >= 20:
                continue
            try:
                self.run_watchdog()
            except Exception as ex:  # noqa: BLE001
                LOG.error('background_failed part=watchdog type=%s', type(ex).__name__)

    def run(self):
        from slack_bolt.adapter.socket_mode import SocketModeHandler
        threading.Thread(target=self.worker, daemon=True, name='bondok-worker').start()
        threading.Thread(target=self.background, daemon=True, name='bondok-notify').start()
        LOG.info('ready model=%s board=5105608159 ops_schema=%s', self.model.model, self.ops.store.schema_version())
        SocketModeHandler(self.app, self.env['SLACK_APP_TOKEN']).start()


def main():
    from dotenv import dotenv_values
    reported = False
    while True:
        e = dict(dotenv_values(ENV_PATH))
        if not all(e.get(k) for k in REQUIRED):
            if not reported:
                LOG.warning('waiting_for_configuration')
                reported = True
            time.sleep(15)
            continue
        if e.get('MONDAY_BOARD_ID') != '5105608159':
            raise RuntimeError('Board scope cannot be changed in env')
        Runtime(e).run()
        return


if __name__ == '__main__':
    try:
        main()
    except Exception as ex:  # noqa: BLE001
        LOG.error('startup_failed type=%s', type(ex).__name__)
        raise SystemExit(1)
