"""Bondok record: the business facts Monday keeps for each item (ADR decision 1, R2 authority migration).

The record is one long-text column (``board.COL['record']``) written by the handler with the display projection.
It holds what the board's own columns cannot show but a cache rebuild must not lose: the Topaz binding, the
caption approval fingerprint, requested vs confirmed time and the reservation origin, the owner's pause/skip,
holds, the selected source, and publication outcome and content identity (duplicate protection).

* Written only while the board has the column (observed in the item's snapshot); a board without it gets no
  write to a missing column.
* Never an owner command: a changed or damaged record on the board is restored from committed state.
* Restore (``_bootstrap``): an item missing from the store whose board record is valid comes back with these
  facts; the board's owner columns still win where they changed meanwhile (they are applied as owner edits
  against the status/time the record says were shown). Media is verified again before scheduling.
"""
from __future__ import annotations

import hashlib
import json

from . import board, rules
from .db import dumps, loads

RECORD_SCHEMA = 1
RECORD_MAX = 2000             # Monday long text limit (characters)


def caption_fp(text) -> str | None:
    return hashlib.sha256(str(text).encode()).hexdigest()[:16] if text else None


def encode(rec: dict) -> str:
    return json.dumps(rec, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def parse(text, item_id) -> dict | None:
    """A valid record for this item, else None (damaged, foreign or from a newer schema)."""
    try:
        rec = json.loads(text) if text else None
    except ValueError:
        return None
    if not isinstance(rec, dict) or rec.get('s') != RECORD_SCHEMA or str(rec.get('id')) != str(item_id):
        return None
    return rec


class RecordMixin:
    def business_record(self, c, it: dict, shown: dict) -> str:
        """Compact record of committed facts. ``shown`` is the display being written (status, Publish at)."""
        res = self.reservation(c, it['item_id'])
        hold = loads(it.get('hold'), None)
        m = self.media_row(c, it.get('verification_id')) if it.get('verification_id') else None
        sha = (loads(m['metadata'], {}) or {}).get('sha256') if m else None
        last = c.execute("SELECT media_id FROM ops_attempts WHERE item_id=? AND stage='published' "
                         'ORDER BY updated DESC LIMIT 1', (it['item_id'],)).fetchone()
        rec = {
            's': RECORD_SCHEMA, 'id': str(it['item_id']), 'f': it.get('format'),
            'cap': caption_fp(it.get('caption')) if it.get('caption_state') == 'approved' else None,
            'co': it.get('caption_origin') if it.get('caption_state') == 'approved' else None,
            'tz': it.get('topaz_asset'), 'src': it.get('source_override_url'),
            'req': it.get('requested_at'), 'rb': it.get('requested_by'),
            'os': it.get('owner_state'), 'osr': (it.get('owner_state_reason') or '')[:120] or None,
            'hold': {'kind': hold.get('kind'), 'reason': str(hold.get('reason') or '')[:240]} if hold else None,
            'pub': it.get('publication'), 'lp': 1 if it.get('legacy_posted') else None,
            'mid': last['media_id'] if last else None,
            'ch': it.get('content_hash'), 'sha': sha,
            'res': {'slot': res['slot'], 'o': res['origin'], 'p': 1 if res['owner_pinned'] else 0} if res else None,
            'st': board.compare_value('status', shown.get('status')),
            'pa': board.compare_value('publish_at', shown.get('publish_at')),
        }
        rec = {k: v for k, v in rec.items() if v is not None}
        text = encode(rec)
        for drop in ('src', 'osr', 'hold'):          # stay within the column limit; identity fields are kept
            if len(text) <= RECORD_MAX:
                break
            if drop == 'hold' and 'hold' in rec:
                rec['hold'] = {'kind': rec['hold']['kind']}
            else:
                rec.pop(drop, None)
            text = encode(rec)
        return text

    def restore_from_record(self, c, iid, snap, rec) -> dict:
        """Facts for a store row rebuilt from the board record (owner columns on the board win)."""
        fmt = snap.get('format') if snap.get('format') in rules.FORMATS else None
        same_format = fmt is not None and fmt == rec.get('f')
        caption = snap.get('caption')
        if fmt == 'Post':
            if caption and rec.get('cap') and caption_fp(caption) == rec['cap']:
                cstate, corigin = 'approved', rec.get('co') or 'approved_draft'
            else:
                cstate, corigin = ('legacy_unapproved', 'legacy') if caption else ('missing', None)
        else:
            cstate, corigin = None, None
        topaz = rec.get('tz') if same_format and snap.get('topaz') == 'Topazed' else None
        hold = rec.get('hold')
        pub = rec.get('pub') if rec.get('pub') in ('not_started', 'published', 'outcome_unknown', 'failed') else \
            ('outcome_unknown' if rec.get('pub') == 'in_progress' else 'not_started')
        return {
            'format': fmt, 'caption_state': cstate, 'caption_origin': corigin, 'topaz_asset': topaz,
            'source_override_url': rec.get('src') if same_format else None,
            # An owner-pinned slot comes back as the owner's request (as when a pinned reservation is released).
            'requested_at': rec.get('req') or (rec['res']['slot'] if (rec.get('res') or {}).get('p') else None),
            'requested_by': rec.get('rb') if rec.get('req') else None,
            'owner_state': rec.get('os') if rec.get('os') in ('active', 'paused', 'skipped') else 'active',
            'owner_state_reason': rec.get('osr'),
            'hold': dumps(hold) if hold and hold.get('kind') else None,
            'publication': pub, 'legacy_posted': 1 if (rec.get('lp') or pub == 'published') else 0,
            'content_hash': rec.get('ch') if same_format else None,
        }

    def restore_side_effects(self, c, iid, rec, fields):
        """Journal identities of published content (duplicate protection). Reservations are not restored: the media
        is verified again first (an unverified item never holds a slot); the owner's requested time comes back as a
        request and is honoured when ready, an automatic slot is chosen again."""
        if fields['publication'] in ('published', 'outcome_unknown') and fields['format']:
            for ident in ({f"{rules.ACCOUNT}|{fields['format']}|src:{rec['ch']}"} if rec.get('ch') else set()) | \
                         ({f"{rules.ACCOUNT}|{fields['format']}|{rec['sha']}"} if rec.get('sha') else set()):
                c.execute('INSERT OR IGNORE INTO assets VALUES(?,?)', (ident, str(iid)))
