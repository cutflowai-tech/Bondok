# Release readiness — R5.2 repair

Date: 2026-10-10. Branch `bondok/r5-monday-authority`. Status: **RELEASE READY · NOT DEPLOYED**.
Rollout: `PRODUCTION_RUNBOOK.md` (change sets C1–C4, each needs the owner's authorization).

## Go / no-go

**Go for C1–C3** (code + workflows), subject to the read-only preflight in the runbook §1 on the day (live
hashes reviewed, no reserved slot within 30 minutes, no attempt in flight).
**C4** (Monday record column) may follow at any time after C1.
"Monday-only" is **not** declared met until the gate 2 owner items below are settled.

## Gates (directive §20)

| Gate | Status | Evidence / what remains |
|---|---|---|
| 1 Live risk and source coverage | PARTIAL | Live baseline 2026-10-10 16:33–16:40 UTC (`HANDOFF_STATUS.md`); 91ee8c5 and its scripts frozen. Remaining: re-read live hashes/deadlines right before rollout; **15 of 28 n8n workflows were unreadable** via MCP and cannot be ruled out as board writers (needs n8n access) |
| 2 Authority | IMPLEMENTED · TESTED · BLOCKED on owner | Owner board edits and Slack act through the same commands (R5 suites); R2 record + rebuild (`test_r5_authority.py`). Needs: C4 column, owner agreement on the publication-journal exception (ADR §5), a distinct integration identity is optional (contract §1 boundary declared) |
| 3 Safety | TESTED | B1 duplicate, Posted→Resume, Arabic polarity/targets, stale source/Topaz, projection race, unknown outcome: `REPAIR_TRACEABILITY.md`; campaign invariants I1/I2 |
| 4 Liveness | TESTED | Missed pins, expired proposals, held items, permanent failures, chunk cancellations; campaign I4 (64 seeds) |
| 5 Data and operational health | TESTED locally | Full-column projection tests, disk alerts with hysteresis, run lifecycle, notification recovery; production replay (`REPLAY_RESULTS.md`). Live check after rollout |
| 6 Shipping | READY | `deploy/build_release.py` (allowlist, manifest, placeholders, credential resolution) and `deploy/deploy.py` (backup + restore test, drift gate, fence/drain, fail-closed timeout, hash-verified rollback, containment): `test_deploy.py`. Candidate built with private inputs: `deployable: code ✔ workflows ✔`, no blockers. Rollback data compatibility: live helper 452da98 runs on a database written by the new code |
| 7 Live acceptance | NOT STARTED | Requires deployment; checklist in the runbook §5 |

## Test evidence

477 tests, 0 skipped, pass on Python 3.12, 3.13 and 3.14 (`REGRESSION_TEST_RESULTS.md`). Soak: campaign seeds
100–159 pass.

## Known limits (stated, not hidden)

* Mocked model/Slack: the language contract is tested with a scripted worst-case model. Only a live, authorized
  check can show whether the production model fills the structured `meaning` reliably (if not: extra questions,
  never wrong actions), that the strict schema is accepted by the provider, and how real Slack events encode
  emoji/RLM/quotes and alert-thread replies.
* Monday has no compare-and-set: the ~1 s window between the guard read and a display write remains (declared).
* Automation and the owner share one Monday identity (declared boundary; see contract §1).
* The live `rules.py` "TEMP owner slots" edit is replaced by the release; owner-chosen times no longer need it.
