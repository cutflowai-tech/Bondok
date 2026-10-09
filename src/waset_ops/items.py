"""Items: import identity, Monday edit observation, owner commands, preparation results."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from . import board, rules
from .core import BONDOK, MONDAY, OWNER, PRE_COMMIT, STALE_SNAPSHOT_SECONDS, Command, Rejected, fingerprint
from .db import audit, dumps, loads

PROTECTED_PUBLICATION = ('published', 'outcome_unknown', 'in_progress')
RELIST_READY_SECONDS = 30 * 60          # metadata-only revision check for ready items
INFRA_RECHECK_SECONDS = 20 * 60
FRESH_READ_SECONDS = 120              # non-WF1 callers read the board right before observing
TIME_KEYS = ('publish_at', 'post_date', 'post_time')
RESUMABLE_HOLDS = ('resume', 'rejected_edit', 'publish_retry_limit')
BACKOFF_STEPS = (600, 1200, 2400, 3600) # waiting/blocked items: fair, bounded backoff


def _h(x) -> str:
    return hashlib.sha256(str(x).encode()).hexdigest()[:12]


def dropbox_url(url: str) -> str:
    u = urlsplit(url or '')
    host = (u.hostname or '').lower()
    if u.scheme != 'https' or u.username or u.password or u.port not in (None, 443):
        raise Rejected('A secure https Dropbox link is required', 'invalid_url')
    if not any(host == d or host.endswith('.' + d) for d in ('dropbox.com', 'dropboxusercontent.com')):
        raise Rejected('A Dropbox link is required', 'invalid_url')
    return url


class ItemsMixin:
    # ------------------------------------------------------------------ import
    def import_plan(self, sources: list[dict], social: list[dict]) -> dict:
        """Decide which eligible source projects need a social item.

        Durable source->social identity prevents duplicates; a mapped item that
        disappeared from the board is reported, never recreated blindly.
        sources: [{id,name,code,format,link,owner_ids}] social: [{id,source_item,code,format}]
        """
        norm = lambda x: re.sub(r'\s+', '', str(x or '')).upper()
        counts = {}
        for s in sources:
            if norm(s.get('code')):
                counts[norm(s['code'])] = counts.get(norm(s['code']), 0) + 1
        creates, findings, record = [], [], []
        social_ids = {str(i['id']) for i in social}
        with self.store.read() as c:
            mapped = {r['source_item_id']: dict(r) for r in c.execute('SELECT * FROM ops_source_map')}
        for s in sources:
            sid, code, fmt = str(s['id']), norm(s.get('code')), s.get('format')
            if not code or fmt not in ('Post', 'Story'):
                continue
            if counts.get(code) != 1 or s.get('duplicate'):
                findings.append({'kind': 'ambiguous_source_code', 'source': sid, 'code': code})
                continue
            m = mapped.get(sid)
            if m:
                if m['social_item_id'] not in social_ids:
                    findings.append({'kind': 'mapped_social_item_missing', 'source': sid,
                                     'social': m['social_item_id']})
                continue
            same = [i for i in social if str(i.get('source_item') or '') == sid or
                    (not i.get('source_item') and norm(i.get('code')) == code)]
            if len(same) == 1:
                record.append({'source': sid, 'social': str(same[0]['id'])})
                continue
            if len(same) > 1:
                findings.append({'kind': 'ambiguous_social_items', 'source': sid,
                                 'social': [str(i['id']) for i in same]})
                continue
            try:
                st = rules.style(code)
            except rules.RuleError:
                findings.append({'kind': 'invalid_code', 'source': sid, 'code': code})
                continue
            cv = {board.COL['source_item']: sid, board.COL['style']: st, board.COL['code']: code,
                  board.COL['format']: {'label': fmt}, 'status': {'label': board.LABELS['checking']},
                  board.COL['topaz']: {'label': 'Not yet'}}
            link = s.get('link')
            if link:
                cv[board.COL['dropbox']] = {'url': link, 'text': 'Source'}
                if '/scl/fo/' in link:
                    cv[board.COL['folder']] = {'url': link, 'text': 'Project folder'}
            if s.get('owner_ids'):
                cv[board.COL['owner']] = {'personsAndTeams': [{'id': int(x), 'kind': 'person'} for x in s['owner_ids']]}
            creates.append({'key': sid, 'name': s.get('name') or code, 'group': board.GROUPS[fmt], 'columns': cv})
        if record:
            self.import_record(record)
        return {'creates': creates, 'findings': findings, 'recorded': len(record)}

    def import_record(self, pairs: list[dict]) -> dict:
        n = 0
        with self.store.tx() as c:
            for p in pairs:
                cur = c.execute("INSERT OR IGNORE INTO ops_source_map VALUES(?,?, 'active', ?)",
                                (str(p['source']), str(p['social']), self.now()))
                n += cur.rowcount
        return {'recorded': n}

    # ------------------------------------------------------------------ observation
    def observe(self, items: list[dict], *, sources: list[dict] | None = None, complete=False,
                actor='monday-poll', limit=20) -> dict:
        """Trusted board adapter: convert human edits into validated commands.

        * First sight of an item imports its state conservatively (no commands).
        * A human-editable column differing from the last observed value is a
          human edit; a system column differing from both the confirmed and the
          pending projection is a human edit too. Our own writes match the
          projection, so they never loop back as requests.
        """
        results, created = [], []
        seen = set()
        src = {str(s['id']): s for s in (sources or [])}
        # Earliest moment this snapshot can have been read: WF1 reads the board after its run started; other
        # callers read just before observing. Only a write acknowledged after that may be missing from the
        # snapshot (stale); an older one is not, so a matching value is a genuine edit (round-2 review #1).
        with self.store.read() as c:
            r = c.execute("SELECT started FROM ops_runs WHERE kind='wf1'").fetchone()
        floor = (r['started'] or 0) if actor == 'service:wf1' and r else self.now() - FRESH_READ_SECONDS
        for raw in items:
            iid = str(raw['id'])
            seen.add(iid)
            snap = board.snapshot(raw)
            with self.store.tx() as c:
                it = self.item(c, iid, required=False)
                if it is None:
                    self._bootstrap(c, iid, snap)
                    created.append(iid)
                    continue
                edits = self._diff(c, it, snap, floor)
                c.execute('UPDATE ops_items SET name=? WHERE item_id=?', (snap.get('name'), iid))
            for e in edits:
                results.append({'item_id': iid, 'edit': e['key'], **self._apply_edit(iid, e)})
            if src and it is not None and it.get('source_item_id') in src:
                s = src[it['source_item_id']]
                if s.get('format') == 'Canceled':
                    r = self.submit(Command(f"source-cancel:{iid}:{it['source_item_id']}", 'source_canceled',
                                            'service:wf1', 'service:wf1', iid, {}))
                    if r['state'] == 'completed' and not r.get('duplicate'):
                        results.append({'item_id': iid, 'edit': 'source_canceled', **r})
        missing = self.board_missing(seen) if complete else []
        return {'edits': results, 'imported': created, 'missing': missing,
                'work': self.work_queue(limit=limit) if actor == 'service:wf1' else None}

    def board_missing(self, seen) -> list:
        """Items known to the store but absent from a complete board snapshot."""
        seen, missing = {str(x) for x in seen}, []
        if not seen:
            return missing        # an empty snapshot is never treated as "everything deleted"
        with self.store.tx() as c:
            for r in c.execute("SELECT fingerprint, item_id FROM ops_findings WHERE resolved IS NULL AND "
                               "kind='missing_on_board'").fetchall():
                if r['item_id'] in seen:
                    self.resolve_finding(c, r['fingerprint'])        # it is back on the board
            for r in c.execute("SELECT item_id FROM ops_items WHERE publication!='published'").fetchall():
                if r['item_id'] not in seen:
                    missing.append(r['item_id'])
                    self.finding(c, 'missing_on_board:' + r['item_id'], r['item_id'], 'missing_on_board',
                                 f"Item {r['item_id']} is in the operational store but not on the board; "
                                 'it will not be recreated automatically.')
        return missing

    def _bootstrap(self, c, iid, snap):
        """Conservative legacy import: never invents verification or receipts."""
        now = self.now()
        status = snap.get('status')
        fmt = snap.get('format') if snap.get('format') in rules.FORMATS else None
        posted = status == 'Posted' or bool(snap.get('post_link')) or bool(snap.get('ig_media'))
        publication = 'published' if posted else ('outcome_unknown' if status == 'Publishing' else 'not_started')
        owner_state = {'Paused': 'paused', 'Skipped': 'skipped'}.get(status, 'active')
        caption = snap.get('caption')
        req = board.requested_instant(snap) if not posted else None
        requested = rules.iso(req) if req and req > self.now_dt() + rules.NEAR_DUE else None
        topaz_asset = snap.get('asset') if snap.get('topaz') == 'Topazed' and snap.get('asset') else None
        observed = {k: snap.get(k) for k in board.HUMAN}
        projected = {k: snap.get(k) for k in board.SYSTEM}
        projected['_group'] = snap.get('group')
        hold = None
        if not fmt:
            hold = {'kind': 'format_missing', 'reason': 'Format must be Post or Story (owner decision)'}
        c.execute('INSERT INTO ops_items(item_id,name,code,source_item_id,format,caption,caption_state,caption_origin,'
                  'collab,variety,notes,folder_url,file_url,asset_key,topaz_asset,readiness,owner_state,owner_state_reason,'
                  'hold,requested_at,requested_by,publication,legacy_posted,observed,projected,waiting_since,created,updated) '
                  'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                  (iid, snap.get('name'), snap.get('code'), snap.get('source_item'), fmt, caption,
                   ('legacy_unapproved' if caption else 'missing') if fmt == 'Post' else None,
                   'legacy' if caption else None, snap.get('collab'), snap.get('variety'), snap.get('notes'),
                   snap.get('folder'), snap.get('dropbox'), snap.get('asset'), topaz_asset, 'unchecked',
                   owner_state, 'imported from board status' if owner_state != 'active' else None,
                   dumps(hold) if hold else None, requested, 'legacy-board' if requested else None,
                   publication, 1 if posted else 0, dumps(observed), dumps(projected), now, now, now))
        if snap.get('source_item'):
            c.execute("INSERT OR IGNORE INTO ops_source_map VALUES(?,?, 'active', ?)", (snap['source_item'], iid, now))
        audit(c, iid, 'bootstrap', 'service:migration', {'status': status, 'publication': publication,
                                                          'owner_state': owner_state, 'requested': requested})
        if publication == 'outcome_unknown':
            self.notify(c, 'legacy-publishing:' + iid, f'Item {iid} ({snap.get("name")}) showed "Publishing" when the '
                        'new system started. Treated as outcome unknown: it will not be published again until you '
                        'confirm on Instagram and tell Bondok.', iid)

    def _diff(self, c, it, snap, floor=None) -> list[dict]:
        observed = loads(it['observed'], {}) or {}
        confirmed = loads(it['projected'], {}) or {}
        pending = loads(it['pending_projection'], {}) or {}
        edits = []
        floor = self.now() - FRESH_READ_SECONDS if floor is None else floor
        recent = {k: x['v'] for k, x in (confirmed.get('_prev') or {}).items()
                  if x.get('at', 0) > floor and self.now() - x.get('at', 0) < STALE_SNAPSHOT_SECONDS}
        for k in board.HUMAN:
            new = snap.get(k)
            if new == observed.get(k):
                continue
            if 'h:' + k in recent and new == recent['h:' + k]:
                continue                       # snapshot read before our write landed (not a human edit)
            if 'h:' + k in pending:
                if new == pending['h:' + k]:
                    observed[k] = new          # our authorized write landed
                    continue
            edits.append({'key': k, 'old': observed.get(k), 'new': new, 'human': True,
                          'asset_shown': snap.get('asset')})
        for k in board.SYSTEM:
            new = snap.get(k)
            if k not in confirmed and k not in pending:
                continue                       # never projected: nothing to compare
            if new == confirmed.get(k) or (k in pending and new == pending.get(k)):
                continue
            if k in recent and new == recent[k]:
                continue                       # snapshot read before our write landed
            if k == 'publish_at' and it.get('requested_at') and new == board.compare_value('publish_at', it['requested_at']):
                continue                       # already recorded as the requested time (not yet reservable)
            if k in ('post_date', 'post_time') and it.get('requested_at') and not snap.get('publish_at'):
                try:
                    at = board.requested_instant({'post_date': snap.get('post_date'), 'post_time': snap.get('post_time')})
                except Exception:   # noqa: BLE001 - unreadable values are handled as edits below
                    at = None
                if at is not None and rules.iso(at) == it['requested_at']:
                    continue                   # the legacy pair names the recorded request: not a new edit
            edits.append({'key': k, 'old': pending.get(k, confirmed.get(k)), 'new': new, 'human': False,
                          'snap': {x: snap.get(x) for x in ('publish_at', 'post_date', 'post_time')}})
        c.execute('UPDATE ops_items SET observed=? WHERE item_id=?', (dumps(observed), it['item_id']))
        return edits

    def _apply_edit(self, iid, e) -> dict:
        """Map one observed board edit to a command (actor: unattributed board user)."""
        with self.store.read() as c:
            v = c.execute('SELECT version FROM ops_items WHERE item_id=?', (iid,)).fetchone()['version']
        cid = f"monday:{iid}:{e['key']}:{v}:{_h(e['new'])}"
        k, new = e['key'], e['new']

        def run(op, args=None):
            return self.submit(Command(cid, op, 'monday', MONDAY, iid, args or {}))

        if e['human']:
            if k == 'format':
                r = run('propose', {'kind': 'change_format', 'format': new})
                if r.get('state') == 'rejected':
                    with self.store.tx() as c:            # write-back survives the rejection (audit MS8)
                        self.write_human(c, self.item(c, iid), {'format': self.item(c, iid)['format']}, guarded=False)
            elif k == 'caption':
                r = run('update_caption', {'text': new or ''})
            elif k == 'topaz':
                # Bound to the file version the person could see when toggling.
                r = run('confirm_topaz', {'confirmed': new == 'Topazed', 'asset_key': e.get('asset_shown')})
            elif k == 'collab':
                r = run('set_collab', {'value': new or ''})
            elif k == 'variety':
                r = run('set_variety', {'value': new or ''})
            elif k == 'code':
                r = run('set_code', {'value': new or ''})
            elif k == 'notes':
                r = run('set_notes', {'value': new or ''})
            else:  # owner/people: informational only
                r = {'state': 'completed', 'note': 'no operational effect'}
            self._mark_observed(iid, k, new, r)
            return r
        r = self._system_edit(iid, e, run)
        # The board value was handled (applied or refused): it is what the board shows now, so it is not an
        # edit again next cycle (a person's Posted / post link no longer re-holds every run; a value that cannot
        # be reverted is not re-rejected every run), and the next display write compares against it and
        # restores the system value where it owns one (round-2 review #2, #3).
        with self.store.tx() as c:
            cur = self.item(c, iid)
            proj = loads(cur['projected'], {}) or {}
            proj[k] = board.compare_value(k, new) if new is not None else None
            c.execute('UPDATE ops_items SET projected=? WHERE item_id=?', (dumps(proj), iid))
            col = board.COL.get(k)
            for j in c.execute("SELECT id, payload FROM ops_outbox WHERE kind='monday' AND item_id=? AND "
                               "state IN ('pending','failed')", (iid,)).fetchall():
                pl = loads(j['payload'], {})
                if col in (pl.get('guard') or {}):     # jobs queued by the command itself compare against it too
                    pl['guard'][col]['was'] = [proj[k]]
                    c.execute('UPDATE ops_outbox SET payload=? WHERE id=?', (dumps(pl), j['id']))
            if r.get('state') != 'rejected' or r.get('reverted'):
                self.project(c, iid)
        if r.get('state') == 'rejected' and not r.get('reverted'):
            # The command's own revert/notice were rolled back with it: restore the board here (audit S7).
            keys = TIME_KEYS if k in TIME_KEYS else (k,)
            self._revert(iid, keys, r.get('reason') or 'Change not accepted')
            r = {**r, 'reverted': k}
        return r

    def _system_edit(self, iid, e, run) -> dict:
        k, new = e['key'], e['new']
        if k == 'status':
            if new == 'Paused':
                return run('pause', {'reason': 'Paused on the board'})
            if new == 'Skipped':
                return run('skip', {'reason': 'Skipped on the board'})
            with self.store.read() as c:
                it = self.item(c, iid)
            if it['owner_state'] in ('paused', 'skipped'):
                return run('propose', {'kind': 'resume', 'from_label': new})
            if new == 'Posted' and it['publication'] != 'published':
                return run('hold', {'kind': 'external_posted', 'reason': 'Marked Posted on the board without a publication '
                                    'receipt. Publication is held; confirm in Slack whether it was posted manually.'})
            return self._revert(iid, k, 'Status is derived by the system; use Pause/Skip or ask Bondok')
        if k == 'publish_at' and not new:
            # Clearing Publish at unschedules; legacy Post Date/Time never stand in for it (contract, audit S2).
            return run('request_reschedule', {'at': None})
        if k in ('publish_at', 'post_date', 'post_time'):
            at = board.requested_instant(e['snap'])
            if k != 'publish_at' and e['snap'].get('publish_at'):
                at = board.requested_instant({'post_date': e['snap'].get('post_date'),
                                              'post_time': e['snap'].get('post_time')})
            if at is None and not any(e['snap'].values()):
                return run('request_reschedule', {'at': None})
            if at is None:
                return self._revert(iid, k, 'Requested time could not be read; use Publish at (Cairo time)')
            return run('request_reschedule', {'at': rules.iso(at)})
        if k == 'dropbox' and new:
            return run('replace_source', {'url': new})
        if k == 'folder' and new:
            return run('set_folder', {'url': new})
        if k in ('post_link', 'ig_media') and new:
            return run('hold', {'kind': 'external_posted', 'reason': 'Publication evidence was typed on the board. '
                                'Publication is held; confirm in Slack whether it was posted manually.'})
        return self._revert(iid, k, 'This column is managed by the system')

    def _mark_observed(self, iid, key, value, result):
        with self.store.tx() as c:
            it = self.item(c, iid)
            observed = loads(it['observed'], {}) or {}
            observed[key] = value
            c.execute('UPDATE ops_items SET observed=? WHERE item_id=?', (dumps(observed), iid))
            # A person's edit wins over our unsent write of the same column (round-2 review #5) - except our
            # write-back of a refused edit, which is meant to overwrite it.
            for j in [] if result.get('state') == 'rejected' else c.execute("SELECT id, payload FROM ops_outbox WHERE kind='monday' AND item_id=? AND "
                               "dedupe_key LIKE 'monday-h:%' AND state IN ('pending','failed')", (iid,)).fetchall():
                if 'h:' + key in (loads(j['payload'], {}).get('compare') or {}):
                    c.execute("UPDATE ops_outbox SET state='superseded', updated=? WHERE id=?", (self.now(), j['id']))
            pend = loads(self.item(c, iid)['pending_projection'], {}) or {}
            if result.get('state') != 'rejected' and pend.pop('h:' + key, None) is not None:
                c.execute('UPDATE ops_items SET pending_projection=? WHERE item_id=?', (dumps(pend) or None, iid))
            if result.get('state') == 'rejected' and key in ('caption', 'topaz', 'collab', 'code'):
                self.release(c, self.item(c, iid), 'rejected board edit')     # a held item keeps no slot (fuzz F8)
                self.update_item(c, iid, 'monday', 'rejected board edit',
                                 hold=dumps({'kind': 'rejected_edit', 'reason': f'Board change to {key} was not accepted: '
                                             + result.get('reason', '')}))

    def _revert(self, iid, key, why):
        keys = tuple(key) if isinstance(key, (tuple, list)) else (key,)
        with self.store.tx() as c:
            self.set_notice(c, iid, 'Board change not applied: ' + str(why)[:300])
            self.project(c, iid, force_keys=keys + ('action',))
            audit(c, iid, 'revert_system_column', 'monday', {'key': keys, 'why': why})
        return {'state': 'rejected', 'reason': why, 'reverted': key}

    def write_human(self, c, it, values: dict, guarded=True):
        """Apply an authorized change to a human column (e.g. owner-approved
        caption or format). Recorded as pending so the old board value is not
        mistaken for a new human edit while the write is in flight."""
        pending = loads(it.get('pending_projection'), {}) or {}
        observed = loads(it.get('observed'), {}) or {}
        cols, compare, guard = {}, {}, {}
        # Earlier writes of the same column: an unsent one is replaced by this write; one already taken by WF2
        # may land first, so its value is an expected board state, not a person's edit (fuzz F3).
        landing = {}
        for j in c.execute("SELECT id, state, payload FROM ops_outbox WHERE kind='monday' AND item_id=? AND "
                           "dedupe_key LIKE 'monday-h:%' AND state IN ('pending','failed','in_flight')",
                           (it['item_id'],)).fetchall():
            jp = loads(j['payload'], {}) or {}
            same = [k for k in values if 'h:' + k in (jp.get('compare') or {})]
            for k in same:
                landing.setdefault(k, []).append(jp['compare']['h:' + k])
            if same and j['state'] != 'in_flight' and set(jp.get('columns') or {}) <= {board.COL[k] for k in values}:
                c.execute("UPDATE ops_outbox SET state='superseded', updated=? WHERE id=?", (self.now(), j['id']))
        for k, v in values.items():
            cols[board.COL[k]] = board.mutation_value(k, v)
            compare['h:' + k] = board.compare_value(k, v)
            # Skip the write if a person typed something else meanwhile (WF2 compare-before-write).
            if guarded:
                guard[board.COL[k]] = {'kind': board.KIND.get(k, 'text'), 'was': [observed.get(k), *landing.get(k, [])],
                                       'new': compare['h:' + k]}
        payload = {'item_id': it['item_id'], 'columns': cols, 'compare': compare, 'group': None}
        if guard:
            payload['guard'] = guard
        self.enqueue(c, 'monday', 'monday-h:' + it['item_id'] + ':' + fingerprint(payload)[:16], payload, it['item_id'])
        c.execute('UPDATE ops_items SET pending_projection=? WHERE item_id=?',
                  (dumps({**pending, **compare}), it['item_id']))

    def finding(self, c, fp, item_id, kind, detail, notify=True) -> bool:
        now = self.now()
        r = c.execute('SELECT * FROM ops_findings WHERE fingerprint=?', (fp,)).fetchone()
        if r and r['resolved'] is None:
            c.execute('UPDATE ops_findings SET last_seen=? WHERE fingerprint=?', (now, fp))
            return False
        c.execute('INSERT OR REPLACE INTO ops_findings VALUES(?,?,?,?,?,?,?,NULL)',
                  (fp, item_id, kind, detail, now, now, 1 if notify else 0))
        if notify:
            self.notify(c, 'finding:' + fp + ':' + str(int(now)), detail, item_id)
        return True

    def resolve_finding(self, c, fp):
        c.execute('UPDATE ops_findings SET resolved=? WHERE fingerprint=? AND resolved IS NULL', (self.now(), fp))

    # ------------------------------------------------------------------ owner/human commands
    def _guard_mutable(self, c, it, allow_paused=False):
        if it['publication'] == 'published':
            raise Rejected('This item is already published; its record is protected', 'published')
        if it['publication'] == 'outcome_unknown':
            raise Rejected('A publication attempt has an unknown outcome; resolve it first', 'outcome_unknown')
        att = self.active_attempt(c, it['item_id'])
        if att and att['stage'] == 'committed':
            raise Rejected('Publication may already be in progress; the change was not applied', 'in_progress')
        if not allow_paused and it['owner_state'] != 'active':
            raise Rejected(f"Item is {it['owner_state']}; ask the owner to resume it first", it['owner_state'])
        return att

    def release(self, c, it, why, keep_request=True):
        res = self.reservation(c, it['item_id'])
        if res:
            c.execute('DELETE FROM ops_reservations WHERE item_id=?', (it['item_id'],))
            audit(c, it['item_id'], 'reservation_released', 'system', {'slot': res['slot'], 'why': why})
            if keep_request and res['owner_pinned'] and not it.get('requested_at'):
                c.execute('UPDATE ops_items SET requested_at=?, requested_by=? WHERE item_id=?',
                          (res['slot'], 'owner', it['item_id']))
        return res

    def op_pause(self, c, cmd: Command):
        it = self.item(c, cmd.item_id)
        if it['publication'] == 'published':
            raise Rejected('Already published; nothing to pause', 'published')
        att = self.active_attempt(c, it['item_id'])
        res = self.release(c, it, 'paused')
        # An owner-chosen slot stays the owner's request; an automatic one is only a preference (audit S3).
        keep = dict(requested_at=res['slot'], requested_by=None if res['owner_pinned'] else 'legacy-board') if res \
            else dict(requested_at=it.get('requested_at'), requested_by=it.get('requested_by'))
        self.update_item(c, it['item_id'], cmd.actor, 'pause', owner_state='paused',
                         owner_state_reason=cmd.args.get('reason') or 'Paused by owner', **keep)
        if att and att['stage'] in ('committed', 'outcome_unknown'):
            return {'paused': True, 'publication': 'may_already_be_in_progress',
                    'message': 'Paused for the future, but publication may already be in progress; '
                               'the outcome will be reconciled and reported.'}
        return {'paused': True, 'publication': 'stopped_before_commit' if att else 'not_started',
                'message': 'Paused. It will not publish until the owner resumes it.'}

    def op_skip(self, c, cmd: Command):
        it = self.item(c, cmd.item_id)
        if it['publication'] == 'published':
            raise Rejected('Already published; nothing to skip', 'published')
        att = self.active_attempt(c, it['item_id'])
        self.release(c, it, 'skipped', keep_request=False)
        self.update_item(c, it['item_id'], cmd.actor, 'skip', owner_state='skipped',
                         owner_state_reason=cmd.args.get('reason') or 'Skipped by owner')
        if att and att['stage'] in ('committed', 'outcome_unknown'):
            return {'skipped': True, 'publication': 'may_already_be_in_progress'}
        return {'skipped': True}

    def op_resume(self, c, cmd: Command):
        it = self.item(c, cmd.item_id)
        hold = loads(it['hold'], None)
        clearable = bool(hold and hold.get('kind') in RESUMABLE_HOLDS)
        if it['owner_state'] == 'active' and not clearable:
            return {'resumed': False, 'message': 'Item is not paused or skipped'}
        # Resume also clears holds that only wait for the owner to say "continue" (audit: a rejected board
        # edit or the publish retry limit on an active item could otherwise never be cleared).
        self.update_item(c, it['item_id'], cmd.actor, 'resume', owner_state='active', owner_state_reason=None,
                         hold=None if clearable else it['hold'])
        out = self.try_schedule(c, it['item_id'], cmd.actor)
        return {'resumed': True, **out}

    def explicit_owner(self, cmd: Command) -> bool:
        return cmd.actor_kind == OWNER and (cmd.auth.get('explicit') is True or bool(cmd.auth.get('proposal')))

    def op_change_format(self, c, cmd: Command):
        if not self.explicit_owner(cmd):
            raise Rejected('Changing Story/Post needs an explicit owner instruction or approval', 'needs_owner')
        new = cmd.args.get('format')
        if new not in rules.FORMATS:
            raise Rejected('Format must be Post or Story', 'invalid')
        it = self.item(c, cmd.item_id)
        return self.execute_change_format(c, {'item_id': it['item_id'], 'format': new}, cmd, cmd.auth.get('proposal'))

    def execute_change_format(self, c, payload, cmd, pid=None):
        it = self.item(c, payload['item_id'])
        new = payload['format']
        self._guard_mutable(c, it, allow_paused=True)
        if it['format'] == new:
            hold = loads(it['hold'], None)
            if hold and hold.get('kind') == 'format_change':
                self.update_item(c, it['item_id'], cmd.actor, 'format confirmed', hold=None)
            return {'format': new, 'changed': False}
        self.release(c, it, 'format changed', keep_request=False)
        c.execute('UPDATE ops_items SET content_rev=content_rev+1 WHERE item_id=?', (it['item_id'],))
        caption_state = it['caption_state']
        if new == 'Post':
            # A caption typed while the item was a Story was never validated: it is approved only if it passes
            # the Post caption checks now (fuzz F7: an invalid caption became approved).
            caption_state = 'approved' if it['caption_origin'] in ('human', 'approved_draft') and it['caption'] and \
                rules.validate_caption(it['caption']) is None else ('legacy_unapproved' if it['caption'] else 'missing')
        it = self.update_item(c, it['item_id'], cmd.actor, f'format {it["format"]}->{new}', format=new,
                              verification_id=None, readiness='checking', block_kind=None, block_reason=None,
                              block_key=None, caption_state=caption_state if new == 'Post' else None, hold=None,
                              requested_at=None, waiting_since=self.now())
        self.write_human(c, it, {'format': new})
        self.request_check(c, it['item_id'], cmd.actor, 'format_change')
        return {'format': new, 'changed': True, 'message': f'Format changed to {new}; the same item will be '
                'rechecked under {new} rules before scheduling.'.format(new=new)}

    def reject_change_format(self, c, payload, cmd):
        it = self.item(c, payload['item_id'])
        hold = loads(it['hold'], None)
        if hold and hold.get('kind') == 'format_change':
            self.update_item(c, it['item_id'], cmd.actor, 'format change rejected', hold=None)
            self.write_human(c, self.item(c, it['item_id']), {'format': it['format']})

    def reject_resume(self, c, payload, cmd):
        self.project(c, payload['item_id'], force_keys=('status',))

    def op_propose(self, c, cmd: Command):
        """Create an approval request. Never executes anything itself."""
        kind = cmd.args.get('kind')
        it = self.item(c, cmd.item_id)
        if kind == 'change_format':
            new = cmd.args.get('format')
            if new not in rules.FORMATS or new == it['format']:
                if cmd.actor_kind == MONDAY:
                    self.write_human(c, it, {'format': it['format']})
                raise Rejected('Invalid format change request', 'invalid')
            if cmd.actor_kind == MONDAY:
                self.release(c, it, 'format change requested on board', keep_request=False)
                self.update_item(c, it['item_id'], cmd.actor, 'format change requested', hold=dumps(
                    {'kind': 'format_change', 'format': new,
                     'reason': f'Format changed to {new} on the board; waiting for owner confirmation in Slack'}))
            p = self.create_proposal(c, 'change_format', [it['item_id']], {'item_id': it['item_id'], 'format': new},
                                     f"Change {it['name']} ({it['item_id']}) from {it['format']} to {new}. "
                                     'Its schedule and media approval will be reset and it will be rechecked.',
                                     cmd.actor, cmd.auth.get('thread'))
            return {'_state': 'awaiting_approval', **p}
        if kind == 'resume':
            if it['owner_state'] == 'active':
                raise Rejected('Item is not paused or skipped', 'invalid')
            p = self.create_proposal(c, 'resume', [it['item_id']], {'item_id': it['item_id']},
                                     f"Resume {it['name']} ({it['item_id']}), currently {it['owner_state']}.",
                                     cmd.actor, cmd.auth.get('thread'))
            self.project(c, it['item_id'], force_keys=('status',))
            return {'_state': 'awaiting_approval', **p}
        raise Rejected('Unsupported proposal kind', 'unsupported')

    def execute_resume(self, c, payload, cmd, pid):
        cmd2 = Command(cmd.id, 'resume', cmd.actor, cmd.actor_kind, payload['item_id'], {}, auth={'proposal': pid})
        return self.op_resume(c, cmd2)

    def op_replace_source(self, c, cmd: Command):
        url = dropbox_url(cmd.args.get('url'))
        it = self.item(c, cmd.item_id)
        self._guard_mutable(c, it, allow_paused=True)
        self.release(c, it, 'source replaced')
        c.execute('UPDATE ops_items SET content_rev=content_rev+1 WHERE item_id=?', (it['item_id'],))
        self.update_item(c, it['item_id'], cmd.actor, 'source replaced', source_override_url=url, file_url=url,
                         asset_key=None, file_id=None, file_rev=None, content_hash=None, verification_id=None,
                         readiness='checking', block_kind=None, block_reason=None, block_key=None,
                         waiting_since=self.now())
        self.request_check(c, it['item_id'], cmd.actor, 'source_replaced')
        return {'accepted': True, 'message': 'New source recorded; the actual file will be checked before scheduling.'}

    def op_set_folder(self, c, cmd: Command):
        url = dropbox_url(cmd.args.get('url'))
        it = self.item(c, cmd.item_id)
        self.update_item(c, it['item_id'], cmd.actor, 'folder set', folder_url=url)
        self.request_check(c, it['item_id'], cmd.actor, 'folder_changed')
        return {'accepted': True}

    def op_update_caption(self, c, cmd: Command):
        text = (cmd.args.get('text') or '').strip()
        it = self.item(c, cmd.item_id)
        if it['format'] != 'Post':
            self.update_item(c, it['item_id'], cmd.actor, 'story caption stored', caption=text or None)
            return {'stored': True, 'note': 'Stories publish without a caption'}
        if text == (it['caption'] or '') and it['caption_state'] == 'approved':
            return {'unchanged': True}
        self._guard_mutable(c, it, allow_paused=True)
        bad = rules.validate_caption(text)
        if bad:
            raise Rejected(bad, 'invalid_caption')
        c.execute('UPDATE ops_items SET content_rev=content_rev+1 WHERE item_id=?', (it['item_id'],))
        c.execute("UPDATE ops_caption_drafts SET state='superseded', updated=? WHERE item_id=? AND state='pending_approval'",
                  (self.now(), it['item_id']))
        it = self.update_item(c, it['item_id'], cmd.actor, 'caption updated', caption=text, caption_state='approved',
                              caption_origin='human')
        if cmd.actor_kind != MONDAY:
            self.write_human(c, it, {'caption': text})
        out = self.reauthorize(c, it['item_id'], cmd.actor, 'caption changed')
        return {'caption_updated': True, **out}

    def op_confirm_topaz(self, c, cmd: Command):
        it = self.item(c, cmd.item_id)
        confirmed = cmd.args.get('confirmed') is True
        asset = cmd.args.get('asset_key') if cmd.actor_kind == MONDAY else (cmd.args.get('asset_key') or it['asset_key'])
        if confirmed:
            if cmd.actor_kind == OWNER and not cmd.auth.get('explicit'):
                raise Rejected('Topaz confirmation must come from an explicit owner statement', 'needs_owner')
            if not it['asset_key']:
                raise Rejected('No source file is selected yet; Topaz confirmation cannot be bound', 'no_asset')
            if asset != it['asset_key']:
                raise Rejected('Topaz confirmation refers to a different file version than the selected one', 'stale')
            self.update_item(c, it['item_id'], cmd.actor, 'topaz confirmed', topaz_asset=it['asset_key'])
            obs = loads(self.item(c, it['item_id'])['observed'], {}) or {}
            obs['_nudge'] = self.now()             # a person acted: prepare it on the next cycle
            c.execute('UPDATE ops_items SET observed=? WHERE item_id=?', (dumps(obs), it['item_id']))
            if cmd.actor_kind != MONDAY:
                self.write_human(c, self.item(c, it['item_id']), {'topaz': 'Topazed'})
        else:
            self.update_item(c, it['item_id'], cmd.actor, 'topaz cleared', topaz_asset=None)
        return {'topaz_asset': it['asset_key'] if confirmed else None, **self.evaluate(c, it['item_id'], cmd.actor)}

    def op_set_notes(self, c, cmd: Command):
        self.update_item(c, cmd.item_id, cmd.actor, 'notes', notes=cmd.args.get('value') or None)
        return {'stored': True, 'reprocessing': False}

    def op_set_variety(self, c, cmd: Command):
        self.update_item(c, cmd.item_id, cmd.actor, 'variety', variety=cmd.args.get('value') or None)
        return {'stored': True, 'reprocessing': False}

    def op_set_code(self, c, cmd: Command):
        value = (cmd.args.get('value') or '').strip()
        try:
            rules.style(value)
        except rules.RuleError as e:
            raise Rejected(str(e), 'invalid_code')
        self.update_item(c, cmd.item_id, cmd.actor, 'code', code=value)
        return {'stored': True}

    def op_set_collab(self, c, cmd: Command):
        value = (cmd.args.get('value') or '').strip() or None
        it = self.item(c, cmd.item_id)
        self.update_item(c, it['item_id'], cmd.actor, 'collab', collab=value)
        return {'stored': True, **self.evaluate(c, it['item_id'], cmd.actor)}

    def op_hold(self, c, cmd: Command):
        if cmd.actor_kind == MONDAY and cmd.args.get('kind') != 'external_posted':
            raise Rejected('Board edits can only hold an item as posted outside the system', 'forbidden')
        it = self.item(c, cmd.item_id)
        if it['publication'] == 'published':
            return {'held': False, 'note': 'already published'}
        if (loads(it['hold'], {}) or {}).get('kind') == cmd.args.get('kind') and not self.reservation(c, it['item_id']):
            return {'held': True, 'unchanged': True}       # same hold already in place: no new notice
        self.release(c, it, 'hold: ' + cmd.args.get('kind', ''))
        self.update_item(c, it['item_id'], cmd.actor, 'hold', hold=dumps({'kind': cmd.args.get('kind'),
                                                                          'reason': cmd.args.get('reason')}))
        self.notify(c, f"hold:{it['item_id']}:{cmd.args.get('kind')}:{it['version']}",
                    f"Held {it['name']} ({it['item_id']}): {cmd.args.get('reason')}", it['item_id'])
        return {'held': True}

    def op_source_canceled(self, c, cmd: Command):
        it = self.item(c, cmd.item_id)
        if it['publication'] in PROTECTED_PUBLICATION or it['owner_state'] != 'active':
            return {'changed': False, 'note': 'protected state kept'}
        att = self.active_attempt(c, it['item_id'])
        if att:
            return {'changed': False, 'note': 'publication attempt active'}
        self.release(c, it, 'source canceled', keep_request=False)
        self.update_item(c, it['item_id'], cmd.actor, 'source canceled', owner_state='skipped',
                         owner_state_reason='Canceled in Customer Projects')
        return {'changed': True}

    def request_check(self, c, item_id, actor, why):
        c.execute('INSERT INTO ops_checks(item_id,kind,key,requested,requested_by,state,updated) '
                  "VALUES(?,?,?,?,?,'requested',?) ON CONFLICT(item_id,kind) DO UPDATE SET key=excluded.key, "
                  "requested=excluded.requested, requested_by=excluded.requested_by, state='requested', updated=excluded.updated",
                  (str(item_id), 'media', why, self.now(), actor, self.now()))

    def op_request_recheck(self, c, cmd: Command):
        it = self.item(c, cmd.item_id)
        chk = c.execute("SELECT * FROM ops_checks WHERE item_id=? AND kind='media'", (it['item_id'],)).fetchone()
        if chk and chk['state'] == 'running':
            return {'_state': 'accepted', 'already_running': True,
                    'message': 'The same check is already running; its result will be reported when it finishes.'}
        self.request_check(c, it['item_id'], cmd.actor, 'explicit_recheck')
        return {'_state': 'accepted', 'message': 'Recheck accepted (not yet performed). The next preparation cycle '
                'will re-read the Dropbox file and re-measure the prepared video; the result will be reported.'}

    # ------------------------------------------------------------------ readiness
    def media_row(self, c, media_id):
        if not media_id:
            return None
        r = c.execute('SELECT * FROM media WHERE id=?', (media_id,)).fetchone()
        return dict(r) if r else None

    def verification_problem(self, c, it) -> str | None:
        m = self.media_row(c, it.get('verification_id'))
        if not m:
            return 'No verified prepared file for the current version'
        info = loads(m['metadata'], {}) or {}
        if m['item'] != it['item_id'] or info.get('assetKey') != it['asset_key']:
            return 'Verified file belongs to a different source version'
        if info.get('format') != it['format']:
            return 'Verified file was checked for a different format'
        if info.get('qaPolicy') != rules.QA_POLICY:
            return 'Verification predates the current quality rules'
        bad = rules.media_failure(info, it['format'])
        if bad:
            return bad
        if not info.get('url'):
            return 'Verified file has no delivery link yet'
        if not Path(m['path']).exists():
            return 'Prepared file is missing from storage'
        return None

    def evaluate(self, c, item_id, actor) -> dict:
        """Recompute readiness from durable facts; release ineligible reservations."""
        it = self.item(c, item_id)
        if it['publication'] in PROTECTED_PUBLICATION:
            return {'readiness': it['readiness']}
        fields = {}
        if self.safe_style(it['code']) is None:
            fields = dict(readiness='blocked', block_kind='config', block_key='invalid_code',
                          block_reason='Code must start with 2-3 letters followed by a number')
        elif it['collab']:
            fields = dict(readiness='blocked', block_kind='review', block_key='collab',
                          block_reason='IG Collab is requested but the publisher cannot add collaborators. '
                                       'It stays blocked until the request is removed or it is published manually.')
        elif it['verification_id']:
            problem = self.verification_problem(c, it)
            if problem:
                fields = dict(readiness='checking', verification_id=None, block_kind=None, block_reason=None,
                              block_key=None)
            elif it['topaz_asset'] != it['asset_key']:
                fields = dict(readiness='blocked', block_kind='editor', block_key='topaz:' + str(it['asset_key']),
                              block_reason='Topaz confirmation is required for the selected file version')
            else:
                fields = dict(readiness='ready', block_kind=None, block_reason=None, block_key=None)
        elif it['readiness'] == 'blocked' and it['block_key'] in ('invalid_code', 'collab'):
            fields = dict(readiness='checking', block_kind=None, block_reason=None, block_key=None)
        elif it['readiness'] == 'blocked' and (it['block_key'] or '').startswith('topaz:') and \
                it['topaz_asset'] == it['asset_key'] and it['asset_key']:
            fields = dict(readiness='checking', block_kind=None, block_reason=None, block_key=None)
        changed = {k: v for k, v in fields.items() if it.get(k) != v}
        if changed:
            it = self.update_item(c, item_id, actor, 'evaluate', **changed)
        if it['readiness'] != 'ready':
            if self.reservation(c, item_id):
                self.release(c, it, 'not ready: ' + (it['block_reason'] or it['readiness']))
                self.project(c, item_id)
            return {'readiness': it['readiness'], 'reason': it['block_reason']}
        return {'readiness': 'ready', **self.try_schedule(c, item_id, actor)}

    # ------------------------------------------------------------------ preparation (WF1)
    def work_queue(self, limit=20) -> list[dict]:
        """Items WF1 should look at this cycle, deterministic and fair."""
        now = self.now()
        out = []
        with self.store.read() as c:
            rows = c.execute("SELECT * FROM ops_items WHERE publication IN ('not_started','failed') AND "
                             "owner_state='active' AND format IN ('Post','Story')").fetchall()
            checks = {r['item_id']: dict(r) for r in c.execute("SELECT * FROM ops_checks WHERE kind='media'")}
            for r in rows:
                it = dict(r)
                if loads(it['hold'], None) and loads(it['hold'], {}).get('kind') in ('format_change', 'format_missing'):
                    continue
                if self.active_attempt(c, it['item_id']):
                    continue
                chk = checks.get(it['item_id'])
                requested = chk and chk['state'] == 'requested'
                last = (loads(it['observed'], {}) or {}).get('_last_prep') or 0
                requested = requested or ((loads(it['observed'], {}) or {}).get('_nudge') or 0) > last
                if requested:
                    due = 0
                elif it['readiness'] == 'ready':
                    due = last + RELIST_READY_SECONDS
                elif it['infra_issue']:
                    due = last + INFRA_RECHECK_SECONDS     # temporary problem: retry, but don't crowd the queue
                elif it['readiness'] == 'blocked':
                    n = (loads(it['observed'], {}) or {}).get('_blocked_cycles', 0)
                    due = last + BACKOFF_STEPS[min(n, len(BACKOFF_STEPS) - 1)]
                else:
                    due = last
                if due > now:
                    continue
                try:
                    rot = rules.rotation_key(it['format'], it['code'], it['name'], it['variety'])
                except rules.RuleError:
                    rot = 'INVALID'
                out.append({'item_id': it['item_id'], 'rotation': rot, 'priority': 0 if requested else 1, '_last': last,
                            '_pending': it['readiness'] == 'checking' or bool(it['infra_issue']),
                            'waiting_since': it['waiting_since'] or it['created'], 'format': it['format'],
                            'code': it['code'], 'name': it['name'], 'source_item_id': it['source_item_id'],
                            'folder_url': it['folder_url'], 'file_url': it['source_override_url'] or it['file_url'],
                            # A brief is fetched (Monday request) only when a draft could follow: never while one
                            # waits, and after a rejected/failed draft at most daily (a changed brief) (audit perf).
                            'needs_caption': it['format'] == 'Post' and it['caption_state'] in ('missing',) and
                                             not c.execute("SELECT 1 FROM ops_caption_drafts WHERE item_id=? AND "
                                                           "(state='pending_approval' OR updated>?)",
                                                           (it['item_id'], now - 86400)).fetchone(),
                            'version': it['version']})
        urgent = [x for x in out if x['priority'] == 0]
        rest = [x for x in out if x['priority'] == 1]
        from .rules import fair_order
        # Coverage: finish work in progress (items waiting for a check result), then never-checked items
        # (rotation-fair), then the least recently checked. Ordering only by publication rotation re-picked
        # the same items every cycle while others were never checked.
        pending = sorted((x for x in rest if x['_last'] and x['_pending']), key=lambda x: (x['_last'], str(x['item_id'])))
        fresh = fair_order([x for x in rest if not x['_last']])
        seen = sorted((x for x in rest if x['_last'] and not x['_pending']), key=lambda x: (x['_last'], str(x['item_id'])))
        # At most half the run goes to items still waiting for a result, so new items always progress
        # (audit MP2: 20 waiting items starved every never-checked item).
        half = max(1, limit // 2)
        picked = (fair_order(urgent) + pending[:half] + fresh + seen + pending[half:])[:limit]
        for x in picked:
            x.pop('_last', None)
            x.pop('_pending', None)
        return picked

    def _touch_prep(self, c, it, blocked: bool):
        obs = loads(it['observed'], {}) or {}
        obs['_last_prep'] = self.now()
        obs['_blocked_cycles'] = (obs.get('_blocked_cycles', 0) + 1) if blocked else 0
        c.execute('UPDATE ops_items SET observed=? WHERE item_id=?', (dumps(obs), it['item_id']))

    def op_prep_source(self, c, cmd: Command):
        """WF1 resolved (or failed to resolve) the exact selected Dropbox file."""
        a = cmd.args
        self.run_check(c, 'wf1', a.get('run_id'), a.get('fence'))
        it = self.item(c, cmd.item_id)
        if it['publication'] in PROTECTED_PUBLICATION or it['owner_state'] != 'active':
            return {'next': 'none', 'note': 'protected'}
        if a.get('expected_format') and a['expected_format'] != it['format']:
            raise Rejected('Format changed during this run; result discarded', 'stale')
        if a.get('folder_url') and not it['folder_url']:
            it = self.update_item(c, it['item_id'], cmd.actor, 'folder created', folder_url=a['folder_url'])
        err = a.get('error')
        if err:
            kind = a.get('error_kind', 'infra')
            self._touch_prep(c, it, True)
            if kind != 'infra' and it['readiness'] == 'blocked' and it['block_key'] == kind + ':' + _h(err) \
                    and it['block_reason'] == str(err)[:500]:
                return {'next': 'none', 'blocked': kind, 'unchanged': True}     # same problem as last time
            if kind == 'infra':
                # Temporary provider/storage problem: keep evidence and reservations.
                self.update_item(c, it['item_id'], cmd.actor, 'infra issue', infra_issue=str(err)[:300])
                return {'next': 'none', 'infra': True}
            key = kind + ':' + _h(err)
            self.update_item(c, it['item_id'], cmd.actor, 'source problem', readiness='blocked',
                             block_kind='editor' if kind == 'editor' else 'config', block_key=key,
                             block_reason=str(err)[:500], infra_issue=None)
            self.release(c, self.item(c, it['item_id']), 'source problem')
            self.project(c, it['item_id'])
            if kind == 'editor':
                self.editor_task(c, it['item_id'], key, str(err))
            return {'next': 'none', 'blocked': kind}
        f = a['file']
        for k in ('id', 'rev', 'content_hash'):
            if not f.get(k):
                raise Rejected('Dropbox version metadata is incomplete', 'invalid')
        asset = f['id'] + '@' + f['rev']
        self._touch_prep(c, it, False)
        changed = asset != it['asset_key'] or f['content_hash'] != it['content_hash']
        if changed:
            att = self.active_attempt(c, it['item_id'])
            if att and att['stage'] == 'committed':
                raise Rejected('Source changed while a publication may be in progress', 'in_progress')
            self.release(c, it, 'source file changed', keep_request=True)
            obs = loads(self.item(c, it['item_id'])['observed'], {}) or {}
            if it['asset_key']:
                obs['_file_changed_at'] = self.now()   # Slack Topaz confirmations near a change need approval
            if obs.get('topaz') == 'Topazed' and it['topaz_asset'] != asset:
                # The confirmation belonged to the previous file: show that on the board, so the editor's next
                # "Topazed" for this version is a visible change (audit MS9).
                self.write_human(c, self.item(c, it['item_id']), {'topaz': 'Not yet'})
                obs = {**(loads(self.item(c, it['item_id'])['observed'], {}) or {}),
                       **({'_file_changed_at': self.now()} if it['asset_key'] else {})}
            c.execute('UPDATE ops_items SET content_rev=content_rev+1, observed=? WHERE item_id=?',
                      (dumps(obs), it['item_id']))
            it = self.update_item(c, it['item_id'], cmd.actor, 'source file changed', asset_key=asset, file_id=f['id'],
                                  file_rev=f['rev'], content_hash=f['content_hash'], file_name=f.get('name'),
                                  file_url=a.get('url'), verification_id=None, readiness='checking',
                                  block_kind=None, block_reason=None, block_key=None, infra_issue=None)
        elif a.get('url') and a['url'] != it['file_url']:
            it = self.update_item(c, it['item_id'], cmd.actor, 'share link', file_url=a['url'], infra_issue=None)
        elif it['infra_issue']:
            it = self.update_item(c, it['item_id'], cmd.actor, 'infra recovered', infra_issue=None)
        chk = c.execute("SELECT * FROM ops_checks WHERE item_id=? AND kind='media'", (it['item_id'],)).fetchone()
        # A recheck stays in progress until its verdict is consumed: the detached job reports on a later
        # cycle, and a ready item must not return 'unchanged' meanwhile (audit MP1).
        recheck = bool(chk and chk['state'] in ('requested', 'running'))
        if chk and chk['state'] == 'requested':
            c.execute("UPDATE ops_checks SET state='running', updated=? WHERE item_id=? AND kind='media'",
                      (self.now(), it['item_id']))
        if it['readiness'] == 'blocked' and (it['block_key'] or '').startswith(('config:', 'editor:')):
            # The block came from this step (folder/file/share problem) and the file now resolves:
            # the problem is gone. Other blocks (Topaz, duration, media, code, collab) keep their own rules.
            it = self.update_item(c, it['item_id'], cmd.actor, 'source problem resolved', readiness='checking',
                                  block_kind=None, block_reason=None, block_key=None)
        if it['readiness'] == 'blocked' and it['block_key'] == 'story_duration:' + asset and \
                it['format'] == 'Story' and self._duration_ok(c, it['item_id'], asset, f['content_hash']):
            # Blocked under an older duration policy; the same measurement now passes (automatic trim).
            it = self.update_item(c, it['item_id'], cmd.actor, 'story duration accepted (automatic trim)',
                                  readiness='checking', block_kind=None, block_reason=None, block_key=None)
        if it['readiness'] == 'ready' and not changed and not recheck:
            return {'next': 'none', 'unchanged': True}
        if it['readiness'] == 'blocked' and not changed and not recheck and it['block_kind'] != 'infra':
            return {'next': 'none', 'unchanged': True, 'blocked': it['block_reason']}
        body = {'itemId': it['item_id'], 'format': it['format'], 'sourceUrl': a.get('url') or it['file_url'],
                'fileId': f['id'], 'revision': f['rev'], 'contentHash': f['content_hash'], 'assetKey': asset,
                'topazed': it['topaz_asset'] == asset, 'recheck': recheck}
        if it['format'] == 'Story' and not self._duration_ok(c, it['item_id'], asset, f['content_hash']):
            return {'next': 'preflight', 'media': body}
        if it['topaz_asset'] != asset:
            trim = self._story_trim(c, it)
            note = (f' The Story is {trim[0]:g} seconds and will be trimmed automatically to {trim[1]:g} seconds;'
                    ' no shorter edit is needed.') if trim else ''
            self.update_item(c, it['item_id'], cmd.actor, 'awaiting topaz', readiness='blocked', block_kind='editor',
                             block_key='topaz:' + asset, block_reason='Topaz confirmation is required for the '
                             'selected file version (the filename is not proof)')
            self.editor_task(c, it['item_id'], 'topaz:' + asset,
                             'Apply Topaz, export at least 1080p short edge and under 300 MB, then confirm '
                             'Topazed on the social board for the selected file version.' + note)
            self._finish_check(c, it['item_id'], 'blocked: Topaz confirmation missing')
            return {'next': 'none', 'blocked': 'topaz'}
        return {'next': 'prepare', 'media': body}

    def _duration_ok(self, c, item_id, asset, content_hash) -> bool:
        r = c.execute("SELECT * FROM ops_checks WHERE item_id=? AND kind='duration'", (str(item_id),)).fetchone()
        res = loads(r['result'], {}) if r else {}
        # Re-evaluated against the current policy (a stored verdict from an older policy is not final).
        return bool(r and r['key'] == asset + '|' + content_hash and
                    rules.story_source_failure(res.get('duration')) is None)

    def _story_trim(self, c, it):
        """(source seconds, target seconds) when the current Story file gets the automatic end trim."""
        if it['format'] != 'Story':
            return None
        r = c.execute("SELECT * FROM ops_checks WHERE item_id=? AND kind='duration'", (str(it['item_id']),)).fetchone()
        if not r or r['key'] != (it['asset_key'] or '') + '|' + (it['content_hash'] or ''):
            return None
        d = (loads(r['result'], {}) or {}).get('duration')
        t = rules.story_trim_target(d)
        return (float(d), t) if t else None

    def op_prep_preflight(self, c, cmd: Command):
        a = cmd.args
        self.run_check(c, 'wf1', a.get('run_id'), a.get('fence'))
        it = self.item(c, cmd.item_id)
        r = a['result']
        if r.get('pending'):
            return {'next': 'none', 'pending': True}
        if r.get('assetKey') != it['asset_key'] or r.get('contentHash') != it['content_hash']:
            raise Rejected('Duration result is for an older file version; discarded', 'stale')
        if r.get('retryable'):
            self.update_item(c, it['item_id'], cmd.actor, 'preflight infra', infra_issue=r.get('reason', '')[:300])
            return {'next': 'none', 'infra': True}
        bad = rules.story_source_failure(r.get('duration')) if it['format'] == 'Story' else None
        key = it['asset_key'] + '|' + it['content_hash']
        c.execute("INSERT OR REPLACE INTO ops_checks(item_id,kind,key,requested,requested_by,state,result,updated) "
                  "VALUES(?,?,?,?,?,?,?,?)", (it['item_id'], 'duration', key, self.now(), cmd.actor, 'done',
                                              dumps({'ok': bad is None, 'duration': r.get('duration')}), self.now()))
        if bad:
            bkey = 'story_duration:' + it['asset_key']
            self.update_item(c, it['item_id'], cmd.actor, 'story too long', readiness='blocked', block_kind='content',
                             block_key=bkey, block_reason=bad, verification_id=None, infra_issue=None)
            self.release(c, self.item(c, it['item_id']), 'story too long', keep_request=False)
            self.project(c, it['item_id'])
            self.editor_task(c, it['item_id'], bkey, bad)
            self._finish_check(c, it['item_id'], 'blocked: ' + bad)
            return {'next': 'none', 'blocked': 'story_duration', 'duration': r.get('duration')}
        return {'next': 'continue', 'duration': r.get('duration')}

    def op_prep_media(self, c, cmd: Command):
        a = cmd.args
        self.run_check(c, 'wf1', a.get('run_id'), a.get('fence'))
        it = self.item(c, cmd.item_id)
        r = a['result']
        if r.get('pending'):
            return {'next': 'none', 'pending': True}
        if r.get('assetKey', it['asset_key']) != it['asset_key'] or r.get('format', it['format']) != it['format']:
            raise Rejected('Media result is for an older file/format; discarded', 'stale')
        if r.get('retryable'):
            self.update_item(c, it['item_id'], cmd.actor, 'media infra', infra_issue=r.get('reason', '')[:300])
            return {'next': 'none', 'infra': True}
        if not r.get('ready'):
            reason = r.get('reason') or 'Media preparation failed'
            key = 'media:' + it['asset_key'] + ':' + _h(reason)
            self.update_item(c, it['item_id'], cmd.actor, 'media failed', readiness='blocked', block_kind='editor',
                             block_key=key, block_reason=reason, verification_id=None, infra_issue=None)
            self.release(c, self.item(c, it['item_id']), 'media failed')
            self.project(c, it['item_id'])
            self.editor_task(c, it['item_id'], key, reason)
            self._finish_check(c, it['item_id'], 'blocked: ' + reason)
            return {'next': 'none', 'blocked': reason}
        m = self.media_row(c, r['mediaId'])
        if not m or m['item'] != it['item_id']:
            raise Rejected('Prepared media record not found', 'invalid')
        info = loads(m['metadata'], {})
        if info.get('url'):
            return self._verified(c, it, r['mediaId'], cmd.actor)
        return {'next': 'upload', 'mediaId': r['mediaId'], 'filePath': m['path']}

    def op_prep_delivered(self, c, cmd: Command):
        a = cmd.args
        self.run_check(c, 'wf1', a.get('run_id'), a.get('fence'))
        it = self.item(c, cmd.item_id)
        url = dropbox_url(a['url'])
        m = self.media_row(c, a['mediaId'])
        if not m or m['item'] != it['item_id']:
            raise Rejected('Prepared media record not found', 'invalid')
        info = loads(m['metadata'], {})
        if info.get('assetKey') != it['asset_key'] or info.get('format') != it['format']:
            raise Rejected('Delivered file is for an older version; discarded', 'stale')
        info['url'] = url
        c.execute('UPDATE media SET metadata=? WHERE id=?', (dumps(info), a['mediaId']))
        return self._verified(c, it, a['mediaId'], cmd.actor)

    def _verified(self, c, it, media_id, actor):
        if (it['verification_id'] or None) != media_id:
            c.execute('UPDATE ops_items SET content_rev=content_rev+1 WHERE item_id=?', (it['item_id'],))
        self.update_item(c, it['item_id'], actor, 'verified', verification_id=media_id, infra_issue=None)
        self._finish_check(c, it['item_id'], 'passed')
        out = self.evaluate(c, it['item_id'], actor)
        return {'next': 'done', **out}

    def _finish_check(self, c, item_id, outcome):
        chk = c.execute("SELECT * FROM ops_checks WHERE item_id=? AND kind='media'", (str(item_id),)).fetchone()
        if chk and chk['state'] in ('running', 'requested'):
            c.execute("UPDATE ops_checks SET state='done', result=?, updated=? WHERE item_id=? AND kind='media'",
                      (dumps({'outcome': outcome, 'at': self.now()}), self.now(), str(item_id)))
            if chk['requested_by'] and chk['key'] == 'explicit_recheck':
                it = self.item(c, item_id)
                self.notify(c, f'recheck:{item_id}:{int(self.now())}',
                            f"Recheck finished for {it['name']} ({item_id}): {outcome}.", str(item_id))

    def editor_task(self, c, item_id, issue_key, reason):
        """Editor subitem work, deduplicated by durable (item, issue) identity."""
        it = self.item(c, item_id)
        if not it['source_item_id']:
            # No projects-board item, so no editor subitem is possible; the board's Action required shows it.
            return False
        body = (f"Social delivery {it['code']} / {it['format']} — item {item_id}\nIssue: {reason}\n"
                f"Selected file: {it.get('file_name') or 'none'} ({it.get('asset_key') or 'no version'})\n"
                'Requirements: Topaz processed, short edge ≥1080 px, file under 300 MB'
                + ('; Story strictly under 60 seconds' if it['format'] == 'Story' else '') +
                f"\nUpload to: {it.get('folder_url') or 'the project folder'}\n"
                'After upload, confirm Topazed on the social board only when Source asset version matches the file.')
        bh = _h(body)
        r = c.execute('SELECT * FROM ops_editor_tasks WHERE item_id=? AND issue_key=?', (str(item_id), issue_key)).fetchone()
        if r and r['body_hash'] == bh:
            return False
        existing = c.execute('SELECT task_id FROM ops_editor_tasks WHERE item_id=? AND task_id IS NOT NULL '
                             'ORDER BY updated DESC LIMIT 1', (str(item_id),)).fetchone()
        c.execute('INSERT OR REPLACE INTO ops_editor_tasks(item_id,issue_key,task_id,body_hash,state,updated) '
                  "VALUES(?,?,?,?, 'pending', ?)", (str(item_id), issue_key, existing['task_id'] if existing else None,
                                                    bh, self.now()))
        self.enqueue(c, 'editor', f'editor:{item_id}:{issue_key}:{bh}', {
            'item_id': str(item_id), 'issue_key': issue_key, 'source_item_id': it['source_item_id'],
            'task_id': existing['task_id'] if existing else None,
            'task_name': f'Social {item_id} — تجهيز أو استبدال الفيديو', 'body': body}, str(item_id))
        return True
