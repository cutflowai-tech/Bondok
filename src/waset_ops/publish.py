"""Publication lifecycle. WF2 is the only component that calls Instagram.

Commitment point: ``commit()`` atomically re-validates lease, fence, content
revision, payload fingerprint, owner state, hold, reservation and due window
and moves the attempt to ``committed`` *before* WF2 sends media_publish.
* A pause/change committed before that point wins: commit is refused.
* After that point a pause is recorded but reported as "may already be in
  progress"; the outcome is reconciled, never assumed.
Exactly-once publication across SQLite, Monday and Instagram is NOT claimed.
"""
from __future__ import annotations

import json
import secrets

from . import rules
from .core import ACTIVE_ATTEMPT, OWNER, PRE_COMMIT, Command, Rejected, fingerprint
from .db import audit, dumps, loads, next_counter

LEASE = 180                    # DEFAULT: deployed publish lease (seconds)
COMMIT_LEASE = 900             # time allowed to deliver the result after commit
RECONCILE_INTERVALS = (300, 600, 900, 1800, 3600, 7200)
MAX_RECONCILE_CHECKS = 12
MAX_TRANSIENT_RETRIES = 3
MAX_ATTEMPTS_PER_SLOT = 3      # each claim creates an Instagram container; stop retrying a failing slot


# Meta Graph API throttling codes: the request was refused before processing, so nothing was posted. Generic
# "unknown/temporary" codes (1, 2) do not prove that and stay a normal failure (round-2 review #8).
TRANSIENT_CODES = {4, 17, 32, 613}


def _transient_provider_error(error) -> bool:
    try:
        e = json.loads(error) if isinstance(error, str) else (error or {})
    except ValueError:
        return False
    e = e.get('error', e) if isinstance(e, dict) else {}
    return isinstance(e, dict) and e.get('code') in TRANSIENT_CODES


def _media_not_ready(error) -> bool:
    """9007 / 2207027: Instagram refused media_publish because the container was not ready yet. The request was
    explicitly refused (nothing posted), so publishing the same slot again is safe."""
    try:
        e = json.loads(error) if isinstance(error, str) else (error or {})
    except ValueError:
        return False
    e = e.get('error', e) if isinstance(e, dict) else {}
    return isinstance(e, dict) and e.get('code') == 9007 and e.get('error_subcode') in (2207027, None)


# Provider error classes (Meta Instagram Graph API error reference, read 2026-10-10). A class decides what is known
# about the request: refused (nothing published) or ambiguous (reconcile before anything else).
AUTH_CODES = {190, 102, 10, 200}          # invalid/expired token, missing permission (Graph API OAuth/permission)
RESTRICTED_CODES = {25}                   # 2207050: account restricted, owner must act in the Instagram app
THROTTLE_CODES = {4, 17, 32, 613}         # request limits: refused before processing
DAILY_LIMIT_CODES = {9}                   # 2207042: daily publishing limit
CONTAINER_GONE_SUBCODES = {2207008, 2207020}
CAPTION_SUBCODES = {2207040, 2207010}    # too many @ tags / caption too long: the owner's caption, not the video
CONTENT_SUBCODES = {2207026, 2207023, 2207004, 2207005, 2207009, 2207057, 2207028, 2207035, 2207036, 2207037, 2207032}
CONTENT_CODES = {352, 36000, 36001, 36003}
FETCH_CODES = {9004}                      # 2207052: media URI could not be fetched
REFUSED_CLASSES = ('auth', 'restricted', 'throttle', 'daily_limit', 'container_gone', 'content', 'caption', 'fetch',
                   'rejected')
BREAKER_PROBE_SECONDS = 15 * 60
MAX_FAILED_SLOTS = 2                      # Dropbox-unverifiable slots before the owner is asked (R5 A2)


def provider_error(error) -> dict:
    """{'code', 'subcode', 'message', 'type'} from a Graph API error body (JSON text, dict or container status)."""
    import re
    if isinstance(error, dict):
        e = error.get('error', error)
    else:
        try:
            e = json.loads(error) if error else {}
            e = e.get('error', e) if isinstance(e, dict) else {}
        except (ValueError, TypeError):
            e = {'message': str(error or '')}
    if not isinstance(e, dict):
        e = {'message': str(e)}
    msg = str(e.get('message') or e.get('error_user_msg') or '')
    sub = e.get('error_subcode')
    if sub is None:
        m = re.search(r'\b(2207\d{3})\b', msg)
        sub = int(m[1]) if m else None
    code = e.get('code')
    try:
        code = int(code) if code is not None else None
        sub = int(sub) if sub is not None else None
    except (TypeError, ValueError):
        code = sub = None
    return {'code': code, 'subcode': sub, 'message': msg[:300], 'type': e.get('type')}


def classify(error, http_status=None, status_code=None) -> str:
    """Provider failure class. Generic/unknown codes (1, 2, -1, -2), 5xx, timeouts and a missing response are
    'transient' before the commitment point and ambiguous after it (R5 B8)."""
    e = provider_error(error)
    code, sub = e['code'], e['subcode']
    if code == 9007 or sub == 2207027:
        return 'not_ready'
    if code in AUTH_CODES or e.get('type') == 'OAuthException':
        return 'auth'
    if code in RESTRICTED_CODES or sub == 2207050:
        return 'restricted'
    if code in THROTTLE_CODES:
        return 'throttle'
    if code in DAILY_LIMIT_CODES or sub == 2207042:
        return 'daily_limit'
    if sub in CONTAINER_GONE_SUBCODES or status_code == 'EXPIRED':
        return 'container_gone'
    if code in FETCH_CODES or sub == 2207052:
        return 'fetch'
    if sub in CAPTION_SUBCODES or code == 36004:
        return 'caption'
    if sub in CONTENT_SUBCODES or code in CONTENT_CODES:
        return 'content'
    if code == 100:
        return 'rejected'                 # invalid parameter without a known subcode: refused, owner/config action
    return 'transient'


def summary(error) -> str:
    e = provider_error(error)
    tag = '/'.join(str(x) for x in (e['code'], e['subcode']) if x is not None)
    return (e['message'] or 'no error message') + (f' (Instagram {tag})' if tag else '')


class PublishMixin:
    # ------------------------------------------------------------------ account circuit breaker (R5 A2)
    def breaker(self, c) -> dict | None:
        r = c.execute("SELECT value FROM ops_meta WHERE key='breaker:instagram'").fetchone()
        b = loads(r['value'], None) if r else None
        if b and b.get('until') and b['until'] <= self.now() and b['kind'] in ('throttle', 'daily_limit'):
            return None                   # documented rate-limit windows recover by time
        return b

    def _open_breaker(self, c, kind, cause, until=None):
        if self.breaker(c):
            return False
        b = {'kind': kind, 'cause': cause[:300], 'since': self.now(), 'until': until, 'last_probe': self.now()}
        c.execute("INSERT OR REPLACE INTO ops_meta VALUES('breaker:instagram',?)", (dumps(b),))
        what = {'auth': 'Instagram access failed (token or permission)',
                'restricted': 'The Instagram account is restricted',
                'throttle': 'Instagram is limiting requests',
                'daily_limit': 'The daily Instagram publishing limit was reached'}.get(kind, 'Instagram refused requests')
        after = ('Publishing resumes automatically after ' + rules.display(rules.instant(self.iso_ts(until)))
                 if until else 'Publishing is paused until Instagram access works again (checked every 15 minutes)')
        self.notify(c, f"breaker:{kind}:{int(b['since'])}", f'⚠️ {what}: {cause[:300]}. Nothing was published. {after}; '
                    'scheduled items wait and are rescheduled when it recovers.')
        audit(c, None, 'breaker_open', 'service:wf2', b)
        return True

    def provider_probe(self, ok: bool, error=None) -> dict:
        """WF2's cheap provider read while the breaker is open. Recovery needs demonstrated access, not time."""
        with self.store.tx() as c:
            b = self.breaker(c)
            if not b:
                return {'open': False}
            if ok:
                c.execute("DELETE FROM ops_meta WHERE key='breaker:instagram'")
                self.notify(c, f"breaker-closed:{int(b['since'])}", '✅ Instagram access is working again; publishing '
                            'continues and waiting items are scheduled.')
                audit(c, None, 'breaker_closed', 'service:wf2', b)
                return {'open': False, 'closed': True}
            b['last_probe'] = self.now()
            b['last_probe_error'] = summary(error)[:200] if error else None
            c.execute("UPDATE ops_meta SET value=? WHERE key='breaker:instagram'", (dumps(b),))
            return {'open': True}

    def read_failure(self, item_id, stage, error=None, http_status=None) -> dict:
        """A read before the claim failed (Monday item/source status, or the claim call): nothing was started.
        Recorded once per item and cause per hour; WF2 tries again next minute without creating containers."""
        with self.store.tx() as c:
            text = summary(error) if error else 'no response'
            new = self.finding(c, f'wf2-read:{item_id}:{stage}:{int(self.now() // 3600)}', str(item_id), 'publish_read',
                               f'Publisher could not {stage.replace("_", " ")} for item {item_id} '
                               f'(HTTP {http_status}): {text[:200]}. Nothing was published; it retries every minute.',
                               notify=False)
            audit(c, item_id, 'publish_read_failed', 'service:wf2', {'stage': stage, 'http_status': http_status})
            return {'recorded': True, 'new': new}

    def due(self, worker: str, limit=5) -> dict:
        """Durable due work. Empty queue -> empty list (no Monday call needed)."""
        now, now_dt = self.now(), self.now_dt()
        work = []
        with self.store.tx() as c:
            c.execute('INSERT OR REPLACE INTO ops_heartbeat VALUES(?,?,?)', ('wf2', now, dumps({'worker': worker})))
            # Worker died after crossing the commitment point: outcome unknown.
            for a in c.execute("SELECT * FROM ops_attempts WHERE stage='committed' AND lease_until<?", (now,)).fetchall():
                self._unknown(c, dict(a), 'Publisher stopped after sending the publish request; no result recorded')
            # Pre-commit attempts whose slot window passed are abandoned (never published).
            for a in c.execute(f"SELECT * FROM ops_attempts WHERE stage IN ('claimed','container_created') "
                               'AND lease_until<?', (now,)).fetchall():
                res = self.reservation(c, a['item_id'])
                end = self.deadline(c, res) if res and res['slot'] == a['slot'] else \
                    rules.instant(a['slot']) + rules.LATE_WINDOW
                if end < now_dt:
                    c.execute("UPDATE ops_attempts SET stage='abandoned', updated=? WHERE id=?", (now, a['id']))
                    audit(c, a['item_id'], 'attempt_abandoned', worker, {'attempt': a['id'], 'why': 'window passed'})
                    self.evaluate(c, a['item_id'], worker)     # a ready item gets its next slot (audit P4)
                    self.project(c, a['item_id'])
            rows = c.execute('SELECT r.*, i.publication, i.owner_state FROM ops_reservations r JOIN ops_items i '
                             'USING(item_id) WHERE r.slot<=? ORDER BY r.slot', (rules.iso(now_dt),)).fetchall()
            for r in rows:
                if r['publication'] != 'not_started' or self.deadline(c, r) < now_dt:
                    continue
                att = self.active_attempt(c, r['item_id'])
                if att and (att['stage'] not in PRE_COMMIT or (att['lease_until'] or 0) > now):
                    continue
                work.append({'kind': 'publish', 'item_id': r['item_id'], 'slot': r['slot']})
            for a in c.execute("SELECT * FROM ops_attempts WHERE stage IN ('outcome_unknown','owner_unpublished') AND "
                               'container_id IS NOT NULL AND checks<? AND (next_check IS NULL OR next_check<=?)',
                               (MAX_RECONCILE_CHECKS, now)).fetchall():
                work.insert(0, {'kind': 'reconcile', 'item_id': a['item_id'], 'attempt_id': a['id'],
                                'container_id': a['container_id']})
            b = self.breaker(c)
            probe = False
            if b:
                # Account-level failure: no new containers for anyone; reconciliation continues (R5 A2).
                work = [w for w in work if w['kind'] != 'publish']
                probe = self.now() - (b.get('last_probe') or 0) >= BREAKER_PROBE_SECONDS
        out = {'work': work[:limit], 'more': len(work) > limit}
        if b:
            out.update(probe=probe, breaker=b['kind'])
        return out

    # ------------------------------------------------------------------ claim
    def claim(self, item_id, worker, *, source_status=None) -> dict:
        now, now_dt = self.now(), self.now_dt()
        with self.store.tx() as c:
            it = self.item(c, item_id)
            res = self.reservation(c, item_id)
            if source_status == 'Canceled':
                # Same outcome WF1 records later; releasing now stops WF2 re-reading Monday every minute and WF3
                # moving the item to the next slot (audit, publishing LOW). A pre-commit attempt is ended too: nothing
                # was published (R5 LOW-11).
                att = self.active_attempt(c, item_id)
                if att and att['stage'] in PRE_COMMIT:
                    c.execute("UPDATE ops_attempts SET stage='abandoned', evidence=?, updated=? WHERE id=?",
                              (dumps({'abandoned': 'source project canceled', 'class': 'canceled'}), now, att['id']))
                if not self.active_attempt(c, item_id):
                    self.release(c, it, 'source canceled', keep_request=False)
                    self.update_item(c, it['item_id'], worker, 'source canceled', owner_state='skipped',
                                     owner_state_reason='Canceled in Customer Projects')
                return self._refuse(c, it, 'Source project was canceled', notify=True)
            if not res:
                return self._refuse(c, it, 'No confirmed reservation', quiet=True)
            legacy = c.execute("SELECT stage FROM publications WHERE item=? AND owner NOT LIKE 'ops:%'",
                               (str(item_id),)).fetchone()
            if legacy and not (loads(it['observed'], {}) or {}).get('_v1_receipt_resolved'):
                # A v1 publication receipt (any stage): the item may already be on Instagram (audit P7).
                reason = (f"A v1 publication record exists for this item (stage {legacy['stage']}). Publication is "
                          'held; confirm in Slack whether it was posted.')
                self.release(c, it, 'v1 publication receipt')
                self.update_item(c, it['item_id'], worker, 'v1 receipt hold',
                                 hold=dumps({'kind': 'external_posted', 'reason': reason}))
                self.notify(c, f"v1-receipt:{it['item_id']}", f"Held {it['name']} ({it['item_id']}): {reason}",
                            it['item_id'])
                return {'claimed': False, 'reason': reason, 'held': True}
            stale = self.active_attempt(c, item_id)
            if stale and stale['stage'] == 'claimed' and not stale['container_id'] and (stale['lease_until'] or 0) < now:
                # The previous worker stopped (or its error output was lost) before saving a container: that was a
                # failed try and counts towards the slot's limit instead of silently creating another (R5 A2).
                c.execute("UPDATE ops_attempts SET stage='abandoned', evidence=?, updated=? WHERE id=?",
                          (dumps({'abandoned': 'worker stopped before an Instagram container was recorded',
                                  'class': 'transient', 'stage': 'create_container'}), now, stale['id']))
                audit(c, item_id, 'attempt_abandoned', worker, {'attempt': stale['id'], 'why': 'no container saved'})
            tries = c.execute("SELECT COUNT(*) FROM ops_attempts WHERE item_id=? AND slot=? AND stage='abandoned'",
                              (str(item_id), res['slot'])).fetchone()[0]
            if tries >= MAX_ATTEMPTS_PER_SLOT and not self.active_attempt(c, item_id) and \
                    all('could not be checked' in (r['evidence'] or '') for r in c.execute(
                        "SELECT evidence FROM ops_attempts WHERE item_id=? AND slot=? AND stage='abandoned'",
                        (str(item_id), res['slot']))):
                # Only Dropbox was unreadable at the commit point: nothing is wrong with the item; stop creating
                # containers for this slot and take the next one without asking the owner (round-2 review #10).
                self.release(c, it, 'source unverifiable at publication time')
                obs = loads(self.item(c, item_id)['observed'], {}) or {}
                obs['_unverifiable_slots'] = obs.get('_unverifiable_slots', 0) + 1
                c.execute('UPDATE ops_items SET observed=? WHERE item_id=?', (dumps(obs), str(item_id)))
                if obs['_unverifiable_slots'] >= MAX_FAILED_SLOTS:
                    # Bounded across slots too: no slot-to-slot churn while Dropbox stays unreadable (R5 A2).
                    reason = (f"Dropbox could not be read at publication time for {obs['_unverifiable_slots']} slots; "
                              'nothing was published. Check the Dropbox connection, then tell Bondok to resume it.')
                    self.update_item(c, item_id, worker, 'unverifiable limit',
                                     hold=dumps({'kind': 'publish_retry_limit', 'reason': reason, 'origin': 'system',
                                                 'party': 'owner', 'recover': 'resume after the cause is fixed'}))
                    self.notify(c, f"retry-limit:{item_id}:{res['slot']}", f"{it['name']} ({item_id}): {reason}", item_id)
                    return {'claimed': False, 'reason': reason, 'held': True}
                out = self.try_schedule(c, item_id, worker)
                self.notify(c, f"unverifiable:{it['item_id']}:{res['slot']}", f"{it['name']} ({it['item_id']}) was not "
                            'published because Dropbox could not be read at publication time; nothing was posted. '
                            f"Next slot: {rules.display(rules.instant(out['scheduled'])) if out.get('scheduled') else 'none yet'}.",
                            it['item_id'])
                return {'claimed': False, 'reason': 'Dropbox unreadable at publication time', 'rescheduled': out}
            if tries >= MAX_ATTEMPTS_PER_SLOT and not self.active_attempt(c, item_id):
                # Container errors or commit refusals repeat every minute, each with a new Instagram container.
                last = c.execute("SELECT evidence FROM ops_attempts WHERE item_id=? AND slot=? AND stage='abandoned' "
                                 'ORDER BY updated DESC LIMIT 1', (str(item_id), res['slot'])).fetchone()
                ev = (loads(last['evidence'], {}) or {}) if last else {}
                why = str(ev.get('abandoned') or ev.get('refused') or ev or 'unknown cause')[:300]
                reason = (f'Publication failed {tries} times at {rules.display(rules.instant(res["slot"]))} '
                          f'({why}). Nothing was published. Tell Bondok to resume it when it should be retried.')
                self.release(c, it, 'publish retry limit')
                self.update_item(c, it['item_id'], worker, 'publish retry limit',
                                 hold=dumps({'kind': 'publish_retry_limit', 'reason': reason, 'origin': 'system',
                                             'party': 'owner', 'recover': 'resume after the cause is fixed'}))
                self.notify(c, f"retry-limit:{it['item_id']}:{res['slot']}", f"{it['name']} ({it['item_id']}): {reason}",
                            it['item_id'])
                return {'claimed': False, 'reason': reason, 'held': True}
            slot = rules.instant(res['slot'])
            if now_dt < slot or now_dt > self.deadline(c, res):
                return self._refuse(c, it, 'Outside the allowed publication window', quiet=now_dt < slot)
            att = self.active_attempt(c, item_id)
            why = self.eligible(c, {**it}) if not att else self._eligible_ignoring_attempt(c, it)
            if why:
                return self._refuse(c, it, why)
            problem = self.verification_problem(c, it)
            if problem:
                self.evaluate(c, item_id, worker)
                # The slot is lost: say so once (a prepared file removed to free disk space was silent; round 4).
                nxt = ('it needs a new time from you once it is ready again' if res['owner_pinned'] else
                       'it will be scheduled again automatically once it is ready')
                self.notify(c, f"claim-unverified:{it['item_id']}:{res['slot']}",
                            f"{it['name']} ({it['item_id']}) was not published at {rules.display(slot)}: {problem}. "
                            f'Nothing was posted; it will be prepared again and {nxt}.', it['item_id'])
                return self._refuse(c, it, problem)
            fp = self.payload_fp(c, it)
            if res['content_rev'] != it['content_rev'] or res['payload_fp'] != fp:
                return self._refuse(c, it, 'Reservation is not authorized for the current content revision')
            payload = self.payload(c, it)
            if att:
                if att['stage'] not in PRE_COMMIT:
                    return {'claimed': False, 'stage': att['stage'], 'quiet': True}
                if (att['lease_until'] or 0) > now and att['worker'] != worker:
                    return {'claimed': False, 'stage': 'lease_busy', 'quiet': True}
                fence = next_counter(c, 'fence:attempt')
                if att['payload_fp'] == fp and att['slot'] == res['slot']:
                    c.execute('UPDATE ops_attempts SET worker=?, fence=?, lease_until=?, updated=? WHERE id=?',
                              (worker, fence, now + LEASE, now, att['id']))
                    audit(c, item_id, 'attempt_resumed', worker, {'attempt': att['id']})
                    return {'claimed': True, 'attempt_id': att['id'], 'fence': fence, 'payload': payload,
                            'container_id': att['container_id'], 'resumed': True, 'source_asset': it['asset_key']}
                # Payload changed: the old container must never be published.
                c.execute("UPDATE ops_attempts SET stage='abandoned', updated=? WHERE id=?", (now, att['id']))
                audit(c, item_id, 'attempt_abandoned', worker, {'attempt': att['id'], 'why': 'payload changed'})
            aid = 'A-' + secrets.token_hex(6)
            fence = next_counter(c, 'fence:attempt')
            c.execute('INSERT INTO ops_attempts(id,item_id,content_rev,payload_fp,payload,slot,worker,fence,lease_until,'
                      "stage,created,updated) VALUES(?,?,?,?,?,?,?,?,?,'claimed',?,?)",
                      (aid, item_id, it['content_rev'], fp, dumps(payload), res['slot'], worker, fence, now + LEASE,
                       now, now))
            audit(c, item_id, 'attempt_claimed', worker, {'attempt': aid, 'rev': it['content_rev']})
            self.project(c, item_id)    # Monday shows Publishing via the sync path
            return {'claimed': True, 'attempt_id': aid, 'fence': fence, 'payload': payload, 'container_id': None,
                    'source_asset': it['asset_key']}

    def _eligible_ignoring_attempt(self, c, it):
        why = self.eligible(c, it)
        return None if why == 'a publication attempt is active' else why

    def _refuse(self, c, it, reason, quiet=False, notify=False):
        if notify:
            self.notify(c, f"claim-refused:{it['item_id']}:{fingerprint(reason)[:8]}:{it['version']}",
                        f"Publication of {it['name']} ({it['item_id']}) was not started: {reason}.", it['item_id'])
        return {'claimed': False, 'reason': reason, 'quiet': quiet}

    def _attempt(self, c, attempt_id, worker=None, fence=None, stages=PRE_COMMIT, lease=True):
        a = c.execute('SELECT * FROM ops_attempts WHERE id=?', (attempt_id,)).fetchone()
        if not a:
            raise Rejected('Unknown publication attempt', 'unknown_attempt')
        a = dict(a)
        if a['stage'] not in stages:
            raise Rejected('Attempt is ' + a['stage'], 'stage')
        if worker is not None and (a['worker'] != worker or (fence is not None and a['fence'] != fence)):
            raise Rejected('Publication lease belongs to another worker', 'fenced')
        if lease and (a['lease_until'] or 0) < self.now():
            raise Rejected('Publication lease expired', 'lease_expired')
        return a

    def container(self, attempt_id, worker, fence, container_id) -> dict:
        with self.store.tx() as c:
            a = self._attempt(c, attempt_id, worker, fence, ('claimed', 'container_created'))
            if a['container_id'] and a['container_id'] != container_id:
                raise Rejected('A different container is already recorded for this attempt', 'conflict')
            c.execute("UPDATE ops_attempts SET stage='container_created', container_id=?, lease_until=?, updated=? "
                      'WHERE id=?', (container_id, self.now() + LEASE, self.now(), attempt_id))
            return {'ok': True, 'container_id': container_id}

    def abandon(self, attempt_id, worker, fence, reason) -> dict:
        """Pre-commit only: the container failed/expired; nothing was published."""
        return self.precommit_failure(attempt_id, worker, fence, 'container_status', error=reason)

    def precommit_failure(self, attempt_id, worker, fence, stage, *, error=None, http_status=None,
                          status_code=None) -> dict:
        """Every failure before the commitment point (create/save/renew/check/verify/commit call) is recorded with
        its real cause and counted; nothing was published (R5 A2, M6). Account-level causes open the breaker;
        content defects block the item for the editor; delivery fetch failures re-check the delivered file."""
        with self.store.tx() as c:
            a = self._attempt(c, attempt_id, worker, fence, PRE_COMMIT, lease=False)
            cls = classify(error, http_status, status_code)
            text = summary(error)
            if status_code and status_code not in ('ERROR', 'EXPIRED'):
                text = f'container status {status_code}: {text}'
            c.execute("UPDATE ops_attempts SET stage='abandoned', evidence=?, updated=? WHERE id=?",
                      (dumps({'abandoned': f'{stage}: {text}'[:400], 'class': cls, 'stage': stage,
                              'http_status': http_status, 'status_code': status_code}), self.now(), attempt_id))
            audit(c, a['item_id'], 'attempt_abandoned', worker, {'attempt': attempt_id, 'stage': stage, 'class': cls})
            it = self.item(c, a['item_id'])
            out = {'abandoned': True, 'class': cls}
            if cls in ('auth', 'restricted'):
                self._open_breaker(c, cls, text)
            elif cls == 'throttle':
                self._open_breaker(c, cls, text, until=self.now() + 3600)
            elif cls == 'daily_limit':
                self._open_breaker(c, cls, text, until=self.next_cairo_midnight())
            elif cls in ('content', 'rejected'):
                key = 'provider:' + str(provider_error(error)['subcode'] or provider_error(error)['code'] or cls)
                reason = f'Instagram cannot use this video: {text}. A corrected export is needed.'
                self.release(c, it, 'provider content error')
                self.update_item(c, it['item_id'], worker, 'provider content error', readiness='blocked',
                                 block_kind='editor', block_key=key + ':' + str(it['asset_key']), block_reason=reason[:500])
                self.editor_task(c, it['item_id'], key + ':' + str(it['asset_key']), reason)
            elif cls == 'fetch':
                # Instagram could not download the delivered copy: re-check and re-deliver it (no editor action yet).
                self.release(c, it, 'delivered file not fetchable')
                self.update_item(c, it['item_id'], worker, 'delivered file not fetchable', readiness='checking',
                                 verification_id=None)
                self.request_check(c, it['item_id'], worker, 'delivery_unfetchable')
            self.project(c, a['item_id'])
            if cls == 'caption':
                reason = f'Instagram refused the caption: {text}. Edit the caption, then tell Bondok to publish it.'
                self.release(c, it, 'provider caption error')
                self.update_item(c, it['item_id'], worker, 'provider caption error', publication='failed')
                self.notify(c, f"caption-refused:{it['item_id']}:{it['content_rev']}", f"{it['name']} ({it['item_id']}): "
                            + reason, it['item_id'])
            if cls not in ('content', 'rejected', 'fetch', 'caption'):
                self.evaluate(c, a['item_id'], worker)
            return out

    def next_cairo_midnight(self) -> float:
        from datetime import timedelta
        local = self.now_dt().astimezone(rules.TZ)
        nxt = (local + timedelta(days=1)).date()
        return rules.cairo_local(nxt.year, nxt.month, nxt.day, 0, 5).timestamp()

    def renew(self, attempt_id, worker, fence) -> dict:
        with self.store.tx() as c:
            self._attempt(c, attempt_id, worker, fence)
            c.execute('UPDATE ops_attempts SET lease_until=?, updated=? WHERE id=?',
                      (self.now() + LEASE, self.now(), attempt_id))
            return {'ok': True}

    # ------------------------------------------------------------------ commitment point
    def commit(self, attempt_id, worker, fence, *, source_asset=None, container_status=None) -> dict:
        now, now_dt = self.now(), self.now_dt()
        with self.store.tx() as c:
            a = self._attempt(c, attempt_id, worker, fence, ('container_created',))
            it = self.item(c, a['item_id'])
            res = self.reservation(c, a['item_id'])
            problems = []
            if container_status != 'FINISHED':
                problems.append('Instagram container is not ready')
            if it['owner_state'] != 'active':
                problems.append('item is ' + it['owner_state'])
            if loads(it['hold'], None):
                problems.append('item is held: ' + (loads(it['hold'], {}) or {}).get('reason', ''))
            if it['content_rev'] != a['content_rev']:
                problems.append('content changed after the claim')
            if not res or res['slot'] != a['slot'] or res['content_rev'] != a['content_rev'] or \
                    res['payload_fp'] != a['payload_fp']:
                problems.append('reservation changed after the claim')
            elif self.payload_fp(c, it) != a['payload_fp']:
                problems.append('publication payload changed after the claim')
            unverifiable = source_asset == 'unverifiable'
            changed = source_asset is not None and not unverifiable and source_asset != it['asset_key']
            if unverifiable:
                # Dropbox could not be read: refuse this commit only; verification and slot stay (audit P5).
                problems.append('Dropbox file version could not be checked (temporary)')
            if changed:
                problems.append('Dropbox file version changed after verification')
            if self.verification_problem(c, it):
                problems.append(self.verification_problem(c, it))
            dup = self.duplicate_of(c, it)
            if dup:
                problems.append(self.duplicate_reason(dup))          # re-checked at the commitment point (R5 B1)
            for o in c.execute("SELECT evidence FROM ops_attempts WHERE item_id=? AND stage='owner_unpublished'",
                               (a['item_id'],)).fetchall():
                ev = loads(o['evidence'], {}) or {}
                lc = ev.get('last_check') or {}
                if not (lc.get('at', 0) >= ev.get('owner_statement_at', 0) and lc.get('status') != 'PUBLISHED'):
                    problems.append('an earlier attempt was reported not published, but its Instagram container was '
                                    'not re-checked yet (R5 B9)')
            slot = rules.instant(a['slot'])
            end = self.deadline(c, res) if res and res['slot'] == a['slot'] else slot + rules.LATE_WINDOW
            if not (slot <= now_dt <= end):
                problems.append('outside the publication window')
            if problems:
                c.execute("UPDATE ops_attempts SET stage='abandoned', evidence=?, updated=? WHERE id=?",
                          (dumps({'refused': problems}), now, attempt_id))
                audit(c, a['item_id'], 'commit_refused', worker, {'attempt': attempt_id, 'problems': problems})
                if changed:
                    self.request_check(c, it['item_id'], worker, 'source_changed_before_publish')
                    # The verified media no longer matches Dropbox: drop the authorization so the next run
                    # does not claim again (each claim creates a new Instagram container).
                    self.release(c, it, 'source changed before publish', keep_request=True)
                    self.update_item(c, it['item_id'], worker, 'source changed before publish', readiness='checking',
                                     verification_id=None)
                    self.notify(c, 'source-changed:' + attempt_id, f"Publication of {it['name']} ({it['item_id']}) at "
                                f"{rules.display(slot)} was stopped: the Dropbox file changed after it was verified. "
                                'Nothing was published; the new version will be checked before it is scheduled again.',
                                it['item_id'])
                self.evaluate(c, a['item_id'], worker)
                self.project(c, a['item_id'])
                return {'committed': False, 'reasons': problems}
            c.execute("UPDATE ops_attempts SET stage='committed', lease_until=?, updated=? WHERE id=?",
                      (now + COMMIT_LEASE, now, attempt_id))
            self._legacy_receipt(c, a, 'publish_requested')
            audit(c, a['item_id'], 'commit', worker, {'attempt': attempt_id})
            return {'committed': True, 'container_id': a['container_id']}

    def _legacy_receipt(self, c, a, stage, extra=None):
        """Mirror into the legacy receipt table so a rolled-back helper still
        refuses to republish (it treats publish_requested/published as final)."""
        info = loads(a['payload'], {})
        data = {'itemId': a['item_id'], 'at': a['slot'], 'format': info.get('format'), 'attempt': a['id'],
                'mediaId': info.get('verification_id'), 'sourceSynced': False, **(extra or {})}
        c.execute('INSERT OR REPLACE INTO publications VALUES(?,?,?,?,?)',
                  (a['item_id'], 'ops:' + a['id'], stage, dumps(data), self.now()))
        self.journal_identities(c, self.item(c, a['item_id']), info)

    def journal_identities(self, c, it, info=None):
        """Append the item's content identities to the local publication journal (legacy `assets`, also read by a
        rolled-back helper). Written at the commitment point and for owner-reported publications; it only ever
        blocks a publication (ADR §5)."""
        ids = set(self.content_identities(c, it))
        if info and info.get('video_sha256'):
            ids.add(f"{rules.ACCOUNT}|{info.get('format')}|{info.get('video_sha256')}")
        for identity in ids:
            c.execute('INSERT OR IGNORE INTO assets VALUES(?,?)', (identity, it['item_id']))

    # ------------------------------------------------------------------ outcomes
    def result(self, attempt_id, worker, *, media_id=None, error=None, http_status=None, definitive=False) -> dict:
        """Record the provider response for a committed attempt.

        Accepted even if the lease expired: confirmed external evidence is never discarded. Only a documented
        refusal (Meta error reference) counts as not published; generic/unknown codes (1, 2, -1), 5xx, timeouts and
        a missing id stay unknown and are reconciled with the container status (R5 B8).
        """
        with self.store.tx() as c:
            a = self._attempt(c, attempt_id, None, None, ('committed', 'outcome_unknown', 'owner_unpublished'),
                              lease=False)
            if media_id:
                return self._published(c, a, worker, {'media_id': str(media_id), 'source': 'media_publish response',
                                                      'published_at': self.iso_ts(self.now())})
            cls = classify(error, http_status) if definitive and http_status and 400 <= int(http_status) < 500 \
                else 'ambiguous'
            if cls == 'not_ready' and a['stage'] == 'committed':
                # Refused because the container was not ready: nothing was published and the slot is still valid.
                # The next run claims again; MAX_ATTEMPTS_PER_SLOT bounds the retries (round 4, end-to-end test).
                c.execute("UPDATE ops_attempts SET stage='abandoned', evidence=?, updated=? WHERE id=?",
                          (dumps({'abandoned': 'Instagram: media not ready for publishing (9007)', 'class': cls,
                                  'http_status': http_status, 'error': str(error)[:300]}), self.now(), attempt_id))
                c.execute('DELETE FROM publications WHERE item=? AND owner=?', (a['item_id'], 'ops:' + attempt_id))
                audit(c, a['item_id'], 'attempt_abandoned', worker, {'attempt': attempt_id, 'why': 'media not ready (9007)'})
                self.project(c, a['item_id'])
                return {'stage': 'abandoned', 'retry': 'same slot'}
            if cls in REFUSED_CLASSES or cls == 'not_ready':
                # (a late 9007 for an attempt that already became unknown: refused, nothing was published)
                return self._refused(c, a, worker, cls, error, http_status)
            if a['stage'] == 'owner_unpublished':
                ev = {**(loads(a['evidence'], {}) or {}), 'late_answer': {'http_status': http_status,
                                                                          'error': str(error)[:300]}}
                c.execute('UPDATE ops_attempts SET evidence=?, updated=? WHERE id=?', (dumps(ev), self.now(), attempt_id))
                return {'stage': 'owner_unpublished'}
            self._unknown(c, a, f'Publish request ended without confirmation (HTTP {http_status}): {summary(error)}')
            return {'stage': 'outcome_unknown'}

    def _refused(self, c, a, worker, cls, error, http_status) -> dict:
        """Documented refusal of media_publish: nothing was published. A late refusal also resolves an attempt
        that had become unknown (R5 A18); the owner is told what actually happens next."""
        text = summary(error)
        c.execute("UPDATE ops_attempts SET stage='failed', evidence=?, updated=? WHERE id=?",
                  (dumps({**(loads(a['evidence'], {}) or {}), 'http_status': http_status, 'error': str(error)[:800],
                          'class': cls, 'transient': cls in ('throttle', 'daily_limit', 'container_gone',
                                                             'not_ready')}),
                   self.now(), a['id']))
        c.execute('DELETE FROM publications WHERE item=? AND owner=?', (a['item_id'], 'ops:' + a['id']))
        it = self.item(c, a['item_id'])
        if it['publication'] == 'published':
            # The owner already reported it as published: keep that; this refusal is evidence only.
            return {'stage': 'failed', 'owner_reported_published': True}
        self.release(c, it, 'publication refused')
        if it['publication'] in ('outcome_unknown', 'in_progress'):
            it = self.update_item(c, it['item_id'], worker, 'refusal resolves the attempt', publication='not_started')
        if cls in ('auth', 'restricted'):
            self._open_breaker(c, cls, text)
            return {'stage': 'failed', 'breaker': cls}
        if cls in ('throttle', 'daily_limit', 'container_gone', 'not_ready'):
            if cls == 'throttle':
                self._open_breaker(c, cls, text, until=self.now() + 3600)
            elif cls == 'daily_limit':
                self._open_breaker(c, cls, text, until=self.next_cairo_midnight())
            tries = c.execute("SELECT COUNT(*) FROM ops_attempts WHERE item_id=? AND stage='failed' AND "
                              "json_extract(evidence,'$.transient')=1", (it['item_id'],)).fetchone()[0]
            if tries <= MAX_TRANSIENT_RETRIES:
                out = self.try_schedule(c, it['item_id'], worker)
                nxt = (f"it is now scheduled for {rules.display(rules.instant(out['scheduled']))}."
                       if out.get('scheduled') else f"it is not scheduled yet ({out.get('waiting')}).")
                self.notify(c, f"pub-transient:{a['id']}", f"Instagram had a temporary error for {it['name']} "
                            f"({it['item_id']}): {text}. Nothing was published; {nxt}", it['item_id'])
                return {'stage': 'failed', 'retry': out}
        if cls == 'content':
            reason = f'Instagram cannot use this video: {text}. A corrected export is needed.'
            key = f"provider:{provider_error(error)['subcode'] or cls}:{it['asset_key']}"
            self.update_item(c, it['item_id'], worker, 'provider content error', readiness='blocked',
                             block_kind='editor', block_key=key, block_reason=reason[:500])
            self.editor_task(c, it['item_id'], key, reason)
            return {'stage': 'failed', 'blocked': 'content'}
        if cls == 'fetch':
            self.update_item(c, it['item_id'], worker, 'delivered file not fetchable', readiness='checking',
                             verification_id=None)
            self.request_check(c, it['item_id'], worker, 'delivery_unfetchable')
            self.notify(c, f"pub-fetch:{a['id']}", f"Instagram could not download the prepared video of {it['name']} "
                        f"({it['item_id']}): {text}. Nothing was published; the video is delivered again and the item "
                        'is scheduled once it is verified.', it['item_id'])
            return {'stage': 'failed', 'redeliver': True}
        it = self.update_item(c, it['item_id'], worker, 'publication failed', publication='failed')
        self.notify(c, f"pub-failed:{a['id']}", f"Instagram rejected {it['name']} ({it['item_id']}): {text}. Nothing "
                    'was published. Fix the cause, then tell Bondok to publish it again.', it['item_id'])
        return {'stage': 'failed'}

    def _unknown(self, c, a, why):
        if a['stage'] in ('outcome_unknown', 'owner_unpublished'):
            return
        c.execute("UPDATE ops_attempts SET stage='outcome_unknown', evidence=?, next_check=?, updated=? WHERE id=?",
                  (dumps({'why': why}), self.now() + RECONCILE_INTERVALS[0], self.now(), a['id']))
        self._legacy_receipt(c, a, 'publish_requested', {'unknown': why[:200]})
        if self.item(c, a['item_id'])['publication'] == 'published':
            return                     # owner-reported publication stays; the attempt is reconciled for evidence
        it = self.update_item(c, a['item_id'], 'service:wf2', 'outcome unknown', publication='outcome_unknown')
        self.notify(c, f"unknown:{a['id']}", f"⚠️ Publication outcome unknown for {it['name']} ({it['item_id']}): {why}. "
                    'It will NOT be published again automatically. Please check Instagram and tell Bondok '
                    '"published" or "not published".', it['item_id'])

    def _published(self, c, a, worker, evidence):
        now = self.now()
        # 'published_at' only when Instagram's answer dates it (media_publish response); otherwise the moment the
        # evidence was recorded is kept as such, never shown as the publication time (R5 LOW-10).
        ev = {**(loads(a['evidence'], {}) or {}), **evidence, 'recorded_at': self.iso_ts(now)}
        c.execute("UPDATE ops_attempts SET stage='published', media_id=COALESCE(?,media_id), evidence=?, updated=? "
                  'WHERE id=?', (evidence.get('media_id'), dumps(ev), now, a['id']))
        self._legacy_receipt(c, a, 'published', {'publishedMediaId': evidence.get('media_id'),
                                                 'publishedAt': ev.get('published_at') or ev['recorded_at']})
        it = self.update_item(c, a['item_id'], worker, 'published', publication='published')
        if it['source_item_id']:
            self.enqueue(c, 'source_monday', f"source-posted:{it['item_id']}",
                         {'source_item_id': it['source_item_id'], 'label': 'Posted'}, it['item_id'])
        if self.config.get('notify_published'):
            self.notify(c, f"published:{a['id']}", f"✅ Published {it['name']} ({it['item_id']}) as "
                        f"{'Reel' if it['format'] == 'Post' else 'Story'}"
                        + (f" — media {evidence['media_id']}" if evidence.get('media_id') else ''), it['item_id'])
        return {'stage': 'published', 'media_id': evidence.get('media_id')}

    def evidence(self, attempt_id, permalink=None) -> dict:
        with self.store.tx() as c:
            a = self._attempt(c, attempt_id, None, None, ('published',), lease=False)
            if permalink:
                c.execute('UPDATE ops_attempts SET permalink=?, updated=? WHERE id=?', (permalink, self.now(), attempt_id))
                self.project(c, a['item_id'])
            return {'ok': True}

    def reconcile(self, attempt_id, container_status=None, error=None) -> dict:
        """Check one specific uncertain attempt with its container status. PUBLISHED is positive evidence and wins
        over an earlier owner "not published" (R5 B9). EXPIRED/ERROR mean the container was never published.
        Anything else stays uncertain; an empty or failed query never means "not published"."""
        with self.store.tx() as c:
            a = self._attempt(c, attempt_id, None, None, ('outcome_unknown', 'owner_unpublished'), lease=False)
            ev = loads(a['evidence'], {}) or {}
            if container_status == 'PUBLISHED':
                said = a['stage'] == 'owner_unpublished'
                out = self._published(c, a, 'service:wf2', {'container_status': 'PUBLISHED',
                                                            'source': 'container status reconciliation'})
                if said:
                    it = self.item(c, a['item_id'])
                    self.notify(c, f"published-after-all:{a['id']}", f"{it['name']} ({it['item_id']}) was published "
                                'after all: Instagram shows the earlier attempt as PUBLISHED although it was reported '
                                'not published. It will not be published again.', it['item_id'])
                return out
            checks = a['checks'] + 1
            ev['last_check'] = {'status': container_status, 'error': (error or '')[:200], 'at': self.now()}
            if container_status in ('EXPIRED', 'ERROR'):
                # The container can no longer be published and was not: definitive non-publication.
                c.execute("UPDATE ops_attempts SET stage='failed', checks=?, evidence=?, updated=? WHERE id=?",
                          (checks, dumps({**ev, 'outcome': 'not_published (container ' + container_status + ')'}),
                           self.now(), attempt_id))
                c.execute('DELETE FROM publications WHERE item=? AND owner=?', (a['item_id'], 'ops:' + attempt_id))
                it = self.item(c, a['item_id'])
                if a['stage'] == 'outcome_unknown' and it['publication'] == 'outcome_unknown':
                    it = self.update_item(c, it['item_id'], 'service:wf2', 'container never published',
                                          publication='not_started')
                    out = self.try_schedule(c, it['item_id'], 'service:wf2')
                    nxt = (f"it is now scheduled for {rules.display(rules.instant(out['scheduled']))}."
                           if out.get('scheduled') else f"it is not scheduled yet ({out.get('waiting')}).")
                    self.notify(c, f"unknown-resolved:{attempt_id}", f"Instagram confirms {it['name']} ({it['item_id']}) "
                                f'was not published (container {container_status}); {nxt}', it['item_id'])
                return {'stage': 'failed', 'checks': checks}
            nxt = self.now() + RECONCILE_INTERVALS[min(checks, len(RECONCILE_INTERVALS) - 1)]
            c.execute('UPDATE ops_attempts SET checks=?, next_check=?, evidence=?, updated=? WHERE id=?',
                      (checks, nxt, dumps(ev), self.now(), attempt_id))
            if checks >= MAX_RECONCILE_CHECKS and a['stage'] == 'outcome_unknown':
                self.notify(c, f'unknown-exhausted:{attempt_id}', f"Automatic checks could not confirm publication of "
                            f"item {a['item_id']} (container status {container_status}). Manual verification is "
                            'required; it stays blocked from republishing.', a['item_id'])
            return {'stage': a['stage'], 'checks': checks}

    def op_report_published(self, c, cmd: Command):
        """The owner reports the item as published (board Posted, a typed post link/media id, or Slack).

        Terminal for automatic publication of this item and of the same video (R5 B2): no Resume proposal, no
        second confirmation (R5 LOW-12). A pre-commit attempt is abandoned (nothing was sent); a committed or
        unknown attempt keeps its record and any later provider evidence is still recorded."""
        it = self.item(c, cmd.item_id)
        if it['publication'] == 'published':
            return {'published': True, 'unchanged': True}
        now = self.now()
        report = {'source': cmd.args.get('source') or 'owner', 'value': (str(cmd.args.get('value') or ''))[:300],
                  'by': cmd.actor, 'at': self.iso_ts(now)}
        att = self.active_attempt(c, it['item_id'])
        if att and att['stage'] in PRE_COMMIT:
            c.execute("UPDATE ops_attempts SET stage='abandoned', evidence=?, updated=? WHERE id=?",
                      (dumps({'abandoned': 'owner reported the item as published'}), now, att['id']))
        elif att and att['stage'] == 'outcome_unknown':
            ev = {**(loads(att['evidence'], {}) or {}), 'owner_report': report}
            c.execute('UPDATE ops_attempts SET evidence=?, updated=? WHERE id=?', (dumps(ev), now, att['id']))
        self.release(c, it, 'owner reported published', keep_request=False)
        obs = loads(it['observed'], {}) or {}
        obs['_owner_report'] = report
        c.execute('UPDATE ops_items SET observed=? WHERE item_id=?', (dumps(obs), it['item_id']))
        # Mirrored into the legacy receipt table so a rolled-back helper refuses to publish it too.
        c.execute("INSERT OR IGNORE INTO publications VALUES(?,?,?,?,?)",
                  (it['item_id'], 'ops:owner-report', 'published', dumps({'itemId': it['item_id'], **report}), now))
        self.journal_identities(c, it)
        self.update_item(c, it['item_id'], cmd.actor, 'owner reported published', publication='published',
                         legacy_posted=1, hold=None)
        self.notify(c, f"owner-posted:{it['item_id']}", f"Recorded {it['name']} ({it['item_id']}) as published by the "
                    f"owner ({report['source']}). It will not be published automatically again.", it['item_id'])
        return {'published': True, 'owner_reported': True,
                'attempt': att['id'] if att and att['stage'] in ('committed', 'outcome_unknown') else None}

    def op_resolve_outcome(self, c, cmd: Command):
        """Owner's manual verification of an unknown/failed publication."""
        it = self.item(c, cmd.item_id)
        outcome = cmd.args.get('outcome')
        if outcome not in ('published', 'not_published'):
            raise Rejected('Outcome must be published or not_published', 'invalid')
        report = (loads(it['observed'], {}) or {}).get('_owner_report')
        provider = c.execute("SELECT 1 FROM ops_attempts WHERE item_id=? AND stage='published'",
                             (it['item_id'],)).fetchone()
        if it['publication'] == 'published' and outcome == 'not_published' and report and not provider and \
                not c.execute("SELECT 1 FROM ops_attempts WHERE item_id=? AND stage IN ('committed','outcome_unknown')",
                              (it['item_id'],)).fetchone():
            # The owner corrects their own report (e.g. Posted clicked by mistake). Both statements are kept;
            # only an owner report without any provider evidence or open attempt can be corrected this way.
            obs = loads(it['observed'], {}) or {}
            obs['_owner_report_corrected'] = {**report, 'corrected_by': cmd.actor, 'corrected_at': self.iso_ts(self.now())}
            obs.pop('_owner_report', None)
            c.execute('UPDATE ops_items SET observed=? WHERE item_id=?', (dumps(obs), it['item_id']))
            c.execute("DELETE FROM publications WHERE item=? AND owner='ops:owner-report'", (it['item_id'],))
            self.update_item(c, it['item_id'], cmd.actor, 'owner report corrected: not published',
                             publication='not_started', legacy_posted=0)
            return {'publication': 'not_started', 'corrected': True, **self.try_schedule(c, it['item_id'], cmd.actor)}
        if it['publication'] == 'published':
            # An old failed attempt must never turn a published item back into schedulable (duplicate post).
            raise Rejected('This item is already published; nothing to resolve', 'already_published')
        stages = ('outcome_unknown', 'committed', 'failed') if it['publication'] == 'failed' else \
            ('outcome_unknown', 'committed')
        a = c.execute(f"SELECT * FROM ops_attempts WHERE item_id=? AND stage IN ({','.join('?' * len(stages))}) "
                      'ORDER BY updated DESC LIMIT 1', (it['item_id'], *stages)).fetchone()
        external = (loads(it['hold'], {}) or {}).get('kind') == 'external_posted'
        if not a:
            if outcome == 'not_published' and (external or it['publication'] in ('failed', 'outcome_unknown')):
                # Failed attempt, board "Posted" mark, or an unknown state imported without an attempt row.
                if external:      # the owner answered for any v1 receipt that caused the hold too
                    obs = loads(it['observed'], {}) or {}
                    obs['_v1_receipt_resolved'] = self.now()
                    c.execute('UPDATE ops_items SET observed=? WHERE item_id=?', (dumps(obs), it['item_id']))
                self.update_item(c, it['item_id'], cmd.actor, 'resolved not published', publication='not_started',
                                 hold=None if external else it['hold'])
                return {'publication': 'not_started', **self.try_schedule(c, it['item_id'], cmd.actor)}
            if outcome == 'published' and (external or it['publication'] == 'outcome_unknown'):
                # Owner confirms a manual/external post: protect it without inventing a receipt.
                self.release(c, it, 'posted externally')
                self.update_item(c, it['item_id'], cmd.actor, 'external post confirmed', publication='published',
                                 legacy_posted=1, hold=None)
                return {'publication': 'published', 'receipt': 'none (owner-confirmed external post)'}
            raise Rejected('No unresolved publication attempt for this item', 'nothing_to_resolve')
        a = dict(a)
        if a['stage'] == 'committed':
            raise Rejected('The publisher is still waiting for the result; try again in a few minutes', 'in_progress')
        if outcome == 'published':
            return self._published(c, a, cmd.actor, {'media_id': cmd.args.get('media_id'),
                                                     'source': 'owner manual verification'})
        ev = {**(loads(a['evidence'], {}) or {}), 'resolved_by': cmd.actor, 'outcome': 'not_published',
              'owner_statement_at': self.now()}
        if a['container_id'] and a['stage'] == 'outcome_unknown':
            # The owner's statement is recorded, the container keeps being checked: a later PUBLISHED still wins and
            # a new attempt cannot commit before the old container was re-checked (R5 B9).
            c.execute("UPDATE ops_attempts SET stage='owner_unpublished', evidence=?, next_check=?, updated=? WHERE id=?",
                      (dumps(ev), self.now(), self.now(), a['id']))
        else:
            c.execute("UPDATE ops_attempts SET stage='failed', evidence=?, updated=? WHERE id=?",
                      (dumps(ev), self.now(), a['id']))
        c.execute('DELETE FROM publications WHERE item=? AND owner=?', (it['item_id'], 'ops:' + a['id']))
        self.release(c, it, 'resolved not published')
        self.update_item(c, it['item_id'], cmd.actor, 'resolved not published', publication='not_started')
        return {'publication': 'not_started', **self.try_schedule(c, it['item_id'], cmd.actor)}
