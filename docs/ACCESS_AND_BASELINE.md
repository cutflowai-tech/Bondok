# Access and Baseline — 2026-10-09

Labels: **VERIFIED LIVE** (observed on the running system today), **VERIFIED IN SOURCE** (read in code/export),
**TESTED LOCALLY**, **HISTORICAL OBSERVATION**, **INFERRED**, **NOT VERIFIED**.
All production access in this phase was read-only. No workflow was executed, edited, activated or deactivated;
no Monday item, Slack message, Instagram object or credential was changed.

## Access matrix

| Dependency | Identity / location | Evidence | Access used | Limitations / blocked |
|---|---|---|---|---|
| Local workspace | `/Volumes/Zeno/Bondok` (mounted Mac volume) | VERIFIED LIVE | read/write | — |
| GitHub | account `cutflowai-tech`; repo `cutflowai-tech/Bondok` | VERIFIED LIVE (`gh`) | read; local commits only | **Repo is PUBLIC** (requirement: private). Nothing pushed by this work. Needs owner decision. |
| Server | `<server-host>` (<server-ip>), Ubuntu 24.04, root SSH key on the Mac | VERIFIED LIVE | read-only commands | 96 GB disk **91% used** (8.9 GB free); 8 GB RAM |
| Old Bondok | `bondok.service`, `/opt/waset-bondok` (no git), user `bondok`, Python 3.12.3 venv; Socket Mode | VERIFIED LIVE (unit, journal, source copy in `reference/deployed_2026-10-09/bondok`) | read-only | `.env` read for key **names** only |
| Bondok model | `openai/gpt-6.1-sol` via OpenRouter Responses API | VERIFIED LIVE (startup log) + catalog lists the id | no paid call | — |
| Caption model | `openai/gpt-5.6-sol` (WF1 node `OpenRouter Model`) | VERIFIED IN SOURCE (live workflow) | — | separate concern from Bondok's model |
| Slack | workspace `<slack-workspace-id>`, app `<bondok-app-id>`, bot `<bondok-bot-user-id>`, owner `<owner-slack-id>`; private channel verified at startup | VERIFIED LIVE (log line `slack_private_channel_verified=true`, deployed README) | none | Channel id only in `.env` (not read) |
| n8n | `https://n8n.wasetco.com`, container `n8n-n8n-1`, image `waset-n8n-media:2.39.8-draft`, Python 3.14.8, ffprobe 8.1.2, Postgres backend | VERIFIED LIVE | MCP read + `docker inspect` | 15 of 28 workflows not readable via MCP (unchanged from reports) |
| WF1/WF2/WF3 | `qI1N5VNgpRjnZAKH` v`e520d122`, `pUIshuf16zIYoYRz` v`9351a690`, `WasetSocialScheduleGuard` v`e37aa4f0`; all active | VERIFIED LIVE: node-for-node and connection-for-connection identical to `workflows/original/` | read | — |
| Helper | `/home/node/.n8n-files/waset-social/helper.py` = volume `waset_social_data`; sha256 `8598b0ed…0fa5` | VERIFIED LIVE: identical to the supplied copy | read + hash | — |
| Operational DB | `state.sqlite` (WAL) in the same volume; also bind-mounted into Bondok at `/var/lib/bondok/pipeline` | VERIFIED LIVE | consistent read-only backup via SQLite backup API → `.local-snapshots/` (gitignored) | No backup arrangement found for this file — NOT VERIFIED that one exists |
| Monday | account "Waset co Studio", user `99154021`, timezone **Africa/Cairo**; board 5105608159 (164 items, 4 groups) | VERIFIED LIVE (MCP read) | read-only snapshot → `.local-snapshots/` | Native board automations/webhooks: not exposed by connector (NOT VERIFIED) |
| Dropbox | two n8n credentials ("Dropbox account", "Unnamed credential 2") | VERIFIED IN SOURCE | none | namespaces NOT VERIFIED |
| Instagram | IG user `17841479950766455`, Graph API v26.0 via "Simplified Custom Auth account" | VERIFIED IN SOURCE | none (no API call made) | account identity and permission scopes NOT VERIFIED live |

## Current production state (2026-10-09 ~17:00 UTC)

* **WF2 is failing every minute** (latest checked execution 51929 at 16:39 UTC: error in `Publication Receipts — Local n8n`,
  "Command failed with exit code 1"; stdout lost). VERIFIED LIVE.
* **Most likely cause (INFERRED, strong evidence):** Bondok (uid 999) opened the shared WAL database at 14:59:48 UTC and
  created `state.sqlite-shm` / `-wal` owned by `bondok`, mode 664, ACL with no entry for uid 1000 (n8n's `node` user,
  = host `uid-1000 user`). Inside the container both files test **not writable**. The helper always runs `PRAGMA journal_mode=WAL`
  plus DDL/health writes, so every call fails. The last successful helper write in the `health` table is 14:59:06 UTC.
  A read-only open as uid 1000 still works. The helper's own stderr is not retained, so the exact SQLite message is NOT VERIFIED.
* Operational DB contents: 0 reservations, 0 verified media, 0 publication receipts, 93 jobs, 49 item_state, 25 audit rows.
* Board: 164 items — 15 Posted (no IG media id/receipt), 44 Skipped status, 0 Scheduled; 59 items carry future
  legacy Post Date/Time values; 35 Posts have captions of unknown approval status.

## Report-versus-live differences

| Report claim | Live finding |
|---|---|
| "helper.py copy not verified on server" | Verified identical (sha256 above). |
| "no Bondok integration" | True for the workflows, but Bondok runs on the same host, reads the board and **writes the pipeline DB directly** (`locks`, `reservations`, `audit_log`) and Monday columns: a competing writer. |
| Outage cause unknown | Permission conflict on SQLite sidecar files created by Bondok (inferred, see above). |
| "`/v1/health` read-only" | It writes a `health` row (and every route updates `health`). |
| `at()` treats Publish at as UTC "while humans use Cairo time" | Monday stores date+time values in UTC and displays them in the user's zone (account zone Africa/Cairo). Treating the API value as UTC is consistent; it is not a blanket offset bug. Encoded and tested as the time contract. |
| Empty publisher queue makes `items(ids:[null])` | Confirmed in source: `Publication Needs Review → Publication Review Payload` runs on the empty marker. |
| Story limit "60–62 s trim band" (preflight docs) | Deployed helper rejects `>= 60` exactly; v2 keeps strict `< 60`. |

## Old Bondok (what exists and is reused)
Slack Socket Mode app with workspace/bot/app-token/private-channel identity checks, event dedupe table, crash-safe
reply states, thread history, owner-only approvals (`اعتمد B-XXXXXXXX`, 30 min, same thread). Reused in `bondok/`.
Removed from active ownership: direct Monday writes, direct pipeline-table writes, the LLM-based 30-minute audit.
