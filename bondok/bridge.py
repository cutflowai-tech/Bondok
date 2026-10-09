"""Trusted adapter between Slack/model tools and the shared command handler.

Authority never comes from model output:
* actor and actor kind come from the authenticated Slack event;
* "explicit owner instruction" is decided here, deterministically, from the
  owner's own message text, not from tool arguments;
* idempotency ids derive from the Slack event, so redelivery and model retries
  cannot repeat an effect.
"""
from __future__ import annotations

import hashlib
import json
import re

from waset_ops import Command, Ops, Rejected, rules
from waset_ops.db import dumps

MUTATING = {'pause_item', 'resume_item', 'skip_item', 'request_recheck', 'request_reschedule', 'request_publish',
            'change_format', 'replace_source', 'update_caption', 'approve_existing_caption', 'confirm_topaz',
            'resolve_publication', 'draft_caption', 'approve_all_existing_captions'}

_FORMAT_WORDS = {'Post': r'(بوست|ريل|ريلز|post|reel)', 'Story': r'(ستوري|استوري|story)'}
_CHANGE = r'(حو[ّ]?ل|غي[ّ]?ر|خلي|خليه|خليها|اعمله|اعملها|change|convert|switch|make\s+(it|this))'


def explicit(op: str, args: dict, text: str) -> bool:
    """True only when the owner's own words state this exact action."""
    t = (text or '').casefold()
    if op == 'change_format':
        target = args.get('format')
        if target not in _FORMAT_WORDS or not re.search(_CHANGE, t):
            return False
        other = 'Story' if target == 'Post' else 'Post'
        last_target = max((m.end() for m in re.finditer(_FORMAT_WORDS[target], t)), default=-1)
        last_other = max((m.end() for m in re.finditer(_FORMAT_WORDS[other], t)), default=-1)
        return last_target > last_other      # the destination format is the one named last
    if op == 'confirm_topaz':
        return bool(re.search(r'(توباز|topaz)', t) and re.search(r'(خلص|اتعمل|تم|اتم|جاهز|done|confirmed|finished|ready)', t)
                    and not re.search(r'(لسه|مش|not yet|لم)', t))
    if op == 'resume':
        return bool(re.search(r'(كمل|كم[ّ]?له|استأنف|استئناف|رجع|رجّع|شغ[ّ]?ل|resume|unpause|continue)', t))
    if op == 'skip':
        return bool(re.search(r'(تخطى|تخطي|اتخطى|سكيب|skip|الغي|الغيه|cancel)', t))
    if op == 'resolve_outcome':
        if args.get('outcome') == 'published':
            return bool(re.search(r'(اتنشر|نزل|منشور|published|posted|is live)', t)) and not re.search(r'(مش|لم|not)', t)
        return bool(re.search(r'(مش منشور|ما ?اتنشرش|متنشرش|لم ينشر|not published|wasn.t posted|not posted)', t))
    if op == 'update_caption':
        caption = (args.get('text') or '').strip()
        return len(caption) >= 10 and caption in (text or '')     # owner supplied the exact text
    return False


class Bridge:
    def __init__(self, ops: Ops, owner_id: str):
        self.ops, self.owner = ops, owner_id

    # ------------------------------------------------------------------ resolution
    def resolve(self, ref) -> dict:
        """Item reference -> exactly one registered social-board item, or candidates."""
        ref = str(ref or '').strip()
        with self.ops.store.read() as c:
            if re.fullmatch(r'\d{1,20}', ref):
                r = c.execute('SELECT item_id,name,code,format FROM ops_items WHERE item_id=?', (ref,)).fetchone()
                if r:
                    return {'item_id': r['item_id']}
                return {'error': 'This id is not an item of the For Social Media board'}
            norm = re.sub(r'\s+', '', ref).upper()
            rows = c.execute('SELECT item_id,name,code,format,owner_state,publication FROM ops_items').fetchall()
            exact = [r for r in rows if re.sub(r'\s+', '', r['code'] or '').upper() == norm]
            hits = exact or [r for r in rows if ref and ref.casefold() in (r['name'] or '').casefold()]
            if len(hits) == 1:
                return {'item_id': hits[0]['item_id']}
            if not hits:
                return {'error': 'No social-board item matches; ask for the item name, code or id'}
            return {'ambiguous': [{'item_id': r['item_id'], 'name': r['name'], 'code': r['code'], 'format': r['format'],
                                   'publication': r['publication']} for r in hits[:8]]}

    # ------------------------------------------------------------------ execution
    def run(self, tool: str, args: dict, *, actor: str, event_id: str, text: str, thread: str) -> dict:
        is_owner = actor == self.owner
        if tool in MUTATING and not is_owner:
            return {'state': 'rejected', 'reason': 'Only the owner can request changes; I can explain or draft.'}
        item = None
        if 'item' in args:
            r = self.resolve(args['item'])
            if 'item_id' not in r:
                return {'state': 'needs_clarification', **r}
            item = r['item_id']
        h = hashlib.sha256(dumps({'tool': tool, 'args': args}).encode()).hexdigest()[:16]
        cid = f'slack:{event_id}:{h}'
        auth = {'thread': thread, 'event': event_id}

        def submit(op, a, explicit_ok=None):
            auth_ = {**auth, 'explicit': bool(explicit_ok)}
            return self.ops.submit(Command(cid, op, actor, 'owner', item, a, auth=auth_))

        def approval_or_run(op, a, summary):
            if explicit(op, a, text):
                return submit(op, a, True)
            with self.ops.store.tx() as c:
                prior = c.execute('SELECT result FROM ops_commands WHERE id=?', (cid,)).fetchone()
                if prior:
                    return {**json.loads(prior['result'] or '{}'), 'duplicate': True}
                p = self.ops.propose_command(c, op, item, a, summary, actor, thread)
                c.execute("INSERT INTO ops_commands(id,payload_hash,op,item_id,actor,actor_kind,auth_ref,state,result,"
                          "created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (cid, h, 'propose:' + op, item, actor, 'owner', dumps(auth), 'awaiting_approval',
                           dumps(p), self.ops.now(), self.ops.now()))
            return {'state': 'awaiting_approval', **p}

        if tool == 'pause_item':
            return submit('pause', {'reason': args.get('reason') or 'Paused by owner in Slack'})
        if tool == 'skip_item':
            return approval_or_run('skip', {'reason': args.get('reason') or 'Skipped by owner in Slack'},
                                   f'Skip item {item}. It will not publish until explicitly resumed.')
        if tool == 'resume_item':
            return approval_or_run('resume', {}, f'Resume item {item}; it returns to normal checks and scheduling.')
        if tool == 'request_recheck':
            return submit('request_recheck', {})
        if tool == 'request_publish':
            return submit('request_publish', {})
        if tool == 'request_reschedule':
            try:
                local = args['cairo_time']
                d = rules.cairo_local(*map(int, re.match(r'(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})$', local).groups()))
            except (TypeError, AttributeError, KeyError, rules.RuleError) as e:
                return {'state': 'rejected', 'reason': f'Time must be Cairo time as YYYY-MM-DD HH:MM ({e})'}
            return submit('request_reschedule', {'at': rules.iso(d)})
        if tool == 'change_format':
            fmt = args.get('format')
            return approval_or_run('change_format', {'format': fmt},
                                   f'Change item {item} to {fmt}. Its schedule and media approval reset; same item is rechecked.')
        if tool == 'replace_source':
            return submit('replace_source', {'url': args.get('url')})
        if tool == 'update_caption':
            a = {'text': args.get('text') or ''}
            return approval_or_run('update_caption', a, f"New caption for item {item}:\n\n{a['text']}")
        if tool == 'approve_existing_caption':
            with self.ops.store.read() as c:
                it = self.ops.item(c, item)
            if not it['caption']:
                return {'state': 'rejected', 'reason': 'This item has no caption to approve'}
            if not re.search(r'(اعتمد|موافق|approve|ok|تمام)', (text or '').casefold()):
                return approval_or_run('update_caption', {'text': it['caption']},
                                       f"Approve the existing caption for item {item}:\n\n{it['caption']}")
            return submit('update_caption', {'text': it['caption']}, True)
        if tool == 'approve_all_existing_captions':
            with self.ops.store.tx() as c:
                return {'state': 'awaiting_approval', **self.ops.propose_legacy_captions(c, actor, thread)}
        if tool == 'confirm_topaz':
            return approval_or_run('confirm_topaz', {'confirmed': True, 'asset_key': None},
                                   f'Confirm Topaz processing for the currently selected file of item {item}.')
        if tool == 'resolve_publication':
            outcome = 'published' if args.get('published') is True else 'not_published'
            return approval_or_run('resolve_outcome', {'outcome': outcome, 'media_id': args.get('media_id')},
                                   f'Record that item {item} was {outcome.replace("_", " ")} on Instagram.')
        if tool == 'draft_caption':
            text_ = (args.get('text') or '').strip()
            ih = hashlib.sha256(dumps({'item': item, 'bondok_draft': text_}).encode()).hexdigest()
            return self.ops.submit(Command(cid, 'caption_draft', 'service:bondok', 'service:bondok', item,
                                           {'input_hash': ih, 'text': text_, 'model': 'bondok'}))
        raise Rejected('Unknown tool', 'unsupported')

    def approve(self, pid, actor, thread, event_id, reject=False) -> dict:
        if actor != self.owner:
            return {'state': 'rejected', 'reason': 'Only the owner can approve or reject proposals.'}
        op = 'reject_proposal' if reject else 'approve_proposal'
        return self.ops.submit(Command(f'slack:{event_id}:{op}', op, actor, 'owner', None, {'proposal_id': pid},
                                       auth={'thread': thread}))
