"""Business rules shared by every caller (n8n helper, Bondok, supervisor).

One implementation of: slot grid, Cairo time contract, media gates, code/style
parsing and deterministic style fairness. Values marked DEFAULT were discovered
in the deployed helper (2026-10-09, sha256 8598b0ed...) and are preserved as-is;
they are implementation defaults, not new owner policy.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Africa/Cairo')
UTC = timezone.utc

ACCOUNT = '<IG_ACCOUNT>'               # IG user id: a placeholder in the public repository, filled in by
                                       # deploy/build_release.py --site (private); unresolved -> not deployable (R5 LOW-18)
BOARD = '5105608159'                   # For Social Media; Bondok business-tool boundary
SOURCE_BOARD = '5091110326'            # Source projects; integration-only access
EDITOR_SUBITEM_BOARD = '5091137380'    # Editor task subitems; integration-only access

FORMATS = ('Post', 'Story')
# Python weekday(): Monday=0 ... Sunday=6
POST_SLOTS = {5: (21, 0), 0: (21, 0), 2: (22, 45), 3: (21, 0)}   # Sat, Mon, Wed, Thu
STORY_SLOTS = ((11, 0), (14, 0), (18, 0), (21, 0), (22, 0))

BUSINESS_MAX_BYTES = 300_000_000       # business rule: strictly below
# Transfer-path constraint, not a business rule: WF1 uploads the prepared file
# with a single Dropbox /2/files/upload request (150 MiB per request). Outputs at
# or above this are refused until an upload-session path is approved and built.
TRANSFER_MAX_BYTES = 145_000_000
MIN_SHORT_EDGE = 1080
STORY_MAX_SECONDS = 60.0               # strictly below; exactly 60.000 fails (final prepared file)
# Owner decision 2026-10-09: a Story source from 60 through 65 seconds inclusive is trimmed at the END to
# STORY_TRIM_SECONDS during preparation (deterministic FFmpeg, original Dropbox file untouched); a longer
# source still needs a shorter edit from the editor.
STORY_TRIM_MAX_SECONDS = 65.0
STORY_TRIM_SECONDS = 59.9
MEDIA_MIN_FREE_BYTES = 6_000_000_000     # free disk needed before a media download starts
MEDIA_RESERVE_BYTES = 2_000_000_000      # a running download stops below this
QA_POLICY = 3                          # DEFAULT: deployed media verification policy

HORIZON_DAYS = 84                      # DEFAULT
RESERVE_LEAD = timedelta(minutes=5)    # DEFAULT: earliest slot for automatic allocation
NEAR_DUE = timedelta(minutes=10)       # DEFAULT: supervisor/Bondok do not move near-due slots
LATE_WINDOW = timedelta(hours=2)       # DEFAULT: publisher accepts 0..2h after slot
VARIETY_FALLBACK = {'Post': timedelta(days=7), 'Story': timedelta(days=1)}  # DEFAULT

CODE_RE = re.compile(r'^\s*([a-z]{2,3})\s*[#_\- ]?\s*\d+', re.I)


# ---------------------------------------------------------------- owner policy (preferences the owner can change)
# Every business rule above that is the owner's preference is also a named policy. The values here are the defaults
# carried over unchanged from the rules in force before the agent migration (2026-10-14, P1-02): no rule changes
# value by being moved. The owner changes a policy (versioned, ``Ops.op_set_policy``) or exempts one item from it
# (``Ops.op_waive_rule``). Platform limits (PLATFORM below) are facts of Instagram/Dropbox, not preferences: an owner
# instruction cannot move them, and the reason given says so.
POLICY = {
    'topaz_required': {'default': True, 'type': 'bool', 'waivable': True,
                       'label': 'Topaz confirmation is required before publishing'},
    'min_short_edge': {'default': MIN_SHORT_EDGE, 'type': 'int', 'min': 240, 'max': 1920, 'waivable': True,
                       'label': 'Minimum short edge of the published video (pixels)'},
    'max_bytes': {'default': BUSINESS_MAX_BYTES, 'type': 'int', 'min': 1_000_000, 'max': 300_000_000,
                  'label': 'Maximum size of the published video (bytes, strictly below)'},
    'story_max_seconds': {'default': STORY_MAX_SECONDS, 'type': 'float', 'min': 3.0, 'max': 60.0,
                          'label': 'Story length limit (seconds, strictly below; Instagram allows 60 at most)'},
    'story_trim_max_seconds': {'default': STORY_TRIM_MAX_SECONDS, 'type': 'float', 'min': 0.0, 'max': 600.0,
                               'label': 'Longest Story source that is trimmed automatically at the end (seconds)'},
    'post_caption_approval': {'default': True, 'type': 'bool',
                              'label': 'A Post needs an owner-approved caption before it is scheduled'},
    'duplicate_video_block': {'default': True, 'type': 'bool', 'waivable': True,
                              'label': 'The same video is not published twice on the account'},
    'story_slots': {'default': [f'{h:02d}:{m:02d}' for h, m in STORY_SLOTS], 'type': 'times',
                    'label': 'Daily Story slots for automatic scheduling (Cairo)'},
    'post_slots': {'default': {'sat': '21:00', 'mon': '21:00', 'wed': '22:45', 'thu': '21:00'}, 'type': 'weekly',
                   'label': 'Weekly Post/Reel slots for automatic scheduling (Cairo)'},
    'late_window_minutes': {'default': int(LATE_WINDOW.total_seconds() // 60), 'type': 'int', 'min': 0,
                            'max': 24 * 60, 'label': 'How late a missed slot may still be published (minutes)'},
    # Operating policy (P1-06). Defaults are the behaviour in force before the migration, except where noted.
    'board_freshness_minutes': {'default': 15, 'type': 'int', 'min': 0, 'max': 7 * 24 * 60,
                                'label': 'When Monday cannot be read just before publishing, publish only if Bondok '
                                         'received the item\'s board state within this many minutes (0 = always '
                                         'read it fresh)'},
    'allowed_boards': {'default': [BOARD, SOURCE_BOARD, EDITOR_SUBITEM_BOARD], 'type': 'ids',
                       'label': 'Monday boards Bondok may write'},
    'dropbox_roots': {'default': ['/Social Media'], 'type': 'paths',
                      'label': 'Dropbox folders whose files Bondok may rename, move or delete'},
    'occupied_slot_needs_approval': {'default': True, 'type': 'bool',
                                     'label': 'An owner time already used by another item moves that item only after '
                                              'one approval naming both (owner decision option B, 2026-10-10)'},
    'require_collab': {'default': True, 'type': 'bool', 'waivable': True,
                       'label': 'An item with IG Collab is published only with its collaborators (Instagram supports '
                                'collaborators on Reels, not on Stories)'},
    'agent_max_steps': {'default': 6, 'type': 'int', 'min': 1, 'max': 30,
                        'label': 'Model steps per owner message (cost and loop limit)'},
    'publish_attempts_per_slot': {'default': 3, 'type': 'int', 'min': 1, 'max': 10,
                                  'label': 'Instagram containers tried for one slot before stopping'},
}
WAIVABLE = tuple(k for k, v in POLICY.items() if v.get('waivable'))
# Instagram Graph API (IG User Media reference, read 2026-10-14) and the current transfer path. Not owner policy.
PLATFORM = {
    'story_max_seconds': 60.0, 'story_max_bytes': 100_000_000, 'reel_max_bytes': 300_000_000,
    'min_seconds': 3.0, 'reel_max_seconds': 900.0, 'max_columns': 1920, 'transfer_max_bytes': TRANSFER_MAX_BYTES,
    'containers_per_day': 400, 'posts_per_day': 100,
}
_WEEKDAY_KEYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')


def policy_defaults() -> dict:
    import copy
    return {k: copy.deepcopy(v['default']) for k, v in POLICY.items()}


def _hhmm(v) -> str:
    m = re.fullmatch(r'\s*([01]?\d|2[0-3])[:.]([0-5]\d)\s*', str(v))
    if not m:
        raise RuleError(f'"{v}" is not a time (HH:MM)')
    return f'{int(m[1]):02d}:{m[2]}'


def coerce_policy(name: str, value):
    """Validated value for a policy, or RuleError naming the problem (and the platform limit when that is the cause)."""
    spec = POLICY.get(name)
    if spec is None:
        raise RuleError(f'Unknown rule "{name}". Known rules: ' + ', '.join(sorted(POLICY)))
    t = spec['type']
    if t == 'bool':
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in ('true', 'yes', 'on', '1', 'required', 'مطلوب', 'شرط'):
            return True
        if s in ('false', 'no', 'off', '0', 'not_required', 'مش شرط', 'مش مطلوب'):
            return False
        raise RuleError(f'{name} is on/off (true or false)')
    if t in ('int', 'float'):
        try:
            v = int(value) if t == 'int' else float(value)
        except (TypeError, ValueError):
            raise RuleError(f'{name} must be a number') from None
        if v < spec.get('min', v) or v > spec.get('max', v):
            limit = (' (Instagram/platform limit)' if name in ('story_max_seconds', 'max_bytes') else '')
            raise RuleError(f"{name} must be between {spec['min']:g} and {spec['max']:g}{limit}")
        return v
    if t == 'times':
        vals = sorted({_hhmm(x) for x in (value if isinstance(value, (list, tuple)) else str(value).split(','))
                       if str(x).strip()})
        if not vals:
            raise RuleError(f'{name} needs at least one time')
        return vals
    if t in ('ids', 'paths'):
        vals = value if isinstance(value, (list, tuple)) else [x for x in re.split(r'[,\n]', str(value))]
        vals = [str(x).strip() for x in vals if str(x).strip()]
        if not vals:
            raise RuleError(f'{name} needs at least one value')
        if t == 'ids' and not all(re.fullmatch(r'\d{3,20}', x) for x in vals):
            raise RuleError(f'{name} is a list of Monday board ids')
        if t == 'paths' and not all(x.startswith('/') for x in vals):
            raise RuleError(f'{name} is a list of Dropbox folders starting with /')
        return sorted(set(vals))
    if t == 'weekly':
        if not isinstance(value, dict):
            raise RuleError(f'{name} is a map of weekday to time, e.g. {{"sat": "21:00"}}')
        out = {}
        for k, v in value.items():
            k = str(k).strip().lower()[:3]
            if k not in _WEEKDAY_KEYS:
                raise RuleError(f'"{k}" is not a weekday (mon..sun)')
            if v not in (None, ''):
                out[k] = _hhmm(v)
        if not out:
            raise RuleError(f'{name} needs at least one weekday slot')
        return out
    raise RuleError(f'{name} cannot be set')


def story_slots(policy=None):
    vals = (policy or {}).get('story_slots')
    return tuple(tuple(map(int, x.split(':'))) for x in vals) if vals else STORY_SLOTS


def post_slots(policy=None):
    vals = (policy or {}).get('post_slots')
    if not vals:
        return POST_SLOTS
    return {_WEEKDAY_KEYS.index(k): tuple(map(int, v.split(':'))) for k, v in vals.items()}


def late_window(policy=None) -> timedelta:
    v = (policy or {}).get('late_window_minutes')
    return LATE_WINDOW if v is None else timedelta(minutes=int(v))


class RuleError(ValueError):
    """A request that violates a business rule. Message is owner-facing."""


# ---------------------------------------------------------------- time contract
def instant(value) -> datetime:
    """Parse an absolute instant and normalise to UTC (seconds precision).

    Naive timestamps are refused: callers must state the zone. Equivalent
    instants written with different offsets normalise to the same value.
    """
    if isinstance(value, datetime):
        d = value
    else:
        s = str(value).strip().replace('Z', '+00:00')
        d = datetime.fromisoformat(s)
    if d.tzinfo is None:
        raise RuleError('Timestamp must include a timezone')
    return d.astimezone(UTC).replace(microsecond=0)


def iso(d: datetime) -> str:
    """Canonical storage form: UTC, seconds, 'Z' suffix. Used for every compare."""
    return instant(d).strftime('%Y-%m-%dT%H:%M:%SZ')


def cairo_local(year, month, day, hour, minute) -> datetime:
    """Cairo wall-clock time to an absolute instant (handles DST via tzdata).

    Non-existent or ambiguous wall times (DST transitions) are refused rather
    than silently shifted, so a requested time is never moved without telling
    the owner.
    """
    try:
        naive = datetime(year, month, day, hour, minute)
    except ValueError as e:              # e.g. 2026-02-30 or 25:00 from a typed request
        raise RuleError(f'Invalid date or time ({e})') from None
    first = naive.replace(tzinfo=TZ, fold=0)
    second = naive.replace(tzinfo=TZ, fold=1)
    # A skipped wall time (spring forward) also has two offsets, so test existence first (audit: it was
    # reported as "ambiguous").
    exists = [d for d in (first, second) if (lambda r: (r.hour, r.minute))(d.astimezone(UTC).astimezone(TZ))
              == (hour, minute)]
    if not exists:
        raise RuleError('This Cairo local time does not exist on that day (clock change); choose another time')
    if first.utcoffset() != second.utcoffset() and len(exists) == 2:
        raise RuleError('This Cairo local time is ambiguous during a clock change; choose another time')
    return exists[0].astimezone(UTC)


def display(d: datetime) -> str:
    """Owner-facing Cairo wall time."""
    return instant(d).astimezone(TZ).strftime('%Y-%m-%d %H:%M (Cairo)')


def monday_publish_at_value(d: datetime) -> dict:
    """Monday date+time column value. Monday stores date+time values in UTC and
    renders them in each viewer's timezone (account zone: Africa/Cairo)."""
    u = instant(d)
    return {'date': u.strftime('%Y-%m-%d'), 'time': u.strftime('%H:%M:%S')}


def monday_legacy_values(d: datetime) -> dict:
    """Legacy Post Date (date4, no time) + Post Time (hour) are Cairo wall time."""
    local = instant(d).astimezone(TZ)
    return {'date4': {'date': local.date().isoformat()},
            'hour_mm7xy9cf': {'hour': local.hour, 'minute': local.minute}}


def parse_monday_publish_at(value) -> datetime | None:
    if not isinstance(value, dict) or not value.get('date') or not value.get('time'):
        return None
    try:
        return instant(f"{value['date']}T{value['time']}+00:00")
    except (ValueError, TypeError):
        return None


def parse_monday_legacy(date_value, hour_value) -> datetime | None:
    if not isinstance(date_value, dict) or not date_value.get('date'):
        return None
    if not isinstance(hour_value, dict) or hour_value.get('hour') is None:
        return None
    try:
        d = date.fromisoformat(date_value['date'])
        return cairo_local(d.year, d.month, d.day, int(hour_value['hour']), int(hour_value.get('minute') or 0))
    except (ValueError, TypeError, RuleError):
        return None


# ---------------------------------------------------------------- slots
def on_grid(fmt: str, d: datetime, policy=None) -> bool:
    local = instant(d).astimezone(TZ)
    if local.second:
        return False
    hm = (local.hour, local.minute)
    if fmt == 'Story':
        return hm in story_slots(policy)
    if fmt == 'Post':
        return post_slots(policy).get(local.weekday()) == hm
    return False


def in_window(at: datetime, window: dict | None) -> bool:
    """Owner scheduling constraints carried with an instruction (contract §4): 'not_before' (UTC instant) and
    'on_date' (a Cairo calendar date, e.g. "tomorrow")."""
    if not window:
        return True
    at = instant(at)
    if window.get('not_before') and at < instant(window['not_before']):
        return False
    if window.get('on_date') and at.astimezone(TZ).date().isoformat() != window['on_date']:
        return False
    return True


def grid(now: datetime, fmt: str, lead: timedelta = RESERVE_LEAD, horizon: int = HORIZON_DAYS, window=None,
         policy=None):
    """Yield every valid slot after now+lead in ascending order (UTC), inside the owner's window if any."""
    now = instant(now)
    start = now.astimezone(TZ).date()
    stories, posts = story_slots(policy), post_slots(policy)
    for offset in range(horizon):
        day = start + timedelta(days=offset)
        if fmt == 'Story':
            times = stories
        else:
            times = [posts[day.weekday()]] if day.weekday() in posts else []
        for hour, minute in times:
            try:
                at = cairo_local(day.year, day.month, day.day, hour, minute)
            except RuleError:
                continue
            if at > now + lead and in_window(at, window):
                yield at


def alternatives(now, fmt, taken: set[str], limit=3, lead=NEAR_DUE, window=None, policy=None):
    out = []
    for at in grid(now, fmt, lead, window=window, policy=policy):
        if iso(at) not in taken:
            out.append(at)
            if len(out) >= limit:
                break
    return out


def validate_requested_slot(now, fmt, at, policy=None) -> datetime:
    at = instant(at)
    if fmt not in FORMATS:
        raise RuleError('Format must be Post or Story')
    if not on_grid(fmt, at, policy):
        raise RuleError('Requested time is outside the agreed ' + fmt + ' schedule')
    if at <= instant(now) + NEAR_DUE:
        raise RuleError('Requested time is in the past or within 10 minutes of publication')
    if at > instant(now) + timedelta(days=HORIZON_DAYS):
        raise RuleError('Requested time is beyond the 84-day scheduling horizon')
    return at


OWNER_MIN_LEAD = timedelta(minutes=1)


def validate_owner_time(now, fmt, at) -> datetime:
    """A time chosen by the owner (board Publish at or Slack): on or off the slot grid (contract §5, postmortem 04).
    The grid applies to automatic allocation only; readiness, conflicts and the commitment point still apply."""
    at = instant(at)
    if fmt not in FORMATS:
        raise RuleError('Format must be Post or Story')
    now = instant(now)
    if at.second:
        raise RuleError('Publication times are set to the minute')
    if at <= now:
        raise RuleError('That time has already passed; choose a future time')
    if at < now + OWNER_MIN_LEAD:
        raise RuleError('That time is less than a minute away; choose a later time')
    if at > now + timedelta(days=HORIZON_DAYS):
        raise RuleError('Requested time is beyond the 84-day scheduling horizon')
    return at


def late_deadline(slot, next_slot=None, policy=None) -> datetime:
    """Latest moment a reserved item may still be published: 2 hours after its slot, and never at or after the
    next reserved slot of the same format (R5 M29: a 21:00 Story must not land on the 22:00 one)."""
    slot = instant(slot)
    end = slot + late_window(policy)
    if next_slot is not None and slot < instant(next_slot) < end:
        end = instant(next_slot)
    return end


# ---------------------------------------------------------------- codes and variety
def style(code: str) -> str:
    m = CODE_RE.match(str(code or ''))
    if not m:
        raise RuleError('Code must begin with 2-3 letters followed by a number')
    return m[1].upper()


def rotation_key(fmt: str, code: str, name: str, variety: str | None) -> str:
    """Variety key used for spreading. Explicit human Story variety wins; the
    derived default is never written back over human input."""
    if variety and variety.strip():
        return variety.strip()
    prefix = style(code)
    if fmt == 'Story':
        m = re.match(r'^([^\W\d_]+)', name or '', re.U)
        return prefix + ':' + (m[1].upper() if m else 'GENERAL')
    return prefix


def choose_slot(now, fmt, key, occupied: list[tuple[datetime, str]], taken: set[str], window=None, policy=None):
    """Deterministic variety preference with bounded fallback (no LLM).

    Prefers the earliest free slot whose neighbours do not share the same
    rotation key; never looks further than VARIETY_FALLBACK past the first free
    slot, so a scarce style cannot starve the queue.
    """
    events = sorted(occupied, key=lambda e: e[0])
    fallback = None
    for at in grid(now, fmt, window=window, policy=policy):
        if iso(at) in taken:
            continue
        if fallback is None:
            fallback = at
        if at - fallback > VARIETY_FALLBACK[fmt]:
            break
        before = [k for d, k in events if d < at]
        after = [k for d, k in events if d > at]
        if (before and before[-1] == key) or (after and after[0] == key):
            continue
        return at
    return fallback


def fair_order(entries: list[dict]) -> list[dict]:
    """Round-robin by rotation key, oldest waiting first inside each key.

    Replaces the LLM 'Diversify Styles' step with a deterministic order.
    entries: dicts with 'rotation' and 'waiting_since' (epoch seconds).
    """
    buckets: dict[str, list[dict]] = {}
    for e in sorted(entries, key=lambda e: (e.get('waiting_since') or 0, str(e.get('item_id')))):
        buckets.setdefault(e.get('rotation') or 'INVALID', []).append(e)
    keys = sorted(buckets, key=lambda k: (buckets[k][0].get('waiting_since') or 0, k))
    out = []
    while any(buckets[k] for k in keys):
        for k in keys:
            if buckets[k]:
                out.append(buckets[k].pop(0))
    return out


# ---------------------------------------------------------------- media gates
def _pv(policy, name):
    v = (policy or {}).get(name)
    return POLICY[name]['default'] if v is None else v


def story_duration_failure(duration, policy=None) -> str | None:
    try:
        d = float(duration)
    except (TypeError, ValueError):
        return 'Story duration could not be measured'
    if not math.isfinite(d) or d <= 0:
        return 'Story duration could not be measured'
    limit = float(_pv(policy, 'story_max_seconds'))
    if d >= limit:
        return (f'Story is {d:g} seconds; it must be strictly under {limit:g} seconds. Supply a shorter edit or ask '
                'the owner to convert it to Post')
    return None


def story_trim_target(duration, policy=None) -> float | None:
    """Target length when a Story source needs the automatic end trim, else None."""
    try:
        d = float(duration)
    except (TypeError, ValueError):
        return None
    limit = float(_pv(policy, 'story_max_seconds'))
    if math.isfinite(d) and limit <= d <= float(_pv(policy, 'story_trim_max_seconds')):
        return min(STORY_TRIM_SECONDS, round(limit - 0.1, 3))
    return None


def story_source_failure(duration, policy=None) -> str | None:
    """Story gate on the selected SOURCE file: under 60 s as is, 60-65 s trimmed automatically,
    longer needs a shorter edit. The prepared file is still held to story_duration_failure."""
    if story_trim_target(duration, policy) is not None:
        return None
    bad = story_duration_failure(duration, policy)
    if bad and bad.startswith('Story is '):
        return (f"Story is {float(duration):g} seconds; automatic trimming covers up to "
                f"{float(_pv(policy, 'story_trim_max_seconds')):g} seconds. Supply an edit under "
                f"{float(_pv(policy, 'story_max_seconds')):g} seconds or ask the owner to convert it to Post")
    return bad


def media_failure(info: dict, fmt: str, policy=None, waived=()) -> str | None:
    """Fail closed on the final prepared file. Topaz is checked separately
    because it is human evidence bound to the source asset, not a measurement. ``waived`` names owner exemptions
    for this item (e.g. ``min_short_edge``); platform limits are never waived."""
    if fmt not in FORMATS:
        return 'A valid Post or Story format is required'
    try:
        width, height = float(info['width']), float(info['height'])
        size, duration = float(info['bytes']), float(info['duration'])
    except (KeyError, TypeError, ValueError):
        return 'Video measurements are missing or invalid'
    if not all(math.isfinite(x) and x > 0 for x in (width, height, size, duration)):
        return 'Video measurements are missing or invalid'
    edge = int(_pv(policy, 'min_short_edge'))
    if min(width, height) < edge and 'min_short_edge' not in waived:
        return f'Video short edge must be at least {edge} pixels'
    limit = int(_pv(policy, 'max_bytes'))
    if size >= limit:
        return f'Final video must be strictly under {limit:,} bytes'
    if size >= TRANSFER_MAX_BYTES:
        return 'Final video exceeds the current single-request Dropbox transfer path (145 MB)'
    if fmt == 'Story':
        return story_duration_failure(duration, policy)
    return None


def validate_caption(text: str) -> str | None:
    """Deterministic checks on Post caption text (human or drafted)."""
    if not isinstance(text, str) or not text.strip():
        return 'Caption is empty'
    if len(text) > 2200:
        return 'Caption exceeds Instagram 2,200 character limit'
    if len(re.findall(r'(?<!\w)#\w+', text)) > 30:
        return 'Caption has more than 30 hashtags'
    # Meta IG User Media reference, `caption`: "Maximum 2200 characters, 30 hashtags, and 20 @ tags" (R5-LOW-15).
    if len(re.findall(r'(?<![\w.@])@[A-Za-z0-9._]+', text)) > 20:
        return 'Caption has more than 20 @ tags (Instagram allows at most 20 @mentions)'
    if '```' in text:
        return 'Caption contains formatting artifacts'
    return None


def validate_draft_caption(text: str) -> str | None:
    """Extra checks on model drafts (house format: hook line, blank, hashtags)."""
    base = validate_caption(text)
    if base:
        return base
    if re.search(r'https?://|www\.', text, re.I):
        return 'Draft contains a link'
    if re.search(r'(?<![\w.])@\w', text):
        return 'Draft contains an @mention'
    tags = re.findall(r'(?<!\w)#\w+', text)
    if not 12 <= len(tags) <= 15:
        return 'Draft must contain 12-15 hashtags'
    if len({t.lower() for t in tags}) != len(tags):
        return 'Draft contains duplicate hashtags'
    return None
