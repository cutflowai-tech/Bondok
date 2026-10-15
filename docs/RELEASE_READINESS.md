# Release readiness — R5.2 repair

Updated 2026-10-12 after the release review (standards/spec review of this branch, then test-first repairs
`f21915d`, `bedd902`). Branch `bondok/r5-monday-authority`. Status: **NOT DEPLOYED**.
Rollout: `PRODUCTION_RUNBOOK.md` (change sets C1–C4, each needs the owner's authorization).

## Go / no-go

**NO-GO until one local check passes, then GO for C1–C3** as one publication cutover (runbook §3), subject to the
read-only preflight on the day (live hashes reviewed, no reserved slot within 30 minutes, no attempt in flight).

Remaining release blocker (local, no production access needed):
* **WF2 intent branch in real n8n.** The new nodes (`Publication Intent?` → `Build Intent Write` → `Read Board
  Before Intent` → `Check Intent Guard` → `Write Publication Intent` → `Acknowledge Publication Intent` →
  `Committed?`) are verified by graph-contract tests and by executing their JavaScript in Node, not yet by an
  n8n run (no n8n runtime in the review environment). Re-run the round-4 WF2 end-to-end publishing check (real
  n8n, Monday/Dropbox/Instagram mocks; `REGRESSION_TEST_RESULTS.md`) on `bedd902` or later: one due item must
  publish exactly once after the intent write, and a Status changed on the mock board after the claim must
  publish nothing.

Owner items (not code): authorization of C1–C3 (then C4); the ADR §5 publication-journal exception; the n8n
workflow inventory (gate 1). Owner decisions recorded 2026-10-12 (`OWNER_AUTHORITY_CONTRACT.md` §0): Monday is
the authoritative business-state database; Story trimming 60–65 s; explicit owner off-grid times.

## Gates (directive §20)

| Gate | Status | Evidence / what remains |
|---|---|---|
| 1 Live risk and source coverage | PARTIAL | Live baseline 2026-10-10 16:33–16:40 UTC (`HANDOFF_STATUS.md`); 91ee8c5 and its scripts frozen. Remaining: re-read live hashes/deadlines right before rollout; **15 of 28 n8n workflows were unreadable** via MCP and cannot be ruled out as board writers (needs n8n access) |
| 2 Authority | IMPLEMENTED · TESTED · BLOCKED on owner | Owner board edits and Slack act through the same commands (R5 suites); R2 record + rebuild (`test_r5_authority.py`). Needs: C4 column, owner agreement on the publication-journal exception (ADR §5), a distinct integration identity is optional (contract §1 boundary declared) |
| 3 Safety | TESTED | Monday intent acknowledged before media_publish (ADR §4), board provenance of Monday commands, empty-Status Pause race end to end (`test_r5_release.py`); B1 duplicate, Posted→Resume, Arabic polarity/targets, stale source/Topaz, projection race, unknown outcome: `REPAIR_TRACEABILITY.md`; campaign invariants I1/I2 |
| 4 Liveness | TESTED | Missed pins, expired proposals, held items, permanent failures, chunk cancellations; campaign I4 (64 seeds) |
| 5 Data and operational health | TESTED locally | Full-column projection tests, disk alerts with hysteresis, run lifecycle, notification recovery; production replay (`REPLAY_RESULTS.md`). Live check after rollout |
| 6 Shipping | READY | `deploy/build_release.py` (allowlist, manifest, placeholders, credential resolution) and `deploy/deploy.py` (backup + restore test, drift gate, fence/drain, fail-closed timeout, hash-verified rollback, containment): `test_deploy.py`. Candidate built with private inputs from the pre-review commit: `deployable: code ✔ workflows ✔`; **rebuild it from the final commit** (`deploy/build_release.py`, private inputs) before C1. Rollback data compatibility: live helper 452da98 runs on a database written by the new code |
| 7 Live acceptance | NOT STARTED | Requires deployment; checklist in the runbook §5 |

## Test evidence

**504 tests collected, 504 run, 0 skipped, all pass** on Python 3.12, 3.13 and 3.14 (2026-10-12, `bedd902`;
`cd tests && python3 -m unittest discover -s . -p 'test_*.py'`). Before the release review: 482 (the 477 stated
here earlier was out of date). New: `test_r5_release.py` (16), `test_r5_owner_status.py` (+6), one assertion in
`test_bondok.py`. Soak: campaign seeds 100–159 pass (earlier run; the four default seeds run in the suite).

## Known limits (stated, not hidden)

* Mocked model/Slack: the language contract is tested with a scripted worst-case model. Only a live, authorized
  check can show whether the production model fills the structured `meaning` reliably (if not: extra questions,
  never wrong actions), that the strict schema is accepted by the provider, and how real Slack events encode
  emoji/RLM/quotes and alert-thread replies.
* Monday has no compare-and-set: the ~1 s window between the guard read and a display or intent write remains
  (declared); after the intent write, a Pause can no longer stop the publish call already in flight.
* Automation and the owner share one Monday identity (declared boundary; see contract §1).
* The live `rules.py` "TEMP owner slots" edit is replaced by the release; owner-chosen times no longer need it.
