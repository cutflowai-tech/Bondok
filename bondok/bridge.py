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
from datetime import date, timedelta

import language as lang
from waset_ops import Command, Ops, Rejected, rules
from waset_ops.db import dumps

explicit = lang.explicit          # deterministic reading (kept for callers and tests)

RECENT_FILE_CHANGE = 6 * 3600
# Tools that change state. All but the last three are protected owner decisions and need structured meaning.
PROTECTED = {'pause_item', 'resume_item', 'skip_item', 'request_reschedule', 'request_publish', 'change_format',
             'replace_source', 'update_caption', 'approve_existing_caption', 'approve_caption_draft', 'confirm_topaz',
             'resolve_publication', 'report_published', 'set_window', 'request_rework', 'waive_rule', 'restore_rule',
             'set_rule', 'publish_now', 'cancel_schedule', 'board_create_item', 'board_archive_item',
             'board_delete_item', 'set_item_field', 'file_action', 'block_publishing', 'allow_publishing'}
# Protected tools that are not about one item.
GLOBAL = {'set_rule', 'board_create_item'}
# Board/file administration and publish-now: read by the generic reader (language.contradiction 'generic').
GENERIC = {'publish_now', 'board_create_item', 'board_archive_item', 'board_delete_item', 'set_item_field',
           'file_action', 'block_publishing', 'allow_publishing'}
MUTATING = PROTECTED | {'request_recheck', 'draft_caption', 'approve_all_existing_captions'}
EXECUTABLE_ACTS = ('imperative', 'completed_statement')
# Reader kind per tool (language.contradiction / supports).
KIND = {'pause_item': 'pause', 'skip_item': 'skip', 'resume_item': 'resume', 'request_publish': 'request_publish',
        'request_reschedule': 'request_reschedule', 'change_format': 'change_format', 'replace_source': 'replace_source',
        'update_caption': 'update_caption', 'approve_existing_caption': 'approve_caption',
        'approve_caption_draft': 'approve_caption', 'confirm_topaz': 'confirm_topaz',
        'report_published': 'report_published', 'set_window': 'set_window', 'request_rework': 'request_rework',
        'waive_rule': 'waive_rule', 'restore_rule': 'restore_rule', 'set_rule': 'set_policy',
        'cancel_schedule': 'cancel_time', **{t: 'generic' for t in GENERIC},
        'block_publishing': 'publish_block', 'allow_publishing': 'publish_allow'}


def key(text: str) -> str:
    """Matching form of a name/code: normalised letters and digits, single spaces. An Arabic one-letter prefix
    written onto a Latin code or a number ("وLIP11", "لـACC15", "بACC2") is separated from it, so the code is a word of
    its own (owner review 2026-10-16: "وقف LIP10 وLIP11" paused only the first item)."""
    t = lang.norm(str(text or '').replace('ـ', ''))
    t = re.sub(r'(?<![^\W_])([وبلف])(?=[a-z0-9])', r'\1 ', t)
    return re.sub(r'\s+', ' ', t).strip()


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
        self.executed = 0                 # protected actions executed for this message (bulk limit, P1-06)
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
    def __init__(self, ops: Ops, owner_id: str, store=None, verifier=None):
        self.ops, self.owner, self.store = ops, owner_id, store
        self.verifier = verifier          # independent meaning check (verifier.py); None: the word rules decide

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

    def remember_files(self, thread, item_id, paths):
        """The files list_files showed for an item in this thread (file_action may act only on these or on a path
        the owner typed)."""
        if self.store and thread:
            f = self.store.kv('files:' + thread) or {}
            f[str(item_id)] = [p.lower() for p in paths]
            self.store.kv('files:' + thread, f)

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
    def execute(self, action: dict, *, actor, cid, thread, event_id, turn: Turn | None = None) -> dict:
        """Submit one validated owner decision to the handler (the only writer). A clear instruction with a defined
        scope runs for every item it names; there is no item-count limit."""
        if turn is not None:
            turn.executed += 1
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
            return self.ops.submit(Command(cid, 'propose_legacy_captions', actor, 'owner', None, {}, auth=auth))
        if tool == 'draft_caption':
            return self._draft(item, args, cid)
        if tool not in PROTECTED:
            raise Rejected('Unknown tool', 'unsupported')
        if item is None and tool not in GLOBAL:
            return {'state': 'needs_clarification', 'error': 'Which item? Name, code or id.'}

        if not isinstance(meaning, dict) or not meaning.get('speech_act'):
            return {'state': 'rejected', 'reason': 'Not executed: the call carried no structured meaning '
                                                   '(speech_act, polarity, targets_as_said, evidence_quote).'}
        # ---- the model's proposal, made concrete
        try:
            action, summary = self._plan(tool, item, args, meaning, turn)
        except Rejected as e:
            if e.code == 'question':
                return self._clarify(e.reason)
            return {'state': 'rejected', 'reason': e.reason}
        speech = str(meaning.get('speech_act'))
        if speech != 'bondok_offer' and self.verifier is not None:
            return self._verified(tool, item, args, meaning, action, summary, text, thread, turn, actor, cid,
                                  event_id)
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
        if not lang.supports(kind, text, quote, action['args'] if kind in ('update_caption', 'set_policy') else args):
            return self._clarify(f'{summary}: not done, your words do not ask for that. Which item, and what '
                                 'exactly should I do?')

        if tool == 'set_rule':
            # A lasting rule change needs lasting words; otherwise one question: this item only, or the rule?
            if not lang.permanent(text) and not args.get('confirmed_permanent'):
                q = (f'{summary}: is this a lasting rule for every item from now on? (For one video only, tell me '
                     'which video and I will exempt just that one.)')
                turn.ask(q + '؟', action)
                return {'state': 'needs_confirmation', 'question': q + '؟'}
            return self.execute(action, actor=actor, cid=cid, thread=thread, event_id=event_id, turn=turn)
        if tool == 'board_create_item':
            return self.execute(action, actor=actor, cid=cid, thread=thread, event_id=event_id, turn=turn)

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
        return self.execute(action, actor=actor, cid=cid, thread=thread, event_id=event_id, turn=turn)

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
        label = self._label(item) if item is not None else ''
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
        if tool in ('waive_rule', 'restore_rule'):
            rule = str(args.get('rule') or '')
            if rule not in rules.POLICY:
                raise Rejected(f'Unknown rule "{rule}"; rules: ' + ', '.join(sorted(rules.POLICY)))
            label = rules.POLICY[rule]['label']
            if tool == 'waive_rule':
                a = {'rule': rule, 'reason': args.get('reason') or 'Exempted by the owner in Slack'}
                if args.get('this_file_only'):
                    with self.ops.store.read() as c:
                        a['asset_key'] = self.ops.item(c, item)['asset_key']
                return {**act, 'op': 'waive_rule', 'args': a}, \
                    f'Exempt {self._label(item)} from: {label} (the rule stays for other items' + \
                    ('; this is not a Topaz confirmation)' if rule == 'topaz_required' else ')')
            return {**act, 'op': 'restore_rule', 'args': {'rule': rule}}, f'Apply again to {self._label(item)}: {label}'
        if tool == 'set_rule':
            rule = str(args.get('rule') or '')
            raw = args.get('value')
            if isinstance(raw, str) and raw.strip().startswith(('{', '[')):
                try:
                    raw = json.loads(raw)
                except ValueError:
                    pass
            try:
                value = rules.coerce_policy(rule, raw)
            except rules.RuleError as e:
                raise Rejected(str(e))
            a = {'rule': rule, 'value': value, 'reason': args.get('reason') or 'Owner instruction in Slack'}
            if args.get('effective_at'):
                a['effective_at'] = args['effective_at']
            return {'tool': tool, 'item': None, 'op': 'set_policy', 'args': a}, \
                f"Change the rule \"{rules.POLICY[rule]['label']}\" to {value!r}"
        if tool == 'publish_now':
            now = self.ops.now_dt()
            at = (now + timedelta(seconds=125)).replace(second=0)
            return {**act, 'op': 'request_reschedule', 'args': {'at': rules.iso(at)}}, \
                f'Publish {label} now (at {rules.display(at)}, as soon as it is ready)'
        if tool == 'cancel_schedule':
            return {**act, 'op': 'request_reschedule', 'args': {'at': None}}, \
                f'Cancel the publication time of {label} (it stays unscheduled until you give a new time)'
        if tool == 'board_create_item':
            a = {k: args.get(k) for k in ('name', 'format', 'code', 'dropbox_url', 'caption') if args.get(k)}
            return {'tool': tool, 'item': None, 'op': 'board_create', 'args': a}, \
                f"Create the {args.get('format')} \"{args.get('name')}\" on the board"
        if tool in ('board_archive_item', 'board_delete_item'):
            mode = 'archive' if tool == 'board_archive_item' else 'delete'
            said = lang.removal(turn.text)
            if said not in (mode, 'both'):
                if said == 'unclear':
                    raise Rejected('Archive it (it can be restored from the board archive) or delete it from the '
                                   'board? Say which.', 'question')
                raise Rejected(f'Not done: your words ask to {said}, not to {mode}.')
            return {**act, 'op': 'board_remove', 'args': {'mode': mode}}, \
                f"{'Delete' if mode == 'delete' else 'Archive'} {label} on the board (it will not be published)"
        if tool == 'set_item_field':
            value = str(args.get('value') or '')
            if value and not lang.contains(turn.text, value):
                raise Rejected('Not done: the new value is not in your message; type it exactly')
            return {**act, 'op': 'board_set_field', 'args': {'field': args.get('field'), 'value': value}}, \
                f"Set {args.get('field')} of {label} to \"{value}\""
        if tool == 'file_action':
            a = {k: args.get(k) for k in ('action', 'path', 'to') if args.get(k)}
            if a.get('to') and not lang.contains(turn.text, a['to'].rsplit('/', 1)[-1]):
                raise Rejected('Not done: the new name or folder is not in your message; type it exactly')
            # The file must be one the owner named, or one listed for this item in this thread (list_files):
            # never a path the model made up or took from file names it read.
            path = str(a.get('path') or '')
            listed = ((self.store.kv('files:' + turn.thread) or {}).get(str(item)) or []) if self.store else []
            name = path.rsplit('/', 1)[-1]
            if not (lang.contains(turn.text, path) or (path.lower() in listed and name and
                                                         lang.contains(turn.text, name.rsplit('.', 1)[0]))):
                raise Rejected('Which file exactly? Name it as list_files shows it.', 'question')
            return {'tool': tool, 'item': item, 'op': 'file_action', 'args': a}, \
                f"{str(a.get('action') or '').capitalize()} {a.get('path')}" + (f" → {a['to']}" if a.get('to') else '')
        if tool == 'block_publishing':
            return {**act, 'op': 'publish_block', 'args': {'reason': (args.get('reason') or turn.text)[:300]}}, \
                f'Never publish {label} until you allow it again (it can still be checked and scheduled)'
        if tool == 'allow_publishing':
            return {**act, 'op': 'publish_allow', 'args': {}}, f'Allow {label} to be published again'
        if tool == 'request_rework':
            return {**act, 'op': 'request_rework',
                    'args': {'reason': args.get('reason') or 'The owner asked for a new edit'}}, \
                f'Send {label} back to the editor for a new edit'
        raise Rejected('Unknown tool', 'unsupported')

    def _verified(self, tool, item, args, meaning, action, summary, text, thread, turn, actor, cid, event_id):
        """The owner's words decide, read for meaning by the independent verifier (not word lists). Deterministic
        checks that stay: the quote is the owner's own words; values the owner must type are in the message."""
        quote = str(meaning.get('evidence_quote') or '').strip()
        if not quote or lang.markers(text, quote).get('located') is not True:
            return self._clarify(f'{summary}: not done, I could not find that request in your message. '
                                 'Say exactly what to do.')
        quoted = [ln.lstrip('> ').strip() for ln in text.splitlines() if ln.lstrip().startswith('>')]
        if quoted and any(lang.compact(quote) in lang.compact(q) for q in quoted):
            return self._clarify(f'{summary}: not done, those words are quoted text (a line starting with ">"), '
                                 'not your instruction.')
        prev = ''
        if self.store is not None:
            hist = [m for m in self.store.history(thread, limit=4) if m.get('role') == 'assistant']
            prev = hist[-1]['content'] if hist else ''
        try:
            v = self.verifier.check(text, prev, summary, {k: v for k, v in (action.get('args') or {}).items()})
        except Exception as e:  # noqa: BLE001 - verifier unavailable: nothing is guessed
            return self._clarify(f'{summary}: not done, I could not check the meaning of your message right now '
                                 f'({type(e).__name__}). Say it again in a moment.')
        if v.get('decision') == 'ask':
            q = (v.get('question') or f'{summary}؟').strip()
            turn.ask(q, action)
            return {'state': 'needs_confirmation', 'question': q}
        if v.get('decision') != 'execute':
            return self._clarify(f"{summary}: not done, {v.get('reason') or 'your message does not ask for it'}.")
        if tool == 'resolve_publication' and args.get('published') is not True:
            q = f'{summary} (it becomes schedulable again)؟'
            turn.ask(q, action)
            return {'state': 'needs_confirmation', 'question': q}
        if tool == 'report_published' and args.get('destination') not in (None, 'studio_account'):
            q = f'Was it published on the studio Instagram account? If yes: {summary}؟'
            turn.ask(q, action)
            return {'state': 'needs_confirmation', 'question': q}
        return self.execute(action, actor=actor, cid=cid, thread=thread, event_id=event_id, turn=turn)

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
