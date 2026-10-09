# Migration, Cutover and Rollback

Nothing in this document has been executed against production. Each change set needs explicit owner authorization.
Server paths below are the existing production paths (they are not moved).

* Data volume (n8n `/home/node/.n8n-files/waset-social`, host `/var/lib/docker/volumes/waset_social_data/_data`, Bondok `/var/lib/bondok/pipeline`)
* Bondok code `/opt/waset-bondok` (service `bondok.service`, user `bondok`), state `/var/lib/bondok/bondok.sqlite`

## Change set A — repair the current outage (small, independent)

**Problem (INFERRED, strong evidence):** `state.sqlite-shm`/`-wal` were created by Bondok (uid 999) and are not writable by
n8n's uid 1000, so every helper call fails (see ACCESS_AND_BASELINE.md).

**Change** (root on the host, data dir):
```
cd /var/lib/docker/volumes/waset_social_data/_data
getfacl -p . state.sqlite state.sqlite-shm state.sqlite-wal > /root/waset-acl-before.txt
setfacl -m u:1000:rw state.sqlite-shm state.sqlite-wal
setfacl -d -m u:1000:rw .          # future sidecars created by either user stay writable by both
```
**Effect:** the existing v1 workflows (and the old Bondok) resume exactly as they behaved before 15:00 UTC, including the
v1 risks listed in `docs/reports/07`. WF2 may publish due items again under v1 rules (currently 0 reservations).
**Verify:** next WF2 execution succeeds; `health` table gets fresh rows; no new `errors` in execution list.
**Rollback:** `setfacl --restore=/root/waset-acl-before.txt` (returns to the failing state; no data change).
Change set B requires A (Bondok v2 and n8n write the same file).

## Change set B — v2 cutover (release = tested commit, see HANDOFF_STATUS.md)

Preconditions: A applied; MANIFEST hashes recorded; owner available on Slack; no slot due within 60 minutes
(`/v2/health` / board check); no running WF2 execution.

1. **Backup (consistent, outside Git, on the server, 0700):** SQLite backup API copy of `state.sqlite`
   (`deploy/server_backup.sh`), live JSON of WF1/WF2/WF3 (+ their version ids), tarball of `/opt/waset-bondok`
   (`.env` copied with `cp -p`, never transmitted), current `helper.py`, ACL listing. Record sha256 of each.
2. **Freeze writers in dependency order:** deactivate WF3, then WF1 (n8n). Stop `bondok.service`
   (removes the old competing writer). Wait until no WF1/WF3 execution is running.
3. **Drain the publisher:** deactivate WF2; wait for running/waiting WF2 executions to finish. Inspect legacy
   `publications` for `claimed/container_created/publish_requested` (today: 0). Unresolved ones become `outcome_unknown`
   v2 attempts by hand-off note, never deleted.
4. **Install helper bundle** into the data volume (uid 1000, 0644): `waset_ops/` + new `helper.py`; keep the old file as
   `helper.v1.py`. Verify sha256 against MANIFEST.
5. **Migrate (additive):** run `/v2/health` once as uid 1000 inside the container
   (`docker exec -u node n8n-n8n-1 python3 …/helper.py <b64>`); confirm `schema: 1`. Legacy rows untouched (rehearsed).
6. **Import v2 workflows** with the same IDs (inactive). Read back node counts and version ids; diff against `release/…/workflows`.
7. **Activate one responsibility at a time:**
   1. WF1 → watch one full execution: bootstrap imports all items with **0 board writes** and 0 notifications (rehearsed on the live board copy: 164 items, 0 edits, 0 writes); preparation starts.
   2. WF2 → display sync + due queue; confirm empty queue ends cleanly.
   3. WF3 → one supervised run (`deferred` if WF1 holds the lease is normal).
8. **Bondok v2:** install `bondok/` + `waset_ops/` into `/opt/waset-bondok` (root-owned, 0644; `.env` untouched except
   adding `SLACK_BOT_USER_ID` and `SLACK_APP_ID`, whose values are the identities already pinned in the deployed code).
   Restart `bondok.service`; expect log `ready model=openai/gpt-6.1-sol … ops_schema=1`.
9. **Read-back verification:** hashes vs MANIFEST; active workflow versions = imported ones; `/v2/health` heartbeats
   for wf1/wf2/wf3; `errors.log` free of `retired` (no v1 caller left); Bondok watchdog quiet; owner sends one read-only
   Slack question. Report DEPLOYED/ACTIVE per component.

Owner experience right after cutover: 35 Posts carry captions of unknown approval → Bondok can present them in one
proposal (`approve_all_existing_captions`). Items wait on Topaz confirmation for their selected file version (0 bound
today). Legacy future dates are honoured only if valid and free.

## Rollback (rehearsed on isolated copies)

Rules: never activate v1 while a v2 writer is active; never restore an older database over newer receipts; keep
confirmed and unknown outcomes.

1. Deactivate WF2, WF1, WF3 (v2). Stop `bondok.service`.
2. Restore `helper.v1.py` → `helper.py` (keep `waset_ops/`, harmless). **Keep the migrated database**: the old helper
   runs on it (verified: snapshot/lock/unlock OK) and the v2 receipts mirrored into `publications`/`assets` make the old
   claim refuse republishing (verified: old claim returns `stage: published`).
3. Restore the previous workflow versions (`e520d122…`, `9351a690…`, `e37aa4f0…`) via version history; activate WF2,
   then WF1, then WF3.
4. Restore old Bondok code from the backup tarball; start `bondok.service`.
5. Post-rollback: v2 reservations are not mirrored; v1 re-reserves from Monday dates on its own cycle (fails closed).
   Any v2 `outcome_unknown` attempt stays listed for manual verification.

## Rehearsal evidence
`python3 deploy/rehearse.py <state snapshot> <board snapshot> reference/deployed_2026-10-09/helper/helper.py`
(isolated temp copies) — results in ACCEPTANCE_EVIDENCE.md.
