"""Monday board contract for board 5105608159 (For Social Media).

Monday is the human interface, not proof of readiness/reservation/publication.
* ``HUMAN`` columns carry human intent. The system never overwrites them from a
  snapshot; it writes them only to apply an explicit authorized command.
* ``SYSTEM`` columns are display projections of committed state. A value that
  differs from both the confirmed and the pending projection is a human edit
  and is converted into a command (or reverted with an explanation).
"""
from __future__ import annotations

import json
import re

from . import rules

COL = {
    'status': 'status', 'format': 'color_mm7xm9b6', 'caption': 'long_text_mm7x2ay1',
    'code': 'text_mm7xqn4e', 'style': 'text_mm7y5kd1', 'variety': 'text_mm7yjmqd',
    'collab': 'text_mm7yhjf1', 'owner': 'multiple_person_mm7yy4tk', 'notes': 'text_mm7xaf3t',
    'folder': 'link_mm7xaep2', 'dropbox': 'link_mm7x8dy7', 'topaz': 'color_mm7xe2j2',
    'version_check': 'text_mm7xemtq', 'measurements': 'text_mm7zjt4m', 'video': 'link_mm7ywc0w',
    'publish_at': 'date_mm7y8s9t', 'post_date': 'date4', 'post_time': 'hour_mm7xy9cf',
    'published_at': 'date_mm7yr4h3', 'ig_media': 'text_mm7yfqhb', 'post_link': 'link_mm7xb56a',
    'action': 'long_text_mm7zbtbn', 'system': 'long_text_mm7ysrbz', 'checked': 'date_mm7zd2b9',
    'source_item': 'text_mm7y4h4a', 'asset': 'text_mm7yy451', 'media': 'text_mm7yp8h',
    'processed': 'text_mm7z139h', 'numbers': 'numeric_mm7xade9',
}
KIND = {
    'status': 'status', 'format': 'status', 'topaz': 'status', 'caption': 'long', 'action': 'long',
    'system': 'long', 'owner': 'people', 'folder': 'link', 'dropbox': 'link', 'video': 'link',
    'post_link': 'link', 'publish_at': 'datetime', 'published_at': 'datetime', 'checked': 'datetime',
    'post_date': 'date', 'post_time': 'hour', 'numbers': 'text',
}
HUMAN = ('format', 'caption', 'code', 'variety', 'collab', 'owner', 'notes', 'topaz')
# System display columns. 'numbers' and legacy columns not listed are never touched.
SYSTEM = ('status', 'folder', 'action', 'system', 'publish_at', 'post_date', 'post_time', 'media', 'video',
          'measurements', 'processed', 'asset', 'version_check', 'dropbox', 'source_item', 'style',
          'published_at', 'ig_media', 'post_link', 'checked')
SNAPSHOT_COLUMNS = sorted({COL[k] for k in HUMAN + SYSTEM})

GROUPS = {'Post': 'topics', 'Story': 'group_mm7xagm', 'Posted': 'group_title', 'Skipped': 'group_mm7y8mkr'}
LABELS = {
    'unscheduled': 'Unscheduled', 'ready': 'Redy For Scheduled', 'working': 'Working on it',
    'scheduled': 'Scheduled', 'publishing': 'Publishing', 'posted': 'Posted', 'review': 'Needs Review',
    'editor': 'Waiting for Editor', 'checking': 'جاري فحص الفيديو', 'long_story': 'ستوري طويل',
    'paused': 'Paused', 'skipped': 'Skipped',
}


def _raw(item, key):
    cid = COL[key]
    return next((c for c in item.get('column_values') or [] if c.get('id') == cid), None)


def _value(cell):
    if not cell:
        return None
    v = cell.get('value')
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


def norm(item: dict, key: str):
    """Comparable string form of one column (None when empty)."""
    cell = _raw(item, key)
    if cell is None:
        return None
    kind = KIND.get(key, 'text')
    text = (cell.get('text') or '').strip()
    v = _value(cell)
    if kind == 'status':
        return text or None
    if kind == 'long':
        if isinstance(v, dict) and isinstance(v.get('text'), str):
            return v['text'].strip() or None
        return text or None
    if kind == 'link':
        return (v or {}).get('url') or None if isinstance(v, dict) else (text or None)
    if kind == 'datetime':
        d = rules.parse_monday_publish_at(v) if isinstance(v, dict) else None
        if d:
            return rules.iso(d)
        return (v or {}).get('date') or None if isinstance(v, dict) else None
    if kind == 'date':
        return (v or {}).get('date') or None if isinstance(v, dict) else None
    if kind == 'hour':
        if isinstance(v, dict) and v.get('hour') is not None:
            return f"{int(v['hour']):02d}:{int(v.get('minute') or 0):02d}"
        return None
    if kind == 'people':
        people = (v or {}).get('personsAndTeams') if isinstance(v, dict) else None
        return ','.join(sorted(str(p.get('id')) for p in people or [])) or None
    return text or None


def snapshot(item: dict) -> dict:
    out = {k: norm(item, k) for k in HUMAN + SYSTEM}
    out['name'] = item.get('name')
    out['group'] = (item.get('group') or {}).get('id')
    return out


def date_only(value) -> str | None:
    """A Publish at value picked without a time (Monday stores only the date): 'YYYY-MM-DD', else None."""
    s = str(value or '').strip()
    return s if re.fullmatch(r'\d{4}-\d{2}-\d{2}', s) else None


def requested_instant(snap: dict):
    """Requested publication time from the board. Publish at (UTC value) wins;
    legacy Post Date/Time (Cairo wall time) is used only when Publish at is
    empty, i.e. never as a fallback after a newer request was cleared."""
    if snap.get('publish_at'):
        try:
            return rules.instant(snap['publish_at'])
        except (ValueError, rules.RuleError):
            return None
    if snap.get('post_date') and snap.get('post_time'):
        h, m = snap['post_time'].split(':')
        return rules.parse_monday_legacy({'date': snap['post_date']}, {'hour': int(h), 'minute': int(m)})
    return None


def mutation_value(key: str, value):
    """Monday change_multiple_column_values representation for a projection."""
    kind = KIND.get(key, 'text')
    if kind == 'status':
        return {'label': value} if value else {}
    if kind == 'long':
        return {'text': value or ''}
    if kind == 'link':
        if not value:
            return {}
        url, label = (value if isinstance(value, (list, tuple)) else (value, value))
        return {'url': url, 'text': label}
    if kind == 'datetime':
        if not value:
            return {}
        if date_only(value):                  # an owner's date without a time is shown as it was entered
            return {'date': date_only(value)}
        return rules.monday_publish_at_value(rules.instant(value))
    if kind == 'date':
        return {'date': value} if value else {}
    if kind == 'hour':
        if not value:
            return {}
        h, m = value.split(':')
        return {'hour': int(h), 'minute': int(m)}
    return value or ''


def compare_value(key, value):
    """Normalise a projection value the same way ``norm`` reads it back."""
    if isinstance(value, (list, tuple)):
        value = value[0]
    if value in ('', None):
        return None
    if KIND.get(key) == 'datetime':
        try:
            return rules.iso(rules.instant(value))
        except (ValueError, TypeError, rules.RuleError):
            return str(value).strip()          # date without a time: compared as read (audit R5 A3)
    return str(value).strip()
