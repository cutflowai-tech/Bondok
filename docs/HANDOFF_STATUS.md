# Handoff Status

Updated: 2026-10-10 ~20:15 UTC. Directive: R5.2 master implementation directive (private; owner handoff).
Milestone: **R5 repair and R2 authority code IMPLEMENTED · TESTED · RELEASE READY. NOT DEPLOYED.**
No production change was made by this or the previous session (read-only Monday queries only).

## Current state (continuation session, 2026-10-10 evening)
* Integration branch `bondok/r5-monday-authority` contains every R5 workstream: urgent safety set, scheduling,
  publisher/WF2, board edits, infra/outbox, n8n transport + deployment tooling (agent worktree a808…),
  media/preparation (agent worktree ada0…), Bondok language reader (agent worktrees a4a5…/a5b1…), M18, LOW-12,
  LOW-13, LOW-18, R2 record + rebuild, campaign tests, grouped notices. Nothing uncommitted.
* Tests: 477 collected / run, 0 skipped, pass on Python 3.12, 3.13, 3.14 (`REGRESSION_TEST_RESULTS.md`).
* Every Round 5 register entry maps to an executable test (`REPAIR_TRACEABILITY.md`).
* Release candidate: built locally from the final commit with private `--creds`/`--site` inputs kept in
  gitignored `.local-snapshots/private/` → `release/<id>/` (`deployable: code ✔ workflows ✔`, no blockers).
* Rollout plan: `PRODUCTION_RUNBOOK.md` (C1 code, C2 workflow import, C3 activation, C4 Monday record column);
  go/no-go and gates: `RELEASE_READINESS.md`; field authority and retirement gate: `MIGRATION_PLAN.md`.
* Agent worktrees under `.claude/worktrees/` are merged; they can be removed (a4a5… still holds the
  uncommitted original copies of `language.py`/`test_r5_language.py`, identical in intent to the merged ones).

## Next executable task
Owner authorization for change sets C1–C3 (then C4), then the runbook: read-only preflight on the day,
`deploy.py code`, `deploy.py workflows`, activation, live acceptance (Gate 7). Owner decisions still open:
publication-journal exception (ADR §5), TEMP Story slots as permanent automatic slots (policy) or not,
distinct Monday integration identity (optional).

---
Earlier state (16:40 UTC baseline, kept for evidence):

## Where things are
* Local root `/Volumes/Zeno/Bondok` · GitHub `cutflowai-tech/Bondok` (public by owner decision).
* Integration branch: **`bondok/r5-monday-authority`** (from `bondok/deep-audit` @ `91ee8c5`). Integration and
  deployment owner: the session "Bondok pipeline rebuild implementation". Other sessions were told (2026-10-10
  16:40 UTC) not to edit the repo or production and not to deploy.
* Contracts: `OWNER_AUTHORITY_CONTRACT.md`, `PIPELINE_ARCHITECTURE_ADR.md`. Source register: `AUDIT_ROUND5_2026-10-10.md`
  (committed unchanged, sha256 9c57d9aa…) and `BUG_REGISTER.md` (BV-01…BV-83).

## Live baseline (read-only, 2026-10-10 16:33–16:40 UTC)
| Component | Live state |
|---|---|
| Helper + `waset_ops` (n8n data volume) | Release **452da98** (≈ base 45b6418) **plus an unattributed manual edit of `rules.py`** (15:12 UTC, backup `.bak-20261010`): Story grid extended with 15:10, 16:00, 18:30 Cairo, marked "TEMP owner slots 2026-10-10" but **not date-limited** (applies every day) |
| Bondok service | 452da98 copy; its `waset_ops/rules.py` is the unmodified grid → **Bondok and the helper disagree on valid Story times** |
| Release 91ee8c5 | **Not deployed** (no staged release on the server). Blocked: carries R5 A3 (date-only Publish at stops WF1) |
| WF1/WF2/WF3 | Heartbeats current (WF2 every minute, WF1 every 10 min, WF3 :05/:35) |
| Publications today | 3 Stories published by WF2 (owner times 15:10, 16:00, 18:30 Cairo); 0 attempts in flight |
| Reservations | 24, **all owner-pinned** (origin: board time edits, including the 00:12–00:21 UTC agent writes) → R5 A1 exposure |
| Next deadlines | Post 21:00 Cairo today, Story 22:00 Cairo today, then Story 14:00 Cairo 11 Oct |
| Holds | 19 Stories "unscheduled" (Publish at cleared by the 00:12 agent writes), no Slack notice → R5 A16 |
| Proposals | 21 `pending` in the DB (17 command, 4 caption), all past their 30-min expiry → R5 A8 / LOW-17 |
| Duplicate content | **Two Post items with the same code select the same Dropbox file** (both blocked on Topaz). If Topaz is confirmed on both, the live code would publish the same video twice → R5 B1 live exposure |
| Manual production edits today (postmortem) | Owner commands run inside the helper by an agent; `ops_reservations` edited directly; `rules.py` edited |

## Freeze actions (local, reversible)
* `deploy/deploy_91ee8c5.sh`, `deploy/import_workflows_91ee8c5.sh` (other session, untracked; one contains an n8n
  project id) now refuse to run (`exit 97` guard line; original text kept) and are excluded locally from Git.
* Local release build `release/91ee8c5` renamed `release/91ee8c5.BLOCKED-R5`.

## Containment needing owner action / authorization (not executed)
1. **Same video on two Post items (B1):** do not confirm Topaz on both; point one of them to its intended file
   (Dropbox Link) or skip it. No production change needed from us while both stay blocked.
2. **Slack wording (B5/B6/A14/A15) until the fix ships:** "بطل تنشرهم", "استنى قبل ما تنشرهم", "رجعها للمونتير",
   "تمام كمل" can *resume* items; "توباز هيبقى جاهز بكره" / "هو التوباز خلص" can confirm Topaz; "كان المفروض يبقى
   منشور امبارح" can mark an item published. Pause/skip from the board's Status column instead.
3. **Paused/Skipped → Posted on the board (B2):** if a Resume proposal appears afterwards, reject it.
4. **TEMP Story slots:** decide whether 15:10/16:00/18:30 should stay for future days; today they apply daily
   to automatic allocation. The repair supports owner off-grid times directly, so the edit can be reverted at
   release (needs authorization).

## Known blockers
* Distinct Monday integration identity (contract §1 boundary): needs a separate Monday user or app (owner).
* Technical journal exception (ADR §5): needs owner agreement before "Monday-only" can be declared met.
* 15 of 28 n8n workflows unreadable via MCP (earlier finding): cannot be ruled out as board writers.


## Test command
`cd tests && python3 -m unittest discover -s . -p 'test_*.py'` (477 OK on 3.12/3.13/3.14; Homebrew python@3.12
and python@3.14 were installed locally for this).
