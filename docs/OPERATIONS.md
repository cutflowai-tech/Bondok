# Operations (since 2026-10-15)

Bondok runs on its own host with no n8n dependency. Paths are the production layout; secrets are never written to
this repository.

## Services and data
| What | Where |
|---|---|
| Chat (Slack Socket Mode) | `bondok.service` → `/opt/waset-bondok/app.py` (venv), config `/opt/waset-bondok/.env` |
| Scheduled work (import, preparation, publishing, display sync, supervision) | `waset-runtime.service` → `python3 -m waset_runtime.service`, config `/opt/waset-bondok/runtime.env` |
| Operational store, media cache, helper releases, generation marker | `/var/lib/bondok/pipeline/` (owner `bondok`, mode 0770; no bind mount, not visible to any container) |
| Conversation store | `/var/lib/bondok/bondok.sqlite` |
| Media tools | `ffmpeg`/`ffprobe` from the host PATH (a missing one raises one owner alert at start) |
| Old n8n volume | holds only a tombstone `helper.py` that refuses every call (`deploy/retired_helper.py`) |

Both config files carry the Monday token and the OpenRouter key; the runtime file also carries Dropbox and Instagram.
A rotated credential is updated in **both** files, then both services are restarted. Monday and OpenRouter credentials
are shared with other company workflows in n8n: regenerating them there breaks Bondok too.

## Deploy
`python3 deploy/build_release.py --site <private site.json>` on a clean commit (the build refuses uncommitted
changes; releases contain no n8n workflows), copy the release to the server, then
`python3 tool/deploy.py code --config config.json --release releases/<id>` as root. The tool backs up the store and
code, fences scheduled work (`deploy-fence.json`), stops both services (waiting up to 18 minutes for publications in
flight), switches, verifies and restarts; any failure rolls back or fails closed with the fence removed. Released
helper code belongs to root. Old code directories and helper releases beyond the newest three are pruned.
`deploy.py workflows` refuses: n8n workflows are retired.

## Backup
`waset-backup.timer` (03:30 UTC) runs `/usr/local/sbin/waset-state-backup` (`deploy/state_backup.sh`): SQLite online
backups of both stores, integrity-checked, plus the unit files and the configuration needed to restore, in
`/root/bondok-backups/daily-*` (root only, kept 14 days, never copied off the host). Off-host copies are not set up.

## Restore (rehearsed offline: `tests/test_owner_decisions_protected.py::RestoreDetection`, `test_p5_resilience.py::A28`)
1. `systemctl stop waset-runtime bondok`.
2. Copy the backup's `state.sqlite` to `/var/lib/bondok/pipeline/state.sqlite` (owner `bondok`); remove stale
   `-wal`/`-shm`. Do **not** restore `store-generation.json`.
3. `systemctl start waset-runtime bondok`. The runtime compares the store with the generation marker and opens the
   restore gap by itself when the store is older than what the host already saw: publishing waits until the posts made
   since the backup are checked on Instagram (all feed pages and current Stories; a gap older than 24 hours always
   waits for the owner, because older Stories cannot be listed). The owner closes it with Bondok ("the check is done").
4. During the gap, board status/time columns are not read as decisions (they show Bondok's own later writes); the
   owner's later board edits are applied after the gap with activity-log evidence.

## Rollback
Code: the backup of each deploy (`/root/bondok-deploy/backups/<ts>-pre-<release>`) restores the previous code
(`deploy.py` does it automatically when a switch fails). Returning scheduled work to n8n is no longer possible without
restoring its archived workflows, its deleted Instagram credential (encrypted export in
`backups/n8n-retire-20261014T180836Z`) and moving the store back to the n8n volume; `set_runtime_owner n8n` refuses
without `legacy_rollback` for that reason.

## Housekeeping
Every preparation cycle: local prepared files of publications older than 30 days, superseded versions after 7 days,
acknowledged display jobs older than 30 days, stale lock files and shadow copies. Once a day: Dropbox `Prepared` copies of
items published more than 30 days ago and superseded copies older than 7 days. Commands, audit and publication records
are history and are kept.

## Attribution corrections (2026-10-15)
`python3 -m waset_runtime.corrections plan|apply` (as `bondok`): restores owner instructions overwritten by board
reads, only with Monday activity-log evidence; idempotent, state-checked, audit-linked. See
`bondok-agent-migration/08-tracker.md` for the run of 2026-10-15.

## Acceptance tests on production
Dedicated items (`ACC<n>`, names `Acceptance …`) are isolated from Instagram by `python3 -m waset_runtime.isolation set
--code-prefix ACC --name-prefix "Acceptance "` (own reservation account: no real slot is taken or moved; never
claimed, never committed). The owner starts an approved, root-owned script with one Slack message
(`bondok/acceptance.py`); an interrupted run resumes after a restart without repeating steps. Cleanup:
`tests/acceptance_cleanup.py` (resumable, test resources only), then `isolation clear`.
