# Handoff Status

Updated: 2026-10-09 (Africa/Cairo evening). Milestone: **IMPLEMENTED and TESTED, awaiting deployment approval.**
Nothing is DEPLOYED or ACTIVE. Production is unchanged by this work.

## Where things are
* Local root: `/Volumes/Zeno/Bondok` · GitHub: `cutflowai-tech/Bondok` (**public by owner decision**) ·
  branch `bondok/execution-authority` (pushed; not merged to `main`).
* Release builder: `python3 deploy/build_release.py --creds .local-snapshots/2026-10-09/n8n_live` →
  `release/<sha>/` with `MANIFEST.json` (sha256 per file). Raw evidence (DB snapshot, board snapshot, live workflow
  JSON with credential ids) is in gitignored `.local-snapshots/2026-10-09/` on the Mac only.

## Component status (2026-10-09 ~18:05 UTC)
| Component | Status |
|---|---|
| Helper v2 + `waset_ops` (data volume) | **DEPLOYED, ACTIVE** — release `60bbab0` (= `f5438c1`), 12/12 files hash-verified; v1 kept as `helper.v1.py` |
| Operational DB schema v1 (additive) | **DEPLOYED** — 16 `ops_*` tables; legacy rows unchanged; integrity ok |
| WF2 Publish When Due v2 | **DEPLOYED, ACTIVE** — active version `4c6e30a8…`; runs every minute, empty queue ends without Monday calls |
| WF3 Schedule Supervisor v2 | **DEPLOYED, ACTIVE** — active version `b037b023…`; first run 18:05 UTC |
| WF1 Prepare & Schedule v2 | **IMPORTED (inactive), NOT ACTIVE** — imported version `36b9fcb7…` (release `60bbab0`). Held: cutover gap found (duplicate editor subitems). Fix in `95a5868` (tested), needs owner approval to import + activate |
| Bondok v2 service | **DEPLOYED, ACTIVE** — 17/17 files hash-verified; `.env` +2 authorized keys; started 17:59:19 UTC, Slack identity/channel checks passed, Socket Mode connected, ops DB read/write verified in-service |
| Change Set A | DEPLOYED (17:39 UTC) |
| Backup | `/root/waset-v2-backup-20261009T175434Z/` (DB, v1 helper, ACLs, Bondok tarball, unit, v1 workflow JSON) |

## Authorization boundary
Granted: read-only inspection, local development, isolated tests, repository pushes (repo is public by owner decision);
**Change Set A with WF1 paused** (executed); adding `SLACK_BOT_USER_ID`/`SLACK_APP_ID` to the production `.env`
**only during an approved v2 deployment**.
**Change Set B authorized** (2026-10-09) and executed through step 8; WF1 activation held (see above).
Not authorized: importing/activating the patched WF1 (`95a5868`), any change to the Instagram token, approving captions on the owner's behalf.

## Known blockers / open items
1. Outage repaired by Change Set A. Rollback record on the server: `/root/waset-changeset-A-20261009T173954Z/` (ACL before/after, stat, DB sha256).
2. 15 of 28 n8n workflows remain unreadable via MCP; none can be ruled out as a board writer (NOT VERIFIED).
   Smallest owner action: enable "Available in MCP" (read) on them or export them.
3. Media transfer: prepared files are capped at 145 MB by the single-request Dropbox upload path; the 300 MB business
   limit is enforced but cannot be reached until an upload-session path is approved (quality-policy decision).
   Official Instagram Story video size limit NOT VERIFIED.
4. Monday native automations/webhooks not visible through the connector (3 board automations known, all inactive).
5. Instagram credential VERIFIED LIVE read-only (valid, @wasetcostudio, publish scopes granted, quota 0/100). Expiry NOT VERIFIED (user token; owner to check). `PUBLISHED` container-status reconciliation not exercised live.
6. No backup arrangement found for `state.sqlite`. Disk: 12.67 GB free after authorized cleanup (build cache + npm download cache only; images, volumes, backups untouched).
7. 35 legacy Post captions have unknown approval status → owner approval needed after cutover (one bulk proposal).

## Next actions (in order)
1. Owner decides on Change Set B (cutover) using `docs/CHANGE_SET_B_CHECKLIST.md`.
2. On B: run `deploy/server_backup.sh`, follow MIGRATION_AND_ROLLBACK.md steps 2–9, record DEPLOYED/ACTIVE with read-back hashes.
3. After cutover: owner approves legacy captions; confirm Topaz per selected file; watch first WF2 publication.

## Test commands
`cd tests && python3 -m unittest test_acceptance test_workflows test_helper_cli test_bondok` → 84 OK (2026-10-09).
