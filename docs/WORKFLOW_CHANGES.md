# Workflow Changes (v2)

Built deterministically by `workflows/build.py` from `workflows/original/` (identical to live versions
WF1 `e520d122`, WF2 `9351a690`, WF3 `e37aa4f0` on 2026-10-09). Output: `workflows/dist/*.v2.json`.
IDs, names, settings (timezone Africa/Cairo, timeouts) and credential references (by name) are preserved; exports
are inactive and carry `<REDACTED>` credential ids; `deploy/build_release.py --creds` maps them to live ids.

## WF2 — Publish When Due (78 → 64 nodes; only publisher)
* Start: two branches — **Display Sync** (outbox take → Monday mutation per job → ack; failures retry with backoff, escalate after 6) and **Due Work** from durable state.
* Empty queue returns no items: no Monday call (old `items(ids:[null])` removed).
* Per item: fresh Monday read of the item + its source status (trusted adapter) → `claim` (observes edits first; validates reservation, revision, payload, verification, owner state, due window 0–2 h) → resume container only if the payload fingerprint matches, else create from the **claimed payload** (caption from durable state) → poll (30 s × ≤20, lease renewed each poll; ERROR/EXPIRED → abandon pre-commit) → Dropbox revision re-check → **commit** (commitment point) → `media_publish` (full response) → `result`.
* Result classification: HTTP 200 + id = published; HTTP 4xx with provider error body = confirmed failed; anything else (timeouts, 5xx, 200 without id) = **outcome unknown**, no retry.
* Reconcile branch: unknown attempts are re-checked by container status; `PUBLISHED` confirms; anything else stays unknown and escalates.
* Removed: all direct Monday status writes ("Needs Review" overwrites, Mark Posted, Save Publication Note), Monday-status-based due queue, v1 helper routes.

## WF3 — Schedule Supervisor (44 → 9 nodes)
* Cron unchanged (`:05`, `:35`). One helper call `/v2/monitor/run`: run lease (contention → `deferred`, not "completed"), read-only inspection, bounded repair commands, findings with fingerprints (new findings notify once; resolved findings close).
* Manual trigger now runs **read-only** `/v2/monitor/inspect` (the old Manual Audit wrote to the board).
* Removed: board-wide Monday reads/writes, repair notes as Monday updates, v1 lock/reserve/commit/log.

## WF1 — Prepare & Schedule (180 → 114 nodes)
* Run lease with fencing (`/v2/run/start` … `/v2/run/finish`); a superseded run's results are discarded. Lease renews on every handler call (15 min TTL) instead of a 35-minute lock that survived crashes.
* Source and social snapshots reused (paginated, refuse partial pages; social snapshot now includes `group`).
* Import: `/v2/import/plan` (durable identity, ambiguity findings) → existing `create_item` batch → `/v2/import/record`.
* Board observation in ≤60 KB chunks (each helper argument < 120 KB, guarded) → edits become commands; `/v2/board/missing` reports items that vanished (never recreated).
* `/v2/prep/queue`: deterministic fair queue (round-robin by variety key, oldest first, explicit rechecks first, bounded backoff for blocked items, 30-min metadata-only re-list for ready items).
* Caption branch (Posts without approved/pending caption): brief → `/v2/caption/needed` (input hash; never regenerates) → Write Caption (`onError: continue`) → `/v2/caption/draft` → owner approval. Media work continues regardless.
* Dropbox branch reused (folder creation, listing, `Candidates` selection rules). `Candidates` now distinguishes wrong folder (`config`) from no final file (`editor`); Dropbox API errors are classified `infra` (timeouts/5xx/429) or `config`.
* `/v2/prep/step`: records the exact file (id, rev, content hash), invalidates on change, runs Story duration preflight before Topaz/encoding, requires Topaz bound to the asset, prepares/reuses immutable media; explicit rechecks re-measure.
* Upload path reused (single-request Dropbox upload) → `/v2/prep/delivered` → handler verifies and allocates a slot.
* Editor tasks drained from the outbox (create subitem + instructions, or update existing task), acked with the task id.
* Maintenance: `/v2/maintenance` deletes local prepared files only for confirmed publications older than 30 days.
* Removed: Light AI Diversify Styles + Small Style Model, Reconcile Reserved Slots, Board Schedule, Preparation Queue, the seven Guard quartets and all Save* Monday writes, Waiting for Editor / Editor Instructions / Validated Media Update / Item Needs Review writers, Reserve/Commit/Cancel slot calls, Early Story Duration nodes (moved into `/v2/prep/step`), Invalidate Changed Content.

## Validation performed (see ACCEPTANCE_EVIDENCE.md)
Connection integrity and reachability, `$('Node')` references, only `/v2` routes that exist in the dispatcher, single publisher, social-board writes only via sync/creation, LLM only behind `Generate Draft?`, payload guard on every helper call, no secrets / credential ids, every Code node parses (Node `--check`), behavioural harness for publish-outcome classification, empty due queue, sync mutation shapes, Dropbox error classification and chunk sizes.

**Not validated:** import into n8n itself and live execution (requires production authorization). n8n-specific semantics (pairing in `runOnceForEachItem`, `fullResponse` shape) follow the existing workflows' usage but are NOT VERIFIED on the instance until a supervised import.
