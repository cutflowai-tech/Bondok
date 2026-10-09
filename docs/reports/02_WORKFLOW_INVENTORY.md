# 02 — Workflow Inventory

Instance: `https://n8n.wasetco.com` (from webhook production URLs). Inventory taken 2026-10-09 through the official n8n MCP server, read-only.

The instance has **28 workflows**. MCP can read **13** of them. The other **15** have *Available in MCP* turned off, so only their name, ID, status and timestamps are visible. None of them were reviewed.

## A. Social media system: reviewed in full

Only these three reference Monday board **5105608159 (For Social Media)**. Their full definitions were read and exported to `workflow_exports/`.

| ID | Name | Active | Nodes | Trigger | Created / updated (UTC) | Export |
|---|---|---|---|---|---|---|
| `qI1N5VNgpRjnZAKH` | Waset Social V2 — 1 Prepare & Schedule | yes | 180 | Schedule | 2026-10-08 / 2026-10-09 15:34 | `qI1N5VNgpRjnZAKH__1_Prepare_and_Schedule.json` |
| `pUIshuf16zIYoYRz` | Waset Social V2 — 2 Publish When Due | yes | 78 | Schedule | 2026-10-08 / 2026-10-09 15:35 | `pUIshuf16zIYoYRz__2_Publish_When_Due.json` |
| `WasetSocialScheduleGuard` | Waset Social V2 — 3 Schedule Supervisor | yes | 44 | Schedule + Manual | 2026-10-09 / 2026-10-09 15:36 | `WasetSocialScheduleGuard__3_Schedule_Supervisor.json` |

All three use `settings.timezone = Africa/Cairo` and have **no `errorWorkflow`** configured. Trigger intervals and node logic are in `03_WORKFLOW_DEEP_DIVES.md`.

## B. Readable, but not part of the social media system

These were read in full. **None of them references board 5105608159** or the social media publishing accounts. They are listed so the architect knows they were checked and excluded.

| ID | Name | Active | Trigger | What it actually does | Boards touched |
|---|---|---|---|---|---|
| `zVWneDIwbAkrsVGs` | Weekly Dropbox Archive Cleanup | yes | Schedule, Fri 04:00 Cairo | Lists all Dropbox files recursively. Deletes video files inside any `Archive` folder that were not modified for 60+ days, but only when a video exists beside that Archive folder. Uses `/2/files/delete_batch` in batches of 1000, then writes a report to `/Waset Co Studio/Reports/Archive Cleanup/`. | none (Dropbox only) |
| `Mj2tFDXXm2SotxhJ` | Captions Prio | yes | Webhook `POST /webhook/captions-prio` (Monday) | Answers Monday's challenge, reads the changed item, finds its mirror on board 5098835785 by `text_mm4fafj5`, and copies a status label into `color_mm4xe8ge`. | 5098835785 |
| `aD64l0E36EoowXdo` | Captions Video Link Update From Editor | yes | Webhook `POST /webhook/captions-video` | Copies a link column (`column_values[11]`) from the source item into `link_mm4f1h5k` on the matching item of board 5091110326 (matched by `text_mm066x8y`). | 5091110326 |
| `DGluKvTQrYDLXZTg` | Editor changes stat webhook | yes | Webhook `POST /webhook/editor-changed-stat` | Mirrors the editor-board status (`color_mm1ehcmm`) into `project_status` on board 5091110326, matched by code `text_mm1ekv9s` → `text_mm066x8y`. | 5091110326 |
| `vvWwTXn1rE6JE3dB` | When Item Is sent to revesions | yes | Webhook `POST /webhook/revisions-update` | Sets column `date` (Client ETA) on board 5091110326 to now + 24h. This is UTC: the comment says 12h but the code adds 24. | 5091110326 |
| `jsT9YGPvC5BkNjpo` | My workflow | yes | Telegram message | AI agent (OpenRouter `google/gemini-2.0-flash-lite-001`) parses "change priority" requests, then calls `pFb16xMOaY6aBPBv`. This is an existing example of a **chat → LLM → sub-workflow command pattern**. | via sub-workflow |
| `pFb16xMOaY6aBPBv` | My Sub-Workflow 1 | yes | Execute Workflow Trigger (inputs `Action`, `Code`, `Prio`) | Finds the item on board 5091110326 by code `text_mm066x8y` and sets `priority_1`. It ignores `Action`. | 5091110326 |
| `CbRtCwZ5x1Ayl3cd` | CEO notifications | **no** | Schedule, every 10 min | Reads group `new_group43041` of board 5091110326, computes minutes to "Client ETA" and ticks `boolean_mm4ggrz9` when ≤ 10 min. It sends no notification. | 5091110326 |
| `b6yHFKUIjusLQ5DJ` | Client prefrences | **no** | Webhook `POST /webhook/client-pref` | Posts a fixed Monday update (a B-roll link) on items for specific clients. One branch has an empty update body. | (item from webhook) |
| `lly6y44MgzKcZWzh` | My workflow 2 | **no** | Webhook `GET /webhook/camera-motion` | Has a single webhook node and does nothing else. | none |

## C. Not accessible: NOT reviewed

These have `availableInMCP = false`, so their definitions could not be read. **They are not marked as reviewed.** Some may touch board 5105608159 or its source boards. The owner should enable *Settings → Available in MCP* on each one, or export them manually, before anyone can rule that out.

| ID | Name | Active | Triggers | Updated (UTC) | Possible relevance (inferred from name only) |
|---|---|---|---|---|---|
| `Nb8fvRaLfumdkYT0` | Project Copy Automation (Office) | yes | 1 | 2026-09-25 | Project creation, possibly the source of items |
| `QUbrWrpK2WD9TS3P` | Create File (Office) | yes | 1 | 2026-09-24 | Dropbox folder creation, possibly the source of `Folder Link` |
| `VsPw6TtZ623OFi7X` | Add new updates to editor board (Office) | yes | 1 | 2026-09-23 | Editor board sync |
| `H4zyWzglgMedpZvq` | Dropbox folder to Replay review link | yes | 1 | 2026-09-19 | Media review links |
| `7D6DDdNoLDw3syFM` | When a Stat Changes In Main Projects (Office) | yes | 1 | 2026-09-15 | Status sync, **may copy finished projects into For Social Media** |
| `gXBd57R6tNnCBzMz` | When a Prio Changes In Main Projects (Office) | yes | 1 | 2026-09-15 | Priority sync |
| `oR268O5OPIb0y0T0` | Final Link Update (Office) | yes | 1 | 2026-09-15 | Final video link, **may write Dropbox/Folder links** |
| `zHOEH1WdlLxRMPkm` | Review Link Update (Office) | yes | 1 | 2026-09-15 | Review links |
| `Ntd38QZ4WbVPw1Ue` | Dropbox - Copy external shared folder | yes | 0 (sub-workflow?) | 2026-09-15 | Dropbox utility |
| `fadmYVBYN8zrlQ0Q` | Office Download | yes | 0 (sub-workflow?) | 2026-09-15 | Download utility |
| `UMWJvjXfTQ4RarQX` | Priority Update | yes | 1 | 2026-09-03 | Priority sync |
| `nrt8tWqI7fDUtijy` | Priority Update for revisions | yes | 1 | 2026-09-03 | Priority sync |
| `KaLUMLnTgp8LH8K7` | Captions Stat Change | yes | 1 | 2026-08-31 | Captions board status |
| `Cs0Z6mmmTSw09Ra6` | Copy To Captions Board | yes | 1 | 2026-08-09 | Captions board item creation |
| `rtvMdWlXvjobYWqg` | My workflow [Ahmed] | no | 0 | 2026-09-10 | Unknown (empty draft?) |

> **Open question:** it is not verified how items first get onto board 5105608159. That could be manual entry, a Monday-native automation, or one of the Section C workflows. See `07_ERRORS_AND_RISKS.md` and the owner questions.

## Other boards referenced (for context)

| Board ID | Seen in | Inferred role |
|---|---|---|
| 5105608159 | V2 workflows 1–3 | **For Social Media** (verified name) |
| 5091110326 | several Section B workflows | Customer/Main Projects board (column `project_status`, `priority_1`, `text_mm066x8y` = project code). Name not verified. |
| 5098835785 | Captions Prio | Captions board (inferred from workflow name) |
