"""Owner-approved acceptance run through Bondok's real chat path (live verification of the deployed agent).

The owner sends one Slack message in Bondok's channel: ``اختبار قبول <script id> <first 12 hex of its sha256>``.
Slack authenticates that message as the owner's; the hash binds the authorization to one exact script. The script
(JSON, written by the operator) must be owned by root and not writable by group/others, in a root-owned directory,
so neither Bondok's own process nor board or file content can change what the owner approved.

Each step is one owner command from that script. It is shown in the thread (posted by the bot, marked as a step of
the approved script) and then goes through the same queue, deduplication, model, tools, handler and reply as a
typed message, with the identity of the owner who approved the script. Nothing else is different: no scripted
model, no direct handler call. A step can be delivered twice (Slack redelivery check). The run waits away from any
live publication (``quiet_minutes`` before and after a reserved slot that is not one of the script's items) and stops
at ``اوقف اختبار القبول``. Every step is logged (event key, times, reply) beside Bondok's conversation store.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import time
from pathlib import Path

TRIGGER = re.compile(r'^\s*اختبار\s+قبول\s+([A-Za-z0-9_.-]{3,64})\s+([0-9a-f]{12})\s*$')
STOP = re.compile(r'^\s*(?:اوقف|أوقف|وقف)\s+اختبار\s+القبول\s*$')
DEFAULT_DIR = '/opt/waset-bondok/acceptance'


class Refused(Exception):
    pass


def _trusted(path: Path):
    """The script and its directory belong to root and only root can write them."""
    for p in (path.parent, path):
        st = p.lstat()
        if stat.S_ISLNK(st.st_mode):
            raise Refused(f'{p.name} is a symbolic link')
        if st.st_uid != 0 or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise Refused(f'{p.name} must be owned by root and writable only by root')


def load(directory, script_id, digest, trust=_trusted) -> dict:
    path = Path(directory) / f'{script_id}.json'
    if not path.is_file():
        raise Refused(f'no approved script {script_id}')
    trust(path)
    raw = path.read_bytes()
    full = hashlib.sha256(raw).hexdigest()
    if full[:12] != digest:
        raise Refused(f'the hash does not match script {script_id} (expected {full[:12]})')
    spec = json.loads(raw)
    if spec.get('id') != script_id or not isinstance(spec.get('steps'), list) or not spec['steps']:
        raise Refused('script id or steps missing')
    for s in spec['steps']:
        if not isinstance(s.get('text'), str) or not s['text'].strip() or not isinstance(s.get('thread'), str):
            raise Refused('every step needs a thread label and the owner text')
    spec['sha256'] = full
    return spec


def live_slots(ops) -> list[float]:
    """Reserved times of real content (test items isolated from Instagram excluded), read now."""
    from waset_ops import rules
    with ops.store.read() as c:
        return [rules.instant(r['slot']).timestamp() for r in c.execute(
            "SELECT r.slot, r.item_id FROM ops_reservations r JOIN ops_items i USING(item_id) "
            "WHERE i.publication='not_started'").fetchall() if not ops.send_isolated(c, r['item_id'])]


def window_text(ops, quiet_minutes, now) -> str:
    from waset_ops import rules
    import datetime as _dt
    future = sorted(s for s in live_slots(ops) if s > now - quiet_minutes * 60)
    if not future:
        return 'لا يوجد نشر حقيقي محجوز.'
    nxt = rules.display(_dt.datetime.fromtimestamp(future[0], _dt.timezone.utc))
    near = future[0] - now < quiet_minutes * 60
    return (f'أقرب نشر حقيقي: {nxt}.' + (f' الاختبار هيستنى لحد ما يعدي بـ{quiet_minutes} دقيقة.' if near else
                                        f' الاختبار بيتوقف تلقائيًا لو قرب نشر حقيقي بأقل من {quiet_minutes} دقيقة.'))


class Run:
    def __init__(self, core, spec, *, pump=None, sleep=time.sleep, clock=time.time):
        self.core, self.spec = core, spec
        self.pump, self.sleep, self.clock = pump, sleep, clock
        self.roots: dict[str, str] = {}
        self.log_path = Path(core.env.get('BONDOK_STATE_DIR', '/var/lib/bondok')) / 'acceptance' / \
            f"{spec['id']}-{int(clock())}.jsonl"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- helpers
    def log(self, **kw):
        with open(self.log_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'at': self.clock(), **kw}, ensure_ascii=False) + '\n')

    def stopped(self) -> bool:
        return bool(self.core.store.kv('acceptance:stop'))

    def _quiet(self):
        """Wait while a live publication (not one of the script's items) is due within quiet_minutes."""
        quiet = int(self.spec.get('quiet_minutes', 45)) * 60
        while not self.stopped():
            now = self.clock()
            slots = live_slots(self.core.ops)
            near = [s for s in slots if -quiet < s - now < quiet]
            if not near:
                return
            self.log(waiting_for_live_slot=max(near))
            self.sleep(min(300, max(30, max(near) + quiet - now)))

    def _wait_item(self, needle, timeout, readiness=None):
        """Until an item with this name or code is in the store (and, with ``readiness``, in that state)."""
        end = self.clock() + timeout
        while self.clock() < end and not self.stopped():
            with self.core.ops.store.read() as c:
                hit = c.execute('SELECT item_id, readiness FROM ops_items WHERE name=? OR upper(code)=upper(?)',
                                (needle, needle)).fetchone()
            if hit and (readiness is None or hit['readiness'] == readiness):
                return hit['item_id']
            self.sleep(15)
        return None

    def _event_state(self, key):
        with self.core.store.db() as c:
            r = c.execute('SELECT state, reply FROM events WHERE id=?', (key,)).fetchone()
        return (r['state'], r['reply']) if r else (None, None)

    def _deliver(self, key, event, timeout=600):
        fresh = self.core.store.enqueue_event(key, event)
        self.core.wake.set()
        end = self.clock() + timeout
        while self.clock() < end:
            if self.pump:
                self.pump()
            state, reply = self._event_state(key)
            if state in ('done', 'send_uncertain'):
                return fresh, state, reply
            self.sleep(2)
        return fresh, 'timeout', None

    # ---------------------------------------------------------------- run
    def run(self):
        env, n = self.core.env, len(self.spec['steps'])
        self.core.store.kv('acceptance:stop', False)
        self.core.post(f"🧪 {self.spec['id']}: بدأ ({n} خطوة).", None)
        self.log(start=self.spec['id'], sha256=self.spec['sha256'], steps=n)
        if self.spec.get('not_before'):
            from waset_ops import rules
            start = rules.instant(self.spec['not_before']).timestamp()
            while self.clock() < start and not self.stopped():
                self.sleep(min(300, start - self.clock()))
        pkey = f"acceptance:progress:{self.spec['id']}:{self.spec['sha256'][:12]}"
        prog = self.core.store.kv(pkey) or {'done': [], 'roots': {}, 'posted': {}}
        self.roots.update(prog.get('roots') or {})
        self.core.store.kv('acceptance:active', {'id': self.spec['id'], 'sha': self.spec['sha256'][:12]})
        for i, step in enumerate(self.spec['steps'], 1):
            if i in prog['done']:
                continue                                   # resumed run: a step with effect is never repeated
            if self.stopped():
                self.log(stopped_before=i)
                break
            self._quiet()
            if self.stopped():
                self.log(stopped_before=i)
                break
            if step.get('wait_item') or step.get('wait_ready'):
                needle = step.get('wait_item') or step['wait_ready']
                found = self._wait_item(needle, int(step.get('timeout', 900)),
                                        'ready' if step.get('wait_ready') else None)
                self.log(step=i, wait_for=needle, ready=bool(step.get('wait_ready')), found=found)
            if step.get('wait_seconds'):
                self.sleep(int(step['wait_seconds']))
            label = step['thread']
            root = self.roots.get(label)
            ts = (prog['posted'].get(str(i)) or {}).get('ts')     # posted before a restart: the same event again
            if not ts:
                shown = f"🧪 {self.spec['id']} · {i}/{n} · أمر المالك من خطة الاختبار المعتمدة " \
                        f"({self.spec['sha256'][:12]}):\n{step['text']}"
                posted = self.core.post(shown, root)
                ts = (posted or {}).get('ts')
                if not ts:
                    self.log(step=i, error='could not post the step')
                    break
                prog['posted'][str(i)] = {'ts': ts, 'root': root}
                self.core.store.kv(pkey, prog)
            else:
                root = prog['posted'][str(i)].get('root')
            if root is None:
                self.roots[label] = root = ts
                prog['roots'] = self.roots
                self.core.store.kv('thread:' + ts, True)
                self.core.store.kv(pkey, prog)
            event = {'user': self.core.owner, 'text': step['text'], 'ts': ts, 'channel': env['SLACK_CHANNEL_ID'],
                     'thread_ts': None if root == ts else root, 'type': 'message',
                     'acceptance': {'script': self.spec['id'], 'sha256': self.spec['sha256'], 'step': i}}
            key = f"{env['SLACK_TEAM_ID']}:{env['SLACK_CHANNEL_ID']}:{ts}"
            t0 = self.clock()
            fresh, state, reply = self._deliver(key, event)
            entry = {'step': i, 'thread': label, 'text': step['text'], 'event': key, 'queued': fresh,
                     'state': state, 'seconds': round(self.clock() - t0, 1), 'reply': reply}
            if step.get('redeliver'):
                again = self.core.store.enqueue_event(key, event)
                entry['redelivered_accepted'] = again           # False: the duplicate collapsed before any work
            self.log(**entry)
            prog['done'].append(i)
            self.core.store.kv(pkey, prog)
        else:
            self.core.store.kv('acceptance:active', False)
        self.log(end=self.spec['id'])
        self.core.post(f"🧪 {self.spec['id']}: انتهى التشغيل. السجل: {self.log_path.name}", None)


def resume(core) -> bool:
    """After a restart, an approved run that had not finished continues with the same script and hash (the owner's
    authorization is bound to that exact script; nothing is widened), from the first step not done."""
    act = core.store.kv('acceptance:active')
    if not act:
        return False
    try:
        spec = load(core.env.get('ACCEPTANCE_DIR') or DEFAULT_DIR, act['id'], act['sha'],
                    trust=getattr(core, 'acceptance_trust', _trusted))
    except (Refused, OSError, ValueError):
        core.store.kv('acceptance:active', False)
        return False
    core._acceptance = threading.Thread(target=Run(core, spec).run, daemon=True, name='bondok-acceptance')
    core._acceptance.start()
    return True


def handle(core, text, actor) -> str | None:
    """Owner trigger/stop messages; None when the text is not one of them."""
    if STOP.match(text or ''):
        if actor != core.owner:
            return None
        core.store.kv('acceptance:stop', True)
        return 'هوقف اختبار القبول بعد الخطوة الجارية.'
    m = TRIGGER.match(text or '')
    if not m:
        return None
    if actor != core.owner:
        return 'اختبار القبول يبدأ برسالة من المالك فقط.'
    if getattr(core, '_acceptance', None) and core._acceptance.is_alive():
        return 'فيه اختبار قبول شغال بالفعل.'
    try:
        spec = load(core.env.get('ACCEPTANCE_DIR') or DEFAULT_DIR, m[1], m[2],
                    trust=getattr(core, 'acceptance_trust', _trusted))
    except Refused as e:
        return f'❌ مش هبدأ الاختبار: {e}.'
    except (OSError, ValueError) as e:
        return f'❌ مش هبدأ الاختبار: {type(e).__name__}.'
    with core.ops.store.read() as c:
        iso = core.ops.isolation(c)
    if spec.get('require_isolation', True) and not iso:
        return '❌ مش هبدأ الاختبار: عناصر الاختبار مش معزولة عن Instagram.'
    run = Run(core, spec)
    window = window_text(core.ops, int(spec.get('quiet_minutes', 45)), core.ops.now())   # checked now, at the trigger
    core._acceptance = threading.Thread(target=run.run, daemon=True, name='bondok-acceptance')
    core._acceptance.start()
    return (f"🧪 بدأ اختبار القبول {spec['id']} ({len(spec['steps'])} خطوة، sha256 {spec['sha256'][:12]}). كل خطوة "
            'هتظهر في ثريد خاص بيها ومعاها رد بندق. عناصر الاختبار معزولة عن الإرسال لـInstagram. ' + window +
            ' «اوقف اختبار القبول» يوقفه.')
