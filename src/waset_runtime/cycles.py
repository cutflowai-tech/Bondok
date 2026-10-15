"""The scheduled work n8n used to run, as plain Python over the same handler calls (agent migration P4).

* ``prepare_cycle`` = WF1 "Prepare & Schedule": import, board observation, file selection, media preparation and
  delivery, caption drafts, editor tasks, file retention. Run lease kind ``wf1`` (the same lease n8n WF1 takes, so the
  two can never run a preparation cycle at the same time).
* ``publish_cycle`` = WF2 "Publish When Due": display synchronization, then due work (reconciliation first,
  publication through claim -> container -> commit -> publication intent -> media_publish -> result -> evidence).
* ``supervise_cycle`` = WF3 "Schedule Supervisor".

The handler decides; this module performs the calls and reports each answer as the workflow did, with the same
request ids, stages and error shapes. Rules that protect publication (lease, fence, commitment point, intent written
on Monday before publishing, unknown outcome reconciled, never re-published blindly) live in the handler and apply
unchanged.
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from waset_ops import Command, media, pipeline, rules
from waset_ops import board as B

from . import selection
from .captions import pick_brief
from .dropbox import DropboxError, raw_link, strip_raw
from .http import HttpError
from .instagram import error_text
from .monday import SOURCE_STATUS, MondayError

LOG = logging.getLogger('waset_runtime')
CHUNK_BYTES = 60_000
CREATE_BATCH = 20
PREPARED_FOLDER = '/Social Media/Prepared'
POLL_SECONDS = 30
MAX_POLLS = 20
SUBITEM_LABEL = {'label': 'Working on it'}


def _txt(it, col):
    return next((str(c.get('text') or '') for c in (it or {}).get('column_values') or [] if c.get('id') == col),
                '').strip()


def _val(it, col):
    c = next((c for c in (it or {}).get('column_values') or [] if c.get('id') == col), None)
    try:
        return json.loads(c.get('value') or 'null') if c else None
    except ValueError:
        return None


def _norm_code(x):
    return ''.join(str(x or '').split()).upper()


def compact(i):
    # Every returned column is kept, empty ones too: dropping empty cells (the n8n payload-size habit) made an empty
    # value indistinguishable from a column missing from the response, which is how empty cells became "cleared
    # by the owner" (owner review 2026-10-15). Absent and empty are different facts.
    return {'id': i['id'], 'name': i.get('name'), 'group': i.get('group') or None,
            'column_values': [{'id': c['id'], 'text': c.get('text') or '', 'value': c.get('value')}
                              for c in i.get('column_values') or []]}


def guard_conflict(guard: dict, current: dict | None) -> tuple[str | None, str | None]:
    """(conflict, read_error) exactly as GUARD_JS: the board must still show a value the system may have put there."""
    if current is None:
        return None, 'item not found on the board'

    def norm(c, kind):
        if not c:
            return None
        if kind == 'long':
            try:
                v = json.loads(c.get('value') or 'null')
                if isinstance(v, dict) and isinstance(v.get('text'), str):
                    return v['text'].strip() or None
            except ValueError:
                pass
        return str(c.get('text') or '').strip() or None
    changed = []
    for cid, g in guard.items():
        cur = norm(current.get(cid), g.get('kind'))
        was = g.get('was')
        ok = [None if v is None else v for v in (was if isinstance(was, list) else [was])]
        if cur not in ok and cur != g.get('new'):
            changed.append(cid)
    return ('conflict: changed on the board by a person (' + ','.join(changed) + ')') if changed else None, None


class Runner:
    def __init__(self, ops, monday, dropbox, instagram=None, captions=None, *, name='bondok', sleep=time.sleep,
                 poll_seconds=POLL_SECONDS, max_polls=MAX_POLLS, publishers=3, maintenance=media.maintenance):
        self.ops, self.monday, self.dropbox, self.ig, self.captions = ops, monday, dropbox, instagram, captions
        self.name, self.sleep, self.poll_seconds, self.max_polls = name, sleep, poll_seconds, max_polls
        self.publishers = publishers
        self.maintenance = maintenance
        self._lock = threading.Lock()
        self._inflight = set()

    def _id(self, kind):
        return f'{self.name}-{kind}-{int(time.time())}-{secrets.token_hex(3)}'

    # ================================================================== WF1
    def prepare_cycle(self, limit=20) -> dict:
        run_id = self._id('wf1')
        lease = self.ops.run_start('wf1', run_id)
        if not lease.get('acquired'):
            return {'deferred': True, **lease}
        fence, step = lease['fence'], 'start'
        out = {'run_id': run_id}
        try:
            step = 'snapshot'
            sources = self.monday.source_snapshot()
            self._social_read_at = self.ops.now()          # decision ordering: the read started here
            social = self.monday.social_snapshot()
            self.ops.run_progress('wf1', run_id, fence, 'import_plan')
            step = 'import'
            out['import'] = self.import_items(sources, social, run_id, fence)
            step = 'observe'
            out['edits'] = self.observe(sources, social, run_id, fence)
            self.ops.run_progress('wf1', run_id, fence, 'board_missing')
            self.ops.board_missing([i['id'] for i in social])
            self.ops.run_progress('wf1', run_id, fence, 'prep_queue')
            step = 'prepare'
            by_id = {str(s['id']): s for s in sources}
            work = self.ops.work_queue(limit=limit)
            out['items'] = [self._prepare_safely(w, by_id.get(str(w.get('source_item_id'))), run_id, fence)
                            for w in work]
            step = 'editor'
            out['editor'] = self.editor_jobs(run_id)
            step = 'maintenance'
            try:
                out['maintenance'] = self.maintenance()
            except Exception as e:  # noqa: BLE001 - retention never fails the cycle (fail=False in WF1)
                out['maintenance'] = {'error': type(e).__name__}
            self.ops.run_finish('wf1', run_id, None, fence=fence)
            return out
        except Exception as e:
            self.ops.run_fail('wf1', run_id, f'{type(e).__name__}: {str(e)[:300]}', step=step, fence=fence)
            raise

    def import_items(self, sources, social, run_id, fence) -> dict:
        counts = {}
        for s in sources:
            c = _norm_code(_txt(s, 'text_mm066x8y'))
            if c:
                counts[c] = counts.get(c, 0) + 1
        src = [{'id': s['id'], 'name': s.get('name'), 'code': _norm_code(_txt(s, 'text_mm066x8y')),
                'format': _txt(s, SOURCE_STATUS), 'duplicate': counts.get(_norm_code(_txt(s, 'text_mm066x8y')), 0) > 1,
                'link': (_val(s, 'link_mm06bswn') or {}).get('url') or None,
                'owners': [{'id': p.get('id'), 'kind': 'team' if p.get('kind') == 'team' else 'person'}
                           for p in ((_val(s, 'project_owner') or {}).get('personsAndTeams') or [])]}
               for s in sources if _txt(s, SOURCE_STATUS) in ('Post', 'Story')]
        soc = [{'id': i['id'], 'source_item': _txt(i, B.COL['source_item']), 'code': _txt(i, B.COL['code']),
                'format': _txt(i, B.COL['format'])} for i in social]
        plan = self.ops.import_plan(src, soc)
        creates = plan.get('creates') or []
        result = {'creates': len(creates), 'pairs': 0, 'failed': 0, 'uncertain': 0}
        for x in range(0, len(creates), CREATE_BATCH):
            batch = creates[x:x + CREATE_BATCH]
            self.ops.run_progress('wf1', run_id, fence, 'import_create')
            pairs, failed, uncertain = self._create_batch(batch)
            self.ops.import_record(pairs, failed=failed, uncertain=uncertain)
            result['pairs'] += len(pairs)
            result['failed'] += len(failed)
            result['uncertain'] += len(uncertain)
        return result

    def _create_batch(self, batch):
        """Record Created Items: created -> pairs; Monday answered without creating -> failed; no usable answer ->
        uncertain (never re-created until a complete snapshot could show it)."""
        names = {str(a['key']): a['name'] for a in batch}
        data, errs, req_fail, req_unsure = None, [], None, None
        try:
            resp = self.monday.create_items(batch)
            data = resp.get('data') if isinstance(resp, dict) and isinstance(resp.get('data'), dict) else None
            errs = (resp or {}).get('errors') or []
            errs = errs if isinstance(errs, list) else [errs]
            if not data:
                req_fail = ('Monday refused the request: ' + '; '.join(str(e.get('message', e))[:300] for e in errs)
                            )[:300] if errs else None
                req_unsure = None if errs else 'empty response'
        except HttpError as e:
            if e.answered and 400 <= e.status < 500 and e.status != 408:
                req_fail = f'Monday refused the request ({e.status}): {str(e)[:300]}'
            else:
                req_unsure = (f'HTTP {e.status}: ' if e.status else '') + str(e)[:300]
        pairs, failed, uncertain = [], [], []
        for a in batch:
            sid, alias = str(a['key']), 's' + str(a['key'])
            v = data.get(alias) if data else None
            name = names.get(sid)
            if v and v.get('id'):
                pairs.append({'source': sid, 'social': str(v['id'])})
            elif req_fail:
                failed.append({'source': sid, 'name': name, 'error': req_fail, 'scope': 'request'})
            elif req_unsure or (data is not None and alias not in data):
                uncertain.append({'source': sid, 'name': name,
                                  'error': req_unsure or 'no result for this item in the response'})
            else:
                own = next((e for e in errs if isinstance(e, dict) and (e.get('path') or [None])[0] == alias), None)
                failed.append({'source': sid, 'name': name, 'scope': 'item' if own else 'request',
                               'error': str((own or (errs[0] if errs else {})).get('message', 'Monday returned no item')
                                            if isinstance(own or (errs[0] if errs else {}), dict) else
                                            'Monday returned no item')[:300]})
        return pairs, failed, uncertain

    def observe(self, sources, social, run_id, fence) -> int:
        canceled = {str(s['id']): {'id': str(s['id']), 'format': 'Canceled'} for s in sources
                    if _txt(s, SOURCE_STATUS) == 'Canceled'}
        chunks, cur, src, size = [], [], {}, 0
        for i in map(compact, social):
            sid = _txt(i, B.COL['source_item'])
            c = canceled.get(sid)
            n = len(json.dumps(i, ensure_ascii=False).encode()) + 1 + \
                (len(json.dumps(c).encode()) + 1 if c else 0)
            if cur and size + n > CHUNK_BYTES:
                chunks.append((cur, list(src.values())))
                cur, src, size = [], {}, 0
            cur.append(i)
            size += n
            if c:
                src[sid] = c
        if cur:
            chunks.append((cur, list(src.values())))
        edits = 0
        for items, srcs in chunks:
            self.ops.run_progress('wf1', run_id, fence, 'observe')
            r = self.ops.observe(items, sources=srcs, actor='service:wf1', read_at=getattr(self, '_social_read_at', None))
            edits += len(r.get('edits') or [])
        return edits

    # ------------------------------------------------------------------ one item
    def _prepare_safely(self, w, source, run_id, fence) -> dict:
        """One item's failure never stops the others (n8n's per-item error branches). A superseded run stops."""
        try:
            return self.prepare_item(w, source, run_id, fence)
        except Exception as e:  # noqa: BLE001
            if getattr(e, 'code', None) == 'fenced':
                raise
            LOG.error('prepare_failed item=%s type=%s detail=%s', w.get('item_id'), type(e).__name__, str(e)[:200])
            return {'item_id': str(w.get('item_id')), 'error': type(e).__name__}

    def prepare_item(self, w, source, run_id, fence) -> dict:
        ctx = selection.context(w, source)
        item = str(w['item_id'])
        if w.get('needs_caption') and source:
            try:
                self.caption(ctx, source)
            except Exception as e:  # noqa: BLE001 - captions never stop preparation (WF1 continues)
                LOG.warning('caption_failed item=%s type=%s', item, type(e).__name__)
        base = {'requestId': f'{run_id}:{item}', 'itemId': item, 'runId': run_id, 'fence': fence,
                'expectedFormat': w.get('format'), 'folderUrl': None,
                'selection': {'mode': w.get('selection_mode') or 'automatic_folder_selection',
                              'url': w.get('selected_url') or None}}
        try:
            body = {**base, **self._select(ctx, base)}
        except selection.SelectionProblem as e:
            body = {**base, 'error': str(e), 'errorKind': e.kind}
        except DropboxError as e:
            body = {**base, 'error': str(e), 'errorKind': e.kind}
        r = pipeline.prep_step(self.ops, body)
        if r.get('state') == 'completed' and r.get('next') in ('upload', 'share', 'identify'):
            r = {**r, 'delivery': self.deliver(item, r, run_id, fence)}
        return {'item_id': item, 'state': r.get('state'), 'next': r.get('next')}

    def _select(self, ctx, base) -> dict:
        if ctx['ownerSelected']:
            link = self.dropbox.link_metadata(ctx['selectedUrl'])
            if link.get('.tag') != 'file' or not link.get('path_lower'):
                selection.owner_file(link, {})              # raises the exact owner-facing problem
            meta = self.dropbox.metadata(link['path_lower'])
            f = selection.owner_file(link, meta)
            return {'file': {k: f[k] for k in ('id', 'rev', 'content_hash', 'name')}, 'url': ctx['selectedUrl']}
        folder = ctx['folderUrl']
        if not folder:
            if ctx.get('code_valid') is False:
                return {'error': selection.MISSING_CODE, 'errorKind': 'config'}
            path = selection.new_folder_path(ctx)
            self.dropbox.ensure_folder(path)
            folder = self.dropbox.share(path)
            base['folderUrl'] = folder
        plan = selection.plan_listing(self.dropbox.link_metadata(folder))
        entries = self.dropbox.list_folder(plan['list']['path'], recursive=plan['list']['recursive'])
        f = selection.candidates(ctx.get('name'), plan, entries)['file']
        links = self.dropbox.shared_links(f['path'])
        url = links[0]['url'] if links else self.dropbox.share(f['path'])
        return {'folderUrl': base['folderUrl'], 'file': {k: f[k] for k in ('id', 'rev', 'content_hash', 'name')},
                'url': url}

    def deliver(self, item, r, run_id, fence) -> dict:
        """Delivery of the verified file (the copy Instagram fetches); every stage reports to the handler."""
        def record(stage, **kw):
            rid = f'{run_id}:{item}:' + ('delivered' if stage == 'shared' else stage if stage in
                                         ('uploaded', 'identified') else 'delivery-failed:' + stage)
            b = {'requestId': rid, 'itemId': item, 'runId': run_id, 'fence': fence, 'mediaId': r['mediaId'],
                 'stage': stage, **kw}
            return self.ops.submit(Command(rid, 'prep_delivered', 'service:wf1', 'service:wf1', item,
                                           pipeline.delivered_args(self.ops, b)))
        stage = 'folder'
        nxt = r['next']
        try:
            path = r.get('deliveryPath')
            if nxt in ('upload', 'identify'):
                if nxt == 'upload':
                    self.dropbox.ensure_folder(PREPARED_FOLDER)
                    stage = 'read'
                    with open(r['filePath'], 'rb'):
                        pass
                    stage = 'upload'
                    meta = self.dropbox.upload(r['filePath'], path)
                else:
                    stage = 'identify'
                    meta = self.dropbox.metadata(path)
                stage = 'record'
                rec = record('uploaded' if nxt == 'upload' else 'identified',
                             upload={k: meta.get(k) for k in ('id', 'rev', 'content_hash', 'path_lower', 'size')})
                if not (rec.get('state') == 'completed' and rec.get('next') == 'share'):
                    return {'stage': 'recorded', 'next': rec.get('next'), 'state': rec.get('state')}
                path = rec.get('deliveryPath') or path
            stage = 'share'
            if not path:
                raise DropboxError('Delivery path missing', kind='infra')
            url = raw_link(self.dropbox.share(path))
            stage = 'register'
            done = record('shared', url=url)
            return {'stage': 'shared', 'state': done.get('state'), 'next': done.get('next')}
        except DropboxError as e:
            return {'stage': stage, 'failed': record(stage, error=str(e), errorKind=e.kind).get('state')}
        except OSError as e:
            kind = 'read' if stage in ('read', 'upload') and isinstance(e, FileNotFoundError) else stage
            return {'stage': kind, 'failed': record(kind, error=f'{type(e).__name__}: {e}'[:300],
                                                    errorKind='infra').get('state')}

    def caption(self, ctx, source):
        if not self.captions:
            return None
        project = self.monday.updates(source['id'])
        b = pick_brief(project, ctx.get('name'))
        need = self.ops.caption_needed(ctx['itemId'], b['title'], b['brief'])
        if not need.get('needed'):
            return need
        text, error = self.captions(b['title'], b['brief'])
        ih = need['input_hash']
        return self.ops.submit(Command('draft:' + ih, 'caption_draft', 'service:wf1', 'service:wf1', ctx['itemId'],
                                       {'input_hash': ih, 'text': text, 'model': getattr(self.captions, 'model', None),
                                        'error': error}))

    # ------------------------------------------------------------------ editor subitems
    def editor_jobs(self, worker, limit=10) -> int:
        n = 0
        for j in self.ops.outbox_take(['editor'], worker, limit):
            p = j['payload']
            n += 1
            if not p.get('source_item_id'):
                self.ops.outbox_ack(j['id'], worker, True, None, {'task_id': None, 'skipped': 'no projects item'})
                continue
            task = None
            try:
                subs = self.monday.subitems(p['source_item_id'])
                prefix = f"Social {p['item_id']} — "
                ids = {str(s['id']) for s in subs}
                stored = str(p['task_id']) if p.get('task_id') and str(p['task_id']) in ids else None
                existing = stored or next((str(s['id']) for s in subs if str(s.get('name') or '').startswith(prefix)),
                                          None)
                if existing and self._already_told(subs, existing, p):
                    self.ops.outbox_ack(j['id'], worker, True, None, {'task_id': existing})
                    continue
                if existing:
                    self.monday.change_column(rules.EDITOR_SUBITEM_BOARD, existing, 'status', SUBITEM_LABEL)
                    task = existing
                else:
                    task = self.monday.create_subitem(p['source_item_id'], p['task_name'], {'status': SUBITEM_LABEL})
                self.monday.create_update(task, p['body'])
                self.ops.outbox_ack(j['id'], worker, True, None, {'task_id': task})
            except Exception as e:  # noqa: BLE001 - one editor job never stops the others
                self.ops.outbox_ack(j['id'], worker, False, f'{type(e).__name__}: {str(e)[:400]}', {'task_id': task})
        return n

    @staticmethod
    def _already_told(subs, existing, p) -> bool:
        import re
        ups = next((s.get('updates') or [] for s in subs if str(s['id']) == existing), [])
        m = re.match(r'^(topaz|story_duration|media):(id:[^@\s]+@[0-9a-f]+)', str(p.get('issue_key') or ''))
        kind, asset = (m[1], m[2]) if m else (None, None)

        def norm(t):
            return ' '.join(str(t or '').split())

        def reason(t):
            r = re.search(r'(?:reason|Issue):\s*([^\n]*)', str(t or ''))
            return r[1] if r else ''

        def same_kind(t):
            if kind == 'topaz':
                return bool(re.search('topaz', reason(t), re.I))
            if kind == 'story_duration':
                return not re.search('topaz', reason(t), re.I) and bool(
                    re.search('مدة الستوري|duration|ثانية|seconds', reason(t), re.I))
            return False
        return any(norm(u.get('text_body')) == norm(p['body']) or
                   (asset and asset in str(u.get('text_body') or '') and same_kind(u.get('text_body'))) for u in ups)

    # ================================================================== WF2
    def sync_cycle(self) -> dict:
        worker = self._id('sync')
        out = self.display_sync(worker)
        try:
            out['evidence'] = self.evidence_jobs(worker)
        except Exception as e:  # noqa: BLE001 - evidence checks never stop the display sync
            LOG.warning('evidence_failed type=%s detail=%s', type(e).__name__, str(e)[:160])
        return out

    def evidence_jobs(self, worker, limit=10) -> int:
        """Board values held because they would erase a decision: look for the edit in Monday's activity log."""
        n = 0
        for j in self.ops.outbox_take(['board_evidence'], worker, limit):
            p = j['payload']
            cols = p.get('columns') or []
            try:
                events = [e for e in self.monday.activity([p['item_id']], cols, float(p.get('since') or 0) - 60)
                          if e.get('column_id') in cols]
            except (MondayError, HttpError) as e:
                self.ops.outbox_ack(j['id'], worker, False, f'activity log unavailable: {str(e)[:200]}')
                continue
            self.ops.resolve_board_evidence(j, events)
            n += 1
        return n

    def publish_cycle(self, pool=None, sync=True) -> dict:
        """Display sync (unless the scheduler runs it on its own timer), reconciliation, then the due items. With
        ``pool`` (the scheduler's executor) each due item is handed off and the cycle returns at once, so a container
        that takes minutes never delays the next minute's work (one n8n execution per minute did the same); the
        claim keeps two runs from taking one item."""
        worker = self._id('wf2')
        out = {'sync': self.display_sync(worker)} if sync else {}
        if self.ig is None:
            return {**out, 'publishing': 'no Instagram client configured'}
        due = self.ops.due(worker, 5)
        if due.get('restore_gap'):
            try:
                out['restore_gap'] = self.ops.restore_gap_check(self.ig.media(50))
            except HttpError as e:
                out['restore_gap'] = {'open': True, 'error': error_text(e)[:200]}
        if due.get('probe'):
            ok, err = self.ig.probe()
            self.ops.provider_probe(ok, err)
        work = due.get('work') or []
        out['reconciled'] = [self.reconcile(w) for w in work if w.get('kind') == 'reconcile']
        pubs = [w for w in work if w.get('kind') != 'reconcile']
        if pubs and pool is not None:
            with self._lock:
                new = [w for w in pubs if str(w['item_id']) not in self._inflight]
                self._inflight.update(str(w['item_id']) for w in new)
            for w in new:
                pool.submit(self._publish_tracked, w)
            out['dispatched'] = [w['item_id'] for w in new]
        elif pubs:
            # One worker identity per item (as one n8n execution per item); several due items in parallel so one
            # container's processing never delays another slot.
            with ThreadPoolExecutor(max_workers=max(1, min(self.publishers, len(pubs)))) as pool:
                out['published'] = list(pool.map(self._publish_safely, pubs))
        return out

    def _publish_tracked(self, w):
        try:
            return self._publish_safely(w)
        finally:
            with self._lock:
                self._inflight.discard(str(w['item_id']))

    def _publish_safely(self, w):
        """A crash of one item's publication never stops the others. Whatever stage it reached is in the store: a
        committed attempt without a result becomes 'outcome unknown' when its lease ends and is reconciled."""
        try:
            return self.publish_one(w, self._id('wf2'))
        except Exception as e:  # noqa: BLE001
            LOG.error('publish_failed item=%s type=%s', w.get('item_id'), type(e).__name__)
            return {'item_id': w.get('item_id'), 'error': type(e).__name__}

    def display_sync(self, worker, limit=10) -> dict:
        stats = {'taken': 0, 'applied': 0, 'conflicts': 0, 'failed': 0}
        for j in self.ops.outbox_take(['monday', 'source_monday'], worker, limit):
            stats['taken'] += 1
            p = j['payload']
            try:
                if j['kind'] == 'source_monday':
                    self.monday.change_column(rules.SOURCE_BOARD, p['source_item_id'], SOURCE_STATUS,
                                              {'label': p['label']})
                    self.ops.outbox_ack(j['id'], worker, True)
                    stats['applied'] += 1
                    continue
                guard = p.get('guard') or None
                if guard:
                    try:
                        cur = self.monday.guard_values(p['item_id'], list(guard))
                    except MondayError as e:
                        self.ops.outbox_ack(j['id'], worker, False, 'read before sync failed: ' + str(e)[:300])
                        stats['conflicts'] += 1
                        continue
                    conflict, read_error = guard_conflict(guard, cur)
                    if conflict or read_error:
                        self.ops.outbox_ack(j['id'], worker, False, conflict or 'read before sync failed: ' + read_error)
                        stats['conflicts'] += 1
                        continue
                self.monday.change_columns(p['item_id'], p.get('columns') or {}, group=p.get('group'))
                self.ops.outbox_ack(j['id'], worker, True)
                stats['applied'] += 1
            except (MondayError, HttpError) as e:
                self.ops.outbox_ack(j['id'], worker, False, str(e)[:500])
                stats['failed'] += 1
        return stats

    def reconcile(self, w) -> dict:
        try:
            st = self.ig.container_status(w['container_id'])
            return self.ops.reconcile(w['attempt_id'], st.get('status_code'), None)
        except HttpError as e:
            return self.ops.reconcile(w['attempt_id'], None, error_text(e)[:200])

    def publish_one(self, w, worker) -> dict:
        item = str(w['item_id'])
        fresh, status = None, None
        read_at = self.ops.now()
        try:
            fresh = self.monday.fresh_item(item)
            status = self.monday.source_status(_txt(fresh, B.COL['source_item']))
        except (MondayError, HttpError) as e:
            stage = 'read_item' if fresh is None else 'read_source'
            if fresh is None and not self.ops.board_fresh(item)['fresh']:
                # Monday cannot be read and Bondok's last board state of the item is older than the owner's
                # freshness rule: nothing is started (no container); retried next minute inside the window.
                return self.ops.read_failure(item, stage, str(e)[:600], getattr(e, 'status', None))
            # Within the freshness rule: what could not be read is taken from the last state received.
        try:
            if fresh is not None:
                self.ops.observe([{k: fresh[k] for k in ('id', 'name', 'column_values', 'group') if k in fresh}],
                                 actor='service:wf2', read_at=read_at)
            cl = self.ops.claim(item, worker, source_status=status)
        except Exception as e:  # noqa: BLE001 - WF2 'claim' read failure
            return self.ops.read_failure(item, 'claim', f'{type(e).__name__}: {str(e)[:500]}', None)
        if not cl.get('claimed'):
            return {'item_id': item, 'claimed': False, 'reason': cl.get('reason')}
        aid, fence, payload = cl['attempt_id'], cl['fence'], cl['payload']

        def fail(stage, e=None, status_code=None, error=None):
            return self.ops.precommit_failure(aid, worker, fence, stage, error=error or (error_text(e) if isinstance(
                e, HttpError) else str(e)[:600] if e else None), http_status=getattr(e, 'status', None),
                status_code=status_code)

        cid = cl.get('container_id')
        if not cid:
            try:
                cid = self.ig.create_container(payload)
            except HttpError as e:
                return fail('create_container', e)
            try:
                self.ops.container(aid, worker, fence, cid)
            except Exception as e:  # noqa: BLE001
                return fail('save_container', e)
        status_code, polls = None, 0
        while True:
            polls += 1
            self.sleep(self.poll_seconds)
            try:
                self.ops.renew(aid, worker, fence)
            except Exception as e:  # noqa: BLE001
                return fail('renew_lease', e)
            try:
                st = self.ig.container_status(cid)
            except HttpError as e:
                return fail('check_container', e)
            status_code = st.get('status_code')
            if status_code == 'FINISHED':
                break
            if status_code == 'IN_PROGRESS' and polls < self.max_polls:
                continue
            return fail('container_status', status_code=status_code or 'unknown',
                        error=str(st.get('status') or f'Container status {status_code} after {polls} polls '
                                  f'({self.poll_seconds} s each)')[:600])
        source_asset = self._asset_of(lambda: self.dropbox.metadata(str(cl['source_asset']).split('@')[0]))
        delivered = self._asset_of(lambda: self.dropbox.link_metadata(strip_raw(payload['video_url'])), rev_only=True)
        # The owner's latest board decisions (a Pause typed while the container was processing) are read just before
        # the commitment point. Reading, not writing: a Monday that cannot be read is judged by the freshness rule.
        try:
            read_at = self.ops.now()
            again = self.monday.fresh_item(item)
            self.ops.observe([{k: again[k] for k in ('id', 'name', 'column_values', 'group') if k in again}],
                             actor='service:wf2', read_at=read_at)
        except (MondayError, HttpError) as e:
            LOG.warning('precommit_read_failed item=%s detail=%s', item, str(e)[:120])
        try:
            c = self.ops.commit(aid, worker, fence, source_asset=source_asset, container_status=status_code,
                                delivered_asset=delivered, record='store')
        except Exception as e:  # noqa: BLE001
            return fail('commit_call', e)
        if not c.get('committed'):
            return {'item_id': item, 'committed': False, 'reasons': c.get('reasons')}
        # ---- past the commitment point: whatever happens is recorded, nothing is retried blindly
        try:
            st, body = self.ig.publish(cid)
            media_id = body.get('id') if st == 200 and isinstance(body, dict) else None
            definitive = not media_id and 400 <= st < 500 and isinstance(body, dict) and bool(body.get('error'))
            error = None if media_id else (json.dumps(body.get('error'))[:600] if isinstance(body, dict) and
                                           body.get('error') else 'no media id returned')
            res = self.ops.result(aid, worker, media_id=media_id, error=error, http_status=st, definitive=definitive)
        except HttpError as e:
            res = self.ops.result(aid, worker, media_id=None, error=str(e)[:600], http_status=None, definitive=False)
        if res.get('stage') == 'published' and res.get('media_id'):
            self.ops.evidence(aid, self.ig.permalink(res['media_id']))
        return {'item_id': item, 'attempt_id': aid, 'stage': res.get('stage'), 'media_id': res.get('media_id')}

    @staticmethod
    def _asset_of(read, rev_only=False) -> str:
        try:
            m = read()
        except (DropboxError, HttpError):
            return 'unverifiable'
        if rev_only:
            return (str(m.get('id') or '') + '@' + m['rev']) if m and m.get('rev') else 'unverifiable'
        return f"{m['id']}@{m['rev']}" if m and m.get('id') and m.get('rev') else 'unverifiable'

    # ================================================================== WF3
    def supervise_cycle(self) -> dict:
        rid = self._id('wf3')
        lease = self.ops.run_start('wf3', rid)
        if not lease.get('acquired'):
            return {'deferred': True, **lease}
        try:
            out = self.ops.repair(rid, lease['fence'])
        except Exception as e:
            self.ops.run_fail('wf3', rid, f'{type(e).__name__}: {str(e)[:300]}', step='repair', fence=lease['fence'])
            raise
        self.ops.run_finish('wf3', rid, {'findings': out.get('findings'), 'repairs': len(out.get('repairs') or [])},
                            fence=lease['fence'])
        return out
