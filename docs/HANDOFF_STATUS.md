# Handoff Status

Updated: 2026-10-10 ~17:30 UTC. Directive: R5.2 master implementation directive (private; owner handoff).
Milestone: **Phase 0 done (baseline, freeze). Phase 1–2 (contracts, urgent safety repair) IN PROGRESS.**
Nothing from this work is DEPLOYED. No production change was made by this session.

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

## Next executable task
Implement and test the urgent safety set on `bondok/r5-monday-authority`: A3 regression, B1, B2, B7, A1, A2,
A4, B3, B4, B5/B6/A14/A15, A6, A8; then the safe deployment tooling; then request one scoped rollout authorization.

## Test command
`cd tests && python3 -m unittest test_acceptance test_workflows test_helper_cli test_bondok` (220 OK at 91ee8c5,
Python 3.13.5). Python 3.12/3.14 are not installed locally (compatibility checked by syntax rules only so far).
