# WASET SOCIAL MEDIA AUTOMATION — CURRENT SYSTEM TECHNICAL DOCUMENTATION

# 05 — Scheduling and Publishing

## 0. Scope and evidence labels

This chapter covers four parts of the system:
- media preparation (WF1 together with `helper.py`),
- slot scheduling (WF1 and the helper),
- publishing to Instagram (WF2 and the helper),
- schedule supervision (WF3 and the helper).

The workflows:

| Short name | n8n ID | Name |
|---|---|---|
| WF1 | `qI1N5VNgpRjnZAKH` | Waset Social V2 — 1 Prepare & Schedule |
| WF2 | `pUIshuf16zIYoYRz` | Waset Social V2 — 2 Publish When Due |
| WF3 | `WasetSocialScheduleGuard` | Waset Social V2 — 3 Schedule Supervisor |

Helper references (`L…`) point to the **local copy** `/Volumes/Zeno/Bondok/helper_reference/helper.py` (657 lines, sha1 `57e158cb…`). Each workflow calls the helper as `python3 /home/node/.n8n-files/waset-social/helper.py <base64 {path, body}>` through a "`<X> — Local n8n`" executeCommand node. Nobody has checked that the server runs the same file.

| Label | Meaning |
|---|---|
| **VERIFIED** | Read in the workflow JSON |
| **DELEGATED-TO-HELPER** | Logic is in the local `helper.py` copy (deployment not verified) |
| **INFERRED** | Deduced, not proven |
| **NOT IMPLEMENTED** | Absent from both the workflows and the helper |

The helper keeps its state in SQLite (`state.sqlite`, WAL mode). The tables are `locks`, `reservations (UNIQUE(format,at))`, `jobs`, `media`, `publications`, `assets`, `item_state`, `audit_log` and `health` (DELEGATED-TO-HELPER L130-149). Every endpoint except preflight and prepare runs as one `BEGIN IMMEDIATE` transaction (L449).

Runtime note (historical, from `notes_exec.md`): from about 15:00 UTC on 2026-10-09, every helper call exited with code 1. The WF1 and WF3 lock calls and the WF2 snapshot call all failed, so **no preparation, supervision or publishing can currently complete**. No Instagram publish has ever been observed.

---

## 1. Media preparation pipeline (WF1 + helper)

| Step | What happens | Where | Label |
|---|---|---|---|
| 1.1 Lock | Takes the global `preparation` lock (owner = execution ID, TTL 2100 s). If the lock is busy, the run ends silently because the false output of `Preparation Lock Acquired?` is not connected. | WF1 `Acquire Preparation Lock*`; helper `/v1/lock` L450-456 | VERIFIED + DELEGATED |
| 1.2 Queue | Builds the work queue: eligible statuses (see 04 §6.1), sorted by Last checked ascending, grouped by rotation key, interleaved round-robin across groups, capped at **20 items per run**. An optional LLM (`openai/gpt-4.1-mini`) reorders the style prefixes; `Validate AI Style Order` accepts only known prefixes and falls back to the deterministic order. | WF1 `Preparation Queue`, `Light AI — Diversify Styles`, `Validate AI Style Order` | VERIFIED |
| 1.3 Source validation | The item needs exactly one source project (by Source item ID, else by Code) and a valid style code. Otherwise: Needs Review. | WF1 `Item Context` | VERIFIED |
| 1.4 Folder | Folder priority: Folder Link, then the first `/scl/fo/` URL among Dropbox Link and the source link, then any Dropbox URL. With no folder, it creates `/Social Media/Production/<code>/<format>` (Dropbox `create_folder_v2`, `autorename:false`), shares it publicly and writes Folder Link (**no guard**). | WF1 `Has Folder?`, `Create Project Folder`, `Share New Folder`, `Save Project Folder` | VERIFIED |
| 1.5 Listing | Lists the folder recursively with Dropbox `list_folder` / `list_folder/continue` (limit 2000). If the link points straight at a file, only that file is used. If the parent folder name contains `#<n>`, the parent folder is listed instead. | WF1 `Folder Metadata`, `Plan Listing`, `List Files` | VERIFIED |
| 1.6 Source file selection | **Considered:** files ending `.mp4`, `.mov` or `.m4v`, in the project folder, a `caption*` subfolder or a project archive subfolder.<br>**Excluded:** `.fcp*bundle` contents, `archive` and `old` folders, and "junk" names (test, tmp, proxy, preview, sample, draft, copy, old, backup, …). Files whose names indicate no captions (before/without/no/pre + cap) are excluded unless nothing else remains.<br>**Ranking:** higher `vN[.M]`, then versioned before unversioned, then `topaz` in the filename, then newest `client_modified`. A captions-folder file is used only when the project folder has no video.<br>**Folder check:** if the folder name and item name keys differ (letters+digits), no file is picked. | WF1 `Candidates`, `Selected File` | VERIFIED |
| 1.7 Version binding | `assetKey = <dropbox file id>@<rev>`. Writes Dropbox Link, Version Check, Source asset version and Processed format. Topazed stays `Topazed` only if the stored asset version equals the new `assetKey`; otherwise it is reset to `Not yet`. On any change it clears Verified media ID and Publish at, and sets status `جاري فحص الفيديو` ("checking the video"). | WF1 `Source Media` → `Save Selected Version` (guarded) | VERIFIED |
| 1.8 Invalidate | If the format or asset changed, the helper deletes the reservation (committed or not) and writes `audit_log content_changed`. It raises an error if a publication receipt already exists. It returns `reuseScheduled = true` when a Scheduled item is unchanged and still verified; in that case the item is finished with no further work. | WF1 `Invalidate Changed Content`, `Unchanged Scheduled Content?`; helper L460-477 | VERIFIED + DELEGATED |
| 1.9 Early Story duration (ffprobe) | For a Post, the result is always `{ready:true, skipped:true}` (L306). For a Story, a detached child job runs `preflight_media` with 2 global capacity slots: it downloads the Dropbox `raw=1` file (≤5 GB), checks the Dropbox content hash, runs `ffprobe` and calls `duration_result`: `≥60 s` gives `replaceRequired`, an invalid duration gives Needs Review (L266-300, L303-340). The first call returns `pending`, and the result is read on a later run. In WF1, `Story Duration Accepted?` additionally requires `0 < duration < 60` and a matching assetKey and contentHash. | WF1 `Early Story Duration*`, `Story Duration Accepted?`, `Early Story Warning`, `Duration Accepted Update`; helper `/v1/media/preflight` | VERIFIED + DELEGATED |
| 1.10 Caption | For a Post with an empty caption, the brief is the oldest source-project update with at least 60 characters once URLs are removed. The LLM (`openai/gpt-5.6-sol`) writes the caption, and hashtags are de-duplicated. The output is **not** validated. With no brief: Needs Review. | WF1 `Read Client Brief`, `Write Caption`, `Build Caption Update`, `Save Caption` | VERIFIED |
| 1.11 Topaz confirmation | A **human** sets Topazed = `Topazed` for the selected asset. The helper refuses to prepare unless `topazed is True` (L193, L344). The stored marker `topazVerification='editor_confirmation_bound_to_asset'` (L247) shows this is a declaration, not a technical detection. A `topaz` filename only affects ranking. | WF1 `Source Media`; helper L193, L247, L344 | VERIFIED + DELEGATED |
| 1.12 1080 preparation (ffmpeg) | A detached child job runs `prepare_media`:<br>• downloads `raw=1` (≤5 GB; needs 6 GB free and stops below 2 GB free) and checks the content hash;<br>• `ffprobe` on the source: Story ≥60 s → editor; short edge <1080 → editor;<br>• scales so the **short edge is exactly 1080** (larger sources are downscaled);<br>• bitrate `min(12 Mbps, 140 MB×8/duration − 160 kbps)`; refused below 500 kbps;<br>• two-pass H.264 `yuv420p` + AAC 128k + `faststart`;<br>• re-probes and runs `media_failure` (Topaz, short edge ≥1080, size <300,000,000, Story <60 s);<br>• then a **hard cap `bytes ≥ 145_000_000` → error**.<br>It stores a `media` row (sha256, orientation, `qaPolicy=3`). Retries back off up to 1 h, max 3 attempts. (L192-263, L343-388, L649) | WF1 `Prepare 1080 Media*`; helper `/v1/media/prepare` | DELEGATED |
| 1.13 Workflow QA re-check | WF1 re-checks the helper result: finite measurements, `topazed===true`, `min(w,h) ≥ 1080`, `bytes < 300000000`, Story `duration < 60`. A failure goes to an editor task or to Needs Review. | WF1 `Prepare 1080 Media`, `Media Passed QA?` | VERIFIED |
| 1.14 Measurements | Video measurements = `W×H \| MB \| s`. System update = `QA passed: …` | WF1 `Validated Media Update` | VERIFIED |
| 1.15 Prepared upload | 1. Ensures `/Social Media/Prepared` exists.<br>2. Reads the helper `filePath` from the n8n disk.<br>3. Uploads with Dropbox `files/upload` to `/Social Media/Prepared/<itemId>-<mediaId>.mp4` (overwrite, mute, 300 s timeout).<br>4. Creates a public shared link and converts it to a `raw=1` URL.<br>5. Calls helper `/v1/media/delivered`, which re-verifies the media and stores the URL (L478-486).<br>6. Writes Publish video, Verified media ID and status `Redy For Scheduled`. | WF1 `Upload Verified 1080 Video`, `Verified Delivery Link`, `Register Verified Delivery`, `Save QA Result` | VERIFIED + DELEGATED |
| 1.16 Editor task | When there is no final file, Topaz is missing, the source is <1080 or too long, or similar: WF1 creates (or reopens) a source-project subitem `Social <itemId> — …` with English instructions and sets the social status to `Waiting for Editor` or `ستوري طويل` ("long story"). | WF1 Stage I nodes | VERIFIED |
| 1.17 Release | Releases the lock (`/v1/unlock`), then runs `/v1/maintenance`, which deletes local mp4s more than 30 days after `source_synced` (L602-609). | WF1 `Release Preparation Lock`, `Retain Published Files` | VERIFIED + DELEGATED |

---

## 2. Scheduling system

### 2.1 Slot grid and time model

| Aspect | Value | Where | Label |
|---|---|---|---|
| Timezone | `Africa/Cairo` for slot generation. Slots are stored as UTC ISO strings. zoneinfo handles DST. | helper `TZ` L30; workflow `settings.timezone` (all three) | DELEGATED + VERIFIED |
| Post (Reels) slots | `REELS = {5:(21,0), 0:(21,0), 2:(22,45), 3:(21,0)}`, using Python weekdays (Mon=0), which gives **Sat 21:00, Mon 21:00, Wed 22:45, Thu 21:00** | helper L32 | DELEGATED |
| Story slots | `STORIES = [(11,0),(14,0),(18,0),(21,0),(22,0)]`, daily | helper L33 | DELEGATED |
| Horizon / lead | 84 days; a slot must be later than now + 5 min | `slots()` L99-107 | DELEGATED |
| One item per slot and format | `reservations UNIQUE(format,at)`, plus the `used` set in `next_slot` | L138, L114-118 | DELEGATED |
| Board time fields | `at(item)`: if **Publish at** (`date_mm7y8s9t`) has a date and time, it is read as **UTC**. Otherwise Post Date + Post Time are read in **Africa/Cairo**. | shared prelude in WF1, WF2 and WF3 | VERIFIED |
| Workflow constants | None. The slot grid does not exist in any workflow JSON. `styleWeekdays:{}` is passed but empty. | WF1, WF2, WF3 `Configuration` | VERIFIED |

### 2.2 Reserve → commit → cancel → reconcile

| Operation | Caller | Behaviour | Label |
|---|---|---|---|
| **Reconcile** `/v1/schedule/reconcile` | WF1 `Reconcile Reserved Slots`, at the start of each run with `events` (all Scheduled and Posted items with a time) | Deletes every reservation, committed or not, whose item is missing from the snapshot, is not Scheduled or Posted, or has a different `at` (L487-493). This frees slots of Paused, Skipped and Needs Review items. | VERIFIED + DELEGATED |
| **Reserve** `/v1/schedule/reserve` | WF1 `Reserve Style Slot` with `{itemId, format, code, rotation, preserveSchedule, requestedAt, styleWeekdays:{}, occupied}` | 1. Drops an existing reservation if its format or requested time differs.<br>2. If one remains, returns it.<br>3. With `preserveSchedule` (set when the item already has a time and the format did not change): keeps `requestedAt` if it is more than 5 min ahead and does not collide with another item. **The grid is not checked.**<br>4. Otherwise calls `next_slot`.<br>5. Inserts an **uncommitted** row and returns `{reserved, at (UTC), date, hour, minute (Cairo)}`, or `reserved:false` (L494-525). | VERIFIED + DELEGATED |
| Pre-schedule recheck | WF1 `Recheck Before Scheduling` → `Schedule Still Allowed` | Fresh read. Requires: eligible status (or unchanged Scheduled migration), source not Canceled, same Format, `Topazed`, same asset version, same Verified media ID, empty Post Link. | VERIFIED |
| Save | WF1 `Save Schedule` (guarded) | Writes Post Date, Post Time, Publish at (UTC) and status `Scheduled` | VERIFIED |
| **Commit** `/v1/schedule/commit` | WF1 `Commit Slot`; WF3 `Commit Repaired Slot` | `UPDATE … SET committed=1 WHERE item=? AND at=?` (exact string match). Always returns `ok`, even when 0 rows change (L526-528). | VERIFIED + DELEGATED |
| **Cancel** `/v1/schedule/cancel` | WF1 `Cancel Reserved Slot` (recheck failed) | Deletes only an **uncommitted** reservation (L596-598) | VERIFIED + DELEGATED |
| Orphans | A guard skip or error after reserve leaves an uncommitted row, which the next run's reconcile removes. A Monday save followed by a failed commit leaves the board Scheduled with an uncommitted slot, so the WF2 claim will refuse it. | — | INFERRED |

### 2.3 Style rotation (adjacent-style diversification)

| Layer | Rule | Where | Label |
|---|---|---|---|
| Code → style | `/^\s*([a-z]{2,3})\s*[#_\- ]?\s*\d+/i` → upper-cased 2–3 letter prefix | prelude `style()` in all workflows; helper `style()` L85-89 | VERIFIED + DELEGATED |
| Rotation key | Story: `STYLE:FIRSTWORD(item name)`. Post: `STYLE`. The board column Story variety is read by `rotation()` in `Board Schedule`, WF3 and WF2, but WF1 `Preparation Queue` computes its own key and then overwrites Story variety with it. | WF1 `Preparation Queue`, `Source Media`; prelude `rotation()` | VERIFIED |
| Queue fairness | Round-robin over rotation groups; optional LLM reordering of the prefixes | WF1 `Preparation Queue`, `Validate AI Style Order` | VERIFIED |
| Slot choice | `next_slot` skips a free slot when the nearest earlier **or** later same-format event has the same key (L123-125) | helper | DELEGATED |
| Supervision | `monitor_plan` reschedules an item that follows the same rotation in its format when another verified rotation exists (L431) | helper via WF3 | DELEGATED |
| Style weekday map | Code exists (L118-119, Posts only) but is **effectively off**: `styleWeekdays:{}` | helper + all `Configuration` nodes | VERIFIED + DELEGATED |

### 2.4 Starvation prevention

| Mechanism | Detail | Where | Label |
|---|---|---|---|
| Bounded diversity | Once a candidate slot is more than **7 days (Post)** or **1 day (Story)** after the first free slot, `next_slot` returns that first free slot even if the adjacent style is the same ("scarce styles cannot starve the queue") | helper L116-122, L127 | DELEGATED |
| Queue ageing | Sort by Last checked ascending, round-robin, 20-item cap | WF1 `Preparation Queue` | VERIFIED |
| Known gap | `Waiting for Editor`, `Media Waiting or Review`, `No Valid Style Slot` and `Build Scheduled Update` do not stamp Last checked. Items without a final file therefore stay at the front of their group on every run and crowd others out within that group. | WF1 | VERIFIED |
| WF3 | No starvation logic (no age data is sent) | WF3 | NOT IMPLEMENTED in WF3 |

---

## 3. Publishing system (WF2 + helper)

### 3.1 Selection

- **Trigger:** every 1 min. **Kill switch:** `Configuration.armed`. (VERIFIED)
- **Snapshot:** a full board snapshot (fail-closed), then helper `/v1/publish/snapshot` to read every receipt (L529-530). Both are VERIFIED / DELEGATED.
- **`Due Queue`** builds the work list (VERIFIED):
  - **Rule A:** every receipt with `stage==='published'` and `!sourceSynced` (a post-publish replay, whatever the Monday status).
  - **Rule B:** status exactly `Scheduled` and `at() ≤ now`.
  - There is no lower bound at this point.
- **`Publication Context`** decides validity (VERIFIED). All of these must hold:
  - Processed format equals Format
  - IG Colab is empty
  - Source item ID is present
  - the style code is valid
  - Format is Post or Story
  - Verified media ID is present
  - Publish at has a time
  - for a Post, the caption is present
- **`Unique Source Project`:** the source project must match the Code and must not be `Canceled`. (VERIFIED)

### 3.2 Claim / lease

Helper `/v1/publish/claim` (L531-564) receives `{account: <IG user id>, assetKey, itemId, format, at, mediaId, topazed, expectedUrl, sourceProjectId, owner}`. DELEGATED-TO-HELPER.

**The claim raises an error (→ Needs Review) if:**
- there is no **committed** reservation matching the format and `at`,
- the media file is missing or fails `verified_media`,
- `topazed` is not true, or the assetKey changed, or
- the stored delivery URL differs from Publish video.

**The claim returns `claimed:false` with one of these stages:**

| Stage | Condition |
|---|---|
| `duplicate_asset` | Another item has already claimed the same `account\|format\|contentHash` |
| `outside_due_window` | The time since `at` is below 0 s or above 7200 s (**2 h**) |
| the existing receipt's stage | A receipt is already at `publish_requested`, `published` or `source_synced` |
| `lease_busy` (`quiet:true`) | The current lease was renewed less than `LEASE_SECONDS = 180` s ago |

**Otherwise** it records the claim, stores the asset identity and returns `{claimed:true, url, receipt}`.

- **Heartbeat** `/v1/publish/heartbeat` runs on every poll. It requires the same owner, stage `claimed` or `container_created`, and an unexpired lease (L590-595).
- **Checkpoints** `/v1/publish/checkpoint` (L610-627) follow the stage order `claimed → container_created → publish_requested → published → source_synced`:
  - a stage may only stay the same or move forward by one;
  - a second `publish_requested` is refused ("never repeat the publish call");
  - the lease is checked only when moving into `container_created` or `publish_requested`.
- **Monday shows no `Publishing` status** during any of this. The status stays `Scheduled`. (VERIFIED)

### 3.3 Graph API calls (VERIFIED)

The IG user ID is configured in the workflow (not reproduced here). The credential is `httpTemplatedCustomAuth`.

| # | Node | Method / endpoint | Body / params | Retry |
|---|---|---|---|---|
| 1 | `Create Container` | `POST https://graph.facebook.com/v26.0/{ig-user-id}/media` | Post: `{media_type:'REELS', video_url, caption, share_to_feed:true}`. Story: `{media_type:'STORIES', video_url}`. `video_url` comes from the claim result. | **none** (explicitly off) |
| 2 | `Check Container` | `GET /v26.0/{container-id}?fields=status_code,status` | — | 3× / 2 s |
| 3 | `Publish To Instagram` | `POST /v26.0/{ig-user-id}/media_publish` | `{creation_id}` | **none** |
| 4 | `Get Permalink` | `GET /v26.0/{media-id}?fields=permalink` | — | 3× / 2 s, neverError |

There is no Facebook Page publishing, no carousel or image support, and no collaborator support. If IG Colab is set, the item goes to manual review. (VERIFIED)

### 3.4 Polling

- If the receipt already has a `containerId`, that container is resumed. Otherwise a new container is created and checkpointed (`container_created`).
- The poll loop is `Wait For Processing` (amount 30, no unit in the export; seconds is INFERRED from the Wait v1.1 default) → heartbeat → `Check Container`.
- `FINISHED` → continue. `IN_PROGRESS` with `attempt < 20` → keep polling. Anything else (ERROR, EXPIRED, poll exhaustion) → Needs Review with a generic reason; the Graph `status` text is not kept.
- At most about 10 min of polling. (VERIFIED)

### 3.5 Final gates before publish

| Gate | Check | Label |
|---|---|---|
| `Verify Source Revision` (Dropbox `files/get_metadata`) → `Source Revision Matches` | `id@rev` equals Source asset version | VERIFIED |
| `Recheck Immediately Before Publishing` → `Still Scheduled & Due` | Fresh read. All of the following:<br>• status `Scheduled`<br>• Topazed<br>• same Verified media ID, asset version, Publish video URL, Code, Format and Caption<br>• IG Colab and Post Link empty<br>• source not Canceled<br>• `at` unchanged, `at ≤ now`, and **now − at ≤ 7,200,000 ms** | VERIFIED |
| Story <60 s, short edge ≥1080, size | **Not re-checked in WF2.** It relies on the helper's `verified_media` at claim (L539-541, L62-67). | VERIFIED absence + DELEGATED |

### 3.6 Outcomes

| Outcome | Behaviour | Label |
|---|---|---|
| Success | 1. Checkpoint `publish_requested`.<br>2. `media_publish`.<br>3. Checkpoint `published` (mediaId; the helper stamps `publishedAt`).<br>4. Permalink, then checkpoint `published` again.<br>5. `Mark Posted in Monday`: Posted, Instagram media ID, Published at, Post Link, System update, **move to the Posted group**.<br>6. Source board `color_mm1ryfcb = Posted`.<br>7. Checkpoint `source_synced`. | VERIFIED + DELEGATED |
| Failure before `publish_requested` | Needs Review (guarded: skipped for Posted, Paused, Skipped, an existing Post Link, or a changed Format). The receipt stays at its stage; the container can be resumed later. | VERIFIED |
| `media_publish` error or timeout, or no ID returned | Needs Review with the error text. The receipt stays at `publish_requested`, and the helper never allows a second publish (L616-617). **No automatic reconciliation with Instagram**: a human must check. | VERIFIED + DELEGATED |
| Failure after `published` | The next run's Rule A replays the Monday and source write-back (self-healing; no attempt cap). | VERIFIED |
| Missed window (more than 2 h late) | The claim returns `outside_due_window` → Needs Review "publish time passed; choose a new time". WF3 also blocks items more than 2 h late (L421). | DELEGATED + VERIFIED |
| Timeouts | WF2 `executionTimeout` is 1800 s and items are processed one after another. The lease expires after 180 s without a heartbeat. | VERIFIED + DELEGATED |

---

## 4. Schedule monitoring (WF3 Supervisor)

| Aspect | Detail | Label |
|---|---|---|
| Trigger | Cron :05 and :35 (Cairo), plus `Manual Audit`, which **performs real writes** | VERIFIED |
| Lock | Same `preparation` lock as WF1, so WF3 never runs alongside WF1. If the lock is busy, the run ends silently. | VERIFIED |
| Plan | `/v1/monitor/plan`, read-only (L391-439). It looks only at `Scheduled` items with no publication receipt, sorted by `at`.<br>**Block** (status Needs Review, or `ستوري طويل` for a long Story) when any of these hold:<br>• a Story job has `replaceRequired`<br>• the media is not verified (Topazed, Processed format, asset, URL, file)<br>• the code is invalid<br>• `at` is missing<br>• `at` is more than 2 h in the past<br>**Skip** items due within 10 min.<br>**Reschedule** when any of these hold:<br>• the time is off the grid or has seconds ≠ 0<br>• two items share the same format and slot<br>• the same rotation follows itself while a verified alternative exists<br>• there is no matching committed reservation | DELEGATED |
| Staleness guard | `Repair Still Applies` re-reads the item live. It must still be Scheduled, with the same Format, media, asset and `at`, and no Post Link. | VERIFIED |
| Reschedule | `/v1/monitor/reserve` (L567-586) refuses when a publication exists, the content is no longer verified, or the item is due within 10 min. It picks `next_slot(now+10 min)` **without** styleWeekdays, replaces the reservation as uncommitted and audits it. WF3 then writes Post Date, Post Time, Publish at and System update, clears المطلوب منك ("action required"), commits, writes the audit log, and posts an Update (comment). | VERIFIED + DELEGATED |
| Block | Status = helper label, Publish at cleared, reason written to المطلوب منك and System update, audit log, Update (comment) | VERIFIED |
| What it does not see | Helper publication leases. Monday status stays `Scheduled` during a publish. Items with any receipt are skipped (L409), which covers claimed items. | VERIFIED + DELEGATED |
| Observed | Run 51712 blocked all 25 Scheduled items (24 → Needs Review "file not approved…", 1 → long story) | Historical (`notes_exec.md`) |

---

## 5. Content lifecycle: 20 steps

| # | Step | What happens | Workflow / node (helper line) | Label |
|---|---|---|---|---|
| 1 | **New video selected** (source project ready) | A project on Customer Projects (5091110326) has a **unique** Code, a Post or Story format (`color_mm1ryfcb`) and a Dropbox link. How or when the editor marks a video "final" is not modelled; the selection is made in step 3. | WF1 `Source Read Page`, `Build Sync Mutations` | VERIFIED |
| 2 | **Imported to For Social Media** | `create_item` in For Posts or For Stories with Source item ID, Code, Style, Format, Topazed = Not yet, links and status `جاري فحص الفيديو`. There is no update after import except Canceled → Skipped. | WF1 `Apply Source Sync` | VERIFIED |
| 3 | **Folder resolution and final-file selection** | Folder found or created, recursive listing, versioned/topaz/newest ranking, folder-name sanity check | WF1 `Has Folder?` … `Selected File` | VERIFIED |
| 4 | **Version binding and replacement detection** | `assetKey = id@rev`. A new asset or format resets Topazed, Verified media ID and Publish at, and the helper drops the reservation. If a publish receipt exists, the change is refused. | WF1 `Source Media`, `Invalidate Changed Content`; helper L460-477 | VERIFIED + DELEGATED |
| 5 | **Story duration preflight** | Background ffprobe. ≥60 s → `ستوري طويل` plus an editor task. Invalid → Needs Review. Post → skipped. | WF1 `Early Story Duration`; helper L266-340 | VERIFIED + DELEGATED |
| 6 | **Caption (Post only)** | LLM from the client brief, only when the caption is empty | WF1 `Write Caption`, `Save Caption` | VERIFIED |
| 7 | **Topaz confirmation** | A human sets Topazed for the selected asset. Without it: editor task, `Waiting for Editor`. | Human; WF1 `Source Media`; helper L193, L344 | VERIFIED + DELEGATED |
| 8 | **1080 preparation and QA** | Download, hash check, probe, scale to a 1080 short edge, two-pass H.264, QA (<145 MB effective) | WF1 `Prepare 1080 Media`, `Media Passed QA?`; helper L192-263 | VERIFIED + DELEGATED |
| 9 | **Editor task / replacement request** | Source subitem created or reopened with instructions. Social status `Waiting for Editor` or `ستوري طويل`. | WF1 Stage I | VERIFIED |
| 10 | **Verified delivery** | Upload to `/Social Media/Prepared`, public `raw=1` link, helper registers the URL, status `Redy For Scheduled` | WF1 `Upload Verified 1080 Video` … `Save QA Result`; helper L478-486 | VERIFIED + DELEGATED |
| 11 | **Slot reservation** | Reconcile, then reserve (keeps an existing future time, or picks the next grid slot with style diversity) | WF1 `Reserve Style Slot`; helper L494-525, L110-127 | VERIFIED + DELEGATED |
| 12 | **Schedule committed** | Fresh recheck, then save Post Date, Post Time, Publish at and `Scheduled`, then commit (cancel if the recheck fails) | WF1 `Schedule Still Allowed`, `Save Schedule`, `Commit Slot` | VERIFIED |
| 13 | **Supervisor audit** | Every 30 min: block or reschedule invalid, off-grid, colliding, same-style or uncommitted items | WF3; helper L391-439, L567-586 | VERIFIED + DELEGATED |
| 14 | **Due selection and claim** | Every minute: Scheduled with `at ≤ now`, then validity, source check, helper claim (committed slot, verified media, Topaz, URL, 0–2 h window, lease) | WF2 `Due Queue` … `Publication Claimed?`; helper L531-564 | VERIFIED + DELEGATED |
| 15 | **Container create and polling** | Graph `/media`, then poll `status_code` up to 20 × 30 s with lease heartbeats | WF2 `Create Container` … `Keep Polling?` | VERIFIED |
| 16 | **Final gate and publish** | Dropbox revision check, fresh Monday gate (≤2 h late), publish intent checkpoint, `media_publish` | WF2 `Still Scheduled & Due`, `Record Publish Intent`, `Publish To Instagram` | VERIFIED |
| 17 | **Post-publish write-back** | Receipt → Monday Posted (IDs, link, Published at) **and move to the Posted group** → source project Posted → `source_synced` | WF2 `Build Posted Update`, `Sync Posted to Customer Projects` | VERIFIED |
| 18 | **Failure, unknown outcome, missed slot** | Needs Review with a reason. A `publish_requested` receipt blocks any retry until a human reconciles it. A missed window needs a manual new time (or a WF1 re-reserve after the status changes). **No automatic Instagram reconciliation and no notification (Slack/email).** | WF2 `Publication Needs Review`; WF3 block; helper L550, L616-617 | VERIFIED + DELEGATED; auto-reconcile and notifications NOT IMPLEMENTED |
| 19 | **Pause / Skip / source cancellation** | Paused or Skipped: excluded from WF1, WF2 and WF3; the reservation is freed by reconcile; the guards refuse writes. A Canceled source project becomes status Skipped (**not moved to the Skipped group**). | WF1 `Preparation Queue`, guards, `Build Sync Mutations`; WF2 `Due Queue`; helper L487-493 | VERIFIED + DELEGATED; Skipped group move NOT IMPLEMENTED |
| 20 | **Story↔Post change** | A human changes Format. WF1 detects `Processed format ≠ Format`, clears Post Date, Post Time, Publish at and Verified media ID, resets the status, invalidates the reservation (refused if a publish receipt exists), and re-prepares under the new format's rules (`media_key` includes format, L73). WF2 refuses while Processed format ≠ Format. **No group move** between For Posts and For Stories. | WF1 `Source Media`, `Invalidate Changed Content`; WF2 `Publication Context`; helper L464-469, L496-498 | VERIFIED + DELEGATED; group move NOT IMPLEMENTED |

---

## 6. Business rules

Columns:
- **Status:** IMPLEMENTED / PARTIAL / NOT IMPLEMENTED, with an evidence label.
- **Enforcement:** HARD (blocks progress), SOFT (preference) or DECLARATIVE (relies on a human statement).
- **Bypassable?:** whether a human or a race can get around the rule through the automation.

| Rule | Expected | Status | Where | Enforcement | Duplicated elsewhere? | Bypassable? |
|---|---|---|---|---|---|---|
| Story < 60 s | Stories strictly under 60 s | IMPLEMENTED (VERIFIED + DELEGATED) | WF1 `Early Story Duration`, `Story Duration Accepted?`, `Prepare 1080 Media`, `Media Passed QA?`; helper L57 (`media_failure`), L227, L269 | HARD | Yes: 4 WF1 nodes + 3 helper places. It is re-checked at claim through `verified_media` (L539-541). WF2 itself has no check. | Not through the automation: the claim needs verified media. A human can still publish manually on Instagram (outside the system). |
| Topaz | Only the Topaz-processed version is published | PARTIAL (declaration only; VERIFIED + DELEGATED) | WF1 `Source Media`, `Prepare 1080 Media`, `Schedule Still Allowed`; WF2 `Still Scheduled & Due`; helper L42, L193, L344, L542 | DECLARATIVE + HARD gate on the flag | Yes: WF1 ×3, WF2, helper ×4 | **Yes.** Setting Topazed on a non-Topaz file passes every check; nothing detects Topaz technically. The flag is tied to the asset version, so it resets when the file changes. |
| Short edge ≥ 1080 | Minimum 1080p on the short edge | IMPLEMENTED (VERIFIED + DELEGATED) | WF1 `Prepare 1080 Media`, `Media Passed QA?`; helper L53, L229, L248 (output scaled to exactly 1080, L233) | HARD | Yes: WF1 ×2, helper ×3 | No (via automation). Note: 4K sources are downscaled to 1080. |
| Size < 300,000,000 bytes | Final file under 300 MB | IMPLEMENTED, **stricter in practice** (VERIFIED + DELEGATED) | WF1 `Prepare 1080 Media` / `Media Passed QA?` (`< 300000000`); helper `MAX_BYTES` L31, L55; **real cap L248: `bytes ≥ 145_000_000` → error**, bitrate target 140 MB (L234), refused below 500 kbps (L235-236) | HARD | Yes: WF1 ×2, helper ×2. The values differ: 300 MB stated, **~145 MB effective**. The L250 message still says "under 300 MB". | No. Long Posts may be refused or heavily compressed. |
| Africa/Cairo | All slots in Cairo time | IMPLEMENTED (VERIFIED + DELEGATED) | `settings.timezone` in WF1, WF2, WF3; prelude `at()`; helper `TZ` L30 | HARD | Yes: every workflow + helper | Partly. Publish at is UTC and takes priority over Post Date/Time, so a human editing only Post Date/Time has no effect while Publish at is set. |
| Post slots Sat 21:00, Mon 21:00, Wed 22:45, Thu 21:00 | Reels only on these slots | IMPLEMENTED in the helper only (DELEGATED); NOT IMPLEMENTED in any workflow JSON | helper `REELS` L32, `slots()` L99-107, monitor `slot_ok` L425 | HARD for automatic picks; corrective in WF3 | No (single definition) | **Yes, temporarily.** A human-entered future time is kept by WF1 `preserveSchedule` without a grid check (L507-513) and is corrected by WF3 at the next :05 or :35, unless the item is within 10 min of publishing. |
| Story slots 11, 14, 18, 21, 22 daily | Stories only on these slots | IMPLEMENTED in the helper only (DELEGATED) | helper `STORIES` L33, L99-107, L425 | as above | No | as above |
| Client/style code extraction | 2–3 letters + number gives the style | IMPLEMENTED (VERIFIED + DELEGATED) | prelude `style()` in all workflows; WF1 `Item Context` (invalid → Needs Review); WF2 `Publication Context`; helper L85-89 | HARD | Yes: 3 workflows + helper | No. Codes must also be unique on the source board (WF1 `Build Sync Mutations`, `Preparation Queue`; WF2 `Unique Source Project`). |
| Adjacent-style diversification | No two consecutive slots of the same style | IMPLEMENTED as SOFT (VERIFIED + DELEGATED) | WF1 `Preparation Queue` round-robin + LLM order; helper `next_slot` L123-125; monitor L431 | SOFT (gives way to the starvation bound) | Yes: WF1 ordering, helper reserve, helper monitor | Yes: the starvation fallback (7 d / 1 d) and human time choices can bypass it. Human Story variety values are overwritten, so they cannot be used to tune it. |
| Starvation prevention | No style or item waits forever | PARTIAL (VERIFIED + DELEGATED) | helper L116-122; WF1 `Preparation Queue` (Last checked ordering, 20 cap) | SOFT | Partly (helper + WF1 queue) | — Gap: items without a final file are never re-stamped, so they monopolise their group's turn (WF1 `Waiting for Editor`). |
| Paused | Paused items are never touched or published | IMPLEMENTED (VERIFIED + DELEGATED) | WF1 `Preparation Queue` and the 7 guards (`protectedStatus`); WF2 `Due Queue` (requires Scheduled), `Still Scheduled & Due`, `Guard Publication Review`; WF3 `Repair Still Applies`; helper reconcile L491 frees the slot | HARD | Yes: every workflow | Small race: a Pause set after WF2's final re-check (one helper call before `media_publish`) is not seen. A `published` receipt is still written back as Posted. |
| Skipped | Skipped items are never touched or published; they belong in the Skipped group | PARTIAL (VERIFIED) | as for Paused; WF1 sets Skipped for a Canceled source | HARD for status; **group move NOT IMPLEMENTED** (`group_mm7y8mkr` unused) | Yes | Same race as Paused. Items stay in For Posts or For Stories. |

---

## 7. Key gaps (summary)

All items below are VERIFIED unless marked otherwise.

1. The slot grid, reservations, claim, lease and due window exist **only** in `helper.py`. The server copy has not been verified, and the helper is failing at runtime (exit 1 since about 15:00 UTC on 2026-10-09; historical observation).
2. The size limit really applied is about 145 MB, not 300 MB.
3. Topaz is a human declaration, not a technical check.
4. No Skipped group move and no group move on a format change. The only group move is WF2 → Posted.
5. Monday never shows a `Publishing` state. WF1 can change the status of an item that WF2 has claimed (INFERRED race).
6. Human time choices: WF1 keeps them (no grid check), then WF3 moves them to the grid. Human Story variety and Dropbox Link values are overwritten.
7. An unknown publish outcome (`publish_requested`) needs manual Instagram reconciliation. There is no alerting.
