# Regression test results — deep audit (2026-10-10)

Branch `bondok/deep-audit`. Command: `cd tests && python3 -m unittest test_acceptance test_workflows test_helper_cli test_bondok`.

## Unit / integration suite
| Module | Tests | Result |
|---|---|---|
| test_acceptance (scenarios, audit regressions, owner journey) | 140 | pass |
| test_workflows (graph contracts, generated n8n code incl. WF2 guard, Dropbox classification) | 34 | pass |
| test_helper_cli (real helper subprocess, real FFmpeg Story trimming, storage failure) | 11 | pass |
| test_bondok (Slack service: intent, approvals, model failures, watchdog) | 35 | pass |
| **Total** | **220** | **all pass** (124 before the audit; 209 after round 3) |

Python 3.14 run; all source parses as Python 3.12 (`ast.parse(feature_version=(3,12))`). Stdlib only in `src/`.

**Fail-before-fix evidence:** every audit regression test class was run against the commit before its fix in a
separate `git worktree` and failed there (outputs recorded in the session; e.g. `PublishingAuditCritical` 5/6
failing, `SchedulingAudit` 7/9, `OutboxRecovery` 4/5, `SlackAuditHigh` 6/6, `RoundTwoRegressions` 9/9,
`FuzzFindings` 4/4, `FuzzRoundThree` 2/2). Where a test passed on old code it was tightened until it reproduced
the bug (`OutboxRecovery.test_failed_older_job_never_overwrites_newer_state`, `MediaAudit.test_waiting_items…`,
`MondaySyncMedium.test_expired_notice…`).

Previous-incident coverage kept green throughout: legacy Post Date/Time and system columns never cleared,
duplicate editor subtasks, Dropbox error classification, queue coverage (not the same 20 items), caption
approval binding, repeated Instagram containers, stale media at commit, Story duration boundaries (real FFmpeg).

## End-to-end replay (real n8n 2.39.8, mocked Monday/Dropbox/Instagram, production baseline 2026-10-09 19:42)
| Run | Cycles | Cleared values | Group moves | Duplicate subtasks | Instagram calls | Errors |
|---|---|---|---|---|---|---|
| Story trimming (271e1d0) | 4 | 0 | 0 | 0 | 0 | 0 |
| Audit branch, after round 1 | 3 | 0 | 0 | 0 | 0 | 0 |
| Audit branch, after round 2 | 3 | 0 | 0 | 0 | 0 | 0 |
| WF2 compare-before-write (person's Skipped kept, other item written, next WF1 cycle converges) | 2 + targeted | — | — | — | 0 | 0 |

## Randomized sequence testing (seeded fuzzer, local scratch)
3–6 items; WF1 prep (versions, preflight, media ready/failed/retryable), owner commands (valid and invalid),
board edits incl. stale snapshots, WF2 claim/container/commit/result incl. unverifiable source and transient/
definitive/unknown provider answers, worker crashes, reconcile, WF3 repair, outbox failures/reordering/late
delivery, clock jumps up to 3 days; invariants after every step and board convergence at the end of each seed.

| Code | Seeds / steps | Safety invariant failures | Display-sync failures |
|---|---|---|---|
| round-1 HEAD `2fe1d51` | ~1,000 / ~500 k | 0 | many (F1–F9) |
| round-2 `71d8d01` | 150 / 60 k (display campaign) | 0 | 74 seeds |
| after `be00eed` | 150 / 60 k | 0 | 8 seeds |
| **final `efb72d0`** | **450 / 300 k** | **0** | **0** |
| round 4 `78d9af1` (BV-74 only suppressed) | 300 / 240 k | 0 | 0 |
| **round 4 final `6d92fa0`+, with Instagram 9007 answers** | **300 / 240 k** | **0** | **0** |

Safety invariants: never two publications per item; no claim/commit for paused, skipped, held, not-ready,
unreserved or unknown-outcome items; commit refused after caption/media/format/source/revision change; one
active attempt per item; unique slots; published never reverted; legacy receipt present for every committed
attempt; no unhandled exceptions. (Past reservations kept by published/unknown items, BV-74, are excluded as by
design.)

## Round 4 (2026-10-10): production re-check, current-state replay, end-to-end publishing

New regression tests (each fails on `45b6418`, the base of the branch, and passes on the branch):
`LegacyDateLoopIncident` (3), `MissingPreparedFileAtSlot`, `LowDiskAlert`, `MediaNotReadyAtPublish` (3; the third
fails on `78d9af1`, found by self-review), test_bondok `OwnerPublishWordsResume` (2); plus one guard without a bug
(`ReconciledPublicationShownOnBoard`). The production loop tests also fail on every audit commit before
`8a1457d`.

### Replay of the audit branch on today's production state
Fresh read-only copy of the production database and board (09:34 UTC), real n8n 2.39.8, mocked Monday/Dropbox/
Instagram, 4 WF1+WF2 cycles:

| Cycle | Board commands | Version bumps | Reservations | Instagram calls | Group moves | Slack notices |
|---|---|---|---|---|---|---|
| 1 | 40 (the 19 cleared pairs, once) | 58 | 23 → 23 | 0 | 0 | 0 |
| 2 | 0 | 15 | 23 → 23 | 0 | 0 | 0 |
| 3 | 0 | 14 | 23 → 21 (source changed*) | 0 | 0 | 0 |
| 4 | 0 | 12 | 21 → 19 (+2 / −4, source changed*) | 0 | 0 | 0 |

Production on the deployed release in the same period: ~330 board commands/hour, +2 versions per churning item per
cycle. *The Dropbox fixtures date from 2026-10-09; files whose mock content hash differs are (correctly) treated as
replaced.

### Rollback rehearsal
The deployed release (`452da98` helper + workflows) run for 3 cycles on the database the audit branch produced:
no workflow failure, no Instagram call, reservations kept, the BV-77 loop resumes for the ~7 requested-but-unreserved
items (expected for the old code). Rollback keeps the database (documented rule), so no ACL or data restore is involved.

### End-to-end publishing through real n8n WF2 (Instagram mock, first production slot 2026-10-11 11:00 UTC)

| Scenario | Deployed `452da98` (live) | Audit branch |
|---|---|---|
| Normal | 1 container, 1 publication | 1 container, 1 publication |
| Container status ERROR | **a new container every minute** (13 in 14 min, BV-08) | 3 containers, then held with a notice |
| Container IN_PROGRESS twice | waits, 1 publication | waits, 1 publication |
| Container creation HTTP 500 once | retried, 1 publication | retried, 1 publication |
| Publish HTTP 500 once / always | outcome unknown, never republished, owner asked | same |
| Publish succeeded, answer lost | reconciled from container status PUBLISHED (+6 min), no duplicate | same |
| Same, permalink failing | reconciled, no duplicate | same |
| Publish 400 "media not ready" (9007) | **marked failed, slot lost** | before `78d9af1`: same; now retried in the same slot (2 containers), 1 publication |
| Board Paused before the slot | not published | not published |
| Board "Posted" before the slot | **published again** (BV-04) | held, Slack note |
| Post link typed before the slot | **published again** (BV-04) | held, Slack note |
| Dropbox file replaced after scheduling | refused at commit (1 container), no publication | same |

No scenario published an item twice on the audit branch (final rerun on the final code: every WF2 run succeeded,
at most one publication per video). Board after a direct publication: status Posted, post link, Posted group;
after a reconciled one (answer lost): Posted and Posted group on the next sync, no post link (BV-60), publication
time = time of confirmation. Harness notes: the mock refuses any Instagram call other
than container create/status, media_publish and permalink; prepared files are local placeholders (verification
checks existence); a stale mock from an earlier replay and a shared n8n task-runner port each invalidated one run
before they were detected — the harness now refuses to start against a foreign mock, gives every replay its own
runner port, and reports failed workflow runs in every summary.

## Round 5 (2026-10-10): R5.2 repair on `bondok/r5-monday-authority`

Command (every module; the earlier four-module command silently left the R5 suites out — LOW-16):
`cd tests && python3 -m unittest discover -s . -p 'test_*.py'`

| Interpreter | Collected | Run | Skipped | Failed | Result |
|---|---|---|---|---|---|
| Python 3.12 (Homebrew) | 477 | 477 | 0 | 0 | OK |
| Python 3.13.5 | 477 | 477 | 0 | 0 | OK |
| Python 3.14 (Homebrew; the n8n image's major) | 477 | 477 | 0 | 0 | OK |

Node and FFmpeg were installed, so the generated-workflow JavaScript and real-media tests ran (none skipped).
Suites: acceptance, workflows, helper CLI, Bondok, deploy, and R5 safety / scheduling / publishing / board /
infra / media / n8n / language / captions / authority (R2 rebuild) / campaign. Mapping: `REPAIR_TRACEABILITY.md`.

### Multi-cycle campaigns (tests/test_r5_campaign.py)

Seeded simulated weeks (4 days of activity + a 7-day settling period, 10-minute cycles): new items including
copies of the same video, owner pause/resume/skip/Posted/time changes, file replacement mid-job, Instagram
answers (published, lost response, unknown, documented refusal), worker crashes, WF2/Monday/Slack outages and
restarts. Invariants are stated against a world model and the owner's decisions (I1 no video twice, I2 no claim
against an owner stop, I3 Slack delivered after outages, I4 every active item eventually published, I5 every
real publication recorded).

* Suite: seeds 11, 23, 37, 41 — pass.
* Soak (not in the suite): seeds 100–159 — 60/60 pass after one fix.
* Finding fixed (391d2a2): a definitively refused attempt left its file in the publication journal; after the item
  published another file, a different item holding the never-published file was blocked for ever.
* Harness corrections (not product changes): a file "replacement" on a paused item is not taken by WF1 (the
  oracle followed it wrongly); `request_publish` on a blocked duplicate answers `accepted`.
