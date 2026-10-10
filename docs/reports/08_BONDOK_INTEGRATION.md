# 08 — Bondok Integration Points

Part of **WASET SOCIAL MEDIA AUTOMATION — CURRENT SYSTEM TECHNICAL DOCUMENTATION**.

This document records where a future controlled command handler ("Bondok") could connect to the current system, and the rules it would have to respect. It documents what exists; it does not design the integration.

Labels: **VERIFIED** (workflow JSON), **DELEGATED-TO-HELPER** (local copy `helper_reference/helper.py`, line numbers; server copy not verified), **INFERRED**, **NOT IMPLEMENTED**, **HISTORICAL OBSERVATION**, non-social workflows were read in full via MCP during the inventory pass (`02_WORKFLOW_INVENTORY.md`) but are not exported.

---

## 1. Current state

| Point | State | Label |
|---|---|---|
| Bondok integration in V2 | None. No node, credential, webhook or comment refers to Bondok in WF1 `qI1N5VNgpRjnZAKH`, WF2 `pUIshuf16zIYoYRz` or WF3 `WasetSocialScheduleGuard`. | VERIFIED |
| Slack in V2 | None (zero Slack nodes in the three exports). | VERIFIED |
| Webhooks in V2 | None. Triggers are schedule-only plus WF3 `Manual Audit`. | VERIFIED |
| Execute Workflow Trigger in V2 | None; V2 workflows cannot be called as sub-workflows today (no trigger node), despite `callerPolicy: workflowsFromSameOwner`. | VERIFIED |
| Human feedback channel | Monday only: Status, `المطلوب منك` (`long_text_mm7zbtbn`), System update (`long_text_mm7ysrbz`), item Updates (WF3 `Save Repair History`), editor subitem updates (WF1 `Save Editor Instructions`, `Reopen Existing Editor Task`). | VERIFIED |
| System health | All V2 runs failing at helper.py since 2026-10-09 15:00 UTC; no publish has ever happened. | HISTORICAL OBSERVATION |

---

## 2. Existing webhooks on the instance

All of these belong to non-social workflows. **None touches board 5105608159.**

| Path | Workflow | Active | What it does | Boards | Auth |
|---|---|---|---|---|---|
| `POST /webhook/captions-prio` | `Mj2tFDXXm2SotxhJ` Captions Prio | yes | Answers Monday challenge; copies a status label to `color_mm4xe8ge` on the mirror item | 5098835785 | none (VERIFIED: n8n reports "No credentials required for this webhook") |
| `POST /webhook/captions-video` | `aD64l0E36EoowXdo` Captions Video Link Update From Editor | yes | Copies a link into `link_mm4f1h5k` | 5091110326 | none (VERIFIED: n8n reports "No credentials required for this webhook") |
| `POST /webhook/editor-changed-stat` | `DGluKvTQrYDLXZTg` Editor changes stat webhook | yes | Mirrors editor status into `project_status` | 5091110326 | none (VERIFIED: n8n reports "No credentials required for this webhook") |
| `POST /webhook/revisions-update` | `vvWwTXn1rE6JE3dB` When Item Is sent to revesions | yes | Sets Client ETA `date` = now + 24 h | 5091110326 | none (VERIFIED: n8n reports "No credentials required for this webhook") |
| `POST /webhook/client-pref` | `b6yHFKUIjusLQ5DJ` Client prefrences | **no** | Posts a fixed Monday update | item from payload | none (VERIFIED: n8n reports "No credentials required for this webhook") |
| `GET /webhook/camera-motion` | `lly6y44MgzKcZWzh` My workflow 2 | **no** | Webhook node only | — | none (VERIFIED: n8n reports "No credentials required for this webhook") |

Webhooks inside the 15 workflows not accessible via MCP are unknown.

---

## 3. Callable sub-workflows and the chat-command precedent

| ID | Name | Pattern | Relevance |
|---|---|---|---|
| `pFb16xMOaY6aBPBv` | My Sub-Workflow 1 | Execute Workflow Trigger with inputs `Action`, `Code`, `Prio`. Finds an item on 5091110326 by `text_mm066x8y` and sets `priority_1`. **Ignores `Action`.** | The only readable callable sub-workflow. Shows the "typed inputs → single Monday write" shape. (VERIFIED: definition read via MCP during inventory) |
| `jsT9YGPvC5BkNjpo` | My workflow | Telegram trigger → AI agent (OpenRouter `google/gemini-2.0-flash-lite-001`) parses a "change priority" request → calls `pFb16xMOaY6aBPBv`. | Existing **chat → LLM → sub-workflow** command precedent on this instance. The Telegram Trigger has no chat/user filter and there is no confirmation step. (VERIFIED: definition read via MCP during inventory) |
| `Ntd38QZ4WbVPw1Ue`, `fadmYVBYN8zrlQ0Q` | Dropbox - Copy external shared folder; Office Download | 0 triggers listed — possibly sub-workflows | Not accessible; contents unknown. |

---

## 4. helper.py as a de-facto internal API

**How it is reached:** only via n8n `executeCommand` nodes running `python3 /home/node/.n8n-files/waset-social/helper.py <base64(JSON {path, body})>` on the n8n host (VERIFIED in all three exports). It is **not** a network service: no port, no HTTP listener, **no authentication or authorization**. Anyone who can run a command in the n8n container, or edit a workflow with an executeCommand node, can call every endpoint. Output is one JSON line on stdout; errors exit 1 with `{"error": …}` (helper L643-657). Credentials for Monday, Dropbox and Instagram stay in n8n; the helper holds none (helper L3).

All rows DELEGATED-TO-HELPER (local copy).

| Endpoint (lines) | Current caller | Input | Output / effect | Kind |
|---|---|---|---|---|
| `/v1/lock` (L450-456) | WF1, WF3 "Acquire Preparation Lock" | `name`, `owner` | `{acquired}`; TTL 2100 s; re-entrant for same owner | write |
| `/v1/unlock` (L457-459) | WF1, WF3 "Release Preparation Lock" | `name`, `owner` | `{released:true}` always; deletes only if owner matches | write |
| `/v1/item/invalidate` (L460-477) | WF1 `Invalidate Changed Content` | `itemId, format, assetKey, mediaId?, scheduled?, at?` | `{ok, changed, reuseScheduled}`; deletes reservation on change; refuses if a receipt exists | write |
| `/v1/media/preflight` (L303-340) | WF1 `Early Story Duration` | `format, sourceUrl, itemId, fileId, revision, contentHash, assetKey` | `{ready, pending, duration, replaceRequired, reason}`; starts a detached job | write (async) |
| `/v1/media/prepare` (L343-388) | WF1 `Prepare 1080 Media` | same + `topazed` | `{ready, mediaId, filePath, width, height, bytes, duration, …}` or `{pending}` / `{needsEditor}`; starts ffmpeg job | write (async, heavy) |
| `/v1/media/delivered` (L478-486) | WF1 `Register Verified Delivery` | `mediaId, itemId, format, url` | `{ready, …metadata}`; stores delivery URL | write |
| `/v1/schedule/reconcile` (L487-493) | WF1 `Reconcile Reserved Slots` | `items` (board events) | `{ok}`; deletes non-matching reservations, committed included | write (destructive) |
| `/v1/schedule/reserve` (L494-525) | WF1 `Reserve Style Slot` | `itemId, format, code, rotation, preserveSchedule, requestedAt, styleWeekdays, occupied` | `{reserved, at, date, hour, minute}` or `{reserved:false, reason}` | write |
| `/v1/schedule/commit` (L526-528) | WF1 `Commit Slot`, WF3 `Commit Repaired Slot` | `itemId, at` | `{ok:true}` always (even 0 rows) | write |
| `/v1/schedule/cancel` (L596-598) | WF1 `Cancel Reserved Slot` | `itemId` | deletes uncommitted reservation | write |
| `/v1/maintenance` (L602-609) | WF1 `Retain Published Files` | — | `{removedPublishedFiles}`; deletes local mp4 > 30 days after `source_synced` | write (destructive) |
| `/v1/publish/snapshot` (L529-530) | WF2 `Publication Receipts` | — | `{receipts:[…]}` | **read-only** |
| `/v1/publish/claim` (L531-564) | WF2 `Claim Publication` | `itemId, format, at, mediaId, topazed, assetKey, expectedUrl, sourceProjectId, owner, account` | `{claimed, url, receipt, stage}`; enforces committed reservation, verified media, due window 0–7200 s, asset dedupe, 180 s lease | write |
| `/v1/publish/heartbeat` (L590-595) | WF2 `Renew Publication Lease` | `itemId, owner` | renews lease or raises | write |
| `/v1/publish/checkpoint` (L610-627) | WF2 receipt nodes | `itemId, owner, stage, data` | advances `claimed → container_created → publish_requested → published → source_synced` | write |
| `/v1/monitor/plan` (L391-439) | WF3 `Inspect Schedule` | `items` (snapshot) | `{plans:[{item, kind: block\|reschedule, reason, status?}]}` | **read-only** |
| `/v1/monitor/reserve` (L567-586) | WF3 `Reserve Corrected Slot` | `itemId, mediaId, assetKey, format, code, rotation, at, reason, occupied` | `{at, date, hour, minute}`; writes `audit_log` | write |
| `/v1/monitor/log` (L587-589) | WF3 `Audit Repair` | any body | inserts `audit_log monitor_applied` | write |
| `/v1/health` (L599-601) | **none** | — | `{qaPolicy, storage, pendingPublications}`; writes `health.last_check` | read (+ stamp) |

Read-only candidates for status reporting: `/v1/publish/snapshot`, `/v1/monitor/plan`, `/v1/health` (the last writes one health row). There is no endpoint to read `reservations`, `audit_log`, `jobs` or `locks` directly.

---

## 5. LLM usage today (WF1 only)

| Node | Model (OpenRouter) | Settings | Purpose | Guarding | Label |
|---|---|---|---|---|---|
| `Write Caption` + `OpenRouter Model` | `openai/gpt-5.6-sol` | maxTokens 600, temperature 0.8 | Writes the Post caption from the video title and the oldest ≥60-char update on the source item (client brief). | `Build Caption Update` strips fences and de-duplicates hashtags only; no format or content validation. **No onError**: a failure aborts the WF1 run. Written through the `Save Caption` guard. | VERIFIED |
| `Light AI — Diversify Styles` + `Small Style Model` | `openai/gpt-4.1-mini` | maxTokens 300, temperature 0.3 | Orders style prefixes for the 20-item queue. Prompt says input values are data, never instructions. | `Validate AI Style Order` keeps only allowed prefixes and falls back to deterministic order; `continueRegularOutput`. | VERIFIED |

WF2 and WF3 use no LLM. Credential type `openRouterApi`. Elsewhere on the instance, `jsT9YGPvC5BkNjpo` uses `google/gemini-2.0-flash-lite-001` (VERIFIED).

---

## 6. Authentication and authorization present today

| Surface | Auth | Label |
|---|---|---|
| Instance webhooks (§2) | None configured | VERIFIED (each webhook trigger: "No credentials required") |
| V2 workflows | No external entry point at all; started only by schedule or by an n8n user (`Manual Audit`) | VERIFIED |
| helper.py | None; trust boundary is "can execute commands on the n8n host" | DELEGATED-TO-HELPER + VERIFIED (invocation) |
| Telegram command workflow `jsT9YGPvC5BkNjpo` | Telegram bot credential only; Telegram Trigger has no chat/user filter (`additionalFields: {}`) | VERIFIED |
| Outbound APIs | n8n credentials: `mondayComApi`, `dropboxOAuth2Api`, generic `oAuth2Api` (Dropbox), `httpTemplatedCustomAuth` (Instagram Graph), `openRouterApi` | VERIFIED (types only; no secrets recorded) |
| Command-level authorization (who may pause, reschedule, publish) | Does not exist; Monday board permissions are the only control | INFERRED |

---

## 7. Existing components suitable for reuse

Candidates only. Reuse would need the constraints in §8.

| Component | Where | What it offers | Caveat |
|---|---|---|---|
| Paginated full-board snapshot (`Social Start … Social Snapshot`, refuses partial pages) | WF1, WF2, WF3 | Reliable read of all 27 columns of 5105608159 | Whole board every call; 500/page |
| Shared Code prelude (`txt`, `val`, `style`, `rotation`, `at`, `gqlUpdate`, `protectedStatus`) | 38 WF1 Code nodes, copied in WF2/WF3 | Consistent parsing of Code, rotation and schedule time (Publish at UTC, else date4+hour Cairo) | Copy-pasted, not a shared module |
| Guard quartet (`Guard Input → Fresh Item → Guard → Allowed?`) | WF1 (7 writes) | Fresh-read check before a Monday write: protected status, format, Post Link | Does not compare other columns |
| `Repair Still Applies` staleness guard | WF3 | Re-read and apply only if status/format/media/asset/`at`/Post Link unchanged | Scheduled-only |
| `Guard Publication Review` + "never overwrite المطلوب منك" | WF2 | Safe Needs Review write | Protects only 3 statuses |
| `Configuration.armed` kill switch | all three | Stop a workflow without deactivating it | Constant in code; needs a workflow edit |
| `/v1/lock` with execution-id owner | helper | Mutual exclusion with WF1/WF3 | No renew; TTL 35 min |
| `/v1/publish/snapshot`, `/v1/monitor/plan`, `/v1/health` | helper | Read-only status of receipts, schedule problems, pending publishes | Reached only via executeCommand |
| `/v1/monitor/reserve` + `/v1/schedule/commit` | helper (used by WF3) | Choosing and committing a new valid slot | Must hold the `preparation` lock; commit can silently no-op |
| Helper-call pattern (`X — Input` → `X — Local n8n` → parse) | all three | Shell-safe (base64) invocation and uniform error parsing | Exit 1 loses the message in execution data |
| Sub-workflow with typed inputs | `pFb16xMOaY6aBPBv` | Callable unit pattern | Ignores `Action`; targets 5091110326, not social |
| Chat → LLM → sub-workflow | `jsT9YGPvC5BkNjpo` | Precedent for natural-language commands | No documented allow-list or confirmation |
| Monday Update posting (`Repair Note` → `Save Repair History`) | WF3 | Human-readable audit on the item | Append-only |

---

## 8. Constraints a future controlled command handler must respect

These follow from current behaviour. They are not a design.

1. **Preparation lock.** Any action that changes reservations or schedule columns (reschedule, unblock, re-prepare) runs alongside WF1/WF3 only if it holds `preparation` via `/v1/lock` and releases it with the same owner (helper L450-459). The lock is not renewable and expires after 2100 s; a crash leaves it held, blocking WF1 and WF3.
2. **Publication lease.** WF2 does not use the `preparation` lock. An item with a fresh receipt lease (<180 s) or stage `publish_requested`/`published`/`source_synced` is in WF2's hands (helper L531-564, L37). The Monday status stays `Scheduled` during that time, so Monday alone cannot show it (C-13 in `07_ERRORS_AND_RISKS.md`).
3. **Reservation ↔ Monday consistency.** WF2 publishes only if a **committed** reservation in SQLite matches the item's format and `at` exactly. Changing `date4`/`hour`/Publish at on Monday without reserve + commit makes the claim fail; WF1's reconcile deletes reservations that do not match Monday (L487-493).
4. **Monday guards.** Posted, Skipped, Paused and Publishing are protected (`protectedStatus`); Post Link set means "already posted"; Format is owned by the social board after import. Writes should re-read the item first, like the guard quartet or `Repair Still Applies`.
5. **Media binding.** Scheduling requires Topazed = `Topazed` for the current `Source asset version` (`id@rev`), a Verified media ID and Publish video URL (WF1 `Schedule Still Allowed`, WF2 `Still Scheduled & Due`). Changing the video invalidates everything downstream (`/v1/item/invalidate`).
6. **Due window.** Claim accepts 0–7200 s after `at`; WF3 blocks Scheduled items more than 2 h late (helper L550, L391-439).
7. **`publish_requested` is terminal.** Nothing may re-publish an item in that stage without a manual check on Instagram (helper L616-617).
8. **Helper availability.** Every write path depends on helper.py, which is failing now. A handler built on it inherits the outage.
9. **No auth exists to build on.** Webhooks are unauthenticated and helper.py has no auth; any new entry point needs its own.
10. **Kill switches are code constants** (`Configuration.armed`); there is no runtime pause flag.

---

## Questions requiring owner clarification

1. What changed on the n8n host around 2026-10-09 15:00 UTC, and what error does `helper.py` print when run by hand? Is the server copy identical to `helper_reference/helper.py` (sha1 `57e158cb…`)?
2. Where is `WASET_SOCIAL_DATA_DIR` on the server, is `state.sqlite` backed up, and who may read or edit it?
3. How do items get onto board 5105608159 besides WF1's import: manual entry, a Monday automation, or one of the 15 inaccessible workflows (e.g. `7D6DDdNoLDw3syFM`)? Can *Available in MCP* be enabled on those 15?
4. Was the WF3 run 51712 that moved all 25 Scheduled items to Needs Review intended, and is "duration/Topaz/resolution/size checks incomplete" the expected blocking policy for items scheduled before V2?
5. Is the old publisher ("Old publisher and waiting executions stopped") fully disabled, and does anything else post to IG account <IG_ACCOUNT>?
6. Should Facebook Page publishing exist? It is not implemented.
7. Who should be alerted when runs fail (none are today), and through which channel (Slack, Telegram, email)? Should Bondok be that channel?
8. What may Bondok do: read-only status, pause/skip/unpause, reschedule, retry preparation, approve Topaz, or trigger a publish? Which actions need a human confirmation?
9. Which people are authorized to issue those commands, and how should they be identified (Slack user, Telegram ID, Monday user)?
10. Should a Paused item resume automatically when un-paused, and should Skipped items be moved to the Skipped group (`group_mm7y8mkr`)? Should Post↔Story changes move groups?
11. Is the effective 145 MB output cap (vs the stated 300 MB) and downscaling 4K to 1080 acceptable?
12. Is Topaz confirmation meant to remain a manual toggle per asset version, and who is responsible for setting it?
13. Is the editor-subitem volume (44 in ~30 min) and repeated subitem updates acceptable, or should editor notifications be rate-limited or batched?
14. How should `publish_requested` receipts that never completed be reconciled with Instagram, and by whom?
15. Should the existing unauthenticated webhooks (`captions-prio`, `captions-video`, `editor-changed-stat`, `revisions-update`, `client-pref`, `camera-motion`) be secured, and is the Telegram command workflow `jsT9YGPvC5BkNjpo` restricted to known senders?
