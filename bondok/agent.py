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
    # ---- owner rules (agent migration P1): the rules are the owner's preferences
    _fn('get_rules', 'Every business rule with its current value, default, version and who set it, plus the real '
        'platform limits that are not rules. Read-only.'),
    _fn('explain_item', 'Why an item is waiting or blocked, which rule holds it and how the owner can change that. '
        'Read-only.', ITEM),
    _fn('waive_rule', 'Owner exempts ONE item from a rule, e.g. "publish it without Topaz" (rule topaz_required), '
        '"publish it even under 1080" (min_short_edge), "publish the same video again" (duplicate_video_block). '
        'This is an exemption, never a claim that the rule was satisfied.',
        {**ITEM, 'rule': {'type': 'string', 'enum': list(rules.WAIVABLE)},
         'this_file_only': {'type': 'boolean', 'description': 'true when the owner limits it to the current file'},
         'reason': {'type': 'string'}, **MEANING}),
    _fn('restore_rule', 'Owner withdraws an exemption: the rule applies to the item again.',
        {**ITEM, 'rule': {'type': 'string', 'enum': list(rules.WAIVABLE)}, **MEANING}),
    _fn('set_rule', 'Owner changes a rule for every item from now on, e.g. "from today Topaz is not required" '
        '(topaz_required=false), "Story slots 12:00, 15:00, 21:00" (story_slots), "allow publishing up to 3 hours '
        'late" (late_window_minutes=180). Values: bool for on/off rules, numbers, ["HH:MM",...] for story_slots, '
        '{"sat":"21:00",...} for post_slots. Existing reservations are not moved.',
        {'rule': {'type': 'string', 'enum': sorted(rules.POLICY)},
         'value': {'type': 'string', 'description': 'New value as text: "true"/"false", a number, "12:00, 15:00" '
                                                     'for story_slots, \'{"sat": "21:00"}\' (JSON) for post_slots'},
         'reason': {'type': ['string', 'null']}, **MEANING}),
    # ---- execution tools (agent migration P2): the owner's instruction is carried out, the result is reported
    _fn('publish_now', 'Owner request: publish an item now (in about two minutes, as soon as it is ready). Use '
        'request_publish for "the next free slot" and request_reschedule for an exact time.', {**ITEM, **MEANING}),
    _fn('cancel_schedule', 'Owner request: remove the item\'s publication time; it stays unscheduled until a new time '
        'or "publish it".', {**ITEM, **MEANING}),
    _fn('board_create_item', 'Owner request: create a new item on the social board.',
        {'name': {'type': 'string'}, 'format': {'type': 'string', 'enum': ['Post', 'Story']},
         'code': {'type': ['string', 'null']}, 'dropbox_url': {'type': ['string', 'null']},
         'caption': {'type': ['string', 'null']}, **MEANING}),
    _fn('board_archive_item', 'Owner request: archive an item on the board (restorable from Monday\'s archive). It '
        'will not be published.', {**ITEM, **MEANING}),
    _fn('board_delete_item', 'Owner request: delete an item from the board. It will not be published. If the owner\'s '
        'words do not say archive or delete, ask which.', {**ITEM, **MEANING}),
    _fn('set_item_field', 'Owner request: set Notes, Code, Story variety or IG Colab of an item (exact value the '
        'owner typed).', {**ITEM, 'field': {'type': 'string', 'enum': ['notes', 'code', 'variety', 'collab']},
                          'value': {'type': 'string'}, **MEANING}),
    _fn('list_files', 'The videos in an item\'s Dropbox project folder, with which one is selected. Read-only.', ITEM),
    _fn('file_action', 'Owner request: rename, move or delete (to Dropbox\'s deleted files) one Dropbox file. Use the '
        'exact path from list_files.',
        {'item': {'type': 'string', 'description': 'The item the file belongs to'},
         'action': {'type': 'string', 'enum': ['rename', 'move', 'delete']}, 'path': {'type': 'string'},
         'to': {'type': ['string', 'null'], 'description': 'New name (rename) or full new path (move)'},
         **MEANING}),
    _fn('block_publishing', 'Owner directive: never publish this item until the owner allows it again (it can still '
        'be checked and scheduled, e.g. a test item). Use for "do not actually publish it".',
        {**ITEM, 'reason': {'type': ['string', 'null']}, **MEANING}),
    _fn('allow_publishing', 'Owner withdraws the do-not-publish directive of an item.', {**ITEM, **MEANING}),
    _fn('refresh_item', 'Read the item from Monday now, apply what the board shows (a board read is not an owner '
        'instruction: values that would erase a decision wait for evidence), and report what changed and why.',
        ITEM),
    _fn('get_history', 'What happened to an item: decisions, publication attempts and results, with who and when. '
        'Read-only.', ITEM),
    _fn('instagram_recent', 'Recent posts on the studio Instagram account, and the 24-hour publishing quota. '
        'Read-only.'),
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
    def __init__(self, bridge: Bridge, model, monday=None, store=None, clients=None):
        self.bridge, self.model, self.monday, self.store = bridge, model, monday, store
        self.clients = clients or {}       # optional runtime clients: dropbox, instagram (read tools)

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
        if name == 'get_rules':
            return ops.policy_report()
        if name == 'refresh_item':
            r = self.bridge.resolve(args.get('item'))
            if 'item_id' not in r:
                return r
            m = self.clients.get('monday')
            if m is None:
                return {'error': 'Monday is not connected to Bondok on this host (configuration).'}
            keys = ('readiness', 'owner_state', 'hold', 'publication', 'reservation', 'requested_at', 'publish_block',
                    'topaz_confirmation', 'exemptions', 'caption_state')
            before = ops.item_status(r['item_id'])
            t0 = ops.now()
            try:
                fresh = m.fresh_item(r['item_id'])
            except Exception as e:  # noqa: BLE001 - reported, never guessed
                return {'error': f'Monday read failed: {str(e)[:200]}', 'status': before}
            seen = ops.observe([{k: fresh[k] for k in ('id', 'name', 'column_values', 'group') if k in fresh}],
                               actor='bondok-refresh', read_at=t0)
            after = ops.item_status(r['item_id'])
            self._shown(r['item_id'], ctx)
            return {'read_at': ops.iso_ts(t0), 'board_edits': seen.get('edits') or [],
                    'changed': {k: {'before': before.get(k), 'after': after.get(k)} for k in keys
                                if before.get(k) != after.get(k)},
                    'status': after}
        if name == 'get_history':
            r = self.bridge.resolve(args.get('item'))
            if 'item_id' not in r:
                return r
            return ops.history(r['item_id'])
        if name == 'list_files':
            r = self.bridge.resolve(args.get('item'))
            if 'item_id' not in r:
                return r
            out = self._files(r['item_id'])
            if out.get('videos'):
                self.bridge.remember_files(ctx['thread'], r['item_id'], [v['path'] for v in out['videos']])
            return out
        if name == 'instagram_recent':
            ig = self.clients.get('instagram')
            if ig is None:
                return {'error': 'Instagram is not connected to Bondok on this host (configuration).'}
            try:
                return {'recent': ig.media(10), 'quota': ig.publishing_limit()}
            except Exception as e:  # noqa: BLE001 - reported, never guessed
                return {'error': f'Instagram read failed: {str(e)[:200]}'}
        if name == 'explain_item':
            r = self.bridge.resolve(args.get('item'))
            if 'item_id' not in r:
                return r
            self._shown(r['item_id'], ctx)
            return ops.explain(r['item_id'])
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

    def _files(self, item_id) -> dict:
        dbx = self.clients.get('dropbox')
        if dbx is None:
            return {'error': 'Dropbox is not connected to Bondok on this host (configuration).'}
        from waset_runtime import selection
        with self.bridge.ops.store.read() as c:
            it = self.bridge.ops.item(c, item_id)
        folder = it.get('folder_url') or it.get('file_url')
        if not folder:
            return {'error': 'This item has no Dropbox folder or file link yet.'}
        try:
            plan = selection.plan_listing(dbx.link_metadata(folder))
            entries = dbx.list_folder(plan['list']['path'], recursive=plan['list']['recursive'])
        except Exception as e:  # noqa: BLE001
            return {'error': f'Dropbox read failed: {str(e)[:200]}'}
        vids = [{'name': e['name'], 'path': e['path_lower'], 'modified': e.get('client_modified'),
                 'selected': bool(it.get('file_id') and e.get('id') == it['file_id'])}
                for e in entries if e.get('.tag') == 'file' and selection.VID.search(e.get('name') or '')]
        return {'folder': plan['folderPath'], 'videos': vids[:50], 'selected_version': it.get('asset_key')}

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
        try:
            with self.bridge.ops.store.read() as c:
                steps = int(self.bridge.ops.policy(c)['agent_max_steps'])   # owner's cost/loop limit (P3-06)
        except Exception:  # noqa: BLE001 - an unreadable store keeps the default
            steps = MAX_STEPS
        for _ in range(steps):
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
            for call in calls:                 # every action the owner named runs: no count limit
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
        return (f'I reached the model step limit for this message (rule agent_max_steps = {steps}); the actions above '
                'were done. Say the rest again, or raise the rule.'), outcomes


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
    detail = r.get('message') or r.get('reason') or r.get('error') or ''
    if r.get('scheduled'):
        detail += f" Slot: {rules.display(rules.instant(r['scheduled']))}."
    return (head + ('\n' + detail.strip() if detail.strip() else '')).strip()


def policy_text():
    return (Path(__file__).with_name('policy.txt')).read_text()
