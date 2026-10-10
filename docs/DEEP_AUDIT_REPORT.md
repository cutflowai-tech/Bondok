# Bondok V2 deep audit — report (2026-10-09/10)

## Scope and method
* Branch `bondok/deep-audit`, based on `45b6418` (deployed release `452da98` + the approved but undeployed Story
  trimming `271e1d0` and Slack model-credit fix `45b6418`). Nothing from this branch is deployed.
* Production was inspected read-only only: health/heartbeats, run leases, outbox, findings, holds, retries,
  versions, legacy receipts. No restart, deploy, workflow change, database write, Monday change or publish.
* Round 1: six parallel read-only audits (scheduling, publishing, media/WF1, Bondok AI/Slack, Monday sync,
  infrastructure). Every candidate was reproduced against the real code before it was accepted.
* Round 2/3: an independent review of all round-1 fixes, and a seeded randomized sequence tester (~1.3 M steps,
  3–6 items, owner/board/WF1/WF2/WF3/outbox/clock events interleaved, crashes and stale snapshots, invariants
  checked after every step, board convergence checked at the end of every seed).
* Each fix: reproduction → root cause → regression test that fails on the pre-fix code (run in a separate git
  worktree) → smallest fix → full suite → replay where the change touched workflows or the sync path.

## Results
* **76 register entries**; **66 fixed and verified**, 10 open (1 owner decision, 9 low). See `BUG_REGISTER.md`.
  * CRITICAL 5 — all fixed.
  * HIGH 25 — all fixed (incl. 2 regressions introduced by round-1 fixes and fixed in round 2).
  * MEDIUM 28 — 27 fixed, 1 open (owner decision BV-56).
  * LOW 18 — 9 fixed, 9 open.
* Final randomized campaigns on the fixed code: **0 invariant failures in 450 seeds / 300,000 steps** (display
  convergence included), down from failures in 74 of 150 seeds of the same campaign on the round-1 code.
* No publication-safety invariant failed in the randomized testing: never two publications of an item, never a
  commit after content/caption/format/source changed, never a claim for paused/skipped/held/unknown items, never
  a published item reverted.
* Production impact: none observed. Publishing had not started (0 reservations, 0 attempts); no stuck retries,
  escalations, holds or version churn existed on 2026-10-10.

### Most important fixes
1. **Owner authority** (BV-01/02): Bondok's "explicit owner words" check matched substrings, so questions and
   negations ("هو 350 مش منشور؟", "Topaz is not done", "postpone") executed protected actions, including
   re-scheduling an item whose publication outcome was unknown. Now whole-word, no question, no negation, and
   "not published" always needs approval.
2. **Duplicate publication paths** (BV-04/05/07): board "Posted"/post link was refused (item published again);
   an old failed attempt could reset a published item; v1 receipts were ignored.
3. **Board ↔ store consistency** (BV-03/19/20/21/65/70/71): stale board snapshots undid owner changes; WF2
   overwrote a person's Pause; out-of-order, dropped or never-resent display writes. Fixed with acked-write
   tracking bounded by the WF1 run start, WF2 compare-before-write, ordered supersede/retry rules.
4. **Instagram container loop** (BV-08): a container error created a new container every minute for 2 hours.
5. **Stuck work** (BV-09/12/26/27): unscheduled ready items, a crash that stopped the board poll, rechecks that
   never finished, temporary media failures that stopped retrying and starved the queue.
6. **Bondok reliability** (BV-22..25): actions hidden after a model failure, broken >6-call steps, replies to
   proposal notices dropped, database-down alert never raised.

### Previous incidents re-verified (replay + tests)
Legacy Post Date/Time, publish video links, System update, Video measurements and Processed format are never
cleared (0 cleared values in 3+3 replay cycles on the production baseline); no duplicate editor subtasks; the
queue progresses to new items (and waiting items take at most half a run); Dropbox errors are classified from
HTTP status only; caption approval binding; no repeated Instagram containers; stale media refused at commit.

## AI cost and performance
* Routine operations make **zero** model calls: approvals (now also when copied with backticks/period),
  notifications, watchdog, WF1/WF2/WF3. The scheduled model use is one caption draft per (item, title, brief).
* Removed unnecessary work: client-brief Monday reads every cycle after a rejected/failed draft; WF2 re-reading
  Monday every minute for a canceled source; 120 Instagram containers per failing slot; repeated Slack notices
  for missing items and board "Posted"; re-submitted requested times (version churn); superseded prepared
  files filling the disk.
* Added: one Monday read per guarded status/human-column write (WF2 compare-before-write), ≈10–15 per WF1 cycle.

## Production changes requiring approval (none applied)
1. Deploy the audit release (helper + `waset_ops`, Bondok `app.py`/`agent.py`/`bridge.py` + `waset_ops`), with
   backup and hash verification; restart Bondok.
2. Re-import **WF1** (Dropbox error classification) and **WF2** (compare-before-write) — both IDs unchanged.
3. This release includes the still-undeployed Story trimming and Slack model-credit changes.
4. Owner action (not a code change): add OpenRouter credit; Bondok answers fail with HTTP 402 when the
   remaining credit cannot cover a reply.
5. Owner decision: BV-56 (which Bondok actions may run without explicit wording).

Details: `BUG_REGISTER.md`, `REGRESSION_TEST_RESULTS.md`, `POST_RELEASE_IMPROVEMENTS.md`.
