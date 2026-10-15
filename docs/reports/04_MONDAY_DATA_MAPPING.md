# WASET SOCIAL MEDIA AUTOMATION — CURRENT SYSTEM TECHNICAL DOCUMENTATION

# 04 — Monday.com Data Mapping

## 0. Scope, sources and evidence labels

This chapter documents every Monday.com read and write done by the three Social Media workflows:

| Short name | n8n ID | Name | Trigger |
|---|---|---|---|
| WF1 | `qI1N5VNgpRjnZAKH` | Waset Social V2 — 1 Prepare & Schedule | Every 10 min (misfire: skip) |
| WF2 | `pUIshuf16zIYoYRz` | Waset Social V2 — 2 Publish When Due | Every 1 min (misfire: skip) |
| WF3 | `WasetSocialScheduleGuard` | Waset Social V2 — 3 Schedule Supervisor | Cron `0 5,35 * * * *` + Manual Audit (the manual run also writes) |

Sources:
- The workflow exports in `/Volumes/Zeno/Bondok/workflow_exports/`. The node code was read in full, and key points were re-checked with `python3 -I` and `grep` for this chapter.
- The local helper copy `/Volumes/Zeno/Bondok/helper_reference/helper.py` (657 lines, sha1 `57e158cb…`). It comes from the bundle whose workflow JSON matches the live workflows node for node. **Nobody has checked that the server runs this exact file.**

Every claim carries one of these labels:

| Label | Meaning |
|---|---|
| **VERIFIED** | Read directly in the workflow JSON (node parameters or code). |
| **DELEGATED-TO-HELPER** | Decided inside `helper.py`. Line numbers (`L…`) refer to the local copy, so the behaviour on the server is not proven. |
| **INFERRED** | Deduced from the code or from naming, not proven. |
| **NOT IMPLEMENTED** | Searched for and not found in the three workflows or in the helper. |

The helper never calls Monday. Every Monday call is a `POST https://api.monday.com/v2` from an n8n HTTP node using the `mondayComApi` credential (VERIFIED; helper docstring L3).

Caption text, client briefs and credentials are not reproduced in this chapter.

---

## 1. Boards touched

| Board ID | Name / role | Read by | Written by | Evidence |
|---|---|---|---|---|
| **5105608159** | "For Social Media", the main social board | WF1, WF2, WF3 | WF1, WF2, WF3 | VERIFIED. Every `gqlUpdate()` hard-codes `board_id:'5105608159'`. |
| **5091110326** | Customer Projects, the source board where projects, Codes and final-video links live | WF1, WF2 | WF1 (subitems only), WF2 (`color_mm1ryfcb` = Posted) | VERIFIED. WF1 `Source Start` and `Create Assigned Editor Task`; WF2 `Build Source Posted Update`. |
| **5091137380** | Subitem board of Customer Projects, holding the editor tasks | WF1 (through the parent's `subitems`) | WF1 (`Reopen Existing Editor Task`: status + update) | VERIFIED as the board ID used in `Reopen Editor Plan`. That it is the Customer Projects subitem board is INFERRED, because subitems are created with `create_subitem(parent_item_id: source.id)`. |
| **5105687146** | Subitem board of "For Social Media" | none | none | VERIFIED. The ID does not appear in any of the three exports (grep count 0). |
| Editor board(s) | Any separate editor board | none | none | VERIFIED: the social workflows do not reference one. "Editor tasks" are subitems of the source project, not items on an editor board. Other workflows sync an editor board to 5091110326 (see `02_WORKFLOW_INVENTORY.md`), but they are outside this system. |

### Groups on board 5105608159

| Group ID | Title | Used by |
|---|---|---|
| `topics` | For Posts | WF1 `Build Sync Mutations` → `Apply Source Sync`: new **Post** items are created here (VERIFIED). |
| `group_mm7xagm` | For Stories | WF1 `Build Sync Mutations`: new **Story** items are created here (VERIFIED). |
| `group_title` | Posted | WF2 `Build Posted Update` → `Mark Posted in Monday`: `move_item_to_group(group_id:"group_title")` (VERIFIED). |
| `group_mm7y8mkr` | Skipped | **Never used.** grep count is 0 in all three exports (VERIFIED). |

> **Correction to the brief.** The brief says no group moves exist. That is not accurate: **exactly one** group move exists, WF2 → Posted (`group_title`). No other group move exists. Nothing moves items to Skipped, between For Posts and For Stories after a format change, or back out of Posted. All of this is VERIFIED by grep for `move_item_to_group`: 1 hit in WF2, 0 in WF1 and WF3.

---

## 2. Columns of board 5105608159 and who touches them

R = read, W = written. "Snapshot" is the 27-column paginated read that all three workflows do: `items_page(limit:500)` followed by `next_items_page`. The snapshot fails closed: `'Missing board page; refusing a partial snapshot'` (VERIFIED in `Social Collect` / `Source Collect` in every workflow).

| Column ID | Name | WF1 | WF2 | WF3 | Notes |
|---|---|---|---|---|---|
| `status` | Status | R/W | R/W | R/W | Written by all three. See §6. |
| `date4` | Post Date | R/W | R | R/W | Cairo local date. |
| `hour_mm7xy9cf` | Post Time | R/W | R | R/W | Cairo local hour and minute. |
| `color_mm7xm9b6` | Format | R/W (only at creation) | R | R | "Social board owns Format after import" (sticky note, VERIFIED). |
| `long_text_mm7x2ay1` | Caption | R/W | R | R (not forwarded) | WF1 writes it only when empty (from the snapshot). |
| `color_mm7xe2j2` | Topazed | R/W | R | R | WF1 writes `Not yet` / `Topazed`. Humans set `Topazed`. |
| `link_mm7x8dy7` | Dropbox Link | R/W | R | R | WF1 overwrites it with the auto-selected file. |
| `link_mm7xb56a` | Post Link | R | R/W | R | Only WF2 writes it (the Instagram permalink). |
| `text_mm7xqn4e` | Code | R/W (only at creation) | R | R | Copied from the source Code. |
| `text_mm7xaf3t` | Notes | — | — | — | Never read or written (VERIFIED, grep 0). |
| `numeric_mm7xade9` | Numbers | — | — | — | Never read or written (VERIFIED, grep 0). |
| `link_mm7xaep2` | Folder Link | R/W | R | R | WF1 writes it **without a guard** (`Save Project Folder`). |
| `text_mm7xemtq` | Version Check | R/W | R | R | `file name \| rev`. |
| `link_mm7ywc0w` | Publish video | R/W | R | R | The prepared 1080 file with `raw=1`. |
| `text_mm7yhjf1` | IG Colab | R | R | R | Human input only. If set, WF2 refuses to publish. |
| `text_mm7y5kd1` | Style | R/W | R | R | `style(code)`. |
| `text_mm7yjmqd` | Story variety | R/W | R | R | WF1 overwrites it with the computed rotation key. |
| `multiple_person_mm7yy4tk` | Social owner | R/W (only if empty) | R | R | Fallback person ID `99154021`. |
| `date_mm7y8s9t` | Publish at | R/W | R | R/W | Stored and read as **UTC**. It takes priority in `at()`. |
| `long_text_mm7ysrbz` | System update | R/W | R/W | R/W | Machine log text. Every write overwrites it (there is no append). |
| `text_mm7y4h4a` | Source item ID | R/W | R | R | |
| `text_mm7yy451` | Source asset version | R/W | R | R | `dropboxFileId@rev`. |
| `text_mm7yp8h` | Verified media ID | R/W | R | R | Helper media ID (sha256). |
| `date_mm7yr4h3` | Published at | R | R/W | R | |
| `text_mm7yfqhb` | Instagram media ID | R | R/W | R | |
| `long_text_mm7zbtbn` | المطلوب منك (Action required) | R/W | R/W | R/W | Human-facing instruction text. |
| `text_mm7zjt4m` | Video measurements | R/W | R | R | |
| `text_mm7z139h` | Processed format | R/W | R | R | Used to detect a Story↔Post change. |
| `date_mm7zd2b9` | Last checked | R/W | R | R | Only WF1 stamps it (UTC). WF2 and WF3 never write it. |

Group membership and item updates (comments) are covered in §4 and §5.

---

## 3. Read operations

| WF | Node | Board | Query shape | Columns | Retry | Label |
|---|---|---|---|---|---|---|
| WF1 | `Source Read Page` (loop) | 5091110326 | `items_page(500)` + `next_items_page` | `text_mm066x8y` (Code), `link_mm06bswn` (link), `color_mm1ryfcb` (format/status: Post / Story / Canceled / Posted), `project_owner` | none | VERIFIED |
| WF1 | `Social Before Sync Read Page` (loop) | 5105608159 | as above | 27-column snapshot | none | VERIFIED |
| WF1 | `Social Read Page` (loop, post-sync) | 5105608159 | as above | 27-column snapshot | none | VERIFIED |
| WF1 | `<X> — Fresh Item` (×7 guard reads) | 5105608159 | `items(ids:[itemId]){column_values}` | all | none | VERIFIED |
| WF1 | `Read Client Brief` | 5091110326 | `items(ids:[source.id]){updates(limit:100)}` | updates `text_body`, `created_at` | 3×/2 s | VERIFIED |
| WF1 | `Read Existing Editor Tasks` | 5091110326 | `items(ids:[source.id]){subitems{id name column_values(ids:["status"])}}` | subitem status | 3×/2 s | VERIFIED |
| WF1 | `Recheck Before Scheduling` | 5105608159 + 5091110326 | aliased `social:` / `source:` `items(ids)` | all | 3×/2 s | VERIFIED |
| WF2 | `Social Read Page` (loop) | 5105608159 | snapshot | 27 columns | none | VERIFIED |
| WF2 | `Find Source Project` | by ID (no board filter) | `items(ids:[Source item ID])` | `text_mm066x8y`, `color_mm1ryfcb` | 3×/2 s | VERIFIED |
| WF2 | `Recheck Immediately Before Publishing` | 5105608159 + source | `items(ids)` ×2 | social columns + `color_mm1ryfcb` | 3×/2 s | VERIFIED |
| WF2 | `Read Status Before Review` | by ID | `items(ids:[itemId])` | all | none | VERIFIED. When the queue is empty it runs with `ids:[null]` every minute and fails (see `notes_exec`). |
| WF3 | `Social Read Page` (loop) | 5105608159 | snapshot | 27 columns | none | VERIFIED |
| WF3 | `Read Current Schedule` | by ID | `items(ids:[plan.item.id])` | all | none | VERIFIED |

---

## 4. Full write table: WORKFLOW → NODE → BOARD → COLUMN → OPERATION → VALUE

Operation codes:
- `CMCV` = `change_multiple_column_values`
- `CCV` = `change_column_value`
- `CI` = `create_item`
- `CSI` = `create_subitem`
- `CU` = `create_update`
- `MOVE` = `move_item_to_group`

Guard = the 4-node freshness guard in WF1 (`<X> — Guard Input / Fresh Item / Guard / Allowed?`). It skips the write when the live item:
- is missing,
- has status Posted, Skipped, Paused or Publishing,
- has a Format different from the context Format, or
- has a Post Link URL.

(VERIFIED in the WF1 `<X> — Guard` nodes.)

### 4.1 WF1 — Prepare & Schedule (`qI1N5VNgpRjnZAKH`)

| Node (write) | Board | Item | Column | Op | Value | Guard | Label |
|---|---|---|---|---|---|---|---|
| `Apply Source Sync` (create path of `Build Sync Mutations`) | 5105608159 | new; name = source item name; group `topics` (Post) or `group_mm7xagm` (Story) | `text_mm7y4h4a` | CI | source item ID | — | VERIFIED |
| 〃 | 〃 | 〃 | `text_mm7y5kd1` | CI | `style(code)` | — | VERIFIED |
| 〃 | 〃 | 〃 | `text_mm7xqn4e` | CI | source Code | — | VERIFIED |
| 〃 | 〃 | 〃 | `color_mm7xm9b6` | CI | `Post` / `Story` | — | VERIFIED |
| 〃 | 〃 | 〃 | `status` | CI | `جاري فحص الفيديو` ("checking the video") | — | VERIFIED |
| 〃 | 〃 | 〃 | `color_mm7xe2j2` | CI | `Not yet` | — | VERIFIED |
| 〃 | 〃 | 〃 | `link_mm7x8dy7` | CI | source link (if it has a URL) | — | VERIFIED |
| 〃 | 〃 | 〃 | `link_mm7xaep2` | CI | same link, only if it contains `/scl/fo/` | — | VERIFIED |
| `Apply Source Sync` (cancel path) | 5105608159 | every matched social item not in {Posted, Skipped, Paused, Publishing} | `status` | CMCV | `Skipped` | built-in status filter | VERIFIED |
| 〃 | 〃 | 〃 | `long_text_mm7ysrbz` | CMCV | `Canceled in Customer Projects` | 〃 | VERIFIED |
| `Save Project Folder` | 5105608159 | itemId | `link_mm7xaep2` | CMCV | `{url, text:'Project folder'}` when the URL contains `/scl/fo/`; otherwise a no-op query | **none** | VERIFIED |
| `Save Selected Version` | 5105608159 | itemId | `link_mm7x8dy7` | CMCV | `{url: share URL of the selected file, text: file name}` | Guard | VERIFIED |
| 〃 | | | `color_mm7xe2j2` | | `Topazed` if the stored asset version equals the new `assetKey` **and** the snapshot says Topazed; otherwise `Not yet` | | VERIFIED |
| 〃 | | | `text_mm7yy451` | | `assetKey` = `fileId@rev` | | VERIFIED |
| 〃 | | | `text_mm7y4h4a`, `text_mm7y5kd1` | | source ID, style | | VERIFIED |
| 〃 | | | `text_mm7yjmqd` | | computed rotation key (overwrites a human value) | | VERIFIED |
| 〃 | | | `text_mm7xemtq` | | `file name \| rev` | | VERIFIED |
| 〃 | | | `text_mm7z139h` | | current Format | | VERIFIED |
| 〃 | | | `date_mm7zd2b9` | | now (UTC) | | VERIFIED |
| 〃 (if `changed`) | | | `text_mm7yp8h` = `''`, `date_mm7y8s9t` = `{}`, `long_text_mm7zbtbn` = `''`, `status` = `جاري فحص الفيديو` | | | | VERIFIED |
| 〃 (if `formatChanged`) | | | `date4` = `{}`, `hour_mm7xy9cf` = `{}`, `long_text_mm7ysrbz` = format-change notice | | | | VERIFIED |
| 〃 (if empty) | | | `multiple_person_mm7yy4tk` | | source `project_owner`, else `99154021` | | VERIFIED |
| 〃 | | | `link_mm7xaep2` | | folder URL if it contains `/scl/fo/` | | VERIFIED |
| `Save Early Warning` | 5105608159 | itemId | `status` | CMCV | `جاري فحص الفيديو` (pending), `ستوري طويل` ("long story", replace required) or `Needs Review` | Guard | VERIFIED |
| 〃 | | | `long_text_mm7zbtbn`, `long_text_mm7ysrbz`, `date_mm7y8s9t` = `{}`, `text_mm7yp8h` = `''`, `text_mm7zjt4m` (duration), `date_mm7zd2b9` | | | | VERIFIED |
| `Save Accepted Duration` | 5105608159 | itemId | `status` | CMCV | `مقبول كبوست` ("accepted as a post", Post) or `Working on it` (Story) | Guard | VERIFIED |
| 〃 | | | `long_text_mm7zbtbn` = `''`, `long_text_mm7ysrbz`, `date_mm7zd2b9`, `text_mm7zjt4m` | | | | VERIFIED |
| `Save Caption` | 5105608159 | itemId | `long_text_mm7x2ay1` | CMCV | LLM caption (only when the snapshot caption was empty; Post only) | Guard (does not check the caption) | VERIFIED |
| `Save QA Result` | 5105608159 | itemId | `status` | CMCV | `Redy For Scheduled` (label spelled this way on the board) | Guard | VERIFIED |
| 〃 | | | `long_text_mm7zbtbn` = `''`, `text_mm7zjt4m` = `W×H \| MB \| s`, `date_mm7zd2b9`, `link_mm7ywc0w` = `{url (raw=1), text:'1080p verified'}`, `text_mm7yp8h` = mediaId, `long_text_mm7ysrbz` = `QA passed: …` | | | | VERIFIED |
| `Save Schedule` | 5105608159 | itemId | `date4` | CMCV | helper `date` (Cairo) | Guard + `Schedule Still Allowed` | VERIFIED (value DELEGATED-TO-HELPER L519-524) |
| 〃 | | | `hour_mm7xy9cf` | | helper `{hour, minute}` (Cairo) | | VERIFIED / DELEGATED |
| 〃 | | | `date_mm7y8s9t` | | UTC date and time of the helper `at` | | VERIFIED |
| 〃 | | | `status` | | `Scheduled` | | VERIFIED |
| 〃 | | | `long_text_mm7zbtbn` = `''`, `long_text_mm7ysrbz` = `Scheduled with variety <rotation> — Africa/Cairo` | | | | VERIFIED |
| `Save Item Note` ← `Waiting for Editor` | 5105608159 | itemId | `status` | CMCV | `Waiting for Editor` or `ستوري طويل` | Guard | VERIFIED |
| 〃 | | | `long_text_mm7zbtbn` = issue, `long_text_mm7ysrbz` = editor task ID + issue, `date_mm7y8s9t` = `{}`, `text_mm7yp8h` = `''` (Last checked is **not** stamped) | | | | VERIFIED |
| `Save Item Note` ← `Media Waiting or Review` | 5105608159 | itemId | `status` | CMCV | `Working on it` (pending) or `Needs Review` | Guard | VERIFIED |
| 〃 | | | `long_text_mm7zbtbn`, `long_text_mm7ysrbz`, `date_mm7y8s9t` = `{}` | | | | VERIFIED |
| `Save Item Note` ← `No Valid Style Slot` | 5105608159 | itemId | `status` = `Needs Review`, `long_text_mm7zbtbn`, `date_mm7y8s9t` = `{}` | CMCV | | Guard | VERIFIED |
| `Save Item Note` ← `Item Needs Review` | 5105608159 | itemId | `status` = `Needs Review`, `long_text_mm7zbtbn`, `long_text_mm7ysrbz` (reason, max 650 chars), `date_mm7y8s9t` = `{}`, `date_mm7zd2b9` | CMCV | | Guard | VERIFIED |
| `Create Assigned Editor Task` | 5091110326 → subitem board | parent = source project | subitem name `Social <itemId> — تجهيز أو استبدال الفيديو` ("prepare or replace the video"); `status` = `Working on it`; `person` = social owner, else source `project_owner`, else `99154021` | CSI | | de-duplicated by name prefix | VERIFIED |
| `Save Editor Instructions` | subitem | new subitem ID | (update) | CU | English instructions: Topaz, ≥1080 short edge, <300 MB, Story <60 s, upload folder, "confirm Topazed manually" | — | VERIFIED |
| `Reopen Existing Editor Task` | **5091137380** | existing subitem ID | `status` | CCV | `Working on it` | only when `refresh` is true | VERIFIED |
| 〃 | 〃 | 〃 | (update) | CU | instructions body | 〃 | VERIFIED |

### 4.2 WF2 — Publish When Due (`pUIshuf16zIYoYRz`)

| Node (write) | Board | Item | Column | Op | Value | Guard | Label |
|---|---|---|---|---|---|---|---|
| `Mark Posted in Monday` (built by `Build Posted Update`) | 5105608159 | `itemId` | `text_mm7yfqhb` | CMCV | Instagram media ID from the helper receipt | Requires a receipt with `publishedMediaId`. **No status guard** (it is a replay of a confirmed publication). | VERIFIED |
| 〃 | | | `date_mm7yr4h3` | | UTC `publishedAt` from the receipt, else now | | VERIFIED |
| 〃 | | | `status` | | `Posted` | | VERIFIED |
| 〃 | | | `long_text_mm7ysrbz` | | `Published on Instagram: <id>`, plus `\| no permalink returned (Story may expire)` when there is no permalink | | VERIFIED |
| 〃 | | | `link_mm7xb56a` | | `{url: permalink, text: 'Instagram Reel' / 'Instagram Story'}` (only if a permalink exists) | | VERIFIED |
| 〃 (same mutation document) | | | group | MOVE | `group_title` (Posted) | | VERIFIED |
| `Sync Posted to Customer Projects` | **5091110326** | receipt `sourceProjectId` | `color_mm1ryfcb` | CCV | `Posted` | — | VERIFIED |
| `Save Publication Note` | 5105608159 | itemId | `status` | CMCV | `Needs Review` | `Guard Publication Review`: skips if the status is Posted, Paused or Skipped, Post Link is set, or the Format changed. **`Publishing` and every other status are not protected.** | VERIFIED |
| 〃 | | | `long_text_mm7zbtbn` | | the existing text if any, else the reason (it never overwrites human text) | | VERIFIED |
| 〃 | | | `long_text_mm7ysrbz` | | `فحص النشر: <reason>` ("publish check: …") | | VERIFIED |

WF2 never writes Publish at, Post Date or Post Time, Topazed, Caption or Last checked (VERIFIED).

### 4.3 WF3 — Schedule Supervisor (`WasetSocialScheduleGuard`)

Every WF3 write happens only after `Repair Still Applies` confirms on a live re-read that the item:
- still has status `Scheduled`,
- has the same Format, Verified media ID and Source asset version,
- has the same `at()`, and
- has an empty Post Link.

(VERIFIED)

| Node (write) | Board | Item | Column | Op | Value | Label |
|---|---|---|---|---|---|---|
| `Save Schedule Repair` ← `Build Reschedule Update` | 5105608159 | plan item | `date4` | CMCV | helper `date` (Cairo) | VERIFIED; value DELEGATED-TO-HELPER L567-586 |
| 〃 | | | `hour_mm7xy9cf` | | helper `{hour, minute}` | VERIFIED |
| 〃 | | | `date_mm7y8s9t` | | UTC from helper `at` | VERIFIED |
| 〃 | | | `long_text_mm7ysrbz` | | `مراقب الجدولة: <reason>؛ الموعد الجديد …` ("schedule supervisor: … new time …") | VERIFIED |
| 〃 | | | `long_text_mm7zbtbn` | | `''` (cleared) | VERIFIED |
| `Save Schedule Repair` ← `Build Block Update` | 5105608159 | plan item | `status` | CMCV | label from the helper plan: `Needs Review` or `ستوري طويل` | VERIFIED write; label DELEGATED-TO-HELPER L410, L416 |
| 〃 | | | `date_mm7y8s9t` | | `{}` (cleared). `date4` and hour are kept. | VERIFIED |
| 〃 | | | `long_text_mm7zbtbn` | | reason | VERIFIED |
| 〃 | | | `long_text_mm7ysrbz` | | `مراقب الجدولة: تم وقف الاعتماد — <reason>` ("approval stopped") | VERIFIED |
| `Save Repair History` ← `Repair Note` | 5105608159 | plan item | (update / comment) | CU | Arabic repair note: reason, new slot or block notice, previous time | VERIFIED |

---

## 5. Item creation, duplication, updates, subitems and group moves

| Operation | Exists? | Where | Label |
|---|---|---|---|
| Item creation on 5105608159 | Yes | WF1 `Build Sync Mutations` → `Apply Source Sync` (batches of 20 aliased mutations). Created only when the source Code is non-empty and unique on the source board, the source format is Post or Story, and no social item matches by Source item ID or (when that is empty) by Code. | VERIFIED |
| Updating existing social items from the source | **Only Canceled → Skipped.** Later changes to source format or link are not propagated ("Social owns format"). | WF1 `Build Sync Mutations` | VERIFIED |
| Item duplication (`duplicate_item`) | **NOT IMPLEMENTED** | grep 0 in all three exports | VERIFIED |
| Item archive / delete | **NOT IMPLEMENTED** | grep 0 | VERIFIED |
| Recreating an item that a human deleted or archived | Possible: the sync matches only against current items, so a deleted item whose source project still qualifies is created again. | WF1 `Build Sync Mutations` | INFERRED |
| Updates (comments) on social items | Yes, WF3 only (`Save Repair History`). WF1 and WF2 post none. | WF3 | VERIFIED |
| Updates on source-board subitems | Yes, WF1 (`Save Editor Instructions`, `Reopen Existing Editor Task`) | WF1 | VERIFIED |
| Subitems on social board (5105687146) | **NOT IMPLEMENTED** | — | VERIFIED |
| Subitems on source projects (editor tasks) | Yes, WF1 `Create Assigned Editor Task` (one per social item, found again by name prefix). Reopened (status → Working on it, plus a new update) when `refresh` is true. | WF1 | VERIFIED |
| Group move → Posted | Yes, WF2 `Build Posted Update` | WF2 | VERIFIED |
| Group move → Skipped | **NOT IMPLEMENTED**. Canceled items get status Skipped but stay in their group. | — | VERIFIED |
| Group move on Story↔Post change | **NOT IMPLEMENTED** | — | VERIFIED |

---

## 6. Status transition matrix

Labels seen in code: `جاري فحص الفيديو` ("checking the video"), `مقبول كبوست` ("accepted as a post"), `ستوري طويل` ("long story"), `Working on it`, `Waiting for Editor`, `Needs Review`, `Redy For Scheduled`, `Scheduled`, `Posted`, `Skipped`.

Labels that are read but never set by any workflow: `Paused`, `Unscheduled`, `Publishing`.

`Publishing` is listed in the `protectedStatus` sets, but no workflow sets it (VERIFIED).

### 6.1 Which statuses each workflow accepts as input

| WF | Picks up items in | Never modifies items in | Label |
|---|---|---|---|
| WF1 | `Unscheduled`, `Working on it`, `Redy For Scheduled`, `Waiting for Editor`, `Needs Review`, `جاري فحص الفيديو`, `ستوري طويل`, `مقبول كبوست`, **and `Scheduled`** (re-checked every run as `migration`) | Posted, Skipped, Paused, Publishing (queue filter + guard) | VERIFIED (`Preparation Queue`, guards) |
| WF2 | `Scheduled` with `at() ≤ now`; also any item with a helper receipt at stage `published` and `sourceSynced` false, **whatever its Monday status** | — (the review write is guarded for Posted, Paused, Skipped) | VERIFIED (`Due Queue`) |
| WF3 | `Scheduled` only | everything else | VERIFIED (`Repair Still Applies`) + DELEGATED-TO-HELPER (`monitor_plan` L405) |

### 6.2 Transitions (FROM → TO, by whom)

| FROM | TO | Set by (workflow / node) | Condition | Label |
|---|---|---|---|---|
| (none, new item) | جاري فحص الفيديو | WF1 `Apply Source Sync` | new source project | VERIFIED |
| any except Posted, Skipped, Paused, Publishing | Skipped | WF1 `Apply Source Sync` | source format = Canceled | VERIFIED |
| WF1-eligible (incl. Scheduled) | جاري فحص الفيديو | WF1 `Save Selected Version` | asset version or format changed | VERIFIED |
| WF1-eligible | جاري فحص الفيديو | WF1 `Save Early Warning` | Story preflight pending | VERIFIED |
| WF1-eligible | ستوري طويل | WF1 `Save Early Warning` / `Save Item Note` (`Waiting for Editor`) | Story ≥ 60 s | VERIFIED |
| WF1-eligible | Needs Review | WF1 `Save Early Warning`, `Save Item Note` (`Media Waiting or Review`, `No Valid Style Slot`, `Item Needs Review`) | invalid duration, QA failure not fixable by an editor, no slot, missing source, missing brief, any per-item error | VERIFIED |
| WF1-eligible | مقبول كبوست | WF1 `Save Accepted Duration` | Post passes preflight (a Post always passes) | VERIFIED |
| WF1-eligible | Working on it | WF1 `Save Accepted Duration` (Story) / `Media Waiting or Review` (pending) | | VERIFIED |
| WF1-eligible | Waiting for Editor | WF1 `Save Item Note` ← `Waiting for Editor` | no final file, Topaz not confirmed, <1080, too large, etc. | VERIFIED |
| WF1-eligible | Redy For Scheduled | WF1 `Save QA Result` | helper QA passed and delivery registered | VERIFIED |
| Redy For Scheduled (same run) or Scheduled (migration) | Scheduled | WF1 `Save Schedule` | slot reserved and `Schedule Still Allowed` | VERIFIED |
| Scheduled | Scheduled (new time) | WF3 `Build Reschedule Update` | helper plan `reschedule` | VERIFIED + DELEGATED (L423-436) |
| Scheduled | Needs Review / ستوري طويل | WF3 `Build Block Update` | helper plan `block` | VERIFIED + DELEGATED (L410-421) |
| Scheduled | Posted | WF2 `Mark Posted in Monday` | Instagram publish confirmed | VERIFIED |
| **any** (including Paused or Skipped set after the publish) | Posted | WF2 reconcile path (`Due Queue` rule A → `Build Posted Update`) | receipt `published`, not source-synced | VERIFIED |
| any except Posted, Paused, Skipped | Needs Review | WF2 `Save Publication Note` | any publish-path failure or gate refusal | VERIFIED |
| any | Paused / Skipped / Unscheduled / other manual labels | **Humans** | — | INFERRED (no workflow sets them) |

### 6.3 Compact matrix (rows = TO label, columns = who sets it)

| TO label | WF1 | WF2 | WF3 | Human |
|---|---|---|---|---|
| جاري فحص الفيديو | ✔ | — | — | possible |
| مقبول كبوست | ✔ | — | — | possible |
| Working on it | ✔ | — | — | possible |
| Waiting for Editor | ✔ | — | — | possible |
| ستوري طويل | ✔ | — | ✔ (block) | possible |
| Needs Review | ✔ | ✔ | ✔ (block) | possible |
| Redy For Scheduled | ✔ | — | — | possible |
| Scheduled | ✔ | — | (keeps it on reschedule) | possible: a human-set Scheduled without a committed reservation is refused by the WF2 claim (DELEGATED L536-537) and blocked or rescheduled by WF3 |
| Posted | — | ✔ | — | possible |
| Skipped | ✔ (Canceled source) | — | — | ✔ |
| Paused | — | — | — | ✔ only |
| Unscheduled | — | — | — | ✔ only |
| Publishing | — | — | — | never set (dead label) |

---

## 7. Columns written by more than one workflow

| Column | Writers | Conflict notes | Label |
|---|---|---|---|
| `status` | WF1, WF2, WF3 | WF1 can move a Scheduled item to جاري فحص الفيديو, Needs Review or Waiting for Editor while WF2 has a live claim in the helper. Monday shows no `Publishing` state. | VERIFIED (writes); race INFERRED |
| `long_text_mm7ysrbz` System update | WF1, WF2, WF3 | Last writer wins. History is lost on every write. | VERIFIED |
| `long_text_mm7zbtbn` المطلوب منك | WF1, WF2, WF3 | WF2 preserves existing text. WF1 and WF3 overwrite or clear it. | VERIFIED |
| `date_mm7y8s9t` Publish at | WF1 (set / clear), WF3 (set / clear) | Both rely on the shared `preparation` lock, so they do not run at the same time (DELEGATED L450-456). | VERIFIED |
| `date4` Post Date | WF1, WF3 | as above | VERIFIED |
| `hour_mm7xy9cf` Post Time | WF1, WF3 | as above | VERIFIED |
| Group membership | WF1 (at creation), WF2 (→ Posted) | — | VERIFIED |

Columns with **one** automation writer:
- WF1 only: Topazed, Dropbox Link, Folder Link, Version Check, Publish video, Style, Story variety, Social owner, Source item ID, Source asset version, Verified media ID, Video measurements, Processed format, Last checked, Caption, Code, Format (at creation).
- WF2 only: Post Link, Instagram media ID, Published at.

Source board column `color_mm1ryfcb` is read by WF1 and WF2 and written by WF2 (`Posted`). Unreviewed Office workflows may also write it (see `02_WORKFLOW_INVENTORY.md` §C, NOT VERIFIED).

---

## 8. Human-vs-automation conflict analysis (per column)

| Column | Human intent | What automation does | Outcome | Label |
|---|---|---|---|---|
| Status → `Paused` / `Skipped` | Stop the item | Excluded from WF1 and WF3; WF2 requires `Scheduled`; the WF1 reconcile frees the slot. | **Honoured.** One exception: a WF2 receipt already at `published` is written back as Posted, which is correct because it was published. Between the WF2 final re-check and `media_publish` there is a gap of one helper call in which a Pause is not seen. | VERIFIED; the gap is INFERRED |
| Status → other labels (e.g. `Unscheduled`) on a Scheduled item | Hold or hand back | WF1 treats Unscheduled as eligible and may re-schedule it automatically. | **Not honoured**: only Paused and Skipped stop automation. | VERIFIED |
| Status → `Scheduled` set by hand | Force a publish | No committed reservation exists: the WF2 claim raises an error, giving Needs Review (DELEGATED L536-537). WF1 re-processes it as `migration`. WF3 blocks it if the media is unverified, or reschedules it if there is no matching reservation (DELEGATED L397-436). | Overridden | VERIFIED + DELEGATED |
| Post Date / Post Time | Choose a time | `at()` **prefers Publish at (UTC)**. If Publish at is set, edits to Post Date and Post Time are ignored. If Publish at is empty, WF1 uses `requestedAt = at(item)` with `preserveSchedule = true` (`Reserve Style Slot — Input`) and the helper keeps that time **without checking the slot grid** (L507-512). WF3 then flags it as off-grid and reschedules it to the grid (L425-430). | Human time overridden, either at once (Publish at present) or within about 30 min (WF3) | VERIFIED + DELEGATED |
| Publish at | Choose a time | Mismatch with the committed reservation: WF3 reschedules ('the written time does not match the approved reservation', L433-436); the WF2 claim refuses. WF1 `migration` reserve drops the old reservation and re-reserves the requested time when it is free (L499-501, L507-512). | Race between WF1 (keeps the human time) and WF3 (moves it). Whichever runs first under the lock wins. | VERIFIED + DELEGATED; race INFERRED |
| Topazed | Declare that Topaz was applied | WF1 `Save Selected Version` writes `Topazed` / `Not yet` from the **run-start snapshot**. A human toggle made during a run (up to about 7 min, see exec notes) can be reset to `Not yet`. It is also reset whenever the asset version changes (intended). | Partly overwritten | VERIFIED |
| Caption | Write or edit the caption | WF1 writes only if the snapshot caption was empty. A caption typed during a run can be overwritten by the LLM caption. The WF2 final gate refuses to publish if the caption changed after the run snapshot, giving Needs Review. | Mostly honoured; small race | VERIFIED |
| Format | Change Story↔Post | Honoured: "Social owns Format". WF1 detects the change through Processed format, invalidates the old approval and schedule, and re-prepares. **The item is not moved to the matching group.** | Honoured (no group move) | VERIFIED |
| Story variety | Override the rotation key | Overwritten every run by the computed key (`Save Selected Version`). `Preparation Queue` ignores the human value. | **Not honoured** | VERIFIED |
| Dropbox Link | Point at a specific file | Overwritten by the auto-selected file (`Candidates` → `Save Selected Version`). Only Folder Link and the source link steer the selection. | **Not honoured** | VERIFIED |
| Folder Link | Point at the project folder | Read first, so it is honoured. WF1 re-writes it **without a guard**, even on Posted, Paused or Skipped items. | Honoured; extra writes | VERIFIED |
| IG Colab | Request collaboration | WF2 refuses to publish and asks for manual review. | Honoured (blocks automation) | VERIFIED |
| Social owner | Assign a person | Written only when empty | Honoured | VERIFIED |
| المطلوب منك | Human or automation instruction | WF1 and WF3 clear or overwrite it; WF2 keeps it | Mixed | VERIFIED |
| Post Link | Mark as posted manually | Every workflow treats a Post Link as "already published" and stops. | Honoured | VERIFIED |
| Notes, Numbers | Free use | Untouched | Honoured | VERIFIED |

---

## 9. Feedback-loop risks

| # | Loop | Mechanism | Effect | Label |
|---|---|---|---|---|
| L1 | WF1 editor-task refresh | For an item with no final file, `Processed format` is never written. `Reopen Editor Plan` therefore sees `format ≠ text_mm7z139h` on every run. It sets the subitem to Working on it and posts a **new update**. `Waiting for Editor` does not stamp Last checked, so the item stays first in its queue. | Up to 6 updates per hour per item on source subitems; starves its style group | VERIFIED (code); impact INFERRED |
| L2 | WF3 block → WF1 re-prepare → WF3 block | WF3 blocks Scheduled items that are not "verified" (helper `verified()`, L397-401). WF1 treats `Needs Review` as eligible, re-prepares and re-schedules. If the verification difference persists (for example a delivery URL mismatch), WF3 blocks again. | Oscillation Scheduled ↔ Needs Review, with repeated comments on social items | INFERRED. The exec notes show WF3 blocking 25 items, then WF1 re-picking them. |
| L3 | WF2 reconcile replay | `Due Queue` rule A replays every minute while `sourceSynced` is false. If `Sync Posted to Customer Projects` keeps failing, Posted, System update and the group move are rewritten every minute, and the review note is suppressed because the status is Posted. | Silent infinite retry (no attempt cap) | VERIFIED |
| L4 | WF1 re-processing of Scheduled items | Every Scheduled item is re-processed every 10 min (`migration`). `Save Selected Version` rewrites about 10 columns each time, and any transient Dropbox or Monday error moves it to Needs Review and clears Publish at. | Activity-log noise; unscheduling caused by transient errors | VERIFIED |
| L5 | WF2 idle review query | With an empty queue, `{empty:true}` flows into `Read Status Before Review` with `ids:[null]`. | About 1,440 failing Monday calls per day (observed) | VERIFIED |
| L6 | WF1 recreation | A deleted or archived social item is recreated from the source on the next sync. | Duplicate work | INFERRED |
| L7 | Monday-native automations | Unknown. If a board automation reacts to Status or group changes made by these workflows, it could create loops. | Unknown | NOT VERIFIED. Board automations were not inspected. |

---

## 10. Webhook handling

- The three Social Media workflows have **no webhook trigger** and no Execute Workflow node (VERIFIED: grep `webhook` = 0 in all three exports; triggers are schedule or manual only).
- No Monday webhook subscription for board 5105608159 was found among the 13 workflows readable through MCP (VERIFIED in `02_WORKFLOW_INVENTORY.md` §B; none references 5105608159).
- 15 workflows could not be read (`availableInMCP = false`). Whether any of them listens to, or writes to, board 5105608159 is **NOT VERIFIED**.
- Consequence: the system is purely polling-based. Human edits take effect at the next WF1 run (≤10 min), WF2 run (≤1 min) or WF3 run (:05 and :35).
