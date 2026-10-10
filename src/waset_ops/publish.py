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


class PublishMixin:
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
                if rules.instant(a['slot']) + rules.LATE_WINDOW < now_dt:
                    c.execute("UPDATE ops_attempts SET stage='abandoned', updated=? WHERE id=?", (now, a['id']))
                    audit(c, a['item_id'], 'attempt_abandoned', worker, {'attempt': a['id'], 'why': 'window passed'})
                    self.evaluate(c, a['item_id'], worker)     # a ready item gets its next slot (audit P4)
                    self.project(c, a['item_id'])
            rows = c.execute('SELECT r.*, i.publication, i.owner_state FROM ops_reservations r JOIN ops_items i '
                             'USING(item_id) WHERE r.slot<=? ORDER BY r.slot', (rules.iso(now_dt),)).fetchall()
            for r in rows:
                if r['publication'] != 'not_started' or rules.instant(r['slot']) + rules.LATE_WINDOW < now_dt:
                    continue
                att = self.active_attempt(c, r['item_id'])
                if att and (att['stage'] not in PRE_COMMIT or (att['lease_until'] or 0) > now):
                    continue
                work.append({'kind': 'publish', 'item_id': r['item_id'], 'slot': r['slot']})
            for a in c.execute("SELECT * FROM ops_attempts WHERE stage='outcome_unknown' AND container_id IS NOT NULL "
                               'AND checks<? AND (next_check IS NULL OR next_check<=?)',
                               (MAX_RECONCILE_CHECKS, now)).fetchall():
                work.append({'kind': 'reconcile', 'item_id': a['item_id'], 'attempt_id': a['id'],
                             'container_id': a['container_id']})
        return {'work': work[:limit], 'more': len(work) > limit}

    # ------------------------------------------------------------------ claim
    def claim(self, item_id, worker, *, source_status=None) -> dict:
        now, now_dt = self.now(), self.now_dt()
        with self.store.tx() as c:
            it = self.item(c, item_id)
            res = self.reservation(c, item_id)
            if source_status == 'Canceled':
                # Same outcome WF1 records later; releasing now stops WF2 re-reading Monday every minute and WF3
                # moving the item to the next slot (audit, publishing LOW).
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
            tries = c.execute("SELECT COUNT(*) FROM ops_attempts WHERE item_id=? AND slot=? AND stage='abandoned'",
                              (str(item_id), res['slot'])).fetchone()[0]
            if tries >= MAX_ATTEMPTS_PER_SLOT and not self.active_attempt(c, item_id) and \
                    all('could not be checked' in (r['evidence'] or '') for r in c.execute(
                        "SELECT evidence FROM ops_attempts WHERE item_id=? AND slot=? AND stage='abandoned'",
                        (str(item_id), res['slot']))):
                # Only Dropbox was unreadable at the commit point: nothing is wrong with the item; stop creating
                # containers for this slot and take the next one without asking the owner (round-2 review #10).
                self.release(c, it, 'source unverifiable at publication time')
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
                why = str((loads(last['evidence'], {}) or {}) if last else '')[:300]
                reason = (f'Publication failed {tries} times at {rules.display(rules.instant(res["slot"]))} '
                          f'({why}). Nothing was published. Tell Bondok to resume it when it should be retried.')
                self.release(c, it, 'publish retry limit')
                self.update_item(c, it['item_id'], worker, 'publish retry limit',
                                 hold=dumps({'kind': 'publish_retry_limit', 'reason': reason}))
                self.notify(c, f"retry-limit:{it['item_id']}:{res['slot']}", f"{it['name']} ({it['item_id']}): {reason}",
                            it['item_id'])
                return {'claimed': False, 'reason': reason, 'held': True}
            slot = rules.instant(res['slot'])
            late = (now_dt - slot).total_seconds()
            if late < 0 or late > rules.LATE_WINDOW.total_seconds():
                return self._refuse(c, it, 'Outside the allowed publication window', quiet=late < 0)
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
        with self.store.tx() as c:
            a = self._attempt(c, attempt_id, worker, fence, PRE_COMMIT, lease=False)
            c.execute("UPDATE ops_attempts SET stage='abandoned', evidence=?, updated=? WHERE id=?",
                      (dumps({'abandoned': str(reason)[:300]}), self.now(), attempt_id))
            audit(c, a['item_id'], 'attempt_abandoned', worker, {'attempt': attempt_id, 'why': str(reason)[:300]})
            self.evaluate(c, a['item_id'], worker)
            self.project(c, a['item_id'])
            return {'abandoned': True}

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
            slot = rules.instant(a['slot'])
            if not (slot <= now_dt <= slot + rules.LATE_WINDOW):
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

        Accepted even if the lease expired: confirmed external evidence is
        never discarded. Only a definitive provider rejection (HTTP 4xx with a
        provider error body) counts as not published; anything else is unknown.
        """
        with self.store.tx() as c:
            a = self._attempt(c, attempt_id, None, None, ('committed', 'outcome_unknown'), lease=False)
            if media_id:
                return self._published(c, a, worker, {'media_id': str(media_id), 'source': 'media_publish response'})
            if a['stage'] == 'committed' and definitive and http_status and 400 <= int(http_status) < 500 and \
                    _media_not_ready(error):
                # Refused because the container was not ready: nothing was published and the slot is still valid.
                # The next run claims again; MAX_ATTEMPTS_PER_SLOT bounds the retries (round 4, end-to-end test).
                c.execute("UPDATE ops_attempts SET stage='abandoned', evidence=?, updated=? WHERE id=?",
                          (dumps({'abandoned': 'Instagram: media not ready for publishing (9007)',
                                  'http_status': http_status, 'error': str(error)[:300]}), self.now(), attempt_id))
                c.execute('DELETE FROM publications WHERE item=? AND owner=?', (a['item_id'], 'ops:' + attempt_id))
                audit(c, a['item_id'], 'attempt_abandoned', worker, {'attempt': attempt_id, 'why': 'media not ready (9007)'})
                self.project(c, a['item_id'])
                return {'stage': 'abandoned', 'retry': 'same slot'}
            if definitive and http_status and 400 <= int(http_status) < 500:
                transient = _transient_provider_error(error)
                c.execute("UPDATE ops_attempts SET stage='failed', evidence=?, updated=? WHERE id=?",
                          (dumps({'http_status': http_status, 'error': str(error)[:800], 'transient': transient}),
                           self.now(), attempt_id))
                # Definitive rejection: nothing was published; drop the rollback guard row.
                c.execute('DELETE FROM publications WHERE item=? AND owner=?', (a['item_id'], 'ops:' + attempt_id))
                self.release(c, self.item(c, a['item_id']), 'publication failed')
                tries = c.execute("SELECT COUNT(*) FROM ops_attempts WHERE item_id=? AND stage='failed' AND "
                                  "json_extract(evidence,'$.transient')=1", (a['item_id'],)).fetchone()[0]
                if transient and tries <= MAX_TRANSIENT_RETRIES:
                    # Rate limit / temporary Meta error: nothing was published; take the next slot (audit P10).
                    out = self.try_schedule(c, a['item_id'], worker)
                    it = self.item(c, a['item_id'])
                    when = rules.display(rules.instant(out['scheduled'])) if out.get('scheduled') else 'the next free slot'
                    self.notify(c, f'pub-transient:{attempt_id}', f"Instagram had a temporary error for {it['name']} "
                                f"({it['item_id']}): {str(error)[:200]}. Nothing was published; it will be tried again "
                                f"at {when}.", it['item_id'])
                    return {'stage': 'failed', 'retry': out}
                if self.item(c, a['item_id'])['publication'] == 'published':
                    # The owner already reported it as published: keep that; this attempt's refusal is evidence only.
                    return {'stage': 'failed', 'owner_reported_published': True}
                it = self.update_item(c, a['item_id'], worker, 'publication failed', publication='failed')
                self.notify(c, f'pub-failed:{attempt_id}', f"Instagram rejected {it['name']} ({it['item_id']}): "
                            f"{str(error)[:300]}. Nothing was published. Tell Bondok to retry when fixed.", it['item_id'])
                return {'stage': 'failed'}
            self._unknown(c, a, f'Publish request ended without confirmation (HTTP {http_status}): {str(error)[:300]}')
            return {'stage': 'outcome_unknown'}

    def _unknown(self, c, a, why):
        if a['stage'] == 'outcome_unknown':
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
        ev = {**(loads(a['evidence'], {}) or {}), **evidence, 'published_at': self.iso_ts(now)}
        c.execute("UPDATE ops_attempts SET stage='published', media_id=COALESCE(?,media_id), evidence=?, updated=? "
                  'WHERE id=?', (evidence.get('media_id'), dumps(ev), now, a['id']))
        self._legacy_receipt(c, a, 'published', {'publishedMediaId': evidence.get('media_id'),
                                                 'publishedAt': ev['published_at']})
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
        """Check one specific unknown attempt using its container status.
        PUBLISHED is positive evidence. Anything else leaves it unknown; an
        empty or failed query never means "not published"."""
        with self.store.tx() as c:
            a = self._attempt(c, attempt_id, None, None, ('outcome_unknown',), lease=False)
            if container_status == 'PUBLISHED':
                return self._published(c, a, 'service:wf2', {'container_status': 'PUBLISHED',
                                                             'source': 'container status reconciliation'})
            checks = a['checks'] + 1
            nxt = self.now() + RECONCILE_INTERVALS[min(checks, len(RECONCILE_INTERVALS) - 1)]
            ev = {**(loads(a['evidence'], {}) or {}), 'last_check': {'status': container_status,
                                                                     'error': (error or '')[:200]}}
            c.execute('UPDATE ops_attempts SET checks=?, next_check=?, evidence=?, updated=? WHERE id=?',
                      (checks, nxt, dumps(ev), self.now(), attempt_id))
            if checks >= MAX_RECONCILE_CHECKS:
                self.notify(c, f'unknown-exhausted:{attempt_id}', f"Automatic checks could not confirm publication of "
                            f"item {a['item_id']} (container status {container_status}). Manual verification is "
                            'required; it stays blocked from republishing.', a['item_id'])
            return {'stage': 'outcome_unknown', 'checks': checks}

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
        c.execute("UPDATE ops_attempts SET stage='failed', evidence=?, updated=? WHERE id=?",
                  (dumps({**(loads(a['evidence'], {}) or {}), 'resolved_by': cmd.actor,
                          'outcome': 'not_published'}), self.now(), a['id']))
        c.execute('DELETE FROM publications WHERE item=? AND owner=?', (it['item_id'], 'ops:' + a['id']))
        self.release(c, it, 'resolved not published')
        self.update_item(c, it['item_id'], cmd.actor, 'resolved not published', publication='not_started')
        return {'publication': 'not_started', **self.try_schedule(c, it['item_id'], cmd.actor)}
