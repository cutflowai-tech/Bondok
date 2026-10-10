# Bondok V2 deep audit — report (2026-10-09/10)

## Round 4 — production re-check (2026-10-10 09:34 UTC) — read this first

**Production status (read-only):** workflows healthy (heartbeats current, errors.log empty, Bondok active), but:
* **BV-77 is live:** since 00:21 UTC a loop on 28 Stories re-submits legacy Post Date/Time edits every WF1 cycle
  (~330 commands/hour, item versions up to 204). Reproduced exactly; fixed on this branch by `8a1457d` (regression
  test `325f46e` fails on the deployed code). A replay of today's production state shows the branch handles the
  edits once and stays quiet.
* **OPS-1, disk:** 4.7 GB free of 96 GB; media preparation needs 6 GB, so it has been blocked since ≈03:00 UTC
  (16 Stories waiting) with no alert (now BV-81). The space is used by other systems on the same server.
* **OPS-2, Sunday 2026-10-11:** 23 Stories are reserved from board dates set at 03:21 Cairo; the first publishes
  at 11:00 UTC (14:00 Cairo). The five Stories the owner chose with Bondok on Friday are paused (BV-79: an
  ambiguous "stop" paused them; the owner's "publish them tomorrow" became approval requests that expired).

**End-to-end publishing (new):** WF2 driven through real n8n against an Instagram mock on today's data, for both
releases (table in REGRESSION_TEST_RESULTS.md). On the deployed release a board "Posted" or typed post link before
the slot **publishes the Story again** (BV-04) and a container error creates **a new Instagram container every
minute** (BV-08); the branch holds the item / stops after 3 containers. A board Pause works on both.

**Round 4 fixes (branch, not deployed):** BV-79 (`2ea6bb4`), BV-80 (`2187315`), BV-81 (`8646598`), BV-82
(`78d9af1`); tests for the production loops (`325f46e`, `b2c4951`). Rollback rehearsal: the deployed code runs
cleanly on a database the branch has used.

**Until the release is deployed (containment):** never post a scheduled Story by hand or mark it "Posted"/type
its link on the board — v2 will publish it again; to stop one, set **Paused** or **Skipped** on the board (verified
on the deployed release) or ask Bondok to pause it.

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
* **83 register entries** (+2 operational findings); **72 fixed and verified**, 11 open (1 owner decision, 10 low).
  See `BUG_REGISTER.md`.
  * CRITICAL 5 — all fixed.
  * HIGH 27 — all fixed (incl. 2 regressions introduced by round-1 fixes and fixed in round 2; BV-77 live in
    production, BV-79 observed in production).
  * MEDIUM 31 — 30 fixed, 1 open (owner decision BV-56).
  * LOW 20 — 10 fixed, 10 open (incl. BV-83, repository hygiene).
* Final randomized campaigns on the fixed code: **0 invariant failures in 450 seeds / 300,000 steps** (display
  convergence included), down from failures in 74 of 150 seeds of the same campaign on the round-1 code.
* No publication-safety invariant failed in the randomized testing: never two publications of an item, never a
  commit after content/caption/format/source changed, never a claim for paused/skipped/held/unknown items, never
  a published item reverted.
* Production impact: none observed in rounds 1–3 (publishing had not started). **Round 4 (09:34 UTC): BV-77 live (board loop), BV-79 observed (five Stories paused), BV-81 live (no low-disk alert); no publication has happened yet (0 attempts).**

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
1. **Deploy the audit release before 2026-10-11 11:00 UTC (first slot)**: helper + `waset_ops`, Bondok `app.py`/
   `agent.py`/`bridge.py`/`policy.txt` + `waset_ops`, with backup and hash verification; restart Bondok; re-import
   **WF1** and **WF2** (IDs unchanged). Includes the still-undeployed Story trimming and Slack model-credit changes.
   Rollback = previous code and workflow versions, keeping the database (rehearsed).
2. **Free disk space** on the server (OPS-1; other systems) or enlarge the disk. Do not delete Bondok's prepared
   media folder.
3. Owner action (not a code change): add OpenRouter credit; Bondok answers fail with HTTP 402 when the remaining
   credit cannot cover a reply.
4. Owner decisions: Sunday's schedule (OPS-2); BV-56 (which Bondok actions may run without explicit wording);
   whether an unknown outcome whose container stays FINISHED may be re-published (POST_RELEASE #5).

Details: `BUG_REGISTER.md`, `REGRESSION_TEST_RESULTS.md`, `POST_RELEASE_IMPROVEMENTS.md`.
