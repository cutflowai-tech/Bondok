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

import language as lang
from agent import Agent, Model, ServiceError, policy_text, render_outcome, speaker
from bridge import Bridge, Turn
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
# to the model). Kept for callers; Core reads approvals with language.approval (decorations, several ids, R5 M26).
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
        self.bridge = Bridge(self.ops, self.owner, self.store)
        self.model = model or Model(env['OPENROUTER_API_KEY'], env.get('OPENROUTER_MODEL') or 'openai/gpt-6.1-sol',
                                    policy_text())
        self.agent = Agent(self.bridge, self.model, monday)
        self.post = post
        self.wake = threading.Event()
        self.stop = threading.Event()

    # ------------------------------------------------------------------ requests
    def handle(self, e: dict) -> str:
        """One Slack message -> reply text. Deterministic paths first (approvals, answers to the single open
        question, acknowledgments, "show it again", replies under alerts: no model call); the model only for
        language understanding, and its proposals are checked against the owner's words (bridge.py)."""
        text = re.sub(r'<@[A-Z0-9]+>', '', e.get('text') or '').strip()
        thread = e.get('thread_ts') or e['ts']
        actor, event_id = e['user'], f"{self.env['SLACK_TEAM_ID']}:{e['channel']}:{e['ts']}"
        quick = self._deterministic(text, actor, thread, event_id)
        if quick is not None:
            self.store.remember(thread, 'user', speaker(actor, self.owner) + text)
            self.store.remember(thread, 'assistant', quick)
            return quick
        ctx = self.store.kv('thread_ctx:' + thread) or {}
        turn = Turn(text, thread, self.bridge.today(), context_items=[ctx['item_id']] if ctx.get('item_id') else [],
                    history_text=self.store.thread_text(thread))
        history = self.store.history(thread)
        self.store.remember(thread, 'user', speaker(actor, self.owner) + text)
        owner = actor == self.owner
        try:
            reply, outcomes = self.agent.answer(text, actor=actor, thread=thread, event_id=event_id, history=history,
                                                turn=turn)
        except ServiceError as e:
            # Model unavailable: nothing more is guessed or executed; automation continues. An open question
            # stays open (its "اه" needs no model).
            LOG.warning('model_error code=%s detail=%s', e.code, e.detail)
            if owner and turn.pending():
                self._set_pending(thread, turn.pending())
            done = getattr(e, 'outcomes', None)
            if done:
                # Actions already ran before the model failed: report them, never "nothing was done" (audit H1).
                reply = '⚠️ الموديل وقف في النص، بس الخطوات دي اتنفذت:' + ''.join('\n\n' + render_outcome(o) for o in done)
                self.store.remember(thread, 'assistant', reply)
                return reply
            return self._fallback(thread, e.code)
        if owner:
            self._set_pending(thread, turn.pending())     # the thread's single open question (or none)
        self._note_proposals(thread, outcomes)
        if getattr(self.model, 'last_budget', None):
            LOG.warning('model_low_credit reduced_output_budget=%s', self.model.last_budget)
        self.store.kv('fallback:' + thread, {'code': None, 'at': 0})
        reply = PLACEHOLDER_ID.sub('رقم المقترح', (reply or '').strip())   # never show a made-up approval id
        for o in outcomes:
            reply += '\n\n' + render_outcome(o)
        self.store.remember(thread, 'assistant', reply)
        return reply or 'No reply.'

    # ------------------------------------------------------------------ deterministic paths (no model)
    def _deterministic(self, text, actor, thread, event_id) -> str | None:
        owner = actor == self.owner
        ap = lang.approval(text)
        if ap:
            kind, ids, quoted = ap
            if ids:
                if quoted and owner:
                    # A quoted approval may be a copy of an old message: confirm before it runs (R5 M26).
                    verb = 'أعتمد' if kind == 'approve' else 'أرفض'
                    self._set_pending(thread, {'kind': kind, 'ids': ids, 'question': f"{verb} {', '.join(ids)}؟"})
                    return f"❓ {verb} {', '.join(ids)}؟ (رد بـ «اه» أو «لا»)"
                return self._decide(ids, kind, actor, thread, event_id)
            if not owner:
                return None
            if self._pending(thread):
                return self._answer('yes' if kind == 'approve' else 'no', actor, thread, event_id)
            return self._open_choice(thread, kind, actor, event_id)
        if not owner:
            return None
        a = lang.answer(text)
        if a == 'ack':
            return 'العفو 🙏'
        if a == 'show':
            return self._show(thread)
        if a in ('yes', 'no'):
            if self._pending(thread) or a == 'no':
                return self._answer(a, actor, thread, event_id)
            if self._open_proposals(thread):
                return self._open_choice(thread, 'approve', actor, event_id)
            if any(t in lang.AFFIRM_COMMAND for t in lang.tokens(text)):
                return None              # "تمام كمل", "go ahead with them": the model reads what to continue
            return 'مفيش سؤال مفتوح هنا أرد عليه بـ «اه»، فمعملتش حاجة. قولّي تحب أعمل إيه بالظبط.'
        return self._alert_reply(text, actor, thread, event_id)

    def _pending(self, thread):
        return self.store.kv('pending:' + thread)

    def _set_pending(self, thread, value):
        if value or self.store.kv('pending:' + thread):
            self.store.kv('pending:' + thread, value or {})

    def _answer(self, a, actor, thread, event_id) -> str:
        """'اه'/'لا' answers only the thread's single open question; an answered question is never reused."""
        p = self._pending(thread)
        if not p:
            return 'تمام، معملتش أي حاجة.'
        self._set_pending(thread, None)
        if a == 'no':
            return 'تمام، لغيت السؤال ومعملتش أي حاجة.'
        if p.get('kind') in ('approve', 'reject'):
            return self._decide(p['ids'], p['kind'], actor, thread, event_id)
        lines = []
        for n, act in enumerate(p.get('actions') or []):
            r = self.bridge.execute(act, actor=actor, cid=f'slack:{event_id}:yes:{n}', thread=thread, event_id=event_id)
            lines.append(render_outcome({'tool': act['tool'], 'args': {}, 'result': r}))
        return '\n\n'.join(lines) or 'تمام.'

    def _open_proposals(self, thread) -> list[dict]:
        """Pending proposals that belong to this thread: made in it, shown in it, or the notification it answers."""
        ctx = self.store.kv('thread_ctx:' + thread) or {}
        ids = set(self.store.kv('thread_props:' + thread) or []) | ({ctx['proposal_id']} if ctx.get('proposal_id') else set())
        now = self.ops.now()
        with self.ops.store.read() as c:
            rows = [dict(r) for r in c.execute("SELECT id,kind,summary,expires,thread,created FROM ops_proposals "
                                               "WHERE state='pending' ORDER BY created")]
        return [r for r in rows if (r['thread'] == thread or r['id'] in ids) and
                (r['expires'] > now or r['kind'] in ('approve_caption', 'approve_captions'))]

    def _open_choice(self, thread, kind, actor, event_id) -> str | None:
        open_ = self._open_proposals(thread)
        if len(open_) == 1:
            return self._decide([open_[0]['id']], kind, actor, thread, event_id)
        if len(open_) > 1:
            # Several open proposals: one question, nothing created or approved (R5 M26).
            return ('❓ فيه أكتر من مقترح مفتوح هنا، أنهي واحد؟\n' +
                    '\n'.join(f"• {p['id']}: {(p['summary'] or '').splitlines()[0][:120]}" for p in open_) +
                    '\nاكتب «اعتمد» ورقم المقترح.')
        return None

    def _decide(self, ids, kind, actor, thread, event_id) -> str:
        out = []
        for pid in ids:
            r = self.bridge.approve(pid, actor, thread, event_id, reject=kind == 'reject')
            if r.get('state') == 'completed':
                out.append(('❌ Rejected ' if kind == 'reject' else '✅ Executed ') + pid +
                           ('' if kind == 'reject' else self._summary(r)))
            else:
                out.append(f"⛔ {pid} not executed: {r.get('reason', r.get('state'))}")
        p = self._pending(thread)
        if p and set(p.get('ids') or []) & set(ids):
            self._set_pending(thread, None)
        return '\n'.join(out)

    def _show(self, thread) -> str | None:
        """'ابعتهولي تاني': the draft/proposal shown in this thread, again, without a model call (R5 M23, A8)."""
        open_ = self._open_proposals(thread)
        if not open_:
            return None
        p = open_[-1]
        return f"{p['summary']}\n\nللاعتماد: «اعتمدها» أو `اعتمد {p['id']}`"

    def _alert_reply(self, text, actor, thread, event_id) -> str | None:
        """Short owner replies under an unknown-outcome notice: "published" records an owner report; "not
        published" asks once (it re-opens scheduling). Anything else goes to the model (R5 M21)."""
        ctx = self.store.kv('thread_ctx:' + thread) or {}
        iid = ctx.get('item_id')
        if not iid or len(lang.tokens(text)) > 8 or (self.bridge.items_named(text) - {iid}):
            return None
        with self.ops.store.read() as c:
            it = self.ops.item(c, iid, required=False)
        if not it or it['publication'] != 'outcome_unknown':
            return None
        label = f"{it['name']} ({it['code'] or iid})"
        if lang.supports('report_published', text, text) and lang.contradiction('report_published', text, text) is None:
            act = {'tool': 'report_published', 'op': 'report_published', 'item': iid,
                   'args': {'source': 'slack reply to the unknown-outcome notice', 'value': text[:300]}}
            r = self.bridge.execute(act, actor=actor, cid=f'slack:{event_id}:alert', thread=thread, event_id=event_id)
            return label + '\n' + render_outcome({'tool': 'report_published', 'args': {}, 'result': r})
        toks = lang.tokens(text)
        if lang.has(toks, lang.COMPLETED_PUBLISH) and lang.markers(text)['negation']:
            q = f'Record that {label} was NOT published on Instagram (it becomes schedulable again)؟'
            act = {'tool': 'resolve_publication', 'op': 'resolve_outcome', 'item': iid,
                   'args': {'outcome': 'not_published', 'media_id': None}}
            self._set_pending(thread, {'kind': 'actions', 'question': q, 'actions': [act]})
            return '❓ ' + q + ' (رد بـ «اه» أو «لا»)'
        return None

    def _note_proposals(self, thread, outcomes):
        ids = [o['result'].get('proposal_id') for o in outcomes
               if isinstance(o.get('result'), dict) and o['result'].get('state') == 'awaiting_approval']
        ids = [i for i in ids if i]
        if ids:
            self.store.kv('thread_props:' + thread, list(dict.fromkeys((self.store.kv('thread_props:' + thread) or [])
                                                                       + ids))[-20:])

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
        """What an executed approval actually did (R5 M22): moved slots, the new slot, the format, and a
        truthful "may already be in progress"."""
        from waset_ops import rules
        parts = []
        if r.get('moved'):
            parts.append('; '.join(f"{m['item_id']} → {rules.display(rules.instant(m['slot']))}" for m in r['moved']))
        if r.get('format'):
            parts.append(f"format is now {r['format']}")
        if r.get('scheduled'):
            parts.append('scheduled ' + rules.display(rules.instant(r['scheduled'])))
        if r.get('publication') == 'may_already_be_in_progress':
            parts.append('publication may already be in progress; the outcome will be reported')
        elif r.get('waiting'):
            parts.append('not scheduled yet: ' + str(r['waiting']))
        return (': ' + '; '.join(parts)) if parts else ''

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
                if ts and not p.get('thread') and (p.get('proposal_id') or job.get('item_id')):
                    # Replies under a proposal or an item alert reach Bondok and bind to it (audit H3, R5 M21).
                    self.store.kv('thread:' + ts, True)
                    self.store.kv('thread_ctx:' + ts, {'item_id': job.get('item_id'),
                                                       'proposal_id': p.get('proposal_id')})
                    self.store.remember(ts, 'assistant', p['text'])
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
        if self._new_offset is not None:               # helper errors reported: advance only now (#13, R5 M20)
            self._commit_errors(self._new_offset)

    def _flip(self, key, value) -> bool:
        """Record a boolean state; True when it changed (first observation of a
        healthy state is not a change worth reporting)."""
        prev = self.store.kv('wd:' + key)
        self.store.kv('wd:' + key, value)
        changed = prev != value and not (prev is None and value is False)
        if changed:
            self._undo.append((key, prev))      # restored if the alert cannot be posted (audit M1)
        return changed

    @staticmethod
    def _read_lines(path, offset, final=False):
        """Complete lines from `offset` (a partial last line is left for the next read unless the file is
        final, e.g. rotated away) and the offset after them."""
        with open(path, 'rb') as f:
            f.seek(offset)
            data = f.read()
        if not final:
            data = data[:data.rfind(b'\n') + 1]
        return data.decode('utf-8', 'replace').splitlines(), offset + len(data)

    def _helper_errors(self) -> list[str]:
        """errors.log is written by helper.py without SQLite, so failures stay visible even when the database is
        the problem. Every distinct failure (path, kind) is reported; repeats of one failure are coalesced for an
        hour and then reported with their count; nothing is consumed before the alert is posted; a rotation keeps
        the unread tail of the old file; a partial last line waits (R5 M20)."""
        log = Path(self.env['PIPELINE_DB']).with_name('errors.log')
        try:
            st = log.stat()
        except OSError:
            return []
        offset = self.store.kv('errors_offset') or 0
        inode = self.store.kv('errors_inode')
        raw = []
        if inode is not None and st.st_ino != inode:
            rotated = log.with_name('errors.log.1')
            try:
                if rotated.stat().st_ino == inode:
                    raw += self._read_lines(rotated, offset, final=True)[0]
            except OSError:
                pass
            offset = 0
        elif st.st_size < offset:
            offset = 0
        try:
            lines, new_offset = self._read_lines(log, offset)
        except OSError:
            return []
        raw += lines
        entries = []
        for x in raw:
            if not x.strip():
                continue
            try:
                entries.append(json.loads(x)) if x.strip().startswith('{') else None
            except ValueError:
                entries.append({'kind': 'unreadable', 'path': 'errors.log', 'error': x[:200]})  # audit M1
        state = self.store.kv('errclass') or {}
        for x in entries:
            if not isinstance(x, dict) or x.get('kind') in ('rejected', 'stale', 'fenced', 'slot_taken', 'retired'):
                continue
            k = f"{x.get('path')}|{x.get('kind')}"
            s = state.setdefault(k, {'path': x.get('path'), 'kind': x.get('kind'), 'last': None, 'pending': 0})
            s['pending'] += 1
            s['error'] = str(x.get('error'))[:200]
        now = time.time()
        due = sorted(k for k, s in state.items() if s['pending'] and (s['last'] is None or now - s['last'] >= 3600))
        alerts = []
        if due:
            alerts.append('⚠️ Automation helper errors: ' + '; '.join(
                f"{state[k]['path']} {state[k]['kind']} ×{state[k]['pending']}: {state[k].get('error')}" for k in due))
            for k in due:
                state[k].update(last=now, pending=0)
        pending = (new_offset, st.st_ino, state)
        if self._defer_offset:
            self._new_offset = pending
        else:
            self._commit_errors(pending)
        return alerts

    def _commit_errors(self, pending):
        offset, inode, state = pending
        self.store.kv('errors_offset', offset)
        self.store.kv('errors_inode', inode)
        self.store.kv('errclass', state)


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
