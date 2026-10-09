# Handoff Status

Updated: 2026-10-09 (Africa/Cairo evening). Milestone: **IMPLEMENTED and TESTED, awaiting deployment approval.**
Nothing is DEPLOYED or ACTIVE. Production is unchanged by this work.

## Where things are
* Local root: `/Volumes/Zeno/Bondok` · GitHub: `cutflowai-tech/Bondok` (**public by owner decision**) ·
  branch `bondok/execution-authority` (pushed; not merged to `main`).
* Release builder: `python3 deploy/build_release.py --creds .local-snapshots/2026-10-09/n8n_live` →
  `release/<sha>/` with `MANIFEST.json` (sha256 per file). Raw evidence (DB snapshot, board snapshot, live workflow
  JSON with credential ids) is in gitignored `.local-snapshots/2026-10-09/` on the Mac only.

## Component status
| Component | Status |
|---|---|
| `waset_ops` handler + helper v2 | IMPLEMENTED, TESTED (3.12, 3.13, 3.14) |
| WF1/WF2/WF3 v2 exports | IMPLEMENTED, contract-TESTED; not imported into n8n |
| Bondok v2 service | IMPLEMENTED, TESTED offline; deployed Bondok is still the old version |
| Migration/rollback | REHEARSED on isolated copies of live data |
| Live v1 system | **BLOCKED/failing since ~15:00 UTC** (WF2 errors every minute) — fix is change set A |

## Authorization boundary
Granted: read-only inspection, local development, isolated tests, repository pushes.
Not granted (needs explicit owner approval per change set): **A** — ACL repair of the SQLite sidecar files (restores v1
operation); **B** — v2 cutover (backup, freeze, helper install, schema migration, workflow import/activation, Bondok
install + restart, two new `.env` keys). See `docs/MIGRATION_AND_ROLLBACK.md` for exact steps and rollback.

## Known blockers / open items
1. Production outage (change set A) — needs approval; cause is INFERRED from file ownership/ACL evidence.
2. 15 of 28 n8n workflows remain unreadable via MCP; none can be ruled out as a board writer (NOT VERIFIED).
   Smallest owner action: enable "Available in MCP" (read) on them or export them.
3. Media transfer: prepared files are capped at 145 MB by the single-request Dropbox upload path; the 300 MB business
   limit is enforced but cannot be reached until an upload-session path is approved (quality-policy decision).
   Official Instagram Story video size limit NOT VERIFIED.
4. Monday native automations/webhooks not visible through the connector (3 board automations known, all inactive).
5. Instagram account scopes and `PUBLISHED` container-status reconciliation not exercised live.
6. No backup arrangement found for `state.sqlite`; server disk is 91% used.
7. 35 legacy Post captions have unknown approval status → owner approval needed after cutover (one bulk proposal).

## Next actions (in order)
1. Owner decides on change set A (outage) and B (cutover).
2. On B: run `deploy/server_backup.sh`, follow MIGRATION_AND_ROLLBACK.md steps 2–9, record DEPLOYED/ACTIVE with read-back hashes.
3. After cutover: owner approves legacy captions; confirm Topaz per selected file; watch first WF2 publication.

## Test commands
`cd tests && python3 -m unittest test_acceptance test_workflows test_helper_cli test_bondok` → 84 OK (2026-10-09).
