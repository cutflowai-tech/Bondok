"""Shared deterministic command handler: the single execution authority.

Every readiness, reservation, scheduling and publication transition goes
through ``Ops``. Callers:
  * n8n WF1/WF2/WF3 via helper.py (service actors, one process per call)
  * Bondok (authenticated Slack owner / bot) in-process
  * Monday edits observed by the trusted board adapter (``observe``)

No network I/O and no media processing happens inside a transaction.
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from dataclasses import dataclass, field

from . import board, rules
from .db import Store, audit, dumps, loads, next_counter

OWNER = 'owner'          # authenticated owner (Slack, via Bondok adapter)
MONDAY = 'monday'        # unattributed human edit on the board (shared account)
BONDOK = 'bondok'        # the assistant itself; never an authority
SERVICES = {'service:wf1', 'service:wf2', 'service:wf3', 'service:bondok', 'service:migration'}

ACTIVE_ATTEMPT = ('claimed', 'container_created', 'committed', 'outcome_unknown')
PRE_COMMIT = ('claimed', 'container_created')

OUTBOX_MAX_ATTEMPTS = 6
JOB_SECONDS = 150                   # worst case per display job: read-before-write + write (60 s timeouts) + ack
# Board values may have been written by v1 or by people. The projection may set
# any system column, but only clears values it wrote itself (tracked as '_ours').
# Found at cutover: clearing v1 dates, delivery links and metadata destroyed data.
PROPOSAL_TTL = 24 * 3600           # interaction expiry; validity comes from the proposal's dependencies (R5 M1, L3)
NOTICE_SECONDS = 24 * 3600          # board notices (Action required) expire after a day
CONFLICT_RETRY_SECONDS = 15 * 60  # WF1 observes every 10 minutes
STALE_SNAPSHOT_SECONDS = 20 * 60   # longer than a WF1 run (lease 15 min): older snapshots can't predate an ack
RUN_TTL = {'wf1': 900, 'wf3': 600, 'wf2': 300}


class Rejected(Exception):
    """Command rejected with an owner-facing reason. Recorded, not retried."""

    def __init__(self, reason: str, code: str = 'rejected', **extra):
        super().__init__(reason)
        self.reason, self.code, self.extra = reason, code, extra


@dataclass
class Command:
    id: str                      # stable idempotency key supplied by the caller
    op: str
    actor: str                   # Slack user id, 'monday', 'service:wf1', ...
    actor_kind: str              # owner | monday | bondok | service:*
    item_id: str | None = None
    args: dict = field(default_factory=dict)
    expected_version: int | None = None
    auth: dict = field(default_factory=dict)   # supplied by trusted adapters only

    def payload_hash(self) -> str:
        return hashlib.sha256(dumps({'op': self.op, 'item': self.item_id, 'args': self.args,
                                     'actor': self.actor, 'kind': self.actor_kind,
                                     'expected': self.expected_version}).encode()).hexdigest()


def fingerprint(x) -> str:
    return hashlib.sha256(dumps(x).encode()).hexdigest()


class CoreMixin:
    def __init__(self, store: Store | str, clock=time.time, config: dict | None = None):
        self.store = store if isinstance(store, Store) else Store(store)
        self.clock = clock
        self.config = {'monday_edits_owner_authorized': False, 'notify_published': True}
        self.config.update(config or {})

    # ------------------------------------------------------------ utilities
    def now(self) -> float:
        return float(self.clock())

    def now_dt(self):
        from datetime import datetime, timezone
        return datetime.fromtimestamp(self.now(), timezone.utc).replace(microsecond=0)

    def item(self, c: sqlite3.Connection, item_id, *, required=True):
        r = c.execute('SELECT * FROM ops_items WHERE item_id=?', (str(item_id),)).fetchone()
        if r is None and required:
            raise Rejected('This item is not registered on the For Social Media board', 'unknown_item')
        return dict(r) if r else None

    def reservation(self, c, item_id):
        r = c.execute('SELECT * FROM ops_reservations WHERE item_id=?', (str(item_id),)).fetchone()
        return dict(r) if r else None

    def active_attempt(self, c, item_id):
        r = c.execute(f"SELECT * FROM ops_attempts WHERE item_id=? AND stage IN ({','.join('?'*len(ACTIVE_ATTEMPT))})",
                      (str(item_id), *ACTIVE_ATTEMPT)).fetchone()
        return dict(r) if r else None

    def update_item(self, c, item_id, actor, reason, **fields):
        """Single write path for ops_items. Bumps version; re-projects display."""
        if not fields:
            return self.item(c, item_id)
        fields['updated'] = self.now()
        cols = ', '.join(f'{k}=?' for k in fields)
        c.execute(f'UPDATE ops_items SET {cols}, version=version+1 WHERE item_id=?',
                  (*fields.values(), str(item_id)))
        audit(c, item_id, 'item_update', actor, {'reason': reason, 'fields': sorted(fields)})
        self.project(c, item_id)
        return self.item(c, item_id)

    def bump_content(self, c, item_id, actor, reason, **fields):
        fields_sql = dict(fields)
        c.execute('UPDATE ops_items SET content_rev=content_rev+1 WHERE item_id=?', (str(item_id),))
        return self.update_item(c, item_id, actor, reason, **fields_sql)

    def heartbeat(self, name: str, detail=None):
        with self.store.tx() as c:
            c.execute('INSERT OR REPLACE INTO ops_heartbeat VALUES(?,?,?)', (name, self.now(), dumps(detail)))

    # ------------------------------------------------------------ command handler
    def submit(self, cmd: Command) -> dict:
        """Idempotent command execution with a recorded outcome.

        * Same id + same payload -> recorded result returned (``duplicate``).
        * Same id + different payload -> rejected.
        * Rejections are durable outcomes; handler errors record ``failed``.
        * Storage errors (locked/full/unreadable store): nothing is applied; one short attempt records ``failed``
          and the result says whether that record exists (``recorded``). A later retry with the same id runs once.
        """
        h = cmd.payload_hash()
        try:
            with self.store.tx() as c:
                prior = c.execute('SELECT * FROM ops_commands WHERE id=?', (cmd.id,)).fetchone()
                if prior:
                    if prior['payload_hash'] != h:
                        return {'state': 'rejected', 'code': 'id_reuse',
                                'reason': 'Request id was already used for a different request'}
                    return {**loads(prior['result'], {}), 'state': prior['state'], 'duplicate': True}
                now = self.now()
                c.execute('INSERT INTO ops_commands(id,payload_hash,op,item_id,expected_version,actor,actor_kind,'
                          'auth_ref,state,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                          (cmd.id, h, cmd.op, cmd.item_id, cmd.expected_version, cmd.actor, cmd.actor_kind,
                           dumps(cmd.auth), 'received', now, now))
                c.execute('SAVEPOINT op')
                try:
                    self.authorize(c, cmd)
                    if cmd.item_id is not None and cmd.expected_version is not None:
                        it = self.item(c, cmd.item_id)
                        if it['version'] != cmd.expected_version:
                            raise Rejected('The item changed since this request was prepared; review it again',
                                           'stale', current_version=it['version'])
                    handler = getattr(self, 'op_' + cmd.op, None)
                    if handler is None:
                        raise Rejected('Unsupported operation', 'unsupported')
                    result = handler(c, cmd) or {}
                    c.execute('RELEASE op')
                    state = result.pop('_state', 'completed')
                except Rejected as e:
                    c.execute('ROLLBACK TO op')
                    c.execute('RELEASE op')
                    result, state = {'reason': e.reason, 'code': e.code, **e.extra}, 'rejected'
                except (ValueError, TypeError, KeyError, AttributeError, IndexError) as e:
                    # A defect or malformed input in one command must not stop the caller's whole run (R5 A3):
                    # nothing of this command is kept, the outcome is recorded and reported once.
                    c.execute('ROLLBACK TO op')
                    c.execute('RELEASE op')
                    result, state = {'reason': f'Internal error ({type(e).__name__}): {str(e)[:200]}',
                                     'code': 'internal_error'}, 'failed'
                    self.notify(c, f"internal:{cmd.op}:{cmd.item_id}:{type(e).__name__}",
                                f"⚠️ A {cmd.op} request for item {cmd.item_id} failed with an internal error "
                                f"({type(e).__name__}: {str(e)[:160]}). Nothing was changed for it; other items "
                                'continue.', cmd.item_id)
                c.execute('UPDATE ops_commands SET state=?, result=?, updated=? WHERE id=?',
                          (state, dumps(result), self.now(), cmd.id))
                audit(c, cmd.item_id, 'command', cmd.actor, {'id': cmd.id, 'op': cmd.op, 'state': state})
                return {**result, 'state': state, 'command_id': cmd.id}
        except sqlite3.Error as e:
            recorded = False
            try:
                with self.store.tx(busy_ms=1000) as c:     # bounded: never a second full busy wait (R5 LOW-03)
                    recorded = c.execute('UPDATE ops_commands SET state=?, result=?, updated=? WHERE id=? AND state=?',
                                         ('failed', dumps({'reason': 'storage error'}), self.now(), cmd.id,
                                          'received')).rowcount == 1
            except sqlite3.Error:
                pass
            return {'state': 'failed', 'code': 'storage', 'recorded': recorded,
                    'reason': 'Operational store unavailable: ' + str(e)[:200]}

    # Which actor kinds may run which operation. Enforced in code, never in prompts.
    PERMISSIONS = {
        'pause': {OWNER, MONDAY},
        'skip': {OWNER, MONDAY},
        'resume': {OWNER, MONDAY},          # a board label change away from Paused/Skipped (contract §3)
        'report_published': {OWNER, MONDAY},  # board Posted / typed post link, Slack "I posted it"
        'change_format': {OWNER, MONDAY},   # a board Format edit is the owner's authorization (R5 A6)
        'replace_source': {OWNER, MONDAY},
        'update_caption': {OWNER, MONDAY},
        'confirm_topaz': {OWNER, MONDAY},
        'request_reschedule': {OWNER, MONDAY},
        'request_recheck': {OWNER, MONDAY, BONDOK, 'service:wf3'},
        'request_publish': {OWNER},
        'set_window': {OWNER},
        'request_rework': {OWNER},
        'approve_caption_draft': {OWNER},
        'repair_missed': {'service:wf3'},
        'repair_schedule': {'service:wf3', 'service:wf1'},
        'resolve_outcome': {OWNER},
        'approve_proposal': {OWNER},
        'reject_proposal': {OWNER},
        'propose': {OWNER, BONDOK, MONDAY, 'service:wf3', 'service:wf1'},
        'set_notes': {OWNER, MONDAY},
        'set_variety': {OWNER, MONDAY},
        'set_collab': {OWNER, MONDAY},
        'set_code': {OWNER, MONDAY},
        'set_folder': {OWNER, MONDAY},
        'hold': {'service:wf1', 'service:wf2', 'service:wf3', 'service:bondok', OWNER, MONDAY},  # monday: external_posted only
        'repair_release': {'service:wf3'},
        'repair_reauthorize': {'service:wf3'},
        'source_canceled': {'service:wf1'},
        'prep_source': {'service:wf1'},
        'prep_preflight': {'service:wf1'},
        'prep_media': {'service:wf1'},
        'prep_delivered': {'service:wf1'},
        'caption_draft': {'service:wf1', 'service:bondok'},
    }

    def authorize(self, c, cmd: Command):
        allowed = self.PERMISSIONS.get(cmd.op)
        if allowed is None:
            raise Rejected('Unsupported operation', 'unsupported')
        if cmd.actor_kind not in allowed:
            raise Rejected('This actor is not permitted to perform this operation', 'forbidden')
        if cmd.item_id is not None and cmd.op not in ('approve_proposal', 'reject_proposal', 'propose'):
            self.item(c, cmd.item_id)  # membership: only registered social-board items

    # ------------------------------------------------------------ worker runs (fencing)
    def run_start(self, kind: str, run_id: str) -> dict:
        ttl = RUN_TTL.get(kind, 600)
        with self.store.tx() as c:
            r = c.execute('SELECT * FROM ops_runs WHERE kind=?', (kind,)).fetchone()
            now = self.now()
            if r and r['run_id'] != run_id and (r['lease_until'] or 0) > now:
                return {'acquired': False, 'deferred': True, 'holder': r['run_id'],
                        'reason': f'{kind} already running; this cycle is deferred, not completed'}
            fence = next_counter(c, 'fence:' + kind)
            c.execute('INSERT OR REPLACE INTO ops_runs(kind,run_id,fence,lease_until,started,heartbeat,last_result) '
                      'VALUES(?,?,?,?,?,?,?)', (kind, run_id, fence, now + ttl, now, now,
                                               r['last_result'] if r else None))
            c.execute('INSERT OR REPLACE INTO ops_heartbeat VALUES(?,?,?)', (kind, now, dumps({'run': run_id})))
            return {'acquired': True, 'run_id': run_id, 'fence': fence}

    def run_check(self, c, kind: str, run_id: str | None, fence: int | None):
        """Fencing: commands from a superseded run are refused."""
        if run_id is None:
            return
        r = c.execute('SELECT * FROM ops_runs WHERE kind=?', (kind,)).fetchone()
        if not r or r['run_id'] != run_id or (fence is not None and r['fence'] != fence):
            raise Rejected('Worker run was superseded; result discarded', 'fenced')
        now = self.now()
        c.execute('UPDATE ops_runs SET lease_until=?, heartbeat=? WHERE kind=?',
                  (now + RUN_TTL.get(kind, 600), now, kind))
        c.execute('INSERT OR REPLACE INTO ops_heartbeat VALUES(?,?,?)', (kind, now, dumps({'run': run_id})))

    def run_finish(self, kind: str, run_id: str, result=None) -> dict:
        with self.store.tx() as c:
            r = c.execute('SELECT * FROM ops_runs WHERE kind=?', (kind,)).fetchone()
            if r and r['run_id'] == run_id:
                c.execute('UPDATE ops_runs SET lease_until=0, last_result=?, heartbeat=? WHERE kind=?',
                          (dumps(result), self.now(), kind))
                return {'released': True}
            return {'released': False}

    # ------------------------------------------------------------ display projection
    def desired_display(self, c, it: dict) -> dict:
        """Derived system-owned Monday values from committed state only."""
        res = self.reservation(c, it['item_id'])
        att = self.active_attempt(c, it['item_id'])
        last = c.execute("SELECT * FROM ops_attempts WHERE item_id=? AND stage='published' "
                         'ORDER BY updated DESC LIMIT 1', (it['item_id'],)).fetchone()
        ver = None
        if it.get('verification_id'):
            m = c.execute('SELECT metadata FROM media WHERE id=?', (it['verification_id'],)).fetchone()
            ver = loads(m['metadata'], {}) if m else None
        hold = loads(it.get('hold'), None)
        action, label = '', None
        if it['publication'] == 'published':
            label = 'posted'
        elif att and att['stage'] in ('claimed', 'container_created', 'committed'):
            label = 'publishing'
        elif it['publication'] == 'outcome_unknown':
            label, action = 'review', ('Instagram did not confirm this publication. Check the account manually; '
                                       'do not republish until it is resolved in Slack.')
        elif it['publication'] == 'failed':
            f = c.execute("SELECT evidence FROM ops_attempts WHERE item_id=? AND stage='failed' ORDER BY updated DESC "
                          'LIMIT 1', (it['item_id'],)).fetchone()
            why = (loads(f['evidence'], {}) or {}).get('error', '') if f else ''
            label, action = 'review', ('Instagram rejected this publication' + (': ' + str(why)[:300] if why else '')
                                       + '. Nothing was published. Fix the cause, then tell Bondok to publish it again.')
        elif it['owner_state'] == 'skipped':
            label = 'skipped'
        elif it['owner_state'] == 'paused':
            label = 'paused'
        elif hold:
            label, action = 'review', hold.get('reason', 'Waiting for an owner decision in Slack')
        elif it['readiness'] == 'blocked':
            label = {'content': 'long_story' if (it.get('block_key') or '').startswith('story_duration') else 'editor',
                     'editor': 'editor'}.get(it.get('block_kind'), 'review')
            action = it.get('block_reason') or ''
        elif it['readiness'] in ('unchecked', 'checking'):
            label = 'checking'
        elif res:
            label = 'scheduled'
        else:
            label = 'ready'
            if it['format'] == 'Post' and it.get('caption_state') != 'approved':
                action = 'Caption needs owner approval in Slack before scheduling'
            elif it.get('requested_at'):
                action = 'Requested time is not available; choose another slot in Slack'
        notice = (loads(it.get('observed'), {}) or {}).get('_notice')
        if notice and self.now() - notice.get('at', 0) < NOTICE_SECONDS and not action:
            action = notice['text']
        system = []
        if it.get('infra_issue'):
            system.append('Temporary system issue (content not rejected): ' + it['infra_issue'])
        if ver and ver.get('trimmedFrom') and it['readiness'] == 'ready':
            system.append(f"Story trimmed automatically from {float(ver['trimmedFrom']):.3f} s to "
                          f"{float(ver['duration']):.3f} s (end cut; original Dropbox file unchanged)")
        elif it['format'] == 'Story' and it['readiness'] != 'ready' and it.get('asset_key'):
            chk = c.execute("SELECT key, result FROM ops_checks WHERE item_id=? AND kind='duration'",
                            (it['item_id'],)).fetchone()
            secs = (loads(chk['result'], {}) or {}).get('duration') if chk and \
                chk['key'] == it['asset_key'] + '|' + (it.get('content_hash') or '') else None
            if rules.story_trim_target(secs):
                system.append(f'Story is {float(secs):.3f} s; it will be trimmed automatically to '
                              f'{rules.STORY_TRIM_SECONDS:g} s when prepared (end cut; no shorter edit needed)')
        if res:
            system.append('Confirmed slot ' + rules.display(rules.instant(res['slot'])))
        # Publish at shows the confirmed slot, else the owner's requested time (visible while not reservable, R5 M4;
        # an automatic slot kept as a preference is not an owner request and is not shown),
        # else a date the owner entered without a time (R5 A3); legacy Post Date/Time mirror the same instant.
        shown = res['slot'] if res else (it.get('requested_at') if it.get('requested_by') != 'legacy-board' else None)
        if not shown and hold and hold.get('kind') == 'incomplete_time':
            shown = hold.get('date')
        legacy = rules.monday_legacy_values(rules.instant(shown)) if shown and not board.date_only(shown) else None
        d = {
            'status': board.LABELS[label],
            'action': action or None,
            'system': '\n'.join(system) or None,
            'publish_at': shown or None,
            'post_date': legacy['date4']['date'] if legacy else None,
            'post_time': f"{legacy['hour_mm7xy9cf']['hour']:02d}:{legacy['hour_mm7xy9cf']['minute']:02d}" if legacy else None,
            'media': it.get('verification_id') if it['readiness'] == 'ready' else None,
            'video': ver.get('url') if ver and it['readiness'] == 'ready' else None,
            'measurements': (f"{ver['width']}x{ver['height']} | {ver['bytes']/1e6:.1f} MB | {float(ver['duration']):.3f} s"
                             if ver and ver.get('width') else None),
            'processed': it['format'] if it.get('verification_id') else None,
            'asset': it.get('asset_key'),
            'version_check': (it['file_name'] + ' | ' + it['file_rev']) if it.get('file_name') and it.get('file_rev') else None,
            'dropbox': [it['file_url'], it.get('file_name') or 'Source video'] if it.get('file_url') else None,
            'folder': [it['folder_url'], 'Project folder'] if it.get('folder_url') else None,
            'source_item': it.get('source_item_id'),
            'style': self.safe_style(it.get('code')),
        }
        if last:
            ev = loads(last['evidence'], {}) or {}
            d['ig_media'] = last['media_id']
            d['published_at'] = rules.iso(rules.instant(ev['published_at'])) if ev.get('published_at') else None
            if not ev.get('published_at'):
                rec = ev.get('recorded_at') or self.iso_ts(last['updated'])
                d['system'] = '\n'.join(x for x in (d['system'], 'Publication confirmed by '
                                                     + str(ev.get('source') or 'reconciliation') + '; recorded at '
                                                     + rules.display(rules.instant(rec)) + ' (Instagram did not report '
                                                     'the exact publication time)') if x)
            d['post_link'] = [last['permalink'], 'Instagram ' + ('Reel' if it['format'] == 'Post' else 'Story')] \
                if last['permalink'] else None
        if it['legacy_posted'] or it['publication'] == 'published':
            # Never clear historical evidence that was written by people or older systems.
            for k in ('ig_media', 'published_at', 'post_link', 'publish_at', 'post_date', 'post_time'):
                d.pop(k, None) if not d.get(k) else None
        if it['publication'] == 'published':
            group = 'Posted'
        elif it['owner_state'] == 'skipped':
            group = 'Skipped'
        else:
            group = it['format']
        d['_group'] = board.GROUPS.get(group)
        if it['legacy_posted'] and not last:
            d['_group'] = None    # posted outside v2: people placed it; never moved by unrelated edits (audit MS12)
        return d

    @staticmethod
    def safe_style(code):
        try:
            return rules.style(code)
        except rules.RuleError:
            return None

    @staticmethod
    def iso_ts(ts):
        from datetime import datetime, timezone
        return datetime.fromtimestamp(ts, timezone.utc).isoformat()

    def project(self, c, item_id, force_keys=()):
        """Queue a Monday display write for changed system columns only.

        Committed state is authoritative; a failed display write is pending
        synchronization, never loss of state.
        """
        it = self.item(c, item_id)
        desired = self.desired_display(c, it)
        group = desired.pop('_group')
        confirmed = loads(it.get('projected'), {}) or {}
        pending = loads(it.get('pending_projection'), {}) or {}
        base = {**confirmed, **pending}
        ours = set(confirmed.get('_ours') or []) | set(pending.get('_ours') or [])
        changes = {}
        for k, v in desired.items():
            cv = board.compare_value(k, v)
            if k in force_keys or base.get(k, '__unset__') != cv:
                if k not in confirmed and k not in pending and cv is None and k not in force_keys:
                    continue  # never projected and empty: nothing to clear
                if cv is None and k not in ours:
                    continue  # never clear a board value the projection did not write (v1 or human data)
                changes[k] = v
        group_change = group if base.get('_group') != group else None
        if not changes and not group_change:
            return None
        columns, compare, group_out = {}, {}, None
        # Merge unsent display jobs so superseding never drops a column write.
        older = c.execute("SELECT id, payload FROM ops_outbox WHERE kind='monday' AND item_id=? AND "
                          "dedupe_key LIKE 'monday:%' AND state IN ('pending','failed','escalated') ORDER BY id",
                          (str(item_id),)).fetchall()
        for o in older:
            op = loads(o['payload'], {})
            columns.update(op.get('columns', {}))
            compare.update(op.get('compare', {}))
            group_out = op.get('group') or group_out
        columns.update({board.COL[k]: board.mutation_value(k, v) for k, v in changes.items()})
        compare.update({k: board.compare_value(k, v) for k, v in changes.items()})
        written = sorted(set(compare.get('_ours') or []) | {k for k, v in compare.items()
                                                           if not k.startswith(('_', 'h:')) and v is not None})
        if written:
            compare['_ours'] = written
        if group_change:
            compare['_group'] = group_change
            group_out = group_change
        guard = {}
        for o in older:
            guard.update(loads(o['payload'], {}).get('guard') or {})
        for k in force_keys:      # a forced write (revert) overwrites deliberately: no inherited guard (#7)
            if k in board.COL:
                guard.pop(board.COL[k], None)
        if 'status' in changes:
            # Always guarded, also from an empty cell (R5 B7) and for a revert (it applies only while the board still
            # shows the value being reverted).
            # WF2 reads the board first and skips the write if a person changed the status since v2 last wrote
            # it (e.g. Paused typed between WF1 observations): the edit must reach observe, not be overwritten.
            # Always the latest confirmed board value (a consumed human edit updates it), never an older job's.
            # A status write WF2 already took may land before this one: its value is an expected board state too
            # (fuzz F5: the in-flight write landed and this job then conflicted forever).
            flying = [ (loads(j['payload'], {}).get('compare') or {}).get('status') for j in c.execute(
                "SELECT payload FROM ops_outbox WHERE kind='monday' AND item_id=? AND state='in_flight'",
                (str(item_id),)).fetchall()]
            guard[board.COL['status']] = {'kind': 'status', 'was': [confirmed.get('status'), *[f for f in flying if f]],
                                          'new': compare.get('status')}
        payload = {'item_id': str(item_id), 'columns': columns, 'compare': compare, 'group': group_out}
        if guard:
            payload['guard'] = guard
        key = 'monday:' + str(item_id) + ':' + fingerprint(payload)[:16]
        prior = c.execute('SELECT id, state FROM ops_outbox WHERE dedupe_key=?', (key,)).fetchone()
        if prior and (prior['state'] not in ('pending', 'failed') or prior['id'] in {o['id'] for o in older}):
            # Same values as an earlier job that already ran (A -> B -> A): a new write is needed, not a
            # duplicate of the old one, which INSERT OR IGNORE would silently drop.
            key += ':' + str(next_counter(c, 'outbox:monday'))
        now = self.now()
        if older:
            c.execute(f"UPDATE ops_outbox SET state='superseded', updated=? WHERE id IN ({','.join('?'*len(older))})",
                      (now, *[o['id'] for o in older]))
        c.execute('INSERT OR IGNORE INTO ops_outbox(kind,dedupe_key,item_id,payload,state,next_at,created,updated) '
                  "VALUES('monday',?,?,?,'pending',0,?,?)", (key, str(item_id), dumps(payload), now, now))
        c.execute('UPDATE ops_items SET pending_projection=? WHERE item_id=?',
                  (dumps({**pending, **compare}), str(item_id)))
        return payload

    # ------------------------------------------------------------ one-time data fixes
    def apply_data_fixes(self) -> list[str]:
        """Idempotent data repairs, each applied once and recorded in ops_meta."""
        fixes = (('ours_backfill', self._backfill_ours), ('story_trim_nudge', self._nudge_trimmable_stories))
        done = []
        with self.store.read() as c:
            have = {r['key'] for r in c.execute("SELECT key FROM ops_meta WHERE key LIKE 'fix:%'")}
        for name, fn in fixes:
            if 'fix:' + name in have:
                continue
            with self.store.tx() as c:
                if not c.execute('SELECT 1 FROM ops_meta WHERE key=?', ('fix:' + name,)).fetchone():
                    n = fn(c)
                    c.execute('INSERT OR REPLACE INTO ops_meta VALUES(?,?)', ('fix:' + name, dumps({'at': self.now(), 'items': n})))
                    done.append(name)
        return done

    def _nudge_trimmable_stories(self, c) -> int:
        """Stories blocked as too long whose measured source now falls under the automatic trim policy
        (60-65 s): ask WF1 to look at them on its next cycle instead of after the blocked backoff."""
        n = 0
        rows = c.execute("SELECT i.item_id, i.observed, k.result FROM ops_items i JOIN ops_checks k ON "
                         "k.item_id=i.item_id AND k.kind='duration' WHERE i.readiness='blocked' AND "
                         "i.block_key LIKE 'story_duration:%' AND k.key=i.asset_key||'|'||i.content_hash").fetchall()
        for r in rows:
            if rules.story_trim_target((loads(r['result'], {}) or {}).get('duration')) is None:
                continue
            obs = loads(r['observed'], {}) or {}
            obs['_nudge'] = self.now()
            c.execute('UPDATE ops_items SET observed=? WHERE item_id=?', (dumps(obs), r['item_id']))
            audit(c, r['item_id'], 'nudge_story_trim', 'service:migration', {})
            n += 1
        return n

    def _backfill_ours(self, c) -> int:
        """Display values delivered before ownership tracking (`_ours`) existed were treated as foreign, so
        v2 could never clear its own outdated messages. Rebuild ownership from v2's own delivered jobs;
        values imported from the v1 board at bootstrap stay foreign."""
        owned = {}
        for r in c.execute("SELECT item_id, payload FROM ops_outbox WHERE kind='monday' AND state='done'"):
            cmp_ = (loads(r['payload'], {}) or {}).get('compare', {})
            owned.setdefault(r['item_id'], set()).update(
                k for k, v in cmp_.items() if not k.startswith(('_', 'h:')) and v is not None)
        n = 0
        for iid, keys in owned.items():
            it = self.item(c, iid, required=False)
            if not it:
                continue
            proj = loads(it['projected'], {}) or {}
            ours = set(proj.get('_ours') or [])
            if keys - ours:
                proj['_ours'] = sorted(ours | keys)
                c.execute('UPDATE ops_items SET projected=? WHERE item_id=?', (dumps(proj), iid))
                audit(c, iid, 'backfill_projection_ownership', 'service:migration', {'keys': sorted(keys - ours)})
                n += 1
        return n

    # ------------------------------------------------------------ outbox
    def enqueue(self, c, kind, dedupe_key, payload, item_id=None):
        now = self.now()
        cur = c.execute('INSERT OR IGNORE INTO ops_outbox(kind,dedupe_key,item_id,payload,state,next_at,created,updated) '
                        "VALUES(?,?,?,?,'pending',0,?,?)", (kind, dedupe_key, item_id, dumps(payload), now, now))
        return cur.rowcount == 1

    def notify(self, c, key, text, item_id=None, **extra):
        """Owner notification (templated, no model). Deduplicated by key."""
        return self.enqueue(c, 'slack', 'slack:' + key, {'text': text, **extra}, item_id)

    def outbox_take(self, kinds, worker, limit=10, lease=300) -> list[dict]:
        now = self.now()
        with self.store.tx() as c:
            # Escalated display/editor jobs keep being retried hourly so the board recovers after an outage
            # (audit I1); a lease that expired means the worker died: that counts as a failed attempt (audit I7).
            # Escalated jobs of every kind, Slack notices included, keep being retried hourly: a long Slack outage
            # delays a notice but never loses it (R5 M19).
            rows = c.execute(f"SELECT * FROM ops_outbox WHERE kind IN ({','.join('?'*len(kinds))}) AND "
                             "((state IN ('pending','failed') AND next_at<=?) OR (state='in_flight' AND lease_until<?) "
                             "OR (state='escalated' AND next_at<=?)) "
                             'ORDER BY id LIMIT ?', (*kinds, now, now, now, limit)).fetchall()
            out = []
            for r in rows:
                crashed = r['state'] == 'in_flight'
                if crashed and r['attempts'] + 1 >= OUTBOX_MAX_ATTEMPTS:
                    c.execute("UPDATE ops_outbox SET state='escalated', attempts=attempts+1, next_at=?, "
                              "last_error='worker stopped while processing', updated=? WHERE id=?",
                              (now + 3600, now, r['id']))
                    if r['kind'] != 'slack':
                        self.notify(c, f"escalate:{r['id']}", f"Display/sync job keeps stopping its worker "
                                    f"({r['kind']}, item {r['item_id']}); it will be retried hourly.", r['item_id'])
                    continue
                # A worker applies its batch one job after another: the n-th job's lease covers the jobs before
                # it, so a slow batch is not taken again by the next run (R5 M28).
                c.execute("UPDATE ops_outbox SET state='in_flight', lease_owner=?, lease_until=?, updated=?, "
                          'attempts=attempts+? WHERE id=?', (worker, now + lease + JOB_SECONDS * len(out), now,
                                                            1 if crashed else 0, r['id']))
                out.append({'id': r['id'], 'kind': r['kind'], 'item_id': r['item_id'], 'payload': loads(r['payload'])})
            return out

    def outbox_ack(self, job_id, worker, ok: bool, error: str | None = None, result=None) -> dict:
        now = self.now()
        with self.store.tx() as c:
            r = c.execute('SELECT * FROM ops_outbox WHERE id=?', (job_id,)).fetchone()
            if not r:
                return {'ok': False, 'reason': 'unknown job'}
            if r['state'] == 'done':
                return {'ok': True, 'duplicate': True}
            if not ok and r['state'] == 'in_flight' and r['lease_owner'] not in (None, worker):
                return {'ok': False, 'stale_lease': True}       # another worker holds it now
            p = loads(r['payload'], {})
            if ok:
                c.execute("UPDATE ops_outbox SET state='done', last_error=NULL, updated=? WHERE id=?", (now, job_id))
                if r['kind'] == 'monday':
                    it = self.item(c, r['item_id'], required=False)
                    if it:
                        compare = p.get('compare', {})
                        human = {k[2:]: v for k, v in compare.items() if k.startswith('h:')}
                        prev = loads(it['projected'], {}) or {}
                        confirmed = {**prev, **{k: v for k, v in compare.items() if not k.startswith('h:')}}
                        confirmed['_ours'] = sorted(set(prev.get('_ours') or []) | set(compare.get('_ours') or []))
                        # Remember what the board showed before this write: a board snapshot read before the
                        # write landed still shows it, and must not be mistaken for a human edit (audit M-1).
                        before = dict(prev.get('_prev') or {})
                        old_observed = loads(it['observed'], {}) or {}
                        for k, v in compare.items():
                            if k.startswith('_'):
                                continue
                            was = old_observed.get(k[2:]) if k.startswith('h:') else prev.get(k)
                            if was != v:
                                before[k] = {'v': was, 'at': now}
                        confirmed['_prev'] = {k: x for k, x in before.items() if now - x['at'] < STALE_SNAPSHOT_SECONDS}
                        # When v2's own values reached the board (R5 B4: which file version a Topazed click saw).
                        if 'asset' in compare:
                            confirmed['_asset_writes'] = (prev.get('_asset_writes') or [])[-5:] + \
                                [{'v': compare['asset'], 'at': now}]
                        hw = {k[2:]: now for k in compare if k.startswith('h:')}
                        if hw:
                            confirmed['_hwrite_at'] = {**(prev.get('_hwrite_at') or {}), **hw}
                        pending = {k: v for k, v in (loads(it['pending_projection'], {}) or {}).items()
                                   if not (k.startswith('h:') and k[2:] in human and v == human[k[2:]])}
                        if human:
                            observed = {**(loads(it['observed'], {}) or {}), **human}
                            c.execute('UPDATE ops_items SET observed=? WHERE item_id=?', (dumps(observed), r['item_id']))
                        live = c.execute("SELECT 1 FROM ops_outbox WHERE kind='monday' AND item_id=? AND "
                                         "state IN ('pending','in_flight','failed','escalated')",
                                         (r['item_id'],)).fetchone()
                        c.execute('UPDATE ops_items SET projected=?, pending_projection=? WHERE item_id=?',
                                  (dumps(confirmed), dumps(pending) if live else None, r['item_id']))
                        newer = c.execute("SELECT 1 FROM ops_outbox WHERE kind='monday' AND item_id=? AND id>? AND "
                                          "state='done'", (r['item_id'], job_id)).fetchone()
                        if newer:
                            # This older write landed after a newer one (retry/overlapping runs): the board now
                            # shows these older values, as recorded above. Re-project the current state (audit MS5).
                            landed = {k for k in compare if not k.startswith('_')}
                            pend = {k: v for k, v in (pending or {}).items() if k not in landed}
                            c.execute('UPDATE ops_items SET pending_projection=? WHERE item_id=?',
                                      (dumps(pend) if pend else None, r['item_id']))
                            self.project(c, r['item_id'])
                elif r['kind'] == 'editor':
                    c.execute("UPDATE ops_editor_tasks SET task_id=COALESCE(?,task_id), state='done', updated=? "
                              'WHERE item_id=? AND issue_key=?',
                              ((result or {}).get('task_id'), now, r['item_id'], p.get('issue_key')))
                return {'ok': True}
            if r['kind'] == 'monday' and r['dedupe_key'].startswith('monday:') and c.execute(
                    "SELECT 1 FROM ops_outbox WHERE kind='monday' AND item_id=? AND id>? AND state!='superseded' "
                    "AND dedupe_key LIKE 'monday:%'", (r['item_id'], job_id)).fetchone():
                # Display jobs only: owner-approved human-column writes (monday-h:) are not re-derived by project()
                # and must keep retrying (round-2 review #6).
                # A newer display job exists: never retry these older values after it (audit MS5). Whatever
                # this job carried is re-derived from the current state instead.
                c.execute("UPDATE ops_outbox SET state='superseded', last_error=?, updated=? WHERE id=?",
                          ((error or '')[:500], now, job_id))
                it = self.item(c, r['item_id'], required=False)
                if it:
                    mine = {k for k in p.get('compare', {}) if not k.startswith('_')}
                    pend = {k: v for k, v in (loads(it['pending_projection'], {}) or {}).items() if k not in mine}
                    c.execute('UPDATE ops_items SET pending_projection=? WHERE item_id=?',
                              (dumps(pend) if pend else None, r['item_id']))
                    self.project(c, r['item_id'])
                return {'ok': False, 'superseded': True}
            if (error or '').startswith('conflict') and r['dedupe_key'].startswith('monday-h:'):
                it = self.item(c, r['item_id'], required=False)
                obs = (loads(it['observed'], {}) or {}) if it else {}
                guard = p.get('guard') or {}
                cols = {board.COL[k[2:]]: k[2:] for k in (p.get('compare') or {}) if k.startswith('h:') and k[2:] in board.COL}
                seen = [k for col, k in cols.items() if col in guard and
                        obs.get(k) not in ([guard[col].get('was')] if not isinstance(guard[col].get('was'), list)
                                            else guard[col]['was'])]
                if it and seen:
                    # WF1 already recorded the person's newer value for this column: it wins (fuzz, round 3).
                    c.execute("UPDATE ops_outbox SET state='superseded', last_error=?, updated=? WHERE id=?",
                              ((error or '')[:500], now, job_id))
                    pend = loads(it['pending_projection'], {}) or {}
                    for k in seen:
                        pend.pop('h:' + k, None)
                    c.execute('UPDATE ops_items SET pending_projection=? WHERE item_id=?',
                              (dumps(pend) if pend else None, r['item_id']))
                    return {'ok': False, 'superseded': True}
            if (error or '').startswith('conflict'):
                # A person changed the board since v2 last wrote: wait for WF1 to observe that edit.
                c.execute("UPDATE ops_outbox SET state='failed', last_error=?, next_at=?, updated=? WHERE id=?",
                          ((error or '')[:500], now + CONFLICT_RETRY_SECONDS, now, job_id))
                return {'ok': False, 'conflict': True}
            attempts = r['attempts'] + 1
            if attempts >= OUTBOX_MAX_ATTEMPTS:
                c.execute("UPDATE ops_outbox SET state='escalated', attempts=?, last_error=?, next_at=?, updated=? "
                          'WHERE id=?', (attempts, (error or '')[:500], now + 3600, now, job_id))
                if r['kind'] != 'slack':
                    self.notify(c, f'escalate:{job_id}',
                                f'Display/sync update keeps failing ({r["kind"]}, item {r["item_id"]}). '
                                'State is safe in the operational store; the board may be out of date. '
                                f'Last error: {(error or "")[:200]}', r['item_id'])
                return {'ok': False, 'escalated': True}
            c.execute("UPDATE ops_outbox SET state='failed', attempts=?, last_error=?, next_at=?, updated=? WHERE id=?",
                      (attempts, (error or '')[:500], now + min(3600, 60 * 2 ** attempts), now, job_id))
            return {'ok': False, 'retry_at': now + min(3600, 60 * 2 ** attempts)}

    # ------------------------------------------------------------ proposals
    def proposal_deps(self, c, kind, iid, payload) -> str:
        """Fingerprint of the facts a proposal depends on (R5 M1): unrelated changes (Notes, readiness re-checks,
        retry timestamps) do not stale it; a change to what it acts on does."""
        it = self.item(c, iid)
        res = self.reservation(c, iid)
        op = payload.get('op') if kind == 'command' else kind
        deps = {'publication': it['publication'], 'slot': res['slot'] if res else None}
        if op == 'change_format':
            deps.update(format=it['format'], content_rev=it['content_rev'])
        elif op in ('resume', 'skip'):
            deps.update(owner_state=it['owner_state'], hold=(loads(it['hold'], {}) or {}).get('kind'))
        elif op == 'confirm_topaz':
            deps.update(asset_key=it['asset_key'])
        elif op == 'update_caption':
            deps.update(caption=it['caption'], caption_state=it['caption_state'], format=it['format'])
        elif op == 'resolve_outcome':
            a = c.execute('SELECT id, stage FROM ops_attempts WHERE item_id=? ORDER BY updated DESC LIMIT 1',
                          (str(iid),)).fetchone()
            deps.update(attempt=dict(a) if a else None)
        elif op == 'swap_slot':
            deps.update(format=it['format'], owner_state=it['owner_state'])
        return fingerprint(deps)

    def create_proposal(self, c, kind, bindings: list[str], payload: dict, summary: str, created_by: str,
                        thread=None, notify=True) -> dict:
        items = []
        for iid in bindings:
            it = self.item(c, iid)
            res = self.reservation(c, iid)
            items.append({'item_id': str(iid), 'version': it['version'], 'slot': res['slot'] if res else None,
                          'deps': self.proposal_deps(c, kind, iid, payload)})
        pid = 'B-' + secrets.token_hex(4).upper()
        now = self.now()
        c.execute('INSERT INTO ops_proposals(id,kind,bindings,payload,payload_hash,summary,created_by,thread,state,'
                  'expires,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                  (pid, kind, dumps(items), dumps(payload), fingerprint(payload), summary, created_by, thread,
                   'pending', now + PROPOSAL_TTL, now, now))
        if notify and not thread:   # Slack-originated proposals are rendered in the thread reply instead
            self.notify(c, 'proposal:' + pid, summary + f'\nTo approve, the owner replies: اعتمد {pid}', None,
                        proposal_id=pid, thread=thread)
        return {'proposal_id': pid, 'expires_in_minutes': PROPOSAL_TTL // 60, 'summary': summary}

    def check_bindings(self, c, p) -> None:
        if p['kind'] in ('approve_caption', 'approve_captions'):
            # Content approvals are bound to the exact text and do not expire with the interaction (R5 A8, L3);
            # execute_* re-checks the text/caption they depend on.
            return
        if p['expires'] < self.now():
            raise Rejected('This proposal expired; ask for an updated one', 'expired')
        if p['kind'] == 'approve_caption':
            # Bound to the exact draft and text instead (checked in execute_approve_caption): system
            # updates such as readiness or media changes bump the item version but do not touch the caption.
            return
        payload = loads(p['payload'], {})
        for b in loads(p['bindings'], []):
            it = self.item(c, b['item_id'])
            res = self.reservation(c, b['item_id'])
            if 'deps' in b:
                changed = self.proposal_deps(c, p['kind'], b['item_id'], payload) != b['deps']
            else:                       # proposals made before dependency binding keep the version rule
                changed = it['version'] != b['version'] or (res['slot'] if res else None) != b['slot']
            if changed:
                raise Rejected(f"Item {b['item_id']} changed after the proposal; it was not executed", 'stale')

    def op_approve_proposal(self, c, cmd: Command):
        pid = cmd.args['proposal_id']
        p = c.execute('SELECT * FROM ops_proposals WHERE id=?', (pid,)).fetchone()
        if not p:
            raise Rejected('Unknown proposal', 'unknown_proposal')
        if p['state'] != 'pending':
            raise Rejected('This proposal was already ' + p['state'] + '; it will not run again', 'decided')
        if p['thread'] and cmd.auth.get('thread') and p['thread'] != cmd.auth.get('thread'):
            raise Rejected('Approve the proposal in the thread where it was made', 'wrong_thread')
        try:
            self.check_bindings(c, p)
        except Rejected as e:
            c.execute("UPDATE ops_proposals SET state=?, result=?, updated=? WHERE id=?",
                      ('expired' if e.code == 'expired' else 'stale', e.reason, self.now(), pid))
            # Record the decision even though the command is rejected.
            c.execute('RELEASE op')
            c.execute('SAVEPOINT op')
            raise
        payload = loads(p['payload'], {})
        result = getattr(self, 'execute_' + p['kind'])(c, payload, cmd, pid)
        c.execute("UPDATE ops_proposals SET state='executed', decided_by=?, result=?, updated=? WHERE id=?",
                  (cmd.actor, dumps(result), self.now(), pid))
        return {'proposal_id': pid, 'executed': True, **result}

    APPROVABLE = ('change_format', 'confirm_topaz', 'resume', 'update_caption', 'resolve_outcome', 'skip')

    def propose_command(self, c, op, item_id, args, summary, created_by, thread=None) -> dict:
        """Owner approval request for one exact operation on one item revision."""
        if op not in self.APPROVABLE:
            raise Rejected('This operation cannot be proposed', 'unsupported')
        self.item(c, item_id)
        return self.create_proposal(c, 'command', [str(item_id)], {'op': op, 'item_id': str(item_id), 'args': args},
                                    summary, created_by, thread)

    def execute_approve_captions(self, c, payload, cmd, pid):
        """One owner approval for the exact listed legacy captions (each bound to
        its item version and caption text; a changed caption is skipped)."""
        done, skipped = [], []
        for x in payload['items']:
            it = self.item(c, x['item_id'])
            if it['caption'] != x['caption'] or it['caption_state'] != 'legacy_unapproved':
                skipped.append(x['item_id'])
                continue
            inner = Command(cmd.id + ':' + x['item_id'], 'update_caption', cmd.actor, OWNER, x['item_id'],
                            {'text': x['caption']}, auth={'proposal': pid})
            self.op_update_caption(c, inner)
            done.append(x['item_id'])
        return {'approved': done, 'skipped_changed': skipped}

    def propose_legacy_captions(self, c, created_by, thread=None) -> dict:
        rows = c.execute("SELECT item_id, name, caption FROM ops_items WHERE caption_state='legacy_unapproved' "
                         "AND format='Post' AND publication='not_started' ORDER BY item_id").fetchall()
        if not rows:
            raise Rejected('There are no unapproved legacy captions', 'nothing')
        open_p = c.execute("SELECT id, summary, expires FROM ops_proposals WHERE kind='approve_captions' AND "
                           "state='pending' AND expires>? ORDER BY created DESC LIMIT 1", (self.now(),)).fetchone()
        if open_p:      # one open batch proposal at a time (audit M6: repeated calls created several)
            return {'proposal_id': open_p['id'], 'summary': open_p['summary'],
                    'expires_in_minutes': max(1, int((open_p['expires'] - self.now()) // 60)), 'existing': True}
        items = [{'item_id': r['item_id'], 'caption': r['caption']} for r in rows]
        lines = '\n'.join(f"• {r['name']} ({r['item_id']}): {(r['caption'] or '').splitlines()[0][:90]}" for r in rows)
        return self.create_proposal(c, 'approve_captions', [r['item_id'] for r in rows], {'items': items},
                                    f'Approve these {len(rows)} existing captions exactly as written:\n{lines}',
                                    created_by, thread)

    def execute_command(self, c, payload, cmd, pid):
        inner = Command(cmd.id + ':exec', payload['op'], cmd.actor, OWNER, payload['item_id'], payload['args'],
                        auth={'proposal': pid, 'explicit': True})
        return getattr(self, 'op_' + payload['op'])(c, inner) or {}

    def op_reject_proposal(self, c, cmd: Command):
        pid = cmd.args['proposal_id']
        p = c.execute('SELECT * FROM ops_proposals WHERE id=?', (pid,)).fetchone()
        if not p or p['state'] != 'pending':
            raise Rejected('No pending proposal with that id', 'unknown_proposal')
        if p['thread'] and cmd.auth.get('thread') and p['thread'] != cmd.auth.get('thread'):
            raise Rejected('Reject the proposal in the thread where it was made', 'wrong_thread')
        c.execute("UPDATE ops_proposals SET state='rejected', decided_by=?, updated=? WHERE id=?",
                  (cmd.actor, self.now(), pid))
        payload = loads(p['payload'], {})
        undo = getattr(self, 'reject_' + p['kind'], None)
        if undo:
            undo(c, payload, cmd)
        return {'proposal_id': pid, 'rejected': True}
