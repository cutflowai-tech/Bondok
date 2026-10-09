# Change Set B — v2 Cutover Checklist (final)

Release: commit `60bbab0` on branch `bondok/execution-authority` (clean build, 33 files). Build locally with
`python3 deploy/build_release.py --creds .local-snapshots/2026-10-09/n8n_live` → `release/60bbab0/`.

| File | sha256 (prefix) |
|---|---|
| `helper/helper.py` | `755f454666f28025` |
| `helper/waset_ops/core.py` | `947a22dfd42718b5` |
| `bondok/app.py` | `807792e54a6fd3a8` |
| `bondok/bridge.py` | `8a52c4c8aa5d86a8` |
| `bondok/agent.py` | `bb6d82a5101034bb` |
| WF1 v2 JSON | `e0229a83644678c1` |
| WF2 v2 JSON | `b27369cc30fbb992` |
| WF3 v2 JSON | `2dd74a945ee8c540` |
Full list: `release/60bbab0/MANIFEST.json`.

## Authorization status
* Change Set A: **authorized and applied** (2026-10-09 17:39 UTC; see HANDOFF_STATUS.md).
* Adding `SLACK_BOT_USER_ID` and `SLACK_APP_ID` (verified values) to `/opt/waset-bondok/.env`: **authorized, only as part of the approved v2 deployment**.
* Change Set B itself: **NOT yet authorized.**

## Go / no-go gates (all must hold immediately before step 1)
- [ ] Owner has explicitly authorized Change Set B and is reachable on Slack for ~1 hour.
- [ ] WF1 still inactive; no running/waiting executions of WF1/WF2/WF3.
- [ ] `reservations`, `publications` (legacy) = 0 or every row accounted for; board shows no item due in the next 60 min.
- [ ] Free disk ≥ 8 GB (today 8.7 GB; media preparation refuses to start below 6 GB). Consider cleanup first.
- [ ] Release built from a clean tree at the approved commit; MANIFEST hashes match this table.
- [ ] Instagram token validity/expiry checked in Meta Business Settings (owner).

## Steps (stop at the first failed check; rollback section below)
1. **Backup** — `deploy/server_backup.sh` (root). Record the backup directory and `SHA256SUMS`. Export WF1/WF2/WF3 JSON + version ids (v1: `e520d122…`, `9351a690…`, `e37aa4f0…`) into the same directory.
2. **Freeze** — deactivate WF3, then WF2 (WF1 already inactive). Wait until no execution is running/waiting. `systemctl stop bondok.service`.
3. **Re-check state** — read-only: legacy `publications`/`reservations` counts; `locks` empty; no detached media job running (`fuser` on `media-capacity-*.lock` empty).
4. **Install helper** (data volume, uid 1000, mode 0644): copy `release/…/helper/waset_ops/` and `helper.py`; keep old file as `helper.v1.py`. Verify sha256 of every installed file against MANIFEST.
5. **Migrate** — as uid 1000 inside the container: `python3 /home/node/.n8n-files/waset-social/helper.py <base64 {"path":"/v2/health","body":{}}>` → expect `{"ok":true,"schema":1,…}`. Confirm legacy row counts unchanged.
6. **Import workflows** — update WF1/WF2/WF3 in place (same IDs) with the release JSON (credential ids already mapped), **inactive**. Read back node counts (114 / 64 / 9), credentials by name, and new version ids.
7. **Activate one at a time**
   1. WF1 → after its first run: bootstrap imported 164 items; Monday writes queued = 0 (`ops_outbox` kind `monday`); run finished; `errors.log` has no new lines.
   2. WF2 → first runs: empty due queue ends without Monday calls; display sync idle; heartbeat `wf2` < 2 min.
   3. WF3 → first run at :05/:35 completes or reports `deferred`; 0 repairs expected.
8. **Bondok v2** — copy `release/…/bondok/*` (incl. `waset_ops/`) into `/opt/waset-bondok` (root:root 0644; keep `venv/`, `.env`); append `SLACK_BOT_USER_ID` and `SLACK_APP_ID` (verified values) to `.env` keeping mode 0600 owner bondok; `systemctl restart bondok.service`; expect journal line `ready model=openai/gpt-6.1-sol board=5105608159 ops_schema=1`.
9. **Read-back verification** — installed hashes = MANIFEST; n8n active versions = imported versions; `/v2/health` heartbeats wf1/wf2/wf3 present; `errors.log` contains no `retired` (no v1 caller left); Bondok `systemctl is-active` = active, no `startup_failed`; owner asks Bondok one read-only question (e.g. "ايه حالة الجدولة؟").
10. **Report** each component as DEPLOYED / ACTIVE with evidence; list open items (legacy caption approval, Topaz confirmations).

## Rollback (any step ≥ 4)
1. Deactivate v2 WF2, WF1, WF3; stop `bondok.service`.
2. Restore `helper.v1.py` → `helper.py`. **Keep the database** (never restore the backup over newer receipts).
3. Restore v1 workflow versions by version id; activate WF2 then WF3 (WF1 stays inactive, as authorized before B).
4. Restore `/opt/waset-bondok` from the backup tarball (remove the two new `.env` lines only if the old code rejects them — it ignores unknown keys); start `bondok.service`.
5. Verify WF2 succeeds again and no v2 attempt is left in `committed`/`outcome_unknown` without a Slack note.

## Expected owner follow-ups after cutover
* Approve legacy captions (35 Posts) via one Bondok proposal, or edit them on the board.
* Confirm Topazed per selected file version (no item is bound today); Bondok/editor tasks will say which.
