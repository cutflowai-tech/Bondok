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

ACCOUNT = '17841479950766455'          # IG user used by WF2 export (verify live before cutover)
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
STORY_MAX_SECONDS = 60.0               # strictly below; exactly 60.000 fails
QA_POLICY = 3                          # DEFAULT: deployed media verification policy

HORIZON_DAYS = 84                      # DEFAULT
RESERVE_LEAD = timedelta(minutes=5)    # DEFAULT: earliest slot for automatic allocation
NEAR_DUE = timedelta(minutes=10)       # DEFAULT: supervisor/Bondok do not move near-due slots
LATE_WINDOW = timedelta(hours=2)       # DEFAULT: publisher accepts 0..2h after slot
VARIETY_FALLBACK = {'Post': timedelta(days=7), 'Story': timedelta(days=1)}  # DEFAULT

CODE_RE = re.compile(r'^\s*([a-z]{2,3})\s*[#_\- ]?\s*\d+', re.I)


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
    naive = datetime(year, month, day, hour, minute)
    first = naive.replace(tzinfo=TZ, fold=0)
    second = naive.replace(tzinfo=TZ, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise RuleError('This Cairo local time is ambiguous during a clock change; choose another time')
    roundtrip = first.astimezone(UTC).astimezone(TZ)
    if (roundtrip.hour, roundtrip.minute) != (hour, minute):
        raise RuleError('This Cairo local time does not exist on that day (clock change); choose another time')
    return first.astimezone(UTC)


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
def on_grid(fmt: str, d: datetime) -> bool:
    local = instant(d).astimezone(TZ)
    if local.second:
        return False
    hm = (local.hour, local.minute)
    if fmt == 'Story':
        return hm in STORY_SLOTS
    if fmt == 'Post':
        return POST_SLOTS.get(local.weekday()) == hm
    return False


def grid(now: datetime, fmt: str, lead: timedelta = RESERVE_LEAD, horizon: int = HORIZON_DAYS):
    """Yield every valid slot after now+lead in ascending order (UTC)."""
    now = instant(now)
    start = now.astimezone(TZ).date()
    for offset in range(horizon):
        day = start + timedelta(days=offset)
        if fmt == 'Story':
            times = STORY_SLOTS
        else:
            times = [POST_SLOTS[day.weekday()]] if day.weekday() in POST_SLOTS else []
        for hour, minute in times:
            try:
                at = cairo_local(day.year, day.month, day.day, hour, minute)
            except RuleError:
                continue
            if at > now + lead:
                yield at


def alternatives(now, fmt, taken: set[str], limit=3, lead=NEAR_DUE):
    out = []
    for at in grid(now, fmt, lead):
        if iso(at) not in taken:
            out.append(at)
            if len(out) >= limit:
                break
    return out


def validate_requested_slot(now, fmt, at) -> datetime:
    at = instant(at)
    if fmt not in FORMATS:
        raise RuleError('Format must be Post or Story')
    if not on_grid(fmt, at):
        raise RuleError('Requested time is outside the agreed ' + fmt + ' schedule')
    if at <= instant(now) + NEAR_DUE:
        raise RuleError('Requested time is in the past or within 10 minutes of publication')
    if at > instant(now) + timedelta(days=HORIZON_DAYS):
        raise RuleError('Requested time is beyond the 84-day scheduling horizon')
    return at


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


def choose_slot(now, fmt, key, occupied: list[tuple[datetime, str]], taken: set[str]):
    """Deterministic variety preference with bounded fallback (no LLM).

    Prefers the earliest free slot whose neighbours do not share the same
    rotation key; never looks further than VARIETY_FALLBACK past the first free
    slot, so a scarce style cannot starve the queue.
    """
    events = sorted(occupied, key=lambda e: e[0])
    fallback = None
    for at in grid(now, fmt):
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
def story_duration_failure(duration) -> str | None:
    try:
        d = float(duration)
    except (TypeError, ValueError):
        return 'Story duration could not be measured'
    if not math.isfinite(d) or d <= 0:
        return 'Story duration could not be measured'
    if d >= STORY_MAX_SECONDS:
        return f'Story is {d:g} seconds; it must be strictly under 60 seconds. Supply a shorter edit or ask the owner to convert it to Post'
    return None


def media_failure(info: dict, fmt: str) -> str | None:
    """Fail closed on the final prepared file. Topaz is checked separately
    because it is human evidence bound to the source asset, not a measurement."""
    if fmt not in FORMATS:
        return 'A valid Post or Story format is required'
    try:
        width, height = float(info['width']), float(info['height'])
        size, duration = float(info['bytes']), float(info['duration'])
    except (KeyError, TypeError, ValueError):
        return 'Video measurements are missing or invalid'
    if not all(math.isfinite(x) and x > 0 for x in (width, height, size, duration)):
        return 'Video measurements are missing or invalid'
    if min(width, height) < MIN_SHORT_EDGE:
        return 'Video short edge must be at least 1080 pixels'
    if size >= BUSINESS_MAX_BYTES:
        return 'Final video must be strictly under 300,000,000 bytes'
    if size >= TRANSFER_MAX_BYTES:
        return 'Final video exceeds the current single-request Dropbox transfer path (145 MB)'
    if fmt == 'Story':
        return story_duration_failure(duration)
    return None


def validate_caption(text: str) -> str | None:
    """Deterministic checks on Post caption text (human or drafted)."""
    if not isinstance(text, str) or not text.strip():
        return 'Caption is empty'
    if len(text) > 2200:
        return 'Caption exceeds Instagram 2,200 character limit'
    if len(re.findall(r'(?<!\w)#\w+', text)) > 30:
        return 'Caption has more than 30 hashtags'
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
