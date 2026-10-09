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
| WF1 Prepare & Schedule v2 | **DEPLOYED, STOPPED** — patched version `8d6686d8…` (`95a5868`) ran once (52060, 18:10–18:14 UTC, success; no duplicate editor subitems). Stopped 18:16 UTC: display projection cleared legacy Post Date/Time on 12 items. Fix in `waset_ops/core.py` (this commit, tested + replayed), not deployed |
| Bondok v2 service | **DEPLOYED, ACTIVE** — 17/17 files hash-verified; `.env` +2 authorized keys; started 17:59:19 UTC, Slack identity/channel checks passed, Socket Mode connected, ops DB read/write verified in-service |
| Change Set A | DEPLOYED (17:39 UTC) |
| Backup | `/root/waset-v2-backup-20261009T175434Z/` (DB, v1 helper, ACLs, Bondok tarball, unit, v1 workflow JSON) |

## Authorization boundary
Granted: read-only inspection, local development, isolated tests, repository pushes (repo is public by owner decision);
**Change Set A with WF1 paused** (executed); adding `SLACK_BOT_USER_ID`/`SLACK_APP_ID` to the production `.env`
**only during an approved v2 deployment**.
**Change Set B authorized** (2026-10-09) and executed through step 8; WF1 activation held (see above).
Not authorized: importing/activating the patched WF1 (`95a5868`), any change to the Instagram token, approving captions on the owner's behalf.

## Incident 2026-10-09 18:14 UTC — legacy dates cleared by first WF1 run
* Effect (corrected after a full-column audit): run 52060 emptied Post Date + Post Time on 12 items and Publish video links on 14 items; runs 52060 and 52081 together emptied v1 metadata — System update (40 items), Video measurements (30), Processed format (30), Version Check (2). Action required was overwritten on 40 items (v2-owned display; 4 show a misleading "Dropbox request failed"). Captions, Format, Topaz, Notes, Story variety, Posted, post links, IG ids, publication fields unchanged; 0 reservations/attempts/publications.
* Restored so far: the 12 Post Date/Time values (owner-approved). Remaining restore list (from the read-only pre-v2 board snapshot) kept locally: `restore_technical.json`.
* Cause: bootstrap recorded v1/human date values as the confirmed projection; for items without a reservation the projection then cleared them as if they were system output.
* Containment: WF1 deactivated; no pending display jobs; 10 of 12 future values retained in `ops_items.requested_at`; all 12 originals in `.local-snapshots/…/restore_legacy_dates.json` (local only).
* Fix 1 (`1ab5e46`, deployed): never clear dates/links not written by v2 — insufficient (other v1 columns still cleared).
* Fix 2 (this commit): the projection never clears ANY board value it did not write; WF1 keeps string-form Dropbox errors. Replay of the real first run (40 items, pre-v2 snapshot): 0 clears.
* WF1 stopped again at 18:26 UTC. Pending owner approval: deploy fix 2, restore the technical values, re-activate WF1.

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

## 2026-10-09 evening — recovery and isolated replay
* Fix 2 (`463bc09`) deployed to helper + Bondok (29 files verified); WF1 imported **inactive**.
* Restored 116 cleared v1 values (Publish video 14, System update 40, Video measurements 30, Processed format 30,
  Version Check 2) after verifying each was still in the incident state; recorded as board-owned in the DB.
* Corrected the misleading "Dropbox request failed" Action text on 7 items (verified cause: WF1 expression bug).
* Isolated replay (real n8n 2.39.8, production copies, mocked externals): see `REPLAY_RESULTS.md`.
  12 defects fixed on this branch, **not deployed**. WF1 stays inactive pending owner approval.

## 2026-10-09 19:43–20:05 UTC — release `452da98` deployed, WF1 active (owner-authorized)
* Backup before deploy: `/root/waset-predeploy-452da98-20261009T194231Z/` (helper, Bondok waset_ops, DB).
* 29 files verified against the manifest; Bondok `ready`; one-time ownership backfill applied (40 items).
* WF1 imported identical to the release (version `f1be304a…`) and activated.
* First run (52214) vs fresh baseline: 0 cleared values, 0 group moves, 20 items all never checked before,
  0 duplicate subitems, 0 repeated editor instructions (10 queued jobs recognised as already sent),
  0 reservations/attempts/publications, 2 new caption drafts pending owner approval (0 approvals executed).
* Second run (52227): 20 more items (2 revisited while waiting for a measurement), 3 first-time editor
  subitems, 0 clear writes. WF2 every minute and WF3 (20:05) successful; no open findings; errors.log empty.
* Post-release review items: see `REPLAY_RESULTS.md` → Follow-up.
