# Migration plan — Monday as the business record (ADR R1 → R2)

Status 2026-10-10: R1 and the R2 code are **IMPLEMENTED and TESTED** on `bondok/r5-monday-authority`; nothing is
deployed. Operational steps: `PRODUCTION_RUNBOOK.md`. Earlier change sets (A, B): `MIGRATION_AND_ROLLBACK.md`.

## Per-field authority

| Business fact | Owner surface | Where it lives after R2 | Rebuilt from the board? |
|---|---|---|---|
| Format | Format column | board column (owner) | yes (column) |
| Caption + approval | Caption column / Slack draft approval | column + record `cap` (fingerprint of the approved text) | yes; an unmatched caption is "needs approval" |
| Topaz confirmation for the shown file | Topaz column | record `tz` (asset key bound at the click) | yes, while the column still says Topazed |
| Selected source file | Dropbox link / Slack | record `src` | yes |
| Requested time | Publish at (or legacy date/time) | record `req`/`rb`; pinned slot `res.p` | yes, as the owner's request |
| Confirmed slot | — (system) | store reservation; record `res` (display) | recomputed after re-verification |
| Pause / skip | Status column / Slack | record `os`/`osr` + status column | yes; a board change meanwhile acts as an owner edit |
| Holds (owner questions) | — | record `hold` (kind, reason) | yes |
| Publication outcome, media id | Posted / Post link / Slack | record `pub`/`mid` + result columns | yes; published content re-journalled |
| Content identity (duplicate guard) | — | record `ch`/`sha` + local journal | yes |

## What stays local (transient execution primitives)

Leases and fences (`ops_runs`), the outbox, media jobs and prepared files, command idempotency records, proposals
in flight, and the **publication journal** (`ops_attempts`, legacy `publications`/`assets`). The journal is the
ADR §5 exception: it can only *block* a publication, never authorize one. **Owner agreement for that exception
is still required** before "Monday-only" is declared met.

## Remaining SQLite dependencies and the retirement gate

1. Until C4 (record column) is live, a lost store means a conservative re-import (owner decisions on the board
   columns survive; Topaz binding, approvals and holds would be asked again). After C4: rebuild from the record.
2. Proposals waiting for an answer are not on Monday (they are re-asked after a rebuild).
3. Retirement gate (declare "Monday-only" met): C4 live for every item for at least one full WF1 cycle; a
   supervised rebuild drill on a copy of the live store (delete the copy's `ops_items`, observe the live board
   read-only into it, compare owner facts); owner agreement on the journal exception.

## Attribution boundary (contract §1)

Automation and the owner write as the same Monday user. A second writer using the owner's token is
indistinguishable from the owner. Mitigation: one applier with compare-before-write; a distinct integration
identity needs a separate Monday seat or app (owner decision, not assumed).

## Pending/legacy data at rollout

* Schema version unchanged (1); no migration step. New values are additive (`no_brief` draft marker, new
  `ops_meta` keys, record column writes).
* Holds and missed owner times from 2026-10-10 are kept and asked/explained by the new code (A1/A16).
* 21 expired `pending` proposals (17 command, 4 caption): the supervisor closes expired command proposals and
  ends their holds (LOW-17); a caption draft stays approvable while it is the current draft (A8).
* The live `rules.py` TEMP slots edit is replaced by the release (see runbook §1).
