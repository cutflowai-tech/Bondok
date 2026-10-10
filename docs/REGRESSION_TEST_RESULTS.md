# Regression test results — deep audit (2026-10-10)

Branch `bondok/deep-audit`. Command: `cd tests && python3 -m unittest test_acceptance test_workflows test_helper_cli test_bondok`.

## Unit / integration suite
| Module | Tests | Result |
|---|---|---|
| test_acceptance (scenarios, audit regressions, owner journey) | 131 | pass |
| test_workflows (graph contracts, generated n8n code incl. WF2 guard, Dropbox classification) | 34 | pass |
| test_helper_cli (real helper subprocess, real FFmpeg Story trimming, storage failure) | 11 | pass |
| test_bondok (Slack service: intent, approvals, model failures, watchdog) | 33 | pass |
| **Total** | **209** | **all pass** (124 before the audit) |

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

Safety invariants: never two publications per item; no claim/commit for paused, skipped, held, not-ready,
unreserved or unknown-outcome items; commit refused after caption/media/format/source/revision change; one
active attempt per item; unique slots; published never reverted; legacy receipt present for every committed
attempt; no unhandled exceptions. (Past reservations kept by published/unknown items, BV-74, are excluded as by
design.)
