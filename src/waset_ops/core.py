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
PROPOSAL_TTL = 30 * 60
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
        * Rejections are durable outcomes; unexpected errors record ``failed``.
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
                c.execute('UPDATE ops_commands SET state=?, result=?, updated=? WHERE id=?',
                          (state, dumps(result), self.now(), cmd.id))
                audit(c, cmd.item_id, 'command', cmd.actor, {'id': cmd.id, 'op': cmd.op, 'state': state})
                return {**result, 'state': state, 'command_id': cmd.id}
        except sqlite3.Error as e:
            try:
                with self.store.tx() as c:
                    c.execute('UPDATE ops_commands SET state=?, result=?, updated=? WHERE id=? AND state=?',
                              ('failed', dumps({'reason': 'storage error'}), self.now(), cmd.id, 'received'))
            except sqlite3.Error:
                pass
            return {'state': 'failed', 'code': 'storage', 'reason': 'Operational store unavailable: ' + str(e)[:200]}

    # Which actor kinds may run which operation. Enforced in code, never in prompts.
    PERMISSIONS = {
        'pause': {OWNER, MONDAY},
        'skip': {OWNER, MONDAY},
        'resume': {OWNER},
        'change_format': {OWNER},
        'replace_source': {OWNER, MONDAY},
        'update_caption': {OWNER, MONDAY},
        'confirm_topaz': {OWNER, MONDAY},
        'request_reschedule': {OWNER, MONDAY},
        'request_recheck': {OWNER, MONDAY, BONDOK, 'service:wf3'},
        'request_publish': {OWNER},
        'resolve_outcome': {OWNER},
        'approve_proposal': {OWNER},
        'reject_proposal': {OWNER},
        'propose': {OWNER, BONDOK, MONDAY, 'service:wf3', 'service:wf1'},
        'set_notes': {OWNER, MONDAY},
        'set_variety': {OWNER, MONDAY},
        'set_collab': {OWNER, MONDAY},
        'set_code': {OWNER, MONDAY},
        'set_folder': {OWNER, MONDAY},
        'hold': {'service:wf1', 'service:wf2', 'service:wf3', 'service:bondok', OWNER},
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
        if notice and self.now() - notice.get('at', 0) < 86400 and not action:
            action = notice['text']
        system = []
        if it.get('infra_issue'):
            system.append('Temporary system issue (content not rejected): ' + it['infra_issue'])
        if res:
            system.append('Confirmed slot ' + rules.display(rules.instant(res['slot'])))
        d = {
            'status': board.LABELS[label],
            'action': action or None,
            'system': '\n'.join(system) or None,
            'publish_at': res['slot'] if res else None,
            'post_date': rules.monday_legacy_values(rules.instant(res['slot']))['date4']['date'] if res else None,
            'post_time': (lambda v: f"{v['hour']:02d}:{v['minute']:02d}")(
                rules.monday_legacy_values(rules.instant(res['slot']))['hour_mm7xy9cf']) if res else None,
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
            d['ig_media'] = last['media_id']
            d['published_at'] = rules.iso(rules.instant(loads(last['evidence'], {}).get('published_at')
                                                         or self.iso_ts(last['updated'])))
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
        changes = {}
        for k, v in desired.items():
            cv = board.compare_value(k, v)
            if k in force_keys or base.get(k, '__unset__') != cv:
                if k not in confirmed and k not in pending and cv is None and k not in force_keys:
                    continue  # never projected and empty: nothing to clear
                changes[k] = v
        group_change = group if base.get('_group') != group else None
        if not changes and not group_change:
            return None
        columns, compare, group_out = {}, {}, None
        # Merge unsent display jobs so superseding never drops a column write.
        older = c.execute("SELECT id, payload FROM ops_outbox WHERE kind='monday' AND item_id=? AND "
                          "dedupe_key LIKE 'monday:%' AND state IN ('pending','failed') ORDER BY id",
                          (str(item_id),)).fetchall()
        for o in older:
            op = loads(o['payload'], {})
            columns.update(op.get('columns', {}))
            compare.update(op.get('compare', {}))
            group_out = op.get('group') or group_out
        columns.update({board.COL[k]: board.mutation_value(k, v) for k, v in changes.items()})
        compare.update({k: board.compare_value(k, v) for k, v in changes.items()})
        if group_change:
            compare['_group'] = group_change
            group_out = group_change
        payload = {'item_id': str(item_id), 'columns': columns, 'compare': compare, 'group': group_out}
        key = 'monday:' + str(item_id) + ':' + fingerprint(payload)[:16]
        now = self.now()
        if older:
            c.execute(f"UPDATE ops_outbox SET state='superseded', updated=? WHERE id IN ({','.join('?'*len(older))})",
                      (now, *[o['id'] for o in older]))
        c.execute('INSERT OR IGNORE INTO ops_outbox(kind,dedupe_key,item_id,payload,state,next_at,created,updated) '
                  "VALUES('monday',?,?,?,'pending',0,?,?)", (key, str(item_id), dumps(payload), now, now))
        c.execute('UPDATE ops_items SET pending_projection=? WHERE item_id=?',
                  (dumps({**pending, **compare}), str(item_id)))
        return payload

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
            rows = c.execute(f"SELECT * FROM ops_outbox WHERE kind IN ({','.join('?'*len(kinds))}) AND "
                             "((state IN ('pending','failed') AND next_at<=?) OR (state='in_flight' AND lease_until<?)) "
                             'ORDER BY id LIMIT ?', (*kinds, now, now, limit)).fetchall()
            out = []
            for r in rows:
                c.execute("UPDATE ops_outbox SET state='in_flight', lease_owner=?, lease_until=?, updated=? WHERE id=?",
                          (worker, now + lease, now, r['id']))
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
            p = loads(r['payload'], {})
            if ok:
                c.execute("UPDATE ops_outbox SET state='done', last_error=NULL, updated=? WHERE id=?", (now, job_id))
                if r['kind'] == 'monday':
                    it = self.item(c, r['item_id'], required=False)
                    if it:
                        compare = p.get('compare', {})
                        human = {k[2:]: v for k, v in compare.items() if k.startswith('h:')}
                        confirmed = {**(loads(it['projected'], {}) or {}),
                                     **{k: v for k, v in compare.items() if not k.startswith('h:')}}
                        pending = {k: v for k, v in (loads(it['pending_projection'], {}) or {}).items()
                                   if not (k.startswith('h:') and k[2:] in human and v == human[k[2:]])}
                        if human:
                            observed = {**(loads(it['observed'], {}) or {}), **human}
                            c.execute('UPDATE ops_items SET observed=? WHERE item_id=?', (dumps(observed), r['item_id']))
                        live = c.execute("SELECT 1 FROM ops_outbox WHERE kind='monday' AND item_id=? AND "
                                         "state IN ('pending','in_flight','failed')", (r['item_id'],)).fetchone()
                        c.execute('UPDATE ops_items SET projected=?, pending_projection=? WHERE item_id=?',
                                  (dumps(confirmed), dumps(pending) if live else None, r['item_id']))
                elif r['kind'] == 'editor':
                    c.execute("UPDATE ops_editor_tasks SET task_id=COALESCE(?,task_id), state='done', updated=? "
                              'WHERE item_id=? AND issue_key=?',
                              ((result or {}).get('task_id'), now, r['item_id'], p.get('issue_key')))
                return {'ok': True}
            attempts = r['attempts'] + 1
            if attempts >= OUTBOX_MAX_ATTEMPTS:
                c.execute("UPDATE ops_outbox SET state='escalated', attempts=?, last_error=?, updated=? WHERE id=?",
                          (attempts, (error or '')[:500], now, job_id))
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
    def create_proposal(self, c, kind, bindings: list[str], payload: dict, summary: str, created_by: str,
                        thread=None, notify=True) -> dict:
        items = []
        for iid in bindings:
            it = self.item(c, iid)
            res = self.reservation(c, iid)
            items.append({'item_id': str(iid), 'version': it['version'], 'slot': res['slot'] if res else None})
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
        if p['expires'] < self.now():
            raise Rejected('This proposal expired; ask for an updated one', 'expired')
        for b in loads(p['bindings'], []):
            it = self.item(c, b['item_id'])
            res = self.reservation(c, b['item_id'])
            if it['version'] != b['version'] or (res['slot'] if res else None) != b['slot']:
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
        c.execute("UPDATE ops_proposals SET state='rejected', decided_by=?, updated=? WHERE id=?",
                  (cmd.actor, self.now(), pid))
        payload = loads(p['payload'], {})
        undo = getattr(self, 'reject_' + p['kind'], None)
        if undo:
            undo(c, payload, cmd)
        return {'proposal_id': pid, 'rejected': True}
