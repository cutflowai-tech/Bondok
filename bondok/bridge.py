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

RECENT_FILE_CHANGE = 6 * 3600
MUTATING = {'pause_item', 'resume_item', 'skip_item', 'request_recheck', 'request_reschedule', 'request_publish',
            'change_format', 'replace_source', 'update_caption', 'approve_existing_caption', 'confirm_topaz',
            'resolve_publication', 'draft_caption', 'approve_all_existing_captions'}

_DIACRITICS = re.compile('[\u0640\u064B-\u0652]')
_PREFIX_NOUN = ('', 'ل', 'ال', 'لل', 'و', 'وال', 'ب', 'بال', 'ف', 'فال')
_PREFIX_VERB = ('', 'و', 'ف')
_SUFFIX = ('', 'ه', 'ها', 'هم', 'ي', 'و', 'ت', 'وا')
_QUESTION_START = {'هل', 'ليه', 'ايه', 'امتي', 'ازاي', 'فين', 'مين', 'is', 'are', 'has', 'have', 'did', 'does', 'do',
                   'was', 'were', 'what', 'why', 'when', 'how', 'can', 'could', 'should', 'will', 'would'}
_NEGATION = {'مش', 'مو', 'لا', 'لم', 'لن', 'مافيش', 'مفيش', 'لسه', 'بلاش', 'not', 'no', 'never', 'dont', 'doesnt',
             'didnt', 'isnt', 'wasnt', 'arent', 'havent', 'hasnt', 'wont', 'cant', 'yet', 'without'}


def _norm(text: str) -> str:
    t = _DIACRITICS.sub('', (text or '').casefold())
    t = re.sub('[أإآ]', 'ا', t).replace('ى', 'ي').replace('ة', 'ه').replace("'", '').replace('\u2019', '')
    return t


def _tokens(text: str) -> list[str]:
    return re.findall(r'[\w]+', _norm(text))


def _has(tokens, stems, prefixes=_PREFIX_NOUN, suffixes=_SUFFIX) -> bool:
    """A whole word from `stems`, allowing Arabic attached prefixes/suffixes (never a substring of
    another word: 'postpone' is not 'post', 'ابريل' is not 'ريل')."""
    stems = {_norm(s) for s in stems}
    for tok in tokens:
        for p in prefixes:
            if not tok.startswith(p):
                continue
            core = tok[len(p):]
            for s in suffixes:
                if s and not core.endswith(s):
                    continue
                if (core[:-len(s)] if s else core) in stems:
                    return True
    return False


def _statement(text: str, tokens) -> bool:
    """A plain affirmative statement/instruction: no question, no negation anywhere."""
    if '?' in text or '؟' in text or (tokens and tokens[0] in _QUESTION_START):
        return False
    if any(t in _NEGATION or (t.startswith('م') and t.endswith('ش') and len(t) >= 4) for t in tokens):
        return False
    # "ما اتنشرش" (negation split over two words); a plain "ما" ("زي ما قلتلك") is not a negation.
    return not any(a == 'ما' and b.endswith('ش') for a, b in zip(tokens, tokens[1:]))


_FORMAT_WORDS = {'Post': ('بوست', 'ريل', 'ريلز', 'post', 'reel', 'reels'),
                 'Story': ('ستوري', 'استوري', 'story', 'stories')}
_CHANGE = ('حول', 'غير', 'خلي', 'اعمل', 'change', 'convert', 'switch', 'make', 'turn')
_TOPAZ = ('توباز', 'topaz', 'topazed')
_DONE = ('خلص', 'خلاص', 'اتعمل', 'تم', 'اتم', 'جاهز', 'done', 'confirmed', 'finished', 'ready', 'complete', 'completed',
         'topazed')
_RESUME = ('كمل', 'استانف', 'رجع', 'شغل', 'resume', 'unpause', 'continue')
_PAUSE = ('وقف', 'ايقاف', 'pause', 'paused', 'hold')
_SKIP = ('تخطي', 'اتخطي', 'تخطا', 'اتخطا', 'سكيب', 'skip', 'الغي', 'cancel')
_PUBLISHED = ('اتنشر', 'منشور', 'published', 'posted', 'live')
_PUBLISHED_EXACT = {'نزلت', 'نزلتي', 'اتنزل', 'اتنزلت'}
_APPROVE = ('اعتمد', 'موافق', 'approve', 'approved', 'ok', 'okay', 'تمام')


def explicit(op: str, args: dict, text: str) -> bool:
    """True only when the owner's own words plainly state this exact action. Questions, negations and
    words that merely contain a keyword never count (fail closed: the action becomes a proposal)."""
    toks = _tokens(text)
    if not _statement(text or '', toks):
        return False
    if op == 'change_format':
        target = args.get('format')
        if target not in _FORMAT_WORDS or not _has(toks, _CHANGE, _PREFIX_VERB):
            return False
        other = 'Story' if target == 'Post' else 'Post'
        pos = lambda words: max((i for i, t in enumerate(toks) if _has([t], words)), default=-1)
        return pos(_FORMAT_WORDS[target]) > pos(_FORMAT_WORDS[other])   # the destination is named last
    if op == 'confirm_topaz':
        return _has(toks, _TOPAZ) and _has(toks, _DONE, _PREFIX_VERB)
    if op == 'resume':
        return _has(toks, _RESUME, _PREFIX_VERB)
    if op == 'skip':
        return _has(toks, _SKIP, _PREFIX_VERB) and not _has(toks, _PAUSE + _RESUME)
    if op == 'resolve_outcome':
        # "Not published" re-opens scheduling and could cause a duplicate post: always an owner approval.
        return args.get('outcome') == 'published' and (_has(toks, _PUBLISHED, _PREFIX_VERB) or
                                                        any(t in _PUBLISHED_EXACT for t in toks))
    if op == 'approve_caption':
        return _has(toks, _APPROVE, _PREFIX_VERB)
    if op == 'update_caption':
        caption = (args.get('text') or '').strip()
        body = (text or '').strip()
        # The owner supplied the exact text, and the message is essentially that text plus a short instruction.
        return len(caption) >= 10 and caption in body and len(body) - len(caption) <= 80
    return False


class Bridge:
    def __init__(self, ops: Ops, owner_id: str):
        self.ops, self.owner = ops, owner_id

    # ------------------------------------------------------------------ resolution
    def resolve(self, ref) -> dict:
        """Item reference -> exactly one registered social-board item, or candidates."""
        ref = str(ref or '').strip()
        with self.ops.store.read() as c:
            ref = ref.translate(str.maketrans('٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹', '01234567890123456789'))
            if re.fullmatch(r'[0-9]{1,20}', ref):
                r = c.execute('SELECT item_id,name,code,format FROM ops_items WHERE item_id=?', (ref,)).fetchone()
                if r:
                    return {'item_id': r['item_id']}
                return {'error': 'This id is not an item of the For Social Media board'}
            norm = re.sub(r'\s+', '', ref).upper()
            rows = c.execute('SELECT item_id,name,code,format,owner_state,publication FROM ops_items').fetchall()
            exact = [r for r in rows if re.sub(r'\s+', '', r['code'] or '').upper() == norm]
            # An exact name wins over partial matches ("Calli 3" is not "Calli 30"); a partial match is used
            # only when it is unique AND bounded by word edges (audit M3).
            named = [r for r in rows if ref and (r['name'] or '').strip().casefold() == ref.casefold()]
            word = re.compile(r'(?<![\w])' + re.escape(ref.casefold()) + r'(?![\w])') if ref else None
            hits = exact or named or [r for r in rows if word and word.search((r['name'] or '').casefold())]
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
                prior = c.execute('SELECT state, result FROM ops_commands WHERE id=?', (cid,)).fetchone()
                if prior:   # same request again in this turn: same rendering as the first time (audit M6)
                    return {**json.loads(prior['result'] or '{}'), 'state': prior['state'], 'duplicate': True}
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
            if not explicit('approve_caption', {}, text):
                return approval_or_run('update_caption', {'text': it['caption']},
                                       f"Approve the existing caption for item {item}:\n\n{it['caption']}")
            return submit('update_caption', {'text': it['caption']}, True)
        if tool == 'approve_all_existing_captions':
            with self.ops.store.tx() as c:
                return {'state': 'awaiting_approval', **self.ops.propose_legacy_captions(c, actor, thread)}
        if tool == 'confirm_topaz':
            # Bound to the exact file version selected now; if the selection changed recently the owner may
            # mean the previous file, so it becomes an approval naming the file (audit MP5).
            with self.ops.store.read() as c:
                it = self.ops.item(c, item)
            changed = (json.loads(it['observed'] or '{}') or {}).get('_file_changed_at')
            a = {'confirmed': True, 'asset_key': it['asset_key']}
            summary = (f"Confirm Topaz for item {item}: file {it.get('file_name') or '?'} "
                       f"(version {it['asset_key'] or 'none selected'}).")
            if changed and self.ops.now() - changed < RECENT_FILE_CHANGE:
                with self.ops.store.tx() as c:
                    p = self.ops.propose_command(c, 'confirm_topaz', item, a, summary + ' The selected file changed '
                                                 'recently; approve only if this exact version was Topazed.', actor, thread)
                return {'state': 'awaiting_approval', **p}
            return approval_or_run('confirm_topaz', a, summary)
        if tool == 'resolve_publication':
            outcome = 'published' if args.get('published') is True else 'not_published'
            return approval_or_run('resolve_outcome', {'outcome': outcome, 'media_id': args.get('media_id')},
                                   f'Record that item {item} was {outcome.replace("_", " ")} on Instagram.')
        if tool == 'draft_caption':
            text_ = (args.get('text') or '').strip()
            ih = hashlib.sha256(dumps({'item': item, 'bondok_draft': text_}).encode()).hexdigest()
            r = self.ops.submit(Command(cid, 'caption_draft', 'service:bondok', 'service:bondok', item,
                                        {'input_hash': ih, 'text': text_, 'model': 'bondok'}))
            # A draft is a proposal (or an invalid draft), never "done" (audit M2).
            if r.get('draft_state') == 'pending_approval':
                return {**r, 'state': 'awaiting_approval'}
            if r.get('draft_state') == 'invalid':
                return {**r, 'state': 'rejected', 'reason': 'Draft not used: ' + (r.get('reason') or 'invalid')}
            return r
        raise Rejected('Unknown tool', 'unsupported')

    def approve(self, pid, actor, thread, event_id, reject=False) -> dict:
        if actor != self.owner:
            return {'state': 'rejected', 'reason': 'Only the owner can approve or reject proposals.'}
        op = 'reject_proposal' if reject else 'approve_proposal'
        return self.ops.submit(Command(f'slack:{event_id}:{op}', op, actor, 'owner', None, {'proposal_id': pid},
                                       auth={'thread': thread}))
