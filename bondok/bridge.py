"""Trusted adapter between Slack/model tools and the shared command handler.

Authority never comes from model output (contract §1, §4):
* actor and actor kind come from the authenticated Slack event;
* the model proposes structured meaning for each protected call (speech act, polarity, targets as said, an
  evidence quote); this module executes it only when the deterministic reader (language.py), looking at the
  owner's own words, finds nothing contradicting it, the words support that kind of action, the targets come
  from the owner's words or the thread's named set, and the constraints said travel with it. Anything else
  becomes one narrow question (never an unrequested action);
* idempotency ids derive from the Slack event, so redelivery and model retries cannot repeat an effect.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date

import language as lang
from waset_ops import Command, Ops, Rejected, rules
from waset_ops.db import dumps

explicit = lang.explicit          # deterministic reading (kept for callers and tests)

RECENT_FILE_CHANGE = 6 * 3600
# Tools that change state. All but the last three are protected owner decisions and need structured meaning.
PROTECTED = {'pause_item', 'resume_item', 'skip_item', 'request_reschedule', 'request_publish', 'change_format',
             'replace_source', 'update_caption', 'approve_existing_caption', 'approve_caption_draft', 'confirm_topaz',
             'resolve_publication', 'report_published', 'set_window', 'request_rework'}
MUTATING = PROTECTED | {'request_recheck', 'draft_caption', 'approve_all_existing_captions'}
EXECUTABLE_ACTS = ('imperative', 'completed_statement')
# Reader kind per tool (language.contradiction / supports).
KIND = {'pause_item': 'pause', 'skip_item': 'skip', 'resume_item': 'resume', 'request_publish': 'request_publish',
        'request_reschedule': 'request_reschedule', 'change_format': 'change_format', 'replace_source': 'replace_source',
        'update_caption': 'update_caption', 'approve_existing_caption': 'approve_caption',
        'approve_caption_draft': 'approve_caption', 'confirm_topaz': 'confirm_topaz',
        'report_published': 'report_published', 'set_window': 'set_window', 'request_rework': 'request_rework'}


def key(text: str) -> str:
    """Matching form of a name/code: normalised letters and digits, single spaces."""
    return re.sub(r'\s+', ' ', lang.norm(text or '')).strip()


def _bounded(needle: str, hay: str) -> bool:
    return bool(needle) and re.search(r'(?<![^\W_])' + re.escape(needle) + r'(?![^\W_])', hay) is not None


class Turn:
    """What trusted code knows about one owner message while the model's tool calls run: the owner's words,
    the thread's named set, and the single open question being built for this thread."""

    def __init__(self, text: str, thread: str, today: date, context_items=(), history_text=''):
        self.text, self.thread, self.today = text or '', thread, today
        self.context_items = set(context_items)
        self.history_text = history_text
        self.questions: list[str] = []
        self.actions: list[dict] = []
        self._named_now = self._named_set = None

    def ask(self, question: str, action: dict | None = None):
        if action and action not in self.actions:
            self.actions.append(action)
        if question not in self.questions:
            self.questions.append(question)

    def pending(self) -> dict | None:
        if not self.actions:
            return None
        return {'kind': 'actions', 'question': '\n'.join(self.questions), 'actions': self.actions}


class Bridge:
    def __init__(self, ops: Ops, owner_id: str, store=None):
        self.ops, self.owner, self.store = ops, owner_id, store

    # ------------------------------------------------------------------ resolution
    def resolve(self, ref) -> dict:
        """Item reference -> exactly one registered social-board item, or candidates. Names and codes are compared
        in normalised form (Arabic-Indic digits, ى/ي, ة/ه, diacritics, zero-width marks); two items that normalise
        to the same name are ambiguous, never chosen between (R5 M27)."""
        raw = str(ref or '').strip()
        k = key(raw)
        with self.ops.store.read() as c:
            if re.fullmatch(r'[0-9]{1,20}', k):
                r = c.execute('SELECT item_id FROM ops_items WHERE item_id=?', (k,)).fetchone()
                if r:
                    return {'item_id': r['item_id']}
                return {'error': 'This id is not an item of the For Social Media board'}
            rows = c.execute('SELECT item_id,name,code,format,owner_state,publication FROM ops_items').fetchall()
        nospace = k.replace(' ', '')
        exact = [r for r in rows if r['code'] and key(r['code']).replace(' ', '') == nospace]
        named = [r for r in rows if k and key(r['name']) == k]
        # An exact name wins over partial matches ("Calli 3" is not "Calli 30"); a partial match is used only when
        # it is unique AND bounded by word edges (audit M3).
        hits = exact or named or [r for r in rows if _bounded(k, key(r['name']))]
        if len(hits) == 1:
            return {'item_id': hits[0]['item_id']}
        if not hits:
            return {'error': 'No social-board item matches; ask for the item name, code or id'}
        return {'ambiguous': [{'item_id': r['item_id'], 'name': r['name'], 'code': r['code'], 'format': r['format'],
                               'publication': r['publication']} for r in hits[:8]]}

    def items_named(self, text: str) -> set[str]:
        """Registered items whose code, id or full name appears (word-bounded) in `text`."""
        t = key(text)
        if not t:
            return set()
        with self.ops.store.read() as c:
            rows = c.execute('SELECT item_id,name,code FROM ops_items').fetchall()
        out = set()
        for r in rows:
            code, name = key(r['code']), key(r['name'])
            if (code and len(code) >= 2 and _bounded(code, t)) or (len(r['item_id']) >= 3 and _bounded(r['item_id'], t)) \
                    or (name and len(name) >= 4 and _bounded(name, t)):
                out.add(r['item_id'])
        return out

    def named_now(self, turn: Turn) -> set[str]:
        if turn._named_now is None:
            turn._named_now = self.items_named(turn.text)
        return turn._named_now

    def named_set(self, turn: Turn) -> set[str]:
        """The thread's named set: items named in the thread (owner, members, Bondok, the notification it
        answers) plus the current message."""
        if turn._named_set is None:
            turn._named_set = self.items_named(turn.history_text) | self.named_now(turn) | set(turn.context_items)
        return turn._named_set

    def remember_shown(self, thread, item_id, asset_key):
        """The file version the owner was shown in this thread (Topaz binds to it, R5 M25)."""
        if self.store and thread:
            shown = self.store.kv('shown:' + thread) or {}
            shown[str(item_id)] = asset_key
            self.store.kv('shown:' + thread, shown)

    def _label(self, item_id) -> str:
        with self.ops.store.read() as c:
            it = self.ops.item(c, item_id)
        return f"{it['name']} ({it['code'] or it['item_id']})" if it['code'] and it['code'] not in (it['name'] or '') \
            else f"{it['name']} ({it['item_id']})"

    # ------------------------------------------------------------------ execution
    def execute(self, action: dict, *, actor, cid, thread, event_id) -> dict:
        """Submit one validated owner decision to the handler (the only writer)."""
        return self.ops.submit(Command(cid, action['op'], actor, 'owner', action['item'], action['args'],
                                       auth={'thread': thread, 'event': event_id, 'explicit': True}))

    def run(self, tool: str, args: dict, *, actor: str, event_id: str, text: str, thread: str,
            turn: Turn | None = None) -> dict:
        is_owner = actor == self.owner
        if tool in MUTATING and not is_owner:
            return {'state': 'rejected', 'reason': 'Only the owner can request changes; I can explain or draft.'}
        args = dict(args or {})
        meaning = args.pop('meaning', None)
        if turn is None:
            turn = Turn(text, thread, self.today())
        item = None
        if args.get('item') is not None:
            r = self.resolve(args['item'])
            if 'item_id' not in r:
                return {'state': 'needs_clarification', **r}
            item = r['item_id']
        h = hashlib.sha256(dumps({'tool': tool, 'args': args}).encode()).hexdigest()[:16]
        cid = f'slack:{event_id}:{h}'
        auth = {'thread': thread, 'event': event_id}

        if tool == 'request_recheck':      # harmless: re-reads the file, decides nothing
            return self.ops.submit(Command(cid, 'request_recheck', actor, 'owner', item, {}, auth=auth))
        if tool == 'approve_all_existing_captions':    # only creates one proposal; nothing changes until approved
            with self.ops.store.tx() as c:
                return {'state': 'awaiting_approval', **self.ops.propose_legacy_captions(c, actor, thread)}
        if tool == 'draft_caption':
            return self._draft(item, args, cid)
        if tool not in PROTECTED:
            raise Rejected('Unknown tool', 'unsupported')
        if item is None:
            return {'state': 'needs_clarification', 'error': 'Which item? Name, code or id.'}

        if not isinstance(meaning, dict) or not meaning.get('speech_act'):
            return {'state': 'rejected', 'reason': 'Not executed: the call carried no structured meaning '
                                                   '(speech_act, polarity, targets_as_said, evidence_quote).'}
        # ---- the model's proposal, made concrete
        try:
            action, summary = self._plan(tool, item, args, meaning, turn)
        except Rejected as e:
            return {'state': 'rejected', 'reason': e.reason}
        speech = str(meaning.get('speech_act'))
        if speech == 'bondok_offer':
            # Bondok's own suggestion: one question naming the exact action; "اه" executes exactly this.
            q = f'{summary}؟'
            turn.ask(q, action)
            return {'state': 'needs_confirmation', 'question': q}
        if speech not in EXECUTABLE_ACTS:
            return self._clarify(f'{summary}: not done, your message was not an instruction to do it ({speech}).')
        if meaning.get('polarity') == 'negative':
            return self._clarify(f'{summary}: not done, your words say not to.')

        # ---- the owner's words decide
        quote = str(meaning.get('evidence_quote') or '').strip()
        if not quote or lang.markers(text, quote).get('located') is not True:
            return self._clarify(f'{summary}: not done, I could not find that request in your message. '
                                 'Say exactly what to do.')
        if tool == 'resolve_publication' and args.get('published') is not True:
            # "Not published" re-opens scheduling and could cause a duplicate post: always one confirmation.
            q = f'{summary} (it becomes schedulable again)؟'
            turn.ask(q, action)
            return {'state': 'needs_confirmation', 'question': q}
        kind = 'resolve_published' if tool == 'resolve_publication' else KIND[tool]
        why = lang.contradiction(kind, text, quote)
        if why:
            if why == 'it mentions another account or platform' and kind in ('report_published', 'resolve_published'):
                q = f'Was it published on the studio Instagram account? If yes: {summary}'
                turn.ask(q + '؟', action)
                return {'state': 'needs_confirmation', 'question': q + '؟'}
            return self._clarify(f'{summary}: not done, {why}.')
        if tool == 'report_published' and args.get('destination') not in (None, 'studio_account'):
            q = f'Was it published on the studio Instagram account? If yes: {summary}؟'
            turn.ask(q, action)
            return {'state': 'needs_confirmation', 'question': q}
        if not lang.supports(kind, text, quote, action['args'] if kind == 'update_caption' else args):
            return self._clarify(f'{summary}: not done, your words do not ask for that. Which item, and what '
                                 'exactly should I do?')

        # ---- targets: the owner's words and the thread's named set, never extra items (R5 B6, A14)
        m = lang.markers(text, quote)
        named = self.named_set(turn)
        if lang.broad(m['sentence']):
            if item in named:
                q = f'{summary}؟'
                turn.ask(q, action)
                return {'state': 'needs_confirmation', 'question': q}
            return self._clarify('Which items exactly? Name them (code or name); I changed nothing.')
        if not self._target_ok(item, m, turn):
            return self._clarify(f'{summary}: not done, you did not name this item here. Which item do you mean?')

        # ---- constraints said with the instruction travel with it (R5 A15)
        problem = self._constraints(tool, action, text, turn)
        if problem:
            return self._clarify(problem)

        # ---- operation-specific binding
        if tool == 'confirm_topaz':
            ask = self._topaz_needs_question(item, action, thread)
            if ask:
                turn.ask(ask, action)
                return {'state': 'needs_confirmation', 'question': ask}
        return self.execute(action, actor=actor, cid=cid, thread=thread, event_id=event_id)

    @staticmethod
    def _clarify(question: str) -> dict:
        return {'state': 'needs_clarification', 'question': question}

    def today(self) -> date:
        return self.ops.now_dt().astimezone(rules.TZ).date()

    def _target_ok(self, item, m, turn) -> bool:
        if item in self.named_now(turn):
            return True
        named = self.named_set(turn)
        ana = lang.anaphor(m['sentence'])
        if ana:
            return item in named and (ana == 'plural' or len(named) == 1)
        # No reference word at all: only the single item the thread is about.
        return not self.named_now(turn) and named == {item}

    def _plan(self, tool, item, args, meaning, turn) -> tuple[dict, str]:
        """(action, summary) for a protected tool call."""
        label = self._label(item)
        act = {'tool': tool, 'item': item}
        if tool == 'pause_item':
            return {**act, 'op': 'pause', 'args': {'reason': args.get('reason') or 'Paused by owner in Slack'}}, \
                f'Pause {label}'
        if tool == 'skip_item':
            return {**act, 'op': 'skip', 'args': {'reason': args.get('reason') or 'Skipped by owner in Slack'}}, \
                f'Skip {label} (it will not publish until resumed)'
        if tool in ('resume_item', 'request_publish', 'set_window'):
            win = self._model_window(args)
            op = {'resume_item': 'resume', 'request_publish': 'request_publish', 'set_window': 'set_window'}[tool]
            verb = {'resume': 'Resume', 'request_publish': 'Publish', 'set_window': 'Set the publication window of'}[op]
            return {**act, 'op': op, 'args': win}, f'{verb} {label}' + self._window_text(win)
        if tool == 'request_reschedule':
            local = args.get('cairo_time')
            try:
                d = rules.cairo_local(*map(int, re.match(r'(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})$',
                                                         str(local)).groups()))
            except (TypeError, AttributeError, rules.RuleError) as e:
                raise Rejected(f'Time must be Cairo time as YYYY-MM-DD HH:MM ({e})')
            return {**act, 'op': 'request_reschedule', 'args': {'at': rules.iso(d)}}, \
                f'Schedule {label} at {rules.display(d)}'
        if tool == 'change_format':
            fmt = args.get('format')
            return {**act, 'op': 'change_format', 'args': {'format': fmt}}, \
                f'Change {label} to {fmt} (schedule and media approval reset; the same item is rechecked)'
        if tool == 'replace_source':
            return {**act, 'op': 'replace_source', 'args': {'url': args.get('url')}}, f'Use the new file link for {label}'
        if tool == 'update_caption':
            try:
                text = lang.decode_slack(args.get('text') or '', strict=True).strip()
            except lang.SlackMarkupError as e:
                raise Rejected(f'Caption not stored: {e}')
            return {**act, 'op': 'update_caption', 'args': {'text': text}}, f'Set this caption for {label}:\n\n{text}'
        if tool == 'approve_existing_caption':
            with self.ops.store.read() as c:
                it = self.ops.item(c, item)
            if not it['caption']:
                raise Rejected('This item has no caption to approve')
            return {**act, 'op': 'update_caption', 'args': {'text': it['caption']}}, \
                f"Approve the existing caption of {label}:\n\n{it['caption']}"
        if tool == 'approve_caption_draft':
            with self.ops.store.read() as c:
                d = c.execute("SELECT text FROM ops_caption_drafts WHERE item_id=? AND state='pending_approval' "
                              'ORDER BY created DESC LIMIT 1', (item,)).fetchone()
            if not d:
                raise Rejected('There is no caption draft waiting for approval for this item')
            return {**act, 'op': 'approve_caption_draft', 'args': {'text': d['text']}}, \
                f"Approve the caption draft of {label}:\n\n{d['text']}"
        if tool == 'confirm_topaz':
            with self.ops.store.read() as c:
                it = self.ops.item(c, item)
            return {**act, 'op': 'confirm_topaz', 'args': {'confirmed': True, 'asset_key': it['asset_key']}}, \
                f"Record Topaz as done for {label}, file {it.get('file_name') or '?'} " \
                f"(version {it['asset_key'] or 'none selected'})"
        if tool == 'resolve_publication':
            outcome = 'published' if args.get('published') is True else 'not_published'
            return {**act, 'op': 'resolve_outcome', 'args': {'outcome': outcome, 'media_id': args.get('media_id')}}, \
                f"Record that {label} was {outcome.replace('_', ' ')} on Instagram"
        if tool == 'report_published':
            quote = str(meaning.get('evidence_quote') or '')[:300]
            return {**act, 'op': 'report_published', 'args': {'source': 'slack', 'value': quote}}, \
                f'Record {label} as published by you on the studio account (it will not be published again)'
        if tool == 'request_rework':
            return {**act, 'op': 'request_rework',
                    'args': {'reason': args.get('reason') or 'The owner asked for a new edit'}}, \
                f'Send {label} back to the editor for a new edit'
        raise Rejected('Unknown tool', 'unsupported')

    # ------------------------------------------------------------------ constraints
    @staticmethod
    def _model_window(args) -> dict:
        win = {}
        if args.get('on_date'):
            win['on_date'] = str(args['on_date']).strip()
        if args.get('not_before'):
            nb = str(args['not_before']).strip()
            m = re.match(r'(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?$', nb)
            if m:
                y, mo, d, hh, mm = (int(x) if x else 0 for x in m.groups())
                win['not_before'] = rules.iso(rules.cairo_local(y, mo, d, hh, mm))
            else:
                win['not_before'] = nb
        return win

    @staticmethod
    def _window_text(win) -> str:
        parts = []
        if win.get('on_date'):
            parts.append('on ' + win['on_date'])
        if win.get('not_before'):
            try:
                parts.append('not before ' + rules.display(rules.instant(win['not_before'])))
            except (ValueError, rules.RuleError):
                parts.append('not before ' + str(win['not_before']))
        return (' ' + ', '.join(parts)) if parts else ''

    def _constraints(self, tool, action, text, turn) -> str | None:
        """Day/time constraints in the owner's words must match the action; ones the model dropped are added.
        Returns a question when they disagree."""
        a = action['args']
        if tool in ('resume_item', 'request_publish', 'set_window'):
            said = lang.days(text, turn.today)
            on = {d.isoformat() for d, k in said if k == 'on'}
            nb = {d.isoformat() for d, k in said if k == 'not_before'}
            if a.get('on_date') and a['on_date'] not in on:
                return f"You did not say {a['on_date']}; which day should it be published?"
            if a.get('not_before'):
                try:
                    day = rules.instant(a['not_before']).astimezone(rules.TZ).date().isoformat()
                except (ValueError, rules.RuleError):
                    return 'Which day should it not be published before?'
                if day not in nb:
                    return f'You did not say "not before {day}"; from which day may it be published?'
            if not a.get('on_date') and on:
                if len(on) > 1:
                    return 'You named more than one day; which day should it be published?'
                a['on_date'] = next(iter(on))
            if not a.get('not_before') and nb:
                d = date.fromisoformat(min(nb))
                a['not_before'] = rules.iso(rules.cairo_local(d.year, d.month, d.day, 0, 0))
            if tool == 'set_window' and not a:
                return 'Which day should it be published, or not before which day?'
            return None
        if tool == 'request_reschedule':
            at = rules.instant(a['at']).astimezone(rules.TZ)
            said = lang.times(text)
            if not lang.time_matches(at.hour, at.minute, said):
                return (f"You did not say {at.strftime('%H:%M')}; what exact time (Cairo) should it be published?")
            days = {d.isoformat() for d, k in lang.days(text, turn.today) if k == 'on'}
            if days and at.date().isoformat() not in days:
                return f"You did not say {at.date().isoformat()}; which day should it be published?"
        return None

    def _topaz_needs_question(self, item, action, thread) -> str | None:
        """Topaz binds to the file version the owner was shown. If the owner was shown another version, or the
        selection changed recently and they were shown nothing, ask naming the exact file (audit MP5, R5 M25)."""
        with self.ops.store.read() as c:
            it = self.ops.item(c, item)
        current = it['asset_key']
        shown = (self.store.kv('shown:' + thread) or {}).get(str(item)) if self.store and thread else None
        changed = (json.loads(it['observed'] or '{}') or {}).get('_file_changed_at')
        recent = bool(changed and self.ops.now() - changed < RECENT_FILE_CHANGE)
        if shown == current and current:
            return None
        if shown is None and not recent:
            return None
        return (f"Was Topaz done on this exact file of {self._label(item)}: {it.get('file_name') or '?'} "
                f"(version {current or 'none selected'})? The selected file changed"
                + (' after I showed you the previous one' if shown else ' recently') + '.')

    # ------------------------------------------------------------------ drafts
    def _draft(self, item, args, cid) -> dict:
        text_ = (args.get('text') or '').strip()
        ih = hashlib.sha256(dumps({'item': item, 'bondok_draft': text_}).encode()).hexdigest()
        r = self.ops.submit(Command(cid, 'caption_draft', 'service:bondok', 'service:bondok', item,
                                    {'input_hash': ih, 'text': text_, 'model': 'bondok'}))
        if r.get('duplicate_draft'):
            # The same draft again: show the existing one, never an error (R5 M23).
            with self.ops.store.read() as c:
                d = c.execute('SELECT * FROM ops_caption_drafts WHERE input_hash=?', (ih,)).fetchone()
            if d and d['state'] == 'pending_approval' and d['proposal_id']:
                return {'state': 'awaiting_approval', 'proposal_id': d['proposal_id'], 'existing': True,
                        'summary': f"Caption draft for {self._label(item)} (already waiting for your approval):"
                                   f"\n\n{d['text']}"}
            return {'state': 'completed', 'message': f"This draft is already {d['state'] if d else 'recorded'}."}
        # A draft is a proposal (or an invalid draft), never "done" (audit M2).
        if r.get('draft_state') == 'pending_approval':
            return {**r, 'state': 'awaiting_approval'}
        if r.get('draft_state') == 'invalid':
            return {**r, 'state': 'rejected', 'reason': 'Draft not used: ' + (r.get('reason') or 'invalid')}
        return r

    # ------------------------------------------------------------------ approvals
    def approve(self, pid, actor, thread, event_id, reject=False) -> dict:
        if actor != self.owner:
            return {'state': 'rejected', 'reason': 'Only the owner can approve or reject proposals.'}
        op = 'reject_proposal' if reject else 'approve_proposal'
        return self.ops.submit(Command(f'slack:{event_id}:{op}:{pid}', op, actor, 'owner', None, {'proposal_id': pid},
                                       auth={'thread': thread}))
