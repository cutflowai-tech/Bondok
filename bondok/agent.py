"""Bondok language layer: named business tools only (no SQL/GraphQL/shell/HTTP).

The model is used for natural-language understanding, explanations and
caption drafting. Routine work (approvals, notifications, monitoring,
publishing) never reaches this module.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path


from waset_ops import rules
from waset_ops.db import dumps

from bridge import MUTATING, Bridge

MAX_STEPS = 6
MAX_CALLS_PER_STEP = 6
HISTORY = 12


def _fn(name, desc, props=None, required=None):
    props = props or {}
    return {'type': 'function', 'name': name, 'description': desc, 'strict': True,
            'parameters': {'type': 'object', 'properties': props, 'required': required or list(props),
                           'additionalProperties': False}}


ITEM = {'item': {'type': 'string', 'description': 'Item id, code (e.g. LIP12) or exact name on the social board'}}
# Structured meaning the model proposes for every protected call (contract §1, §4). Trusted code checks it against
# the owner's own words; it never authorizes anything by itself.
MEANING = {'meaning': {
    'type': 'object', 'additionalProperties': False,
    'required': ['speech_act', 'polarity', 'targets_as_said', 'evidence_quote'],
    'description': "What the owner's latest message means for THIS call.",
    'properties': {
        'speech_act': {'type': 'string', 'enum': ['imperative', 'completed_statement', 'bondok_offer', 'question',
                                                  'plan_or_future', 'conditional', 'quotation', 'acknowledgment',
                                                  'other'],
                       'description': 'imperative = the owner tells you to do it now; completed_statement = the '
                                      'owner states it already happened (e.g. Topaz done, I posted it); '
                                      'bondok_offer = YOUR suggestion the owner has not asked for (it becomes one '
                                      'question the owner answers with "اه"/"لا"); anything else is not executed.'},
        'polarity': {'type': 'string', 'enum': ['positive', 'negative']},
        'targets_as_said': {'type': 'string', 'description': 'The words the owner used for the item(s), verbatim '
                                                             '(e.g. "LIP12", "them", "انشرهم"); empty if none.'},
        'evidence_quote': {'type': 'string', 'description': "Exact words copied from the owner's latest message "
                                                            'that ask for this action; empty for bondok_offer.'}}}}
WINDOW = {'on_date': {'type': ['string', 'null'], 'description': 'YYYY-MM-DD Cairo day the owner said ("tomorrow")'},
          'not_before': {'type': ['string', 'null'], 'description': 'YYYY-MM-DD HH:MM Cairo, "not before ..."'}}
TOOLS = [
    _fn('find_items', 'Search social-board items by name/code. Read-only.', {'query': {'type': 'string'}}),
    _fn('get_item_status', 'Current authoritative status of one item: readiness, block reason, schedule, '
        'publication, pending approvals, plus its board text. Read-only.', ITEM),
    _fn('get_schedule', 'Confirmed reservations for the next N days (Cairo time). Read-only.',
        {'days': {'type': 'integer'}}),
    _fn('get_system_health', 'Automation health: heartbeats, unknown publications, sync backlog. Read-only.'),
    _fn('get_operation_status', 'Outcome of a previous request or proposal id. Read-only.', {'id': {'type': 'string'}}),
    _fn('pause_item', 'Owner request: pause publication of an item.', {**ITEM, 'reason': {'type': 'string'}, **MEANING}),
    _fn('resume_item', 'Owner request: resume a paused/skipped item (with any day constraint the owner said).',
        {**ITEM, **WINDOW, **MEANING}),
    _fn('skip_item', 'Owner request: skip an item.', {**ITEM, 'reason': {'type': 'string'}, **MEANING}),
    _fn('request_recheck', 'Owner request: re-check the selected file and prepared video.', ITEM),
    _fn('request_reschedule', 'Owner request: publish an item at an exact Cairo time the owner said (may be off the '
        'usual grid).', {**ITEM, 'cairo_time': {'type': 'string', 'description': 'YYYY-MM-DD HH:MM Africa/Cairo'},
                         **MEANING}),
    _fn('request_publish', 'Owner request: schedule an item at the earliest free valid slot (inside any day '
        'constraint the owner said, e.g. tomorrow).', {**ITEM, **WINDOW, **MEANING}),
    _fn('set_window', 'Owner constraint without a publish request, e.g. "not before tomorrow".',
        {**ITEM, **WINDOW, **MEANING}),
    _fn('change_format', 'Owner request: change Story<->Post. Never use on your own initiative.',
        {**ITEM, 'format': {'type': 'string', 'enum': ['Post', 'Story']}, **MEANING}),
    _fn('replace_source', 'Owner request: use a different Dropbox file link.',
        {**ITEM, 'url': {'type': 'string'}, **MEANING}),
    _fn('update_caption', 'Owner supplied caption text for a Post (pass the text exactly as typed).',
        {**ITEM, 'text': {'type': 'string'}, **MEANING}),
    _fn('approve_existing_caption', 'Owner approves the caption already on the item.', {**ITEM, **MEANING}),
    _fn('approve_caption_draft', 'Owner approves the caption draft waiting for approval on the item.',
        {**ITEM, **MEANING}),
    _fn('approve_all_existing_captions', 'Owner wants to review/approve all captions imported from the old system '
        'in one proposal (lists every caption; nothing changes until approved).'),
    _fn('draft_caption', 'Submit a caption you drafted; it goes to the owner for approval.',
        {**ITEM, 'text': {'type': 'string'}}),
    _fn('confirm_topaz', 'Owner states Topaz processing is done for the currently selected file.', {**ITEM, **MEANING}),
    _fn('resolve_publication', 'Owner reports the manual Instagram check of an uncertain publication.',
        {**ITEM, 'published': {'type': 'boolean'}, 'media_id': {'type': ['string', 'null']}, **MEANING}),
    _fn('report_published', 'Owner says they posted the item themselves on the studio Instagram account.',
        {**ITEM, 'destination': {'type': 'string', 'enum': ['studio_account', 'other', 'unclear']}, **MEANING}),
    _fn('request_rework', 'Owner sends the item back to the editor for a new edit (never a resume).',
        {**ITEM, 'reason': {'type': 'string'}, **MEANING}),
]
TOOL_NAMES = {t['name'] for t in TOOLS}


class ServiceError(Exception):
    """Model unavailable. `code` is the cause (model_credits, model_http_<n>, model_unreachable,
    model_incomplete); `detail` is the provider's message without links."""

    def __init__(self, code, detail=''):
        super().__init__(code)
        self.code, self.detail = code, re.sub(r'https?://\S+', '<link>', str(detail or ''))[:300]


MAX_OUTPUT_TOKENS = 3000
MIN_OUTPUT_TOKENS = 1500          # below this a tool-using answer with reasoning is not reliable


class Model:
    """OpenRouter Responses API client (unchanged provider and model)."""

    def __init__(self, key, model='openai/gpt-6.1-sol', policy=''):
        import httpx
        self._httpx = httpx
        self.key, self.model, self.policy = key, model, policy
        self.http = httpx.Client(timeout=150, follow_redirects=False)
        self.calls = 0
        self.last_budget = None          # reduced output budget used after a credit refusal, for logging

    def __call__(self, history, tools):
        budget, self.last_budget = MAX_OUTPUT_TOKENS, None
        for attempt in range(2):
            self.calls += 1
            payload = {'model': self.model, 'input': history, 'store': False, 'reasoning': {'effort': 'medium'},
                       'instructions': self.policy + '\nCurrent time: ' + datetime.now(rules.TZ).isoformat(),
                       'tools': tools, 'tool_choice': 'auto', 'max_output_tokens': budget}
            try:
                r = self.http.post('https://openrouter.ai/api/v1/responses', json=payload,
                                   headers={'Authorization': 'Bearer ' + self.key, 'X-Title': 'Waset Bondok'})
            except self._httpx.HTTPError as e:
                raise ServiceError('model_unreachable', type(e).__name__) from None
            if r.status_code == 402:
                # OpenRouter refuses a request whose maximum cost exceeds the remaining credit and says what it
                # can afford. Retry once within that budget; otherwise report the credit problem as such.
                m = re.search(r'can only afford (\d+)', r.text)
                afford = int(m.group(1)) if m else 0
                if attempt == 0 and afford - 100 >= MIN_OUTPUT_TOKENS:
                    budget = self.last_budget = afford - 100
                    continue
                raise ServiceError('model_credits', _error_message(r))
            if r.status_code != 200:
                raise ServiceError(f'model_http_{r.status_code}', _error_message(r))
            data = r.json()
            if data.get('error') or data.get('status') not in (None, 'completed') or not data.get('output'):
                raise ServiceError('model_incomplete', str(data.get('error') or data.get('incomplete_details') or
                                                           data.get('status')))
            return data
        raise ServiceError('model_credits', 'credit refusal after reduced budget')


def _error_message(r) -> str:
    try:
        return str((r.json().get('error') or {}).get('message') or '')
    except ValueError:
        return r.text[:300]


class Agent:
    def __init__(self, bridge: Bridge, model, monday=None, store=None):
        self.bridge, self.model, self.monday, self.store = bridge, model, monday, store

    # ------------------------------------------------------------------ tools
    def tool(self, name, args, ctx) -> dict:
        ops = self.bridge.ops
        if name not in TOOL_NAMES:
            return {'error': 'tool not allowed'}
        if name == 'find_items':
            r = self.bridge.resolve(args.get('query'))
            if 'item_id' in r:
                self._shown(r['item_id'], ctx)
                return {'matches': [ops.item_status(r['item_id'])]}
            return r
        if name == 'get_item_status':
            r = self.bridge.resolve(args.get('item'))
            if 'item_id' not in r:
                return r
            status = ops.item_status(r['item_id'])
            self._shown(r['item_id'], ctx)
            if self.monday:
                try:
                    board = self.monday.item(r['item_id'])     # membership verified before contents
                    status['board_text'] = {c['id']: c.get('text') for c in board.get('column_values', [])
                                            if c.get('text')}
                except Exception as e:  # board read failure is reported, never guessed
                    status['board_text_error'] = type(e).__name__
            return status
        if name == 'get_schedule':
            return {'reservations': ops.schedule(max(1, min(int(args.get('days') or 14), 84)))}
        if name == 'get_system_health':
            return ops.health()
        if name == 'get_operation_status':
            ref = str(args.get('id') or '')
            if ref.startswith('B-'):
                with ops.store.read() as c:
                    p = c.execute('SELECT id,kind,state,summary,result,expires FROM ops_proposals WHERE id=?',
                                  (ref,)).fetchone()
                return dict(p) if p else {'error': 'unknown proposal'}
            return ops.command_status(ref) or {'error': 'unknown request id'}
        return self.bridge.run(name, args, actor=ctx['actor'], event_id=ctx['event_id'], text=ctx['text'],
                               thread=ctx['thread'], turn=ctx.get('turn'))

    def _shown(self, item_id, ctx):
        with self.bridge.ops.store.read() as c:
            it = self.bridge.ops.item(c, item_id)
        self.bridge.remember_shown(ctx['thread'], item_id, it['asset_key'])

    # ------------------------------------------------------------------ conversation
    def answer(self, text, *, actor, thread, event_id, history=(), turn=None):
        """Bounded tool loop. Returns (reply, outcomes). Outcomes are rendered by
        trusted code; the model's own summary never stands in for them."""
        ctx = {'actor': actor, 'thread': thread, 'event_id': event_id, 'text': text, 'turn': turn}
        convo = list(history)[-HISTORY:] + [{'role': 'user', 'content': speaker(actor, self.bridge.owner) + text[:6000]}]
        outcomes = []
        for _ in range(MAX_STEPS):
            try:
                data = self.model(convo, TOOLS)
            except ServiceError as e:
                e.outcomes = outcomes          # actions already taken must still be reported (audit H1)
                raise
            out = data['output']
            convo += out
            calls = [x for x in out if x.get('type') == 'function_call']
            if not calls:
                reply = '\n'.join(y['text'] for x in out if x.get('type') == 'message'
                                  for y in x.get('content', []) if y.get('type') == 'output_text').strip()
                return reply, outcomes
            for n, call in enumerate(calls):
                if n >= MAX_CALLS_PER_STEP:
                    # Every function_call needs an output or the next request fails (audit H2).
                    convo.append({'type': 'function_call_output', 'call_id': call['call_id'],
                                  'output': dumps({'error': f'Not executed: at most {MAX_CALLS_PER_STEP} actions per '
                                                            'step; ask for the rest separately'})})
                    continue
                try:
                    args = json.loads(call.get('arguments') or '{}')
                    value = self.tool(call['name'], args, ctx)
                except (ValueError, KeyError, TypeError) as e:
                    value = {'error': type(e).__name__}
                except Exception as e:  # handler rejection or storage error: report, never retry blindly
                    value = {'error': str(e)[:300]}
                if call['name'] in MUTATING:
                    outcomes.append({'tool': call['name'], 'args': args if isinstance(args, dict) else {},
                                     'result': value})
                convo.append({'type': 'function_call_output', 'call_id': call['call_id'],
                              'output': dumps(value)[:20000]})
        return 'I reached the step limit for this request; split it into smaller requests.', outcomes


def speaker(actor, owner) -> str:
    """Label every user turn so earlier member words never read as the owner's (R5 B5)."""
    return '[owner] ' if actor == owner else '[member] '


def render_outcome(o) -> str:
    """Trusted, templated acknowledgement. 'accepted' is never shown as 'done'."""
    r = o['result']
    st = r.get('state')
    name = o['tool'].replace('_', ' ')
    if st == 'completed':
        head = f'✅ Done: {name}'
        if r.get('duplicate'):
            head = f'↩️ Already handled earlier: {name}'
        if r.get('publication') == 'may_already_be_in_progress':
            head = f'⚠️ {name} recorded, but publication may already be in progress; I will report the outcome.'
    elif st == 'accepted':
        head = f'🕒 Accepted (not yet performed): {name}'
    elif st == 'awaiting_approval':
        return (f"📝 Proposal {r['proposal_id']} — not executed yet\n{r.get('summary', '')}\n"
                f"To approve reply in this thread: `اعتمد {r['proposal_id']}` (valid {r.get('expires_in_minutes', 30)} min)")
    elif st == 'needs_confirmation':
        return '❓ ' + r['question'] + ' (رد بـ «اه» عشان أنفذ، أو «لا»)'
    elif st == 'needs_clarification' and r.get('question'):
        return '❓ ' + r['question']
    elif st == 'needs_clarification' and r.get('error'):
        return '❓ ' + r['error']
    elif st == 'needs_clarification':
        return '❓ Which item? ' + ', '.join(f"{a['name']} ({a['code']}, {a['item_id']})" for a in r.get('ambiguous', []))
    else:
        head = f'⛔ Not done: {name}'
    detail = r.get('message') or r.get('reason') or ''
    if r.get('scheduled'):
        detail += f" Slot: {rules.display(rules.instant(r['scheduled']))}."
    return (head + ('\n' + detail.strip() if detail.strip() else '')).strip()


def policy_text():
    return (Path(__file__).with_name('policy.txt')).read_text()
