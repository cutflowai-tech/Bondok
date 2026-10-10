# ADR: Monday as the business record (R5.2)

Status: accepted for implementation 2026-10-10. Owner policy: `OWNER_AUTHORITY_CONTRACT.md`.

## Context

V2 made `state.sqlite` (`ops_*`) the business authority and Monday a display. Owner edits on the board became
"unattributed" proposals, approvals lived only in SQLite, and an agent had to edit SQLite and `rules.py` by hand
to make owner decisions effective (postmortem 2026-10-10). Round 5 traced most defects to that design (L1–L11).

## Verified platform facts (2026-10-10)

| Fact | Evidence |
|---|---|
| Automation, agents and the owner write as the same Monday user; activity-log entries have `user_id`/`account_id` only, no app/source field; `action_record_uuid` is null for API and UI column changes | live read-only activity-log query (local evidence) + monday API reference "Activity logs" |
| Activity logs: unique event `id`, `previous_value`, 17-digit `created_at`, filters by item/column/user, reverse chronological, up to 10,000 per query | monday API reference |
| Long text column: up to 2,000 characters | monday API reference "Long text" |
| No conditional (compare-and-set) column write, no uniqueness constraint, no multi-item transaction is documented | monday API reference (absence); treated as unavailable |
| Integration actions appear as the token's user | monday community feature request (non-official) |

A distinguishable integration identity therefore needs a separate Monday user (account seat) or an app whose
writes are attributed differently (not documented). Both need the owner; neither is assumed.

## Decision

1. **Business record on Monday.** Owner decisions live in owner columns (contract §2). System facts that must
   survive a cache rebuild (selected source and mode, Topaz binding, caption approval fingerprint, requested vs
   confirmed time, reservation origin, hold reason/recovery, delivered media identity, publication intent and
   outcome) are written by the handler into one technical long-text column, **Bondok record** (compact JSON with a
   schema version, < 2,000 characters), plus the existing result columns. History (decisions, attempts, evidence)
   goes to item updates.
2. **SQLite is a cache plus execution journal.** `ops_items`/`ops_reservations` are rebuilt from the board when
   missing or stale; leases, fences, run records, outbox, media jobs and the publication journal stay local.
   Rebuild test: delete the cache, read the board, and owner decisions, reservations and publication protection
   come back unchanged (`tests/test_r5_authority.py`).
3. **Attribution: causal own-write recognition + guarded writes** (contract §1). Automation writes go through
   one outbox and one applier (WF2) with compare-before-write against the last observed value (also when that
   value is empty). An owner value is never overwritten by a stale projection; the residual window between the
   guard read and the write (about one second) is declared, not hidden. Commands derive their idempotency key
   from the observation sequence, so A→X→A and repeated owner assertions are separate events.
4. **Publication.** WF2 is the only dispatcher; a local lease/fence gives exclusion. Before `media_publish` the
   intent (attempt, payload fingerprint, content fingerprint) is acknowledged on Monday; if that write fails,
   nothing is published. Duplicate protection keys on account + format + source content hash and delivered video
   hash, across items, including owner-reported and legacy publications.
5. **Technical journal exception (needs owner agreement).** Monday cannot keep a deleted item's publication
   evidence, so a deleted original would free its content fingerprint (trace: publish A → delete A → duplicate
   item B with the same file → B publishes). The smallest exception: the local append-only publication journal
   (`ops_attempts` + legacy `publications`/`assets`) may **block** a publication; it never authorizes one and is
   mirrored to Monday. Until the owner agrees, the strict "Monday-only" target is not declared met.
6. **Monday outage.** No new publication starts without a fresh authoritative read; in-flight attempts are kept and
   reconciled on recovery.

## Transition

* **R1 (urgent safety, this branch):** contract semantics implemented on the existing store (SQLite still holds
  the state; documented temporary dependency). Fixes the release-critical R5 traces.
* **R2 (authority migration):** adds the Bondok record column (live schema change, needs authorization),
  writes it for every item, enables rebuild-from-board, then retires SQLite business columns as authority.
  Remaining SQLite dependencies and the retirement gate are tracked in `MIGRATION_PLAN.md`.
  *Status 2026-10-10:* code implemented and tested (`src/waset_ops/record.py`, `tests/test_r5_authority.py`).
  The column id is fixed as `bondok_record` (monday `create_column` accepts a user id); records are written only
  while the board shows the column (an unknown id in `column_values(ids:)` is omitted by Monday, verified
  read-only), so the code can ship before the column exists. Reservations are not restored from the record:
  media is verified again first; owner times come back as requests.

## Consequences

* Board edits by the owner act directly; Slack and board reach the same commands.
* A second Monday writer with the owner's token is indistinguishable from the owner (declared boundary).
* One extra Monday write precedes each publication.
