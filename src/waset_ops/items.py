"""Items: import identity, Monday edit observation, owner commands, preparation results."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from . import board, media, record, rules
from .core import BONDOK, MONDAY, OWNER, PRE_COMMIT, STALE_SNAPSHOT_SECONDS, Command, Rejected, fingerprint
from .db import audit, dumps, loads

PROTECTED_PUBLICATION = ('published', 'outcome_unknown', 'in_progress')
RELIST_READY_SECONDS = 30 * 60          # metadata-only revision check for ready items
INFRA_RECHECK_SECONDS = 20 * 60         # first retry after a temporary problem; doubles per consecutive failure
INFRA_MAX_BACKOFF = 6 * 3600            # ... up to this interval (R5 M10)
INFRA_ESCALATE_AFTER = 6                # consecutive temporary failures before one escalation notice
MAX_DELIVERY_ATTEMPTS = 3               # failed deliveries (145 MB uploads) per prepared file before escalation (R5 M9)
MAX_IDENTIFY_ATTEMPTS = 2               # legacy deliveries: tries to bind the existing Dropbox copy (R5 M17)
RECHECK_POLL_SECONDS = 600              # a running recheck expects its result on the next cycle
PREPARED_FOLDER = '/Social Media/Prepared'
FRESH_READ_SECONDS = 120              # non-WF1 callers read the board right before observing
TIME_KEYS = ('publish_at', 'post_date', 'post_time')
# Holds that only wait for the owner to say "continue" (or give a time): resume clears them (contract §3/§7).
RESUMABLE_HOLDS = ('resume', 'rejected_edit', 'publish_retry_limit', 'unscheduled', 'missed_time',
                   'incomplete_time', 'rework')
BACKOFF_STEPS = (600, 1200, 2400, 3600) # waiting/blocked items: fair, bounded backoff
OWNER_SELECTED, AUTOMATIC = 'owner_selected_file', 'automatic_folder_selection'


def _h(x) -> str:
    return hashlib.sha256(str(x).encode()).hexdigest()[:12]


def dropbox_url(url: str) -> str:
    try:
        u = urlsplit(url or '')
        host, port = (u.hostname or '').lower(), u.port
    except ValueError:                         # e.g. a bad port or bracket: an item-local error (R5 A3)
        raise Rejected('The Dropbox link is not a valid address', 'invalid_url') from None
    if u.scheme != 'https' or u.username or u.password or port not in (None, 443):
        raise Rejected('A secure https Dropbox link is required', 'invalid_url')
    if not any(host == d or host.endswith('.' + d) for d in ('dropbox.com', 'dropboxusercontent.com')):
        raise Rejected('A Dropbox link is required', 'invalid_url')
    return url


class ItemsMixin:
    # ------------------------------------------------------------------ import
    IMPORT_RETRY_SECONDS = 3600        # Monday refused a create: retry hourly (owner told once)
    IMPORT_UNCERTAIN_SECONDS = 1800    # no answer to a create: wait for the board to show it before re-creating
    IMPORT_FINDINGS = ('ambiguous_source_code', 'invalid_code', 'ambiguous_social_items', 'mapped_social_item_missing')

    @staticmethod
    def _owner_value(s) -> dict | None:
        """Monday people column value; teams stay teams (R5 A11: teams were sent as people and refused)."""
        owners = s.get('owners')
        if owners is None:                                        # older callers sent bare ids (people)
            owners = [{'id': x, 'kind': 'person'} for x in s.get('owner_ids') or []]
        out = []
        for o in owners:
            try:
                out.append({'id': int(o['id']), 'kind': 'team' if o.get('kind') == 'team' else 'person'})
            except (KeyError, TypeError, ValueError):
                continue                                          # an unreadable owner is left out, not guessed
        return {'personsAndTeams': out} if out else None

    def import_plan(self, sources: list[dict], social: list[dict]) -> dict:
        """Decide which eligible source projects need a social item.

        Durable source->social identity prevents duplicates; a mapped item that
        disappeared from the board is reported, never recreated blindly.
        sources: [{id,name,code,format,link,owners:[{id,kind}]}] social: [{id,source_item,code,format}]
        Every reason a project is not imported is kept as an open finding (notified once, resolved when the
        cause is gone, R5 M15). A create Monday refused or never answered is not repeated blindly (R5 A11).
        """
        norm = lambda x: re.sub(r'\s+', '', str(x or '')).upper()
        counts, names = {}, {}
        for s in sources:
            if norm(s.get('code')):
                counts[norm(s['code'])] = counts.get(norm(s['code']), 0) + 1
                names.setdefault(norm(s['code']), []).append(f"{s.get('name') or s['id']} ({s['id']})")
        creates, findings, record = [], [], []
        social_ids = {str(i['id']) for i in social}
        now = self.now()
        with self.store.read() as c:
            mapped = {r['source_item_id']: dict(r) for r in c.execute('SELECT * FROM ops_source_map')}
            known = {r['item_id'] for r in c.execute('SELECT item_id FROM ops_items')}
        for s in sources:
            sid, code, fmt = str(s['id']), norm(s.get('code')), s.get('format')
            label = f"{s.get('name') or code or sid} ({code or 'no code'}, project {sid})"
            if not code or fmt not in ('Post', 'Story'):
                continue
            if counts.get(code) != 1 or s.get('duplicate'):
                findings.append({'kind': 'ambiguous_source_code', 'source': sid, 'code': code, 'fp': code,
                                 'notify': True,
                                 'detail': f'Code {code} is used by more than one project on Customer Projects '
                                           f"({', '.join(names.get(code, [label]))}); no social item is created for "
                                           'it until the code is unique.'})
                continue
            m = mapped.get(sid)
            same = [i for i in social if str(i.get('source_item') or '') == sid or
                    (not i.get('source_item') and norm(i.get('code')) == code)]
            if m and m['state'] == 'active':
                if m['social_item_id'] not in social_ids:
                    findings.append({'kind': 'mapped_social_item_missing', 'source': sid, 'fp': sid,
                                     'social': m['social_item_id'], 'notify': m['social_item_id'] not in known,
                                     'detail': f"The social item {m['social_item_id']} of {label} is no longer on the "
                                               'social board; it is not recreated automatically.'})
                continue
            if len(same) == 1:
                record.append({'source': sid, 'social': str(same[0]['id'])})
                continue
            if len(same) > 1:
                findings.append({'kind': 'ambiguous_social_items', 'source': sid, 'fp': sid, 'notify': True,
                                 'social': [str(i['id']) for i in same],
                                 'detail': f"{label} matches {len(same)} social items "
                                           f"({', '.join(str(i['id']) for i in same)}); none is linked until only one "
                                           'remains.'})
                continue
            if m and m['state'] == 'create_failed' and now - (m['created'] or 0) < self.IMPORT_RETRY_SECONDS:
                continue                       # Monday refused it recently; the owner was told; retried hourly
            if m and m['state'] == 'create_uncertain' and now - (m['created'] or 0) < self.IMPORT_UNCERTAIN_SECONDS:
                continue                       # may exist already: wait until a complete snapshot can show it
            try:
                st = rules.style(code)
            except rules.RuleError as e:
                findings.append({'kind': 'invalid_code', 'source': sid, 'code': code, 'fp': sid, 'notify': True,
                                 'detail': f'{label}: the code is not valid ({e}); no social item is created.'})
                continue
            cv = {board.COL['source_item']: sid, board.COL['style']: st, board.COL['code']: code,
                  board.COL['format']: {'label': fmt}, 'status': {'label': board.LABELS['checking']},
                  board.COL['topaz']: {'label': 'Not yet'}}
            link = s.get('link')
            if link:
                cv[board.COL['dropbox']] = {'url': link, 'text': 'Source'}
                if '/scl/fo/' in link:
                    cv[board.COL['folder']] = {'url': link, 'text': 'Project folder'}
            owner = self._owner_value(s)
            if owner:
                cv[board.COL['owner']] = owner
            creates.append({'key': sid, 'name': s.get('name') or code, 'group': board.GROUPS[fmt], 'columns': cv})
        with self.store.tx() as c:
            raised = set()
            for f in findings:
                fp = f"import:{f['kind']}:{f.pop('fp')}"
                raised.add(fp)
                self.finding(c, fp, None, 'import_' + f['kind'], f['detail'], notify=f.pop('notify'))
            for r in c.execute("SELECT fingerprint FROM ops_findings WHERE resolved IS NULL AND fingerprint LIKE 'import:%'"):
                kind = r['fingerprint'].split(':')[1]
                if kind in self.IMPORT_FINDINGS and r['fingerprint'] not in raised:
                    self.resolve_finding(c, r['fingerprint'])          # the cause is gone (complete source list)
        if record:
            self.import_record(record)
        return {'creates': creates, 'findings': findings, 'recorded': len(record)}

    def import_record(self, pairs: list[dict], failed: list[dict] | None = None,
                      uncertain: list[dict] | None = None) -> dict:
        """Per-item outcome of the social-item creates (R5 A11).

        pairs: created (or found on the board) -> durable mapping. failed: Monday answered and did not create
        it (retried after IMPORT_RETRY_SECONDS; an item-specific refusal is told to the owner once). uncertain: no
        usable answer (timeout, 5xx) -> not re-created until a complete snapshot had the chance to show it.
        """
        n = 0
        now = self.now()
        with self.store.tx() as c:
            for p in pairs:
                sid, social = str(p['source']), str(p['social'])
                if c.execute('SELECT 1 FROM ops_source_map WHERE social_item_id=? AND source_item_id!=?',
                             (social, sid)).fetchone():
                    continue                                  # that social item belongs to another project
                row = c.execute('SELECT state FROM ops_source_map WHERE source_item_id=?', (sid,)).fetchone()
                if row is None:
                    c.execute("INSERT INTO ops_source_map VALUES(?,?, 'active', ?)", (sid, social, now))
                    n += 1
                elif row['state'] != 'active':
                    c.execute("UPDATE ops_source_map SET social_item_id=?, state='active', created=? "
                              'WHERE source_item_id=?', (social, now, sid))
                    n += 1
                for kind in ('create_failed', 'create_uncertain'):
                    self.resolve_finding(c, f'import:{kind}:{sid}')
            for kind, rows in (('create_failed', failed or []), ('create_uncertain', uncertain or [])):
                for f in rows:
                    sid = str(f['source'])
                    cur = c.execute("INSERT INTO ops_source_map VALUES(?, NULL, ?, ?) ON CONFLICT(source_item_id) DO "
                                    "UPDATE SET state=excluded.state, created=excluded.created "
                                    "WHERE ops_source_map.state!='active'", (sid, kind, now))
                    if not cur.rowcount:
                        continue                                  # already linked to a social item
                    what = f"{f.get('name') or 'project'} (project {sid})"
                    err = str(f.get('error') or 'no answer')[:300]
                    if kind == 'create_failed':
                        detail = (f'Monday refused to create the social item for {what}: {err}. Other projects '
                                  'continue; it is retried hourly.')
                        notify = f.get('scope') == 'item'
                    else:
                        detail = (f'Creating the social item for {what} got no usable answer ({err}); it is not '
                                  'created again until the board shows whether it exists.')
                        notify = False
                    self.finding(c, f'import:{kind}:{sid}', None, 'import_' + kind, detail, notify=notify)
        return {'recorded': n, 'failed': len(failed or []), 'uncertain': len(uncertain or [])}

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
            iid = str(raw.get('id'))
            seen.add(iid)
            try:
                self._observe_one(raw, iid, src, floor, results, created)
            except Exception as e:  # noqa: BLE001 - one malformed item never stops the board (R5 A3)
                results.append({'item_id': iid, 'edit': '_item', 'state': 'failed',
                                'reason': f'{type(e).__name__}: {str(e)[:200]}'})
                with self.store.tx() as c:
                    self.finding(c, f'observe_failed:{iid}:{type(e).__name__}', iid, 'observe_failed',
                                 f'Board item {iid} could not be read ({type(e).__name__}: {str(e)[:160]}); '
                                 'other items continue. It is retried every cycle.')
        missing = self.board_missing(seen) if complete else []
        return {'edits': results, 'imported': created, 'missing': missing,
                'work': self.work_queue(limit=limit) if actor == 'service:wf1' else None}

    def _observe_one(self, raw, iid, src, floor, results, created):
        snap = board.snapshot(raw)
        with self.store.tx() as c:
            it = self.item(c, iid, required=False)
            if it is None:
                restored = self._bootstrap(c, iid, snap)
                created.append(iid)
                if not restored:
                    return
                # Rebuilt from the board record: what the owner changed on the board since the record was written
                # (status, time, caption, Topaz ...) is applied as owner edits against what the record says was shown.
                it = self.item(c, iid)
            edits = self._diff(c, it, snap, floor)
            c.execute('UPDATE ops_items SET name=? WHERE item_id=?', (snap.get('name'), iid))
        for e in edits:
            results.append({'item_id': iid, 'edit': e['key'], **self._apply_edit(iid, e)})
        if src and it.get('source_item_id') in src:
            s = src[it['source_item_id']]
            if s.get('format') == 'Canceled':
                r = self.submit(Command(f"source-cancel:{iid}:{it['source_item_id']}", 'source_canceled',
                                        'service:wf1', 'service:wf1', iid, {}))
                if r['state'] == 'completed' and not r.get('duplicate'):
                    results.append({'item_id': iid, 'edit': 'source_canceled', **r})

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

    def _bootstrap(self, c, iid, snap) -> bool:
        """Conservative legacy import: never invents verification or receipts. An item whose board record is valid
        is rebuilt from it instead (R2: Monday keeps the business facts); returns True then."""
        rec = record.parse(snap.get('record'), iid) if snap.get('_has_record') else None
        if rec:
            self._restore(c, iid, snap, rec)
            return True
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
        if snap.get('_has_record'):
            observed['_record_col'] = True
            c.execute('UPDATE ops_items SET observed=? WHERE item_id=?', (dumps(observed), iid))
        audit(c, iid, 'bootstrap', 'service:migration', {'status': status, 'publication': publication,
                                                          'owner_state': owner_state, 'requested': requested})
        if publication == 'outcome_unknown':
            self.notify(c, 'legacy-publishing:' + iid, f'Item {iid} ({snap.get("name")}) showed "Publishing" when the '
                        'new system started. Treated as outcome unknown: it will not be published again until you '
                        'confirm on Instagram and tell Bondok.', iid)

    def _restore(self, c, iid, snap, rec):
        """Rebuild a store row from the board record. Media is verified again; nothing is published or scheduled
        from the record alone."""
        now = self.now()
        f = self.restore_from_record(c, iid, snap, rec)
        hold = f['hold']
        if not f['format'] and not hold:
            hold = dumps({'kind': 'format_missing', 'reason': 'Format must be Post or Story (owner decision)'})
        observed = {k: snap.get(k) for k in board.HUMAN}
        observed['_record_col'] = True
        projected = {k: snap.get(k) for k in board.SYSTEM}
        projected.update({'status': rec.get('st'), 'record': snap.get('record'), '_group': snap.get('group')})
        if f['publication'] == 'not_started':
            projected['publish_at'] = rec.get('pa')      # a different board time is the owner's change meanwhile
        # (a published item's time columns are historical evidence the display never changes)
        c.execute('INSERT INTO ops_items(item_id,name,code,source_item_id,format,caption,caption_state,caption_origin,'
                  'collab,variety,notes,folder_url,file_url,source_override_url,content_hash,asset_key,topaz_asset,'
                  'readiness,owner_state,owner_state_reason,hold,requested_at,requested_by,publication,legacy_posted,'
                  'observed,projected,waiting_since,created,updated) '
                  'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                  (iid, snap.get('name'), snap.get('code'), snap.get('source_item'), f['format'], snap.get('caption'),
                   f['caption_state'], f['caption_origin'], snap.get('collab'), snap.get('variety'), snap.get('notes'),
                   snap.get('folder'), snap.get('dropbox'), f['source_override_url'], f['content_hash'],
                   snap.get('asset'), f['topaz_asset'], 'unchecked', f['owner_state'], f['owner_state_reason'], hold,
                   f['requested_at'], f['requested_by'], f['publication'], f['legacy_posted'], dumps(observed),
                   dumps(projected), now, now, now))
        if snap.get('source_item'):
            c.execute("INSERT OR IGNORE INTO ops_source_map VALUES(?,?, 'active', ?)", (snap['source_item'], iid, now))
        self.restore_side_effects(c, iid, rec, f)
        audit(c, iid, 'restore_from_record', 'service:migration', {'record': rec})

    def _diff(self, c, it, snap, floor=None) -> list[dict]:
        observed = loads(it['observed'], {}) or {}
        confirmed = loads(it['projected'], {}) or {}
        pending = loads(it['pending_projection'], {}) or {}
        edits = []
        floor = self.now() - FRESH_READ_SECONDS if floor is None else floor
        # Which file version could the person have seen when they changed Topazed (R5 B4)? What the previous read
        # showed, every version v2 displayed since that read, and what this read shows. One value: bound to it.
        prev_seen = observed.get('_asset_seen')
        writes = confirmed.get('_asset_writes') or []
        # The click happened after the previous read and after v2's own last Topazed write (it replaced it).
        start = max((prev_seen or {}).get('at', 0), (confirmed.get('_hwrite_at') or {}).get('topaz', 0))
        before = [w for w in writes if w.get('at', 0) <= start]
        at_start = before[-1]['v'] if before else (prev_seen['v'] if prev_seen else snap.get('asset'))
        shown = {at_start, snap.get('asset')} | {w['v'] for w in writes if w.get('at', 0) > start}
        observed['_asset_seen'] = {'v': snap.get('asset'), 'at': floor}
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
                          'asset_shown': next(iter(shown)) if len(shown) == 1 else None,
                          'asset_candidates': sorted(str(x) for x in shown if x)})
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
        # R2 record column: a value nobody but the handler writes. A changed or damaged record is restored from
        # committed state (never an owner command); a board that gains the column gets its first record.
        had_col, has_col = bool(observed.get('_record_col')), bool(snap.get('_has_record'))
        observed['_record_col'] = has_col
        rec_fix = has_col and (not had_col or (
            snap.get('record') != confirmed.get('record', snap.get('record')) and
            not ('record' in pending and snap.get('record') == pending.get('record')) and
            not ('record' in recent and snap.get('record') == recent['record'])))
        times = [e for e in edits if e['key'] in TIME_KEYS]
        if len(times) > 1:
            # One logical date/time change is one scheduling decision (R5 M2); Publish at leads when it changed.
            lead = next((e for e in times if e['key'] == 'publish_at'), times[0])
            lead['also'] = [e['key'] for e in times if e is not lead]
            edits = [e for e in edits if e is lead or e['key'] not in TIME_KEYS]
        c.execute('UPDATE ops_items SET observed=? WHERE item_id=?', (dumps(observed), it['item_id']))
        if rec_fix:
            self.project(c, it['item_id'], force_keys=('record',) if had_col else ())
        return edits

    def _apply_edit(self, iid, e) -> dict:
        """Map one observed board edit to a command. A change by anyone but the automation is the owner's decision
        (contract §1). The idempotency key includes a per-column edit counter, so a repeated value after another
        value (A -> X -> A) is a new decision, while a retried observation of the same edit is not (R5 M3)."""
        with self.store.read() as c:
            row = c.execute('SELECT version, observed FROM ops_items WHERE item_id=?', (iid,)).fetchone()
        v, n = row['version'], ((loads(row['observed'], {}) or {}).get('_n') or {}).get(e['key'], 0)
        cid = f"monday:{iid}:{e['key']}:{v}:{n}:{_h(e['new'])}"
        k, new = e['key'], e['new']

        def run(op, args=None):
            return self.submit(Command(cid, op, 'monday', MONDAY, iid, args or {}))

        with self.store.read() as c:
            before_id = c.execute('SELECT COALESCE(MAX(id), 0) FROM ops_outbox').fetchone()[0]
        if e['human']:
            if k == 'format':
                # The owner's Format change is the authorization for that exact conversion (contract §3, R5 A6).
                r = run('change_format', {'format': new})
                if r.get('state') == 'rejected':
                    with self.store.tx() as c:            # write-back survives the rejection (audit MS8)
                        self.write_human(c, self.item(c, iid), {'format': self.item(c, iid)['format']}, guarded=False)
            elif k == 'caption':
                r = run('update_caption', {'text': new or ''})
            elif k == 'topaz':
                # Bound to the file version the person could see when toggling (R5 B4).
                r = run('confirm_topaz', {'confirmed': new == 'Topazed', 'asset_key': e.get('asset_shown'),
                                          'candidates': e.get('asset_candidates') or []})
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
            self._mark_observed(iid, k, new, r, before_id)
            return r
        r = self._system_edit(iid, e, run)
        # The board value was handled (applied or refused): it is what the board shows now, so it is not an
        # edit again next cycle (a person's Posted / post link no longer re-holds every run; a value that cannot
        # be reverted is not re-rejected every run), and the next display write compares against it and
        # restores the system value where it owns one (round-2 review #2, #3).
        with self.store.tx() as c:
            cur = self.item(c, iid)
            proj = loads(cur['projected'], {}) or {}
            pend = loads(cur['pending_projection'], {}) or {}
            for key in [k, *e.get('also', [])]:
                val = new if key == k else (e.get('snap') or {}).get(key)
                proj[key] = board.compare_value(key, val) if val is not None else None
                pend.pop(key, None)  # what the board shows now is known; an older in-flight belief must not mask it
            obs = loads(cur['observed'], {}) or {}
            obs.setdefault('_n', {})[k] = obs.get('_n', {}).get(k, 0) + 1
            c.execute('UPDATE ops_items SET observed=? WHERE item_id=?', (dumps(obs), iid))
            c.execute('UPDATE ops_items SET projected=?, pending_projection=? WHERE item_id=?',
                      (dumps(proj), dumps(pend) if pend else None, iid))
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
            # Owner-reported publication is terminal and is decided before any pause/resume meaning (R5 B2).
            if new == 'Posted':
                return run('report_published', {'source': 'board status', 'value': new})
            if new == 'Paused':
                return run('pause', {'reason': 'Paused on the board'})
            if new == 'Skipped':
                return run('skip', {'reason': 'Skipped on the board'})
            with self.store.read() as c:
                it = self.item(c, iid)
            if it['publication'] == 'published':
                return self._revert(iid, k, 'This item is recorded as published; it will not be published again. '
                                            'If it was not actually posted, tell Bondok "not published"')
            if it['owner_state'] in ('paused', 'skipped'):
                # The owner changed Paused/Skipped to another label: that is the decision to resume (contract §3).
                return run('resume', {'source': 'board', 'from_label': new})
            return self._revert(iid, k, 'Status shows the system state; use Paused/Skipped/Posted, Publish at, '
                                        'or ask Bondok')
        if k == 'publish_at' and not new:
            # Clearing Publish at unschedules; legacy Post Date/Time never stand in for it (contract, audit S2).
            return run('request_reschedule', {'at': None})
        if k == 'publish_at' and board.date_only(new):
            # A date without a time is an incomplete request: kept as entered, never midnight or a legacy value.
            return run('request_reschedule', {'at': None, 'incomplete_date': board.date_only(new)})
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
            return run('report_published', {'source': 'board ' + ('post link' if k == 'post_link' else 'media id'),
                                            'value': new})
        with self.store.read() as c:
            it = self.item(c, iid)
            mine = self.desired_display(c, it).get(k)
            ours = k in set((loads(it['projected'], {}) or {}).get('_ours') or [])
        if mine is None and not ours:
            # v2 shows nothing in this column for the item: the text is kept as written, without a false claim
            # that it was reverted, and without a loop (R5 LOW-14).
            return {'state': 'completed', 'note': 'text kept (no system value for this column)'}
        return self._revert(iid, k, 'This column is managed by the system; the system value was restored')

    def _mark_observed(self, iid, key, value, result, before_id=None):
        with self.store.tx() as c:
            it = self.item(c, iid)
            observed = loads(it['observed'], {}) or {}
            observed[key] = value
            observed.setdefault('_n', {})[key] = observed.get('_n', {}).get(key, 0) + 1   # next edit = new event
            c.execute('UPDATE ops_items SET observed=? WHERE item_id=?', (dumps(observed), iid))
            # A person's edit wins over our unsent write of the same column (round-2 review #5) - except our
            # write-back of a refused edit, which is meant to overwrite it.
            for j in [] if result.get('state') == 'rejected' else c.execute("SELECT id, payload FROM ops_outbox WHERE kind='monday' AND item_id=? AND "
                               "dedupe_key LIKE 'monday-h:%' AND state IN ('pending','failed')", (iid,)).fetchall():
                if before_id is not None and j['id'] > before_id:
                    continue        # queued by the command for this very edit (e.g. a Topazed write-back): kept
                if 'h:' + key in (loads(j['payload'], {}).get('compare') or {}):
                    c.execute("UPDATE ops_outbox SET state='superseded', updated=? WHERE id=?", (self.now(), j['id']))
            pend = loads(self.item(c, iid)['pending_projection'], {}) or {}
            if result.get('state') != 'rejected' and pend.pop('h:' + key, None) is not None:
                c.execute('UPDATE ops_items SET pending_projection=? WHERE item_id=?', (dumps(pend) or None, iid))
            if result.get('state') == 'rejected':
                # Visible and repairable by correcting the same column; never a separate hold that outlives the
                # corrected value (R5 A7).
                self.set_notice(c, iid, f'Board change to {key} not applied: ' + str(result.get('reason', ''))[:300])
                self.project(c, iid)

    def _revert(self, iid, key, why):
        keys = tuple(key) if isinstance(key, (tuple, list)) else (key,)
        with self.store.tx() as c:
            self.set_notice(c, iid, 'Board change not applied: ' + str(why)[:300])
            self.project(c, iid, force_keys=keys + ('action',))
            audit(c, iid, 'revert_system_column', 'monday', {'key': keys, 'why': why})
        return {'state': 'rejected', 'reason': why, 'reverted': key}

    def write_human(self, c, it, values: dict, guarded=True, expect: dict | None = None):
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
                was = [expect[k]] if expect and k in expect else [observed.get(k), *landing.get(k, [])]
                guard[board.COL[k]] = {'kind': board.KIND.get(k, 'text'), 'was': was, 'new': compare['h:' + k]}
        payload = {'item_id': it['item_id'], 'columns': cols, 'compare': compare, 'group': None}
        if guard:
            payload['guard'] = guard
        key = 'monday-h:' + it['item_id'] + ':' + fingerprint(payload)[:16]
        if c.execute("SELECT 1 FROM ops_outbox WHERE dedupe_key=? AND state IN ('done','superseded','escalated')",
                     (key,)).fetchone():
            # The same values were written before (e.g. a second rejected Format edit): a new write is needed, not a
            # duplicate of the old job, which INSERT OR IGNORE would silently drop (R5 M3).
            from .db import next_counter
            key += ':' + str(next_counter(c, 'outbox:monday-h'))
        self.enqueue(c, 'monday', key, payload, it['item_id'])
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
            # Findings of one kind raised together reach Slack as one message (e.g. a storage loss affecting
            # 20 items); a single finding keeps its own sentence.
            self.notify(c, 'finding:' + fp + ':' + str(int(now)), detail, item_id, digest='finding:' + str(kind),
                        line=detail, head='Schedule check, ' + str(kind).replace('_', ' ') + ':\n')
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
        win = self.parse_window(cmd.args)
        if it['publication'] == 'published':
            # Terminal publication protection is never cleared by an ordinary resume (R5 B2).
            if it['owner_state'] != 'active':
                self.update_item(c, it['item_id'], cmd.actor, 'resume (published)', owner_state='active',
                                 owner_state_reason=None)
            return {'resumed': False, 'message': 'This item is recorded as published; it will not be published again'}
        if it['publication'] == 'failed':
            # Explicit owner retry after a definitive rejection (nothing was published) (R5 M7).
            it = self.update_item(c, it['item_id'], cmd.actor, 'owner retry after rejection', publication='not_started')
            clearable = True
        if it['owner_state'] == 'active' and not clearable and not win:
            return {'resumed': False, 'message': 'Item is not paused or skipped'}
        # Resume also clears holds that only wait for the owner to say "continue" (audit: a rejected board
        # edit or the publish retry limit on an active item could otherwise never be cleared).
        self.update_item(c, it['item_id'], cmd.actor, 'resume', owner_state='active', owner_state_reason=None,
                         hold=None if clearable else it['hold'])
        if win:
            # Constraints said with the instruction travel with it ("publish them tomorrow, nothing more", R5 A15).
            self._store_window(c, it['item_id'], win, cmd.actor)
            cur = self.item(c, it['item_id'])
            if cur['requested_at'] and not rules.in_window(rules.instant(cur['requested_at']), win):
                self.update_item(c, it['item_id'], cmd.actor, 'request outside window', requested_at=None,
                                 requested_by=None)
            res = self.reservation(c, it['item_id'])
            if res and not rules.in_window(rules.instant(res['slot']), win):
                self.release(c, self.item(c, it['item_id']), 'outside the owner window', keep_request=False)
        out = self.try_schedule(c, it['item_id'], cmd.actor)
        return {'resumed': True, **({'window': win} if win else {}), **out}

    def op_request_rework(self, c, cmd: Command):
        """Owner sends the item back to the editor ("رجعها للمونتير"): never a resume. Held until a new file
        version arrives or the owner resumes it; the editor gets one task."""
        it = self.item(c, cmd.item_id)
        self._guard_mutable(c, it, allow_paused=True)
        reason = (cmd.args.get('reason') or 'The owner asked for a new edit').strip()[:300]
        self.release(c, it, 'rework requested', keep_request=False)
        self.update_item(c, it['item_id'], cmd.actor, 'rework requested', hold=dumps(
            {'kind': 'rework', 'asset': it['asset_key'], 'origin': 'owner', 'party': 'editor',
             'recover': 'a new file version, or the owner resumes it', 'reason': 'Owner asked for a new edit: ' + reason}))
        self.editor_task(c, it['item_id'], 'rework:' + str(it['asset_key']), 'Owner asked for a new edit: ' + reason)
        return {'rework': True, 'message': 'Sent back to the editor; it will not be scheduled until a new version '
                                           'arrives or you resume it.'}

    def explicit_owner(self, cmd: Command) -> bool:
        # A board edit of the column itself is the owner's explicit decision (contract §1/§3).
        return cmd.actor_kind == MONDAY or \
            (cmd.actor_kind == OWNER and (cmd.auth.get('explicit') is True or bool(cmd.auth.get('proposal'))))

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
        if cmd.actor_kind != MONDAY:
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
        # R5 B3: the explicit selection mode and its reason are persisted; the owner's exact file outranks the
        # project folder until the owner selects another one.
        now = self.now()
        c.execute('INSERT INTO ops_checks(item_id,kind,key,requested,requested_by,state,result,updated) '
                  "VALUES(?,'selection',?,?,?,'active',?,?) ON CONFLICT(item_id,kind) DO UPDATE SET key=excluded.key, "
                  'requested=excluded.requested, requested_by=excluded.requested_by, state=excluded.state, '
                  'result=excluded.result, updated=excluded.updated',
                  (it['item_id'], OWNER_SELECTED, now, cmd.actor, dumps({
                      'mode': OWNER_SELECTED, 'url': url, 'by': cmd.actor, 'at': now,
                      'reason': 'Owner selected this file (' + ('board Dropbox Link' if cmd.actor_kind == MONDAY
                                                                else 'Slack') + ')'}), now))
        self.request_check(c, it['item_id'], cmd.actor, 'source_replaced')
        return {'accepted': True, 'message': 'New source recorded; the actual file will be checked before scheduling.'}

    def selection(self, c, it) -> dict:
        """Source selection mode (R5 B3): the owner's exact file (board Dropbox Link or Slack replace_source) or the
        automatic choice from the project folder."""
        if it.get('source_override_url'):
            r = c.execute("SELECT result FROM ops_checks WHERE item_id=? AND kind='selection'", (it['item_id'],)).fetchone()
            rec = loads(r['result'], {}) if r else {}
            return {'mode': OWNER_SELECTED, 'url': it['source_override_url'], 'file_id': rec.get('file_id'),
                    'reason': rec.get('reason') or 'Owner selected this file'}
        return {'mode': AUTOMATIC, 'url': None}

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
        if bad and cmd.actor_kind == MONDAY:
            # The board shows the owner's text; it is not silently replaced by the previous caption and it is
            # not published: the item waits until the caption is corrected on the board (R5 A7, M3).
            c.execute('UPDATE ops_items SET content_rev=content_rev+1 WHERE item_id=?', (it['item_id'],))
            self.update_item(c, it['item_id'], cmd.actor, 'caption not valid', caption=text or None,
                             caption_state='invalid', caption_origin='human')
            self.set_notice(c, it['item_id'], f'Caption not accepted: {bad}. Correct it on the board.')
            self.notify(c, f"caption-invalid:{it['item_id']}:{_h(text)}", f"{it['name']} ({it['item_id']}): the caption "
                        f'on the board was not accepted ({bad}). It will not be published until the caption is corrected.',
                        it['item_id'])
            return {'caption_updated': False, 'invalid': bad, **self.reauthorize(c, it['item_id'], cmd.actor,
                                                                                  'caption not valid')}
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
        """Topaz is the owner's fact about one exact file version (contract §6). A board toggle is bound to the
        version shown when it was set; if that cannot be told (the selection changed meanwhile, or no file was
        selected yet) it is not applied and shown as 'Not yet', so a deliberate re-confirmation is visible (R5 B4)."""
        it = self.item(c, cmd.item_id)
        confirmed = cmd.args.get('confirmed') is True
        asset = cmd.args.get('asset_key') if cmd.actor_kind == MONDAY else (cmd.args.get('asset_key') or it['asset_key'])
        if not confirmed:
            self.update_item(c, it['item_id'], cmd.actor, 'topaz cleared', topaz_asset=None)
            return {'topaz_asset': None, **self.evaluate(c, it['item_id'], cmd.actor)}
        if cmd.actor_kind == OWNER and not cmd.auth.get('explicit'):
            raise Rejected('Topaz confirmation must come from an explicit owner statement', 'needs_owner')
        if cmd.actor_kind == MONDAY and (not asset or not it['asset_key']):
            why = ('no file was selected yet' if not it['asset_key'] else
                   'the selected file changed while Topazed was being set')
            self.write_human(c, it, {'topaz': 'Not yet'}, expect={'topaz': 'Topazed'})
            self.set_notice(c, it['item_id'], f'Topazed was not applied: {why}. Set Topazed again when the file shown '
                            'in Source asset version is the Topazed one.')
            self.notify(c, f"topaz-ambiguous:{it['item_id']}:{it['version']}", f"{it['name']} ({it['item_id']}): "
                        f'Topazed was not applied because {why}. Set Topazed again for the file shown on the board '
                        f"({it.get('file_name') or 'not selected yet'}) once Topaz was applied to it.", it['item_id'])
            return {'topaz_asset': it['topaz_asset'], 'ambiguous': True}
        if not it['asset_key']:
            raise Rejected('No source file is selected yet; Topaz confirmation cannot be bound', 'no_asset')
        if asset != it['asset_key']:
            if cmd.actor_kind != MONDAY:
                raise Rejected('Topaz confirmation refers to a different file version than the selected one', 'stale')
            # Confirmed for the version that was shown, which is no longer the selected one: recorded for that
            # version only; the board shows that the new file still needs it.
            self.update_item(c, it['item_id'], cmd.actor, 'topaz confirmed for an earlier version', topaz_asset=asset)
            self.write_human(c, self.item(c, it['item_id']), {'topaz': 'Not yet'}, expect={'topaz': 'Topazed'})
            self.notify(c, f"topaz-older:{it['item_id']}:{asset}", f"{it['name']} ({it['item_id']}): Topazed was set "
                        'for the previous file version; the selected file has changed since. Set Topazed again once '
                        'Topaz was applied to the new file.', it['item_id'])
            return {'topaz_asset': asset, 'superseded': True, **self.evaluate(c, it['item_id'], cmd.actor)}
        self.update_item(c, it['item_id'], cmd.actor, 'topaz confirmed', topaz_asset=it['asset_key'])
        obs = loads(self.item(c, it['item_id'])['observed'], {}) or {}
        obs['_nudge'] = self.now()             # a person acted: prepare it on the next cycle
        c.execute('UPDATE ops_items SET observed=? WHERE item_id=?', (dumps(obs), it['item_id']))
        if cmd.actor_kind != MONDAY:
            self.write_human(c, self.item(c, it['item_id']), {'topaz': 'Topazed'})
        return {'topaz_asset': it['asset_key'], **self.evaluate(c, it['item_id'], cmd.actor)}

    def op_set_notes(self, c, cmd: Command):
        self.update_item(c, cmd.item_id, cmd.actor, 'notes', notes=cmd.args.get('value') or None)
        return {'stored': True, 'reprocessing': False}

    def op_set_variety(self, c, cmd: Command):
        self.update_item(c, cmd.item_id, cmd.actor, 'variety', variety=cmd.args.get('value') or None)
        return {'stored': True, 'reprocessing': False}

    def op_set_code(self, c, cmd: Command):
        """The board's Code is the record: an invalid code is stored and shown as a block, and correcting it on the
        board clears that block (R5 M12)."""
        value = (cmd.args.get('value') or '').strip()
        if cmd.actor_kind != MONDAY:
            try:
                rules.style(value)
            except rules.RuleError as e:
                raise Rejected(str(e), 'invalid_code')
        self.update_item(c, cmd.item_id, cmd.actor, 'code', code=value or None)
        return {'stored': True, **self.evaluate(c, cmd.item_id, cmd.actor)}

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
        # A running check finishes with a typed result (R5 A9); one left running for a day (item paused meanwhile)
        # does not swallow a new request.
        if chk and chk['state'] == 'running' and self.now() - (chk['updated'] or 0) < 86400:
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

    def data_roots(self) -> list:
        """Data roots of THIS runtime: the directory of the operational store (n8n: WASET_SOCIAL_DATA_DIR; Bondok:
        its bind mount of the same volume), plus WASET_SOCIAL_DATA_DIR when set elsewhere (R5 A4)."""
        roots = [self.store.path.parent]
        env = os.environ.get('WASET_SOCIAL_DATA_DIR')
        if env and Path(env) not in roots:
            roots.append(Path(env))
        return roots

    def local_media(self, m):
        return media.local_file(m, self.data_roots())

    def delivered_identity(self, c, it) -> str | None:
        """'<Dropbox id>@<rev>' of the delivered copy bound to the item's verification, None when not bound (R5 M17)."""
        m = self.media_row(c, it.get('verification_id'))
        d = (loads(m['metadata'], {}) or {}).get('delivered') if m else None
        return f"{d['id']}@{d['rev']}" if d and d.get('id') and d.get('rev') else None

    def drop_delivery(self, c, media_id):
        """The delivered copy no longer matches: forget it so the verified file is delivered again (R5 M17)."""
        m = self.media_row(c, media_id)
        if m:
            info = loads(m['metadata'], {}) or {}
            for k in ('url', 'delivered', 'delivery'):
                info.pop(k, None)
            c.execute('UPDATE media SET metadata=? WHERE id=?', (dumps(info), media_id))

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
        # The local file is a cache in the runtime's own data root (R5 A4). Publication uses the delivered Dropbox
        # copy; once that copy's identity is bound (and rechecked at the commitment point), a missing local cache is
        # not a lost delivery (contract §6). Without a bound identity the local file is still required.
        if self.local_media(m) is None and not info.get('delivered'):
            return 'Prepared file is missing from storage'
        return None

    def evaluate(self, c, item_id, actor) -> dict:
        """Recompute readiness from durable facts; release ineligible reservations."""
        it = self.item(c, item_id)
        if it['publication'] in PROTECTED_PUBLICATION:
            return {'readiness': it['readiness']}
        hold = loads(it['hold'], None)
        if hold and hold.get('kind') == 'rework' and it['asset_key'] and hold.get('asset') != it['asset_key']:
            it = self.update_item(c, item_id, actor, 'new version after rework request', hold=None)
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
            elif self.duplicate_of(c, it):
                dup = self.duplicate_of(c, it)
                fields = dict(readiness='blocked', block_kind='review', block_key='duplicate:' + str(dup['item_id']),
                              block_reason=self.duplicate_reason(dup))
                if it.get('block_key') != fields['block_key']:
                    self.notify(c, f"dup:{item_id}:{dup['item_id']}:{it['content_rev']}",
                                f"{it['name']} ({item_id}): {fields['block_reason']}", item_id)
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
                obs = loads(it['observed'], {}) or {}
                last = obs.get('_last_prep') or 0
                requested = requested or (obs.get('_nudge') or 0) > last
                if requested:
                    due = 0
                elif chk and chk['state'] == 'running':
                    due = last + RECHECK_POLL_SECONDS          # a recheck's result is expected (R5 A9)
                elif it['readiness'] == 'ready':
                    due = last + RELIST_READY_SECONDS
                elif it['infra_issue']:
                    # Temporary problem: retried with a growing interval, escalated once (R5 M10).
                    n = max(1, int(obs.get('_infra_failures') or 1))
                    due = last + min(INFRA_RECHECK_SECONDS * 2 ** (n - 1), INFRA_MAX_BACKOFF)
                elif it['readiness'] == 'blocked' and (it['block_key'] or '').startswith('config:'):
                    # Permanent link/folder/configuration problem: no timer-driven retries; the next visit follows
                    # changed evidence (Code, link, folder, format edits), an explicit recheck or an owner action.
                    if obs.get('_block_evidence') == self._evidence(it):
                        continue
                    due = 0
                elif it['readiness'] == 'blocked':
                    n = obs.get('_blocked_cycles', 0)
                    due = last + BACKOFF_STEPS[min(n, len(BACKOFF_STEPS) - 1)]
                else:
                    due = last
                if due > now:
                    continue
                try:
                    rot = rules.rotation_key(it['format'], it['code'], it['name'], it['variety'])
                except rules.RuleError:
                    rot = 'INVALID'
                sel = self.selection(c, it)
                out.append({'item_id': it['item_id'], 'rotation': rot, 'priority': 0 if requested else 1, '_last': last,
                            '_pending': it['readiness'] == 'checking' or bool(it['infra_issue']),
                            'waiting_since': it['waiting_since'] or it['created'], 'format': it['format'],
                            'code': it['code'], 'name': it['name'], 'source_item_id': it['source_item_id'],
                            # A folder is only created for a valid Code (R5-LOW-06).
                            'code_valid': self.safe_style(it['code']) is not None,
                            'selection_mode': sel['mode'], 'selected_url': sel['url'],
                            'folder_url': it['folder_url'], 'file_url': it['source_override_url'] or it['file_url'],
                            # A brief is fetched (Monday request) only when a draft could follow: never while one
                            # waits, and after a rejected/failed draft at most daily (a changed brief) (audit perf).
                            'needs_caption': it['format'] == 'Post' and it['caption_state'] in ('missing',) and
                                             not c.execute("SELECT 1 FROM ops_caption_drafts WHERE item_id=? AND "
                                                           "(state='pending_approval' OR (state='no_brief' AND "
                                                           "updated>?) OR (state!='no_brief' AND updated>?))",
                                                           (it['item_id'], now - self.NO_BRIEF_RECHECK,
                                                            now - 86400)).fetchone(),
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

    def _touch_prep(self, c, it, blocked: bool | None):
        """Record a WF1 visit. blocked=True: the item stays blocked without new evidence (its backoff grows, R5 M13);
        False: progress (backoff reset); None: visit only."""
        obs = loads(self.item(c, it['item_id'])['observed'], {}) or {}
        obs['_last_prep'] = self.now()
        if blocked is not None:
            obs['_blocked_cycles'] = (obs.get('_blocked_cycles', 0) + 1) if blocked else 0
        c.execute('UPDATE ops_items SET observed=? WHERE item_id=?', (dumps(obs), it['item_id']))

    @staticmethod
    def _evidence(it) -> str:
        """Fingerprint of the facts a permanent source problem depends on (R5 M10): a change re-opens the item."""
        return _h('|'.join(str(it.get(k) or '') for k in ('code', 'folder_url', 'source_override_url', 'file_url',
                                                            'format')))

    def _check_running(self, c, item_id) -> bool:
        chk = c.execute("SELECT state FROM ops_checks WHERE item_id=? AND kind='media'", (str(item_id),)).fetchone()
        return bool(chk and chk['state'] in ('requested', 'running'))

    def _infra(self, c, it, kind, text, actor='service:wf1', count=True) -> int:
        """Temporary problem of one preparation stage (source, media, delivery): visible on the board, retried with
        a growing interval, counted across runs (R5 M9/M10/A10). Returns the consecutive-failure count."""
        cur = self.item(c, it['item_id'])
        obs = loads(cur['observed'], {}) or {}
        n = int(obs.get('_infra_failures') or 0) + (1 if count else 0)
        obs.update(_infra_failures=n, _infra_kind=kind, _last_prep=self.now())
        c.execute('UPDATE ops_items SET observed=? WHERE item_id=?', (dumps(obs), it['item_id']))
        if cur['infra_issue'] != str(text)[:300]:
            self.update_item(c, it['item_id'], actor, kind + ' infra issue', infra_issue=str(text)[:300])
        return n

    def _infra_clear(self, c, it, kind=None, actor='service:wf1'):
        """The stage that had the temporary problem works again (kind None: every stage)."""
        cur = self.item(c, it['item_id'])
        obs = loads(cur['observed'], {}) or {}
        if kind is not None and obs.get('_infra_kind') not in (None, kind):
            return cur
        if obs.get('_infra_err'):
            self.resolve_finding(c, obs['_infra_err'])
        if any(k in obs for k in ('_infra_failures', '_infra_kind', '_infra_err')):
            for k in ('_infra_failures', '_infra_kind', '_infra_err'):
                obs.pop(k, None)
            c.execute('UPDATE ops_items SET observed=? WHERE item_id=?', (dumps(obs), it['item_id']))
        if cur['infra_issue']:
            return self.update_item(c, it['item_id'], actor, 'infra recovered', infra_issue=None)
        return self.item(c, it['item_id'])

    def op_prep_source(self, c, cmd: Command):
        """WF1 resolved (or failed to resolve) the exact selected Dropbox file."""
        a = cmd.args
        self.run_check(c, 'wf1', a.get('run_id'), a.get('fence'))
        it = self.item(c, cmd.item_id)
        if it['publication'] in PROTECTED_PUBLICATION or it['owner_state'] != 'active':
            return {'next': 'none', 'note': 'protected'}
        if a.get('expected_format') and a['expected_format'] != it['format']:
            raise Rejected('Format changed during this run; result discarded', 'stale')
        sel = self.selection(c, it)
        got = a.get('selection') or {}
        if sel['mode'] == OWNER_SELECTED and (got.get('mode') != OWNER_SELECTED or got.get('url') != sel['url']):
            # R5 B3: the owner chose an exact file. A result from the folder listing (or for an earlier choice) is
            # refused: the rejected file is never selected again, and nothing about the item changes.
            self._touch_prep(c, it, None)
            audit(c, it['item_id'], 'prep_result_refused', cmd.actor, {'why': 'not the owner-selected file',
                                                                       'selection': got, 'file': a.get('file')})
            return {'next': 'none', 'refused': 'not_owner_selected',
                    'reason': 'The owner selected a specific file for this item; this result is for another selection'}
        if a.get('folder_url') and not it['folder_url']:
            if self.safe_style(it['code']) is None:
                # R5-LOW-06: a folder named after a missing/invalid Code (".../null/Story") is shared by unrelated
                # items; it is never recorded as this item's project folder.
                audit(c, it['item_id'], 'folder_refused', cmd.actor, {'folder_url': a['folder_url'], 'code': it['code']})
            else:
                it = self.update_item(c, it['item_id'], cmd.actor, 'folder created', folder_url=a['folder_url'])
        err = a.get('error')
        if err:
            return self._source_error(c, it, cmd, str(err), a.get('error_kind') or 'infra')
        f = a['file']
        for k in ('id', 'rev', 'content_hash'):
            if not f.get(k):
                raise Rejected('Dropbox version metadata is incomplete', 'invalid')
        asset = f['id'] + '@' + f['rev']
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
                                  block_kind=None, block_reason=None, block_key=None)
            it = self._infra_clear(c, it, None, cmd.actor)          # new evidence: earlier failures do not count
        elif a.get('url') and a['url'] != it['file_url']:
            it = self.update_item(c, it['item_id'], cmd.actor, 'share link', file_url=a['url'])
        it = self._infra_clear(c, it, 'source', cmd.actor)
        if sel['mode'] == OWNER_SELECTED and sel.get('file_id') != f['id']:
            r = c.execute("SELECT result FROM ops_checks WHERE item_id=? AND kind='selection'", (it['item_id'],)).fetchone()
            if r:                                            # the exact file the owner's link resolved to (audit)
                c.execute("UPDATE ops_checks SET result=?, updated=? WHERE item_id=? AND kind='selection'",
                          (dumps({**(loads(r['result'], {}) or {}), 'file_id': f['id'], 'resolved_at': self.now()}),
                           self.now(), it['item_id']))
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
        body = {'itemId': it['item_id'], 'format': it['format'], 'sourceUrl': a.get('url') or it['file_url'],
                'fileId': f['id'], 'revision': f['rev'], 'contentHash': f['content_hash'], 'assetKey': asset,
                'topazed': it['topaz_asset'] == asset, 'recheck': recheck}
        if it['readiness'] == 'blocked' and (it['block_key'] or '').startswith('duplicate:') and not changed \
                and not recheck:
            self._touch_prep(c, it, True)
            return {'next': 'none', **self.evaluate(c, it['item_id'], cmd.actor)}   # cleared once the other item is gone
        if it['readiness'] == 'ready' and not changed and not recheck:
            self._touch_prep(c, it, False)
            if self._needs_delivery_identity(c, it):
                # Verified before delivered identities were recorded: bind the existing Dropbox copy (R5 M17).
                return {'next': 'prepare', 'media': body}
            return {'next': 'none', 'unchanged': True}
        if it['readiness'] == 'blocked' and not changed and not recheck and it['block_kind'] != 'infra':
            self._touch_prep(c, it, True)                       # unchanged block: the backoff grows (R5 M13)
            return {'next': 'none', 'unchanged': True, 'blocked': it['block_reason']}
        if it['format'] == 'Story' and not self._duration_ok(c, it['item_id'], asset, f['content_hash']):
            self._touch_prep(c, it, False)
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
            self._finish_check(c, it['item_id'], 'blocked: Topaz confirmation missing', 'blocked')
            self._touch_prep(c, it, True)
            return {'next': 'none', 'blocked': 'topaz'}
        self._touch_prep(c, it, False)
        return {'next': 'prepare', 'media': body}

    def _source_error(self, c, it, cmd, err, kind):
        """WF1 could not resolve the source. 'infra' is temporary (retried with a growing interval, escalated once);
        'config'/'editor' are permanent for the current evidence (R5 M10)."""
        if kind == 'infra':
            n = self._infra(c, it, 'source', err, cmd.actor)
            if n >= INFRA_ESCALATE_AFTER:
                fp = 'prep_infra:' + _h(err)
                obs = loads(self.item(c, it['item_id'])['observed'], {}) or {}
                obs['_infra_err'] = fp
                c.execute('UPDATE ops_items SET observed=? WHERE item_id=?', (dumps(obs), it['item_id']))
                self.finding(c, fp, it['item_id'], 'prep_infra',
                             f"Preparation of {it['name']} ({it['item_id']}) keeps failing ({n} attempts): {err[:300]}. "
                             f'It is retried at most every {INFRA_MAX_BACKOFF // 3600} hours; nothing was rejected. '
                             'If Dropbox access was changed, fix it or ask Bondok to recheck the item.')
                # A pending recheck ends with this typed result instead of waiting for a recovery (R5 A9).
                self._finish_check(c, it['item_id'], 'could not run (temporary problem, automatic retries '
                                   'continue): ' + err[:200], 'temporary_failure')
            return {'next': 'none', 'infra': True, 'failures': n}
        key = kind + ':' + _h(err)
        self._touch_prep(c, it, True)
        if not (it['readiness'] == 'blocked' and it['block_key'] == key and it['block_reason'] == err[:500]):
            it = self.update_item(c, it['item_id'], cmd.actor, 'source problem', readiness='blocked',
                                  block_kind='editor' if kind == 'editor' else 'config', block_key=key,
                                  block_reason=err[:500])
            fresh = True
        else:
            fresh = False                                       # same problem as last time
        it = self._infra_clear(c, it, None, cmd.actor)
        obs = loads(it['observed'], {}) or {}
        obs['_block_evidence'] = self._evidence(it)     # a permanent problem waits for changed evidence
        c.execute('UPDATE ops_items SET observed=? WHERE item_id=?', (dumps(obs), it['item_id']))
        if not fresh:
            self._finish_check(c, it['item_id'], 'blocked: ' + err[:200], 'blocked')
            return {'next': 'none', 'blocked': kind, 'unchanged': True}
        self.release(c, self.item(c, it['item_id']), 'source problem')
        self.project(c, it['item_id'])
        if kind == 'editor':
            self.editor_task(c, it['item_id'], key, err)
        self._finish_check(c, it['item_id'], 'blocked: ' + err[:200], 'blocked')
        return {'next': 'none', 'blocked': kind}

    def _needs_delivery_identity(self, c, it) -> bool:
        m = self.media_row(c, it.get('verification_id'))
        info = loads(m['metadata'], {}) if m else {}
        return bool(m and info.get('url') and not info.get('delivered') and
                    int(info.get('identifyAttempts') or 0) < MAX_IDENTIFY_ATTEMPTS and self.local_media(m) is not None)

    def _duration_ok(self, c, item_id, asset, content_hash) -> bool:
        r = c.execute("SELECT * FROM ops_checks WHERE item_id=? AND kind='duration'", (str(item_id),)).fetchone()
        res = loads(r['result'], {}) if r else {}
        # Re-evaluated against the current policy (a stored verdict from an older policy is not final).
        d = res.get('duration')
        return bool(r and r['key'] == asset + '|' + content_hash and rules.story_source_failure(d) is None and
                    float(d) >= media.IG_MIN_SECONDS)

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

    def _job_failure(self, c, it, cmd, r, recheck, stage) -> dict | None:
        """Typed outcome of a media job that did not produce a usable result (R5 A10). None: not a failure."""
        cls = r.get('failureClass')
        reason = r.get('reason') or 'Media preparation failed'
        if r.get('retryable'):
            # Temporary (transport, tool, killed worker) or local disk: retried within the job's budget.
            self._infra(c, it, 'media', reason, cmd.actor, count=cls != 'capacity')
            if recheck:
                self._finish_check(c, it['item_id'], 'could not complete (temporary problem; automatic retries '
                                   'continue): ' + reason[:200], 'temporary_failure')
            return {'next': 'none', 'infra': True, 'attempts': r.get('attempts')}
        if cls == 'exhausted':
            # The retry budget is used up: one escalation, then wait for a new file revision or an explicit recheck.
            key = 'prep_exhausted:' + str(it['asset_key'])
            first = it.get('block_key') != key
            self.update_item(c, it['item_id'], cmd.actor, 'preparation escalated', readiness='blocked',
                             block_kind='review', block_key=key, verification_id=None,
                             block_reason=(reason + '. Nothing was rejected; ask Bondok to recheck it, or upload a new '
                                           'version.')[:500])
            self._infra_clear(c, it, None, cmd.actor)
            self.release(c, self.item(c, it['item_id']), 'preparation escalated')
            self.project(c, it['item_id'])
            if first:
                self.notify(c, f"prep-exhausted:{it['item_id']}:{it['asset_key']}:{int(self.now())}",
                            f"Preparation of {it['name']} ({it['item_id']}) stopped retrying after repeated temporary "
                            f"failures: {reason[:300]}. Nothing was published. Ask Bondok to recheck it when the cause "
                            'is fixed.', it['item_id'])
            self._finish_check(c, it['item_id'], 'escalated: ' + reason[:200], 'escalated')
            return {'next': 'none', 'blocked': 'exhausted', 'reason': reason}
        if stage == 'media' and not r.get('ready'):
            # A permanent file defect or a measured verdict: item-local block and one editor task.
            key = 'media:' + it['asset_key'] + ':' + _h(reason)
            self.update_item(c, it['item_id'], cmd.actor, 'media failed', readiness='blocked', block_kind='editor',
                             block_key=key, block_reason=reason, verification_id=None)
            self._infra_clear(c, it, None, cmd.actor)
            self.release(c, self.item(c, it['item_id']), 'media failed')
            self.project(c, it['item_id'])
            self.editor_task(c, it['item_id'], key, reason)
            self._finish_check(c, it['item_id'], 'blocked: ' + reason, 'blocked')
            return {'next': 'none', 'blocked': reason}
        return None

    def op_prep_preflight(self, c, cmd: Command):
        a = cmd.args
        self.run_check(c, 'wf1', a.get('run_id'), a.get('fence'))
        it = self.item(c, cmd.item_id)
        r = a['result']
        if r.get('pending'):
            return {'next': 'none', 'pending': True}
        if r.get('assetKey') != it['asset_key'] or r.get('contentHash') != it['content_hash']:
            raise Rejected('Duration result is for an older file version; discarded', 'stale')
        out = self._job_failure(c, it, cmd, r, self._check_running(c, it['item_id']), 'preflight')
        if out:
            return out
        self._infra_clear(c, it, 'media', cmd.actor)
        dur = r.get('duration')
        # The duration policy applies to a measured duration; an unmeasurable file is a defect with its own cause.
        policy = rules.story_source_failure(dur) if it['format'] == 'Story' and dur is not None else None
        bad = policy or (r.get('reason') or 'The file could not be checked' if r.get('ready') is False else None)
        key = it['asset_key'] + '|' + it['content_hash']
        c.execute("INSERT OR REPLACE INTO ops_checks(item_id,kind,key,requested,requested_by,state,result,updated) "
                  "VALUES(?,?,?,?,?,?,?,?)", (it['item_id'], 'duration', key, self.now(), cmd.actor, 'done',
                                              dumps({'ok': bad is None, 'duration': dur}), self.now()))
        if bad:
            if policy:                                         # duration policy: shorter edit (or Post) needed
                bkey, kind = 'story_duration:' + it['asset_key'], 'content'
            else:                                              # file defect / platform minimum (R5 A10, LOW-15)
                bkey, kind = 'media:' + it['asset_key'] + ':' + _h(bad), 'editor'
            self.update_item(c, it['item_id'], cmd.actor, 'story source rejected', readiness='blocked',
                             block_kind=kind, block_key=bkey, block_reason=bad, verification_id=None, infra_issue=None)
            self.release(c, self.item(c, it['item_id']), 'story source rejected', keep_request=False)
            self.project(c, it['item_id'])
            self.editor_task(c, it['item_id'], bkey, bad)
            self._finish_check(c, it['item_id'], 'blocked: ' + bad, 'blocked')
            return {'next': 'none', 'blocked': 'story_duration' if policy else 'media', 'duration': dur}
        return {'next': 'continue', 'duration': dur}

    def op_prep_media(self, c, cmd: Command):
        a = cmd.args
        self.run_check(c, 'wf1', a.get('run_id'), a.get('fence'))
        it = self.item(c, cmd.item_id)
        r = a['result']
        if r.get('pending'):
            return {'next': 'none', 'pending': True}
        if r.get('assetKey', it['asset_key']) != it['asset_key'] or r.get('format', it['format']) != it['format']:
            raise Rejected('Media result is for an older file/format; discarded', 'stale')
        recheck = self._check_running(c, it['item_id'])
        out = self._job_failure(c, it, cmd, r, recheck, 'media')
        if out:
            return out
        m = self.media_row(c, r['mediaId'])
        if not m or m['item'] != it['item_id']:
            raise Rejected('Prepared media record not found', 'invalid')
        it = self._infra_clear(c, it, 'media', cmd.actor)
        if (it['block_key'] or '').startswith('prep_exhausted:'):
            it = self.update_item(c, it['item_id'], cmd.actor, 'preparation recovered', readiness='checking',
                                  block_kind=None, block_reason=None, block_key=None)
        info = loads(m['metadata'], {}) or {}
        dv = info.get('delivery') or {}
        if recheck and dv.get('failures'):
            # An explicit recheck gives delivery a fresh, bounded budget (R5 M9).
            dv.update(failures=0, nextAt=0, round=int(dv.get('round') or 0) + 1)
            info['delivery'] = dv
            c.execute('UPDATE media SET metadata=? WHERE id=?', (dumps(info), m['id']))
            if (it['block_key'] or '').startswith('delivery:'):
                it = self.update_item(c, it['item_id'], cmd.actor, 'delivery retried', readiness='checking',
                                      block_kind=None, block_reason=None, block_key=None)
        path = self.delivery_path(it, m['id'])
        if info.get('url'):
            if it['verification_id'] == m['id'] and it['readiness'] == 'ready' and self._needs_delivery_identity(c, it):
                # Verified and delivered before delivered identities were recorded: bind the existing Dropbox copy
                # (best effort, bounded); the item stays ready meanwhile (R5 M17).
                return {'next': 'identify', 'mediaId': m['id'], 'deliveryPath': path}
            return self._verified(c, it, r['mediaId'], cmd.actor)
        if dv.get('failures', 0) >= MAX_DELIVERY_ATTEMPTS:
            return {'next': 'none', 'blocked': 'delivery'}
        if dv.get('nextAt', 0) > self.now():
            return {'next': 'none', 'waiting': 'delivery retry', 'retryAt': dv['nextAt']}
        if dv.get('stage') == 'uploaded' and dv.get('path'):
            # The upload was confirmed earlier; only sharing/registration is repeated (R5 M9).
            return {'next': 'share', 'mediaId': m['id'], 'deliveryPath': dv['path']}
        local = self.local_media(m)
        return {'next': 'upload', 'mediaId': m['id'], 'filePath': str(local or m['path']), 'deliveryPath': path}

    @staticmethod
    def delivery_path(it, media_id) -> str:
        return f"{PREPARED_FOLDER}/{it['item_id']}-{media_id}.mp4"

    def op_prep_delivered(self, c, cmd: Command):
        """Delivery of the verified file to Dropbox (the copy Instagram fetches): a recorded upload (with its Dropbox
        identity), a registered share link, or a failure at one stage (R5 M9, M17)."""
        a = cmd.args
        self.run_check(c, 'wf1', a.get('run_id'), a.get('fence'))
        it = self.item(c, cmd.item_id)
        m = self.media_row(c, a['mediaId'])
        if not m or m['item'] != it['item_id']:
            raise Rejected('Prepared media record not found', 'invalid')
        info = loads(m['metadata'], {}) or {}
        if info.get('assetKey') != it['asset_key'] or info.get('format') != it['format']:
            raise Rejected('Delivered file is for an older version; discarded', 'stale')
        stage = a.get('stage') or 'shared'
        if a.get('error'):
            return self._delivery_failed(c, it, cmd, m, info, stage, str(a['error']), a.get('error_kind') or 'infra')
        if stage in ('uploaded', 'identified'):
            return self._delivery_uploaded(c, it, cmd, m, info, stage, a.get('upload') or {}, a.get('local_hash'))
        url = dropbox_url(a['url'])
        info['url'] = url
        dv = info.pop('delivery', None) or {}
        if dv.get('stage') == 'uploaded':
            info['delivered'] = {k: dv[k] for k in ('id', 'rev', 'content_hash', 'path', 'size', 'at') if k in dv}
        c.execute('UPDATE media SET metadata=? WHERE id=?', (dumps(info), a['mediaId']))
        return self._verified(c, it, a['mediaId'], cmd.actor)

    def _delivery_uploaded(self, c, it, cmd, m, info, stage, up, local_hash):
        for k in ('id', 'rev', 'content_hash', 'path_lower'):
            if not up.get(k):
                raise Rejected('Dropbox upload metadata is incomplete', 'invalid')
        expect = info.get('dropboxHash') or local_hash
        rec = {'id': up['id'], 'rev': up['rev'], 'content_hash': up['content_hash'], 'path': up['path_lower'],
               'size': up.get('size'), 'at': self.now()}
        if stage == 'identified':
            info['identifyAttempts'] = int(info.get('identifyAttempts') or 0) + 1
            if not expect:
                c.execute('UPDATE media SET metadata=? WHERE id=?', (dumps(info), m['id']))
                return {'next': 'none', 'identified': False, 'reason': 'local file hash unavailable'}
            if up['content_hash'] != expect:
                # The copy Instagram would fetch is not the verified file: deliver the verified file again before
                # anything is published (R5 M17).
                for k in ('url', 'delivered', 'delivery'):
                    info.pop(k, None)
                c.execute('UPDATE media SET metadata=? WHERE id=?', (dumps(info), m['id']))
                if it['verification_id'] == m['id']:
                    self.release(c, it, 'delivered copy differs from the verified file')
                    self.update_item(c, it['item_id'], cmd.actor, 'delivered copy differs', verification_id=None,
                                     readiness='checking')
                    self.notify(c, f"delivered-mismatch:{it['item_id']}:{m['id']}",
                                f"{it['name']} ({it['item_id']}): the delivered copy in Dropbox differs from the "
                                'verified file; it will be delivered again before it is scheduled.', it['item_id'])
                return {'next': 'none', 'identified': False, 'mismatch': True}
            info['delivered'] = rec
            info.setdefault('dropboxHash', expect)
            c.execute('UPDATE media SET metadata=? WHERE id=?', (dumps(info), m['id']))
            audit(c, it['item_id'], 'delivered_identity_bound', cmd.actor, {'media': m['id'], 'rev': up['rev']})
            return {'next': 'done', 'identified': True}
        if expect and up['content_hash'] != expect:
            return self._delivery_failed(c, it, cmd, m, info, 'upload', 'The uploaded copy does not match the prepared '
                                         'file (Dropbox content hash differs)', 'infra')
        dv = info.get('delivery') or {}
        info['delivery'] = {**dv, 'stage': 'uploaded', **rec}
        if expect:
            info.setdefault('dropboxHash', expect)
        c.execute('UPDATE media SET metadata=? WHERE id=?', (dumps(info), m['id']))
        return {'next': 'share', 'mediaId': m['id'], 'deliveryPath': up['path_lower']}

    def _delivery_failed(self, c, it, cmd, m, info, stage, err, kind):
        if stage == 'identify':
            # Binding an earlier delivery is best effort: it never blocks the item; after the allowed attempts the
            # delivery keeps its earlier (unbound) status.
            info['identifyAttempts'] = int(info.get('identifyAttempts') or 0) + 1
            c.execute('UPDATE media SET metadata=? WHERE id=?', (dumps(info), m['id']))
            audit(c, it['item_id'], 'delivered_identity_unavailable', cmd.actor, {'media': m['id'], 'error': err[:200]})
            return {'next': 'none', 'identified': False}
        dv = info.get('delivery') or {}
        n = int(dv.get('failures') or 0) + 1
        if stage == 'read':
            # The local prepared file is gone or unreadable: prepare it again. The count survives the new media row
            # (item observation), so a file that can never be read escalates instead of looping.
            n = self._infra(c, it, 'delivery', 'The prepared file could not be read for delivery; it will be '
                            'prepared again (' + err[:150] + ')', cmd.actor)
            if n < MAX_DELIVERY_ATTEMPTS:
                if it['verification_id'] != m['id']:
                    c.execute('DELETE FROM media WHERE id=?', (m['id'],))
                return {'next': 'none', 'reprepare': True}
        dv.update(failures=n, lastStage=stage, lastError=err[:300],
                  nextAt=self.now() + min(INFRA_RECHECK_SECONDS * 2 ** (n - 1), INFRA_MAX_BACKOFF))
        info['delivery'] = dv
        c.execute('UPDATE media SET metadata=? WHERE id=?', (dumps(info), m['id']))
        text = f'Delivery of the prepared file to Dropbox failed at {stage}: {err[:200]}'
        if kind == 'config' or n >= MAX_DELIVERY_ATTEMPTS:
            key = 'delivery:' + m['id']
            self.update_item(c, it['item_id'], cmd.actor, 'delivery escalated', readiness='blocked',
                             block_kind='review', block_key=key,
                             block_reason=(text + f' ({n} attempt(s)). Nothing was published; ask Bondok to recheck it '
                                           'once Dropbox is fixed.')[:500])
            self._infra_clear(c, it, None, cmd.actor)
            self.release(c, self.item(c, it['item_id']), 'delivery escalated')
            self.project(c, it['item_id'])
            self.notify(c, f"delivery-escalated:{it['item_id']}:{m['id']}:{int(dv.get('round') or 0)}",
                        f"{it['name']} ({it['item_id']}): the prepared video could not be delivered to Dropbox "
                        f"({n} attempt(s); last at {stage}: {err[:200]}). Nothing was published. Ask Bondok to "
                        'recheck it once Dropbox is fixed.', it['item_id'])
            self._finish_check(c, it['item_id'], 'escalated: ' + text, 'escalated')
            return {'next': 'none', 'blocked': 'delivery', 'stage': stage}
        self._infra(c, it, 'delivery', text, cmd.actor)
        return {'next': 'none', 'infra': True, 'stage': stage, 'retryAt': dv['nextAt']}

    def _verified(self, c, it, media_id, actor):
        if (it['verification_id'] or None) != media_id:
            c.execute('UPDATE ops_items SET content_rev=content_rev+1 WHERE item_id=?', (it['item_id'],))
        self.update_item(c, it['item_id'], actor, 'verified', verification_id=media_id, infra_issue=None)
        self._infra_clear(c, it, None, actor)
        if (self.item(c, it['item_id'])['block_key'] or '').startswith(('delivery:', 'prep_exhausted:')):
            self.update_item(c, it['item_id'], actor, 'delivery recovered', readiness='checking', block_kind=None,
                             block_reason=None, block_key=None)
        self._finish_check(c, it['item_id'], 'passed', 'passed')
        out = self.evaluate(c, it['item_id'], actor)
        return {'next': 'done', **out}

    def _finish_check(self, c, item_id, outcome, kind=None):
        """A recheck always ends with one typed result (R5 A9): passed | blocked | temporary_failure | escalated."""
        chk = c.execute("SELECT * FROM ops_checks WHERE item_id=? AND kind='media'", (str(item_id),)).fetchone()
        if chk and chk['state'] in ('running', 'requested'):
            kind = kind or ('passed' if outcome == 'passed' else 'blocked' if outcome.startswith('blocked') else 'done')
            c.execute("UPDATE ops_checks SET state='done', result=?, updated=? WHERE item_id=? AND kind='media'",
                      (dumps({'outcome': outcome, 'kind': kind, 'at': self.now()}), self.now(), str(item_id)))
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
        owner = self.selection(c, it)['mode'] == OWNER_SELECTED
        body = (f"Social delivery {it['code']} / {it['format']} — item {item_id}\nIssue: {reason}\n"
                f"Selected file: {it.get('file_name') or 'none'} ({it.get('asset_key') or 'no version'})\n"
                'Requirements: Topaz processed, short edge ≥1080 px, file under 300 MB'
                + ('; Story strictly under 60 seconds' if it['format'] == 'Story' else '') +
                (f"\nThis file was selected by the owner (Dropbox Link on the social board); a corrected file is used "
                 'once the owner selects it there.' if owner else
                 f"\nUpload to: {it.get('folder_url') or 'the project folder'}") +
                '\nAfter upload, confirm Topazed on the social board only when Source asset version matches the file.')
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
