# 07 — Errors, Execution History and Risks

Part of **WASET SOCIAL MEDIA AUTOMATION — CURRENT SYSTEM TECHNICAL DOCUMENTATION**.

Workflows: WF1 `qI1N5VNgpRjnZAKH` (1 Prepare & Schedule), WF2 `pUIshuf16zIYoYRz` (2 Publish When Due), WF3 `WasetSocialScheduleGuard` (3 Schedule Supervisor). Helper: `helper_reference/helper.py` (local copy; **server copy not verified**).

Labels: **VERIFIED** (workflow JSON), **DELEGATED-TO-HELPER** (local copy, line numbers), **INFERRED**, **NOT IMPLEMENTED**, **HISTORICAL OBSERVATION** (execution records only).

> **Current state, HISTORICAL OBSERVATION:** since **2026-10-09 15:00 UTC every run of all three workflows fails** at a helper.py `executeCommand` node with "Command failed with exit code 1". **Nothing has ever been published** by V2.

---

## (a) Error handling and retry mechanisms

### Workflow-level settings (VERIFIED)

| Setting | WF1 | WF2 | WF3 |
|---|---|---|---|
| `errorWorkflow` | absent | absent | absent |
| `executionTimeout` | 1800 s | 1800 s | 600 s |
| Trigger misfire policy | `skip` | `skip` | not set (n8n default) |
| Save success/error data | all | all | all |
| Kill switch | `Configuration.armed` (true) | same | same |
| Automatic re-run of failed executions | none (`retryOf` null on every execution — HISTORICAL OBSERVATION) | none | none |

### Per-node mechanisms

| Mechanism | WF1 | WF2 | WF3 |
|---|---|---|---|
| `retryOnFail` (3 tries, 2000 ms) | Reads only: `Read Client Brief`, `Find Folder Link`, `Folder Metadata`, `List Files`, `Existing File Link`, `Read Existing Editor Tasks`, `Find Verified Share`, `Recheck Before Scheduling`. **No retries on writes.** | `Find Source Project`, `Check Container`, `Recheck Immediately Before Publishing`, `Get Permalink`, `Verify Source Revision`. `retryOnFail: false` set explicitly on `Create Container` and `Publish To Instagram`. | **None on any node.** |
| `onError: continueErrorOutput` | Nearly every per-item node. Errors go to `Item Needs Review`; guard-quartet errors go to `Item Finished` (no note). | Every node from `Publication Context` on, except IFs and a few Code nodes. Errors go to `Publication Needs Review`; review-sink errors go to `Publication Item Finished`. | `Read Current Schedule`, `Repair Still Applies`, `Reserve Corrected Slot*`, `Save Schedule Repair`, `Repair Saved`, `Save Repair History`. Errors loop back to `Each Schedule Repair` with no record. |
| `onError: continueRegularOutput` | `Light AI — Diversify Styles` (deterministic fallback in `Validate AI Style Order`) | — | — |
| `neverError: true` HTTP | `Create Project Folder`, `Share New Folder`, `Share Final File`, `Ensure Prepared Dropbox Folder`, `Share Verified Video` (error interpreted in code) | `Get Permalink` | — |
| Run-fatal nodes (no onError) | `Configuration`, lock nodes, all Stage B–D reads, `Build Sync Mutations`, `Apply Source Sync`, `Sync Acknowledged`, both Reconcile nodes, `Preparation Queue`, `Write Caption` + models, Release/Retain nodes | `Configuration`, `Social *` snapshot, `Publication Receipts*`, `Due Queue`, `Publication Needs Review` | `Configuration`, lock nodes, `Social Collect`, `Inspect Schedule*`, `Commit Repaired Slot*`, `Audit Repair*`, Release nodes |
| Partial-snapshot refusal | `* Collect` throws "Missing board page; refusing a partial snapshot" | same | same |
| Helper call error contract | Parse nodes throw on non-zero `exitCode` or `result.error` | same | same |

### helper.py error behaviour (DELEGATED-TO-HELPER)

- Any exception → prints `{"error": "<≤900 chars>"}` and exits 1 (L655-657). n8n records only "Command failed with exit code 1"; stderr/stdout is **not** kept in the execution record (HISTORICAL OBSERVATION).
- Each DB endpoint is one `BEGIN IMMEDIATE` transaction; on failure it rolls back and writes `health[path]={ok:false}` (L633-636).
- SQLite busy timeout 30 s → "database is locked" → exit 1 (L130-149).
- Media jobs: failures stored as retryable, max 3 attempts, backoff `min(3600, 60·2^attempt)` s (L649).
- Lock `preparation`: TTL 2100 s, no renew endpoint (L450-456). Publication lease 180 s, renewed by `/v1/publish/heartbeat` (L37, L590-595).

---

## (b) Execution history findings — HISTORICAL OBSERVATION

Everything in this section comes from n8n execution records collected 2026-10-09 ≈16:02 UTC, covering 14:00–16:01 UTC only. Items are referred to by Monday item ID.

### Stats

| Workflow | Total | Success | Error | Canceled | Last success | Errors | Success duration | Cadence seen |
|---|---|---|---|---|---|---|---|---|
| WF1 Prepare | 12 | 3 | 7 | 2 (1 trigger, 1 manual) | 51754 (14:50–14:56) | 51767 (15:00) … 51853 (16:00), all | 380–410 s | :x0:11; no runs at 14:10, 14:20 |
| WF2 Publish | 99 | 37 | 62 | 0 | 51765 (14:59:04) | 51766 (15:00:04) … 51862 (16:01:04), every run | 2.7–12.1 s | every minute at :04; from 14:24 |
| WF3 Supervisor | 5 | 3 (2 `cli`, 1 trigger) | 2 | 0 | 51731 (14:35, exited: lock busy) | 51777 (15:05), 51819 (15:35) | 0.6 s / 6.3 s / 72.9 s | :05 / :35 |

### Current outage (since 15:00 UTC)

| Workflow | Failing node | Helper request (decoded) |
|---|---|---|
| WF1 | `Acquire Preparation Lock — Local n8n` (4th node) | `/v1/lock` `{name:"preparation"}` |
| WF2 | `Publication Receipts — Local n8n` (after a successful Monday snapshot) | `/v1/publish/snapshot` `{}` |
| WF3 | `Acquire Preparation Lock — Local n8n` | `/v1/lock` |

- The last good helper call was WF2 51765 at 14:59:07; the first failure WF2 51766 at 15:00:04. WF1 51754 had released the lock cleanly at 14:56.
- **INFERRED cause:** helper.py itself, or its data directory / `state.sqlite`, broke around 15:00. Lock contention is ruled out: a busy lock returns `{"acquired": false}` with exit 0 (seen in WF3 51731), and `/v1/publish/snapshot` does not use the lock. Candidates: a file change on the server, a missing dependency, a corrupt or unwritable state file, permissions. The workflows were updated at 15:34–15:36 UTC, after the outage began.
- Effect: no preparation, no schedule repair, no publishing. Items that become due will not publish until the helper recovers; once more than 2 h late they fall outside the due window (see C-12).

### Silent errors inside "successful" runs

1. **WF3 51710 (14:10, `cli`), status success:** `Read Current Schedule` returned GraphQL "invalid syntax" for all 25 repair candidates; nothing was applied, the run was green. Run 51712 two minutes later succeeded (INFERRED: workflow fix deployed in between).
2. **WF2, every idle success run:** `Due Queue` emits `{empty:true}`, which flows to `Publication Review Payload` → `Read Status Before Review` with `ids:[null]`. Monday rejects it; `Guard Publication Review` absorbs it. One failing Monday call per minute (seen in 51765, 51732, 51723).

### Supervisor run that moved 25 items to Needs Review

- WF3 **51712** (14:12, `cli`): `Inspect Schedule` checked 164 items, found 25 Scheduled, and returned `kind=block` for all 25.
- 24 → **Needs Review** with reason "file not approved for publishing: duration/Topaz/resolution/size checks incomplete"; item 3267620270 → long-story status (Story over 60 s).
- Each block wrote a schedule save, a helper audit entry and a Monday Update.
- Afterwards the board had **zero** Scheduled items, which is why WF2's due queue was always empty.
- Affected items: 3267478554, 3267620270, 3267612740, 3267614891, 3267609055, 3267607029, 3267607019, 3267607030, 3267613000, 3267612162, 3267614944, 3267606804, 3267620547, 3267612672, 3267612905, 3267620836, 3267619639, 3267612742, 3267621998, 3267612163, 3267607248, 3267613913, 3267607350, 3267607351, 3267607108.
- Several of these were picked up by WF1 in the next runs and given editor tasks (e.g. 3267609055, 3267612162, 3267607019, 3267607350, 3267607351 in 51724).

### 44 editor subitems in about 30 minutes

- WF1 success runs 51724, 51738, 51754 each processed 20 items. `Create Assigned Editor Task` (create_subitem on 5091110326) created **16 + 15 + 13 = 44 subitems** between ≈14:30 and 14:56.
- Per run also: ≈20 `Save Item Note`, 20 `Save Project Folder`, 13 `Save Selected Version`, 12 `Save Accepted Duration`, 27 Dropbox list-files pages.
- Item 3267606806 appeared in all three runs; its subitem was created once (task 3273505510) and found afterwards (`create:false`). Subitem creation de-duplicated correctly in that case.

### Other history

- Board snapshot at 14:59 (WF2 51765): 164 items — Needs Review 25, Posted 15, Skipped 44, Unscheduled 29, Waiting for Editor 46, long-story 5, Scheduled 0. (The 15 Posted were not posted by V2; INFERRED from "no publishes observed".)
- No Instagram Graph node ran in any inspected execution. No publish, container or IG error has ever been observed.
- WF1 51699 (14:00) and manual 51713 were canceled; both have empty runData, so their side effects and lock release are unknown. The lock was free by 14:10:30.
- No WF1 trigger runs at 14:10/14:20 and WF2 trigger runs start at 14:24 (INFERRED: redeploy/reactivation window).
- No overlapping WF1 runs (6.3–6.8 min inside a 10-min interval). WF3 51731 at 14:35 overlapped WF1 51724, got `acquired:false` and skipped its whole slot.

---

## (c) VERIFIED ISSUES

Each issue follows directly from the workflow JSON or the local helper copy. Helper-sourced issues assume the server copy matches.

| # | Issue | Evidence | Label |
|---|---|---|---|
| C-1 | **No error workflow and no alerting.** The current outage (all runs failing) produced no notification to anyone. | `settings.errorWorkflow` absent in all three; no Slack/email nodes | VERIFIED |
| C-2 | **Lock leak on abort.** Unlock is reached only on normal loop completion. Any run-fatal error or the execution timeout leaves `preparation` held until TTL (35 min), blocking WF1 and WF3 (≈3 WF1 runs, 1 WF3 slot). | WF1 `Release Preparation Lock*` only after `Each Content Item` done; WF3 same after `Each Schedule Repair`; helper L455 TTL 2100 | VERIFIED + DELEGATED-TO-HELPER |
| C-3 | **Lock-busy exits are silent.** False branches of `Preparation Lock Acquired?` (WF1) and `Supervisor Lock Acquired?` (WF3) are unconnected. When WF1 holds the lock at :05/:35, WF3 skips the whole 30-min slot without trace. | WF1, WF3 IF nodes | VERIFIED; seen in WF3 51731 |
| C-4 | **WF3 partial write on commit/audit failure.** `Save Schedule Repair` writes Monday before `/v1/schedule/commit`; `Commit Repaired Slot*` and `Audit Repair*` have no onError, so a failure aborts the run with Monday changed, the helper slot uncommitted and the lock held. | WF3 | VERIFIED |
| C-5 | **Commit can silently do nothing.** `/v1/schedule/commit` matches `at` by exact string and returns `{ok:true}` even if 0 rows changed. The item is then Scheduled on Monday, but claim later refuses it. | helper L526-528, claim L537 | DELEGATED-TO-HELPER |
| C-6 | **Reconcile deletes committed reservations** for any item missing from the supplied events, not Scheduled/Posted, or with a different `at`. WF1 builds `events` from a full snapshot (partial pages refused), so a truncated read is guarded, but any status flip away from Scheduled (e.g. a transient Needs Review) drops the committed slot. | helper L487-493; WF1 `Board Schedule`, `Reconcile Reserved Slots — Input` | DELEGATED-TO-HELPER + VERIFIED |
| C-7 | **Effective size cap is 145 MB, not 300 MB.** Output rejected at `bytes >= 145_000_000`; bitrate targets 140 MB; message still says "under 300 MB". The workflow checks `< 300000000` (`Prepare 1080 Media`, `Media Passed QA?`). Long Posts get lower bitrate; below 500 kbps they are refused. | helper L234-236, L248-250; MAX_BYTES L31 | DELEGATED-TO-HELPER |
| C-8 | **All outputs downscaled to exactly 1080 short edge**, including 4K Topaz sources. | helper L233 | DELEGATED-TO-HELPER |
| C-9 | **Transient failures unschedule Scheduled items.** Scheduled items are reprocessed every WF1 run (`migration`); any Dropbox/Monday error in Stages F–H routes to `Item Needs Review`, which sets Needs Review and clears Publish at. Then reconcile (C-6) drops the slot. | WF1 `Preparation Queue`, `Item Needs Review` | VERIFIED |
| C-10 | **Editor-update spam for items without a final video.** `Reopen Editor Plan` refresh includes `x.format !== text_mm7z139h`, but Processed format is only written by `Source Media`, which never runs when no file is found. `Waiting for Editor` does not stamp Last checked, so such items sort first and are picked every run: up to 6 subitem status resets + updates per hour per item. | WF1 `Reopen Editor Plan`, `Waiting for Editor`, `Preparation Queue` | VERIFIED |
| C-11 | **Stale-snapshot overwrites.** `Source Media` writes Topazed from the run-start snapshot; `Needs Caption?` uses snapshot caption; the guards check only status, format and Post Link. A human Topazed toggle or caption typed during a run (up to ~7 min, 20 items) can be overwritten. `Save Selected Version` also overwrites Dropbox Link and Story variety on every run, so a manual Story variety never takes effect. | WF1 `Source Media`, guard quartets, `Preparation Queue` | VERIFIED |
| C-12 | **Max lateness checked after container creation.** WF2 has no lateness bound at selection; `Still Scheduled & Due` enforces ≤ 7200 s only after `Create Container` and up to 20 polls. The helper claim also enforces 0–7200 s (`outside_due_window`), which catches most cases earlier. | WF2 `Due Queue`, `Still Scheduled & Due`; helper L550 | VERIFIED + DELEGATED-TO-HELPER |
| C-13 | **No `Publishing` status on Monday.** Status stays Scheduled during a publish; the lease lives only in SQLite. Humans, WF1 and WF3 cannot see an in-flight publish. | WF2 (no Publishing write); `protectedStatus` unused in WF2 | VERIFIED |
| C-14 | **Human edits during a publish flip the item to Needs Review.** `Guard Publication Review` protects only Posted/Paused/Skipped, Post Link and format change; a legitimate reschedule or WF1/WF3 status change is overwritten. | WF2 `Guard Publication Review` | VERIFIED |
| C-15 | **`publish_requested` is terminal without manual action.** If `media_publish` fails or times out, or the `published` checkpoint fails after a successful publish, the receipt stays `publish_requested`; claim returns the stage and nothing reconciles with Instagram. The media ID may exist only in execution data. | WF2 `Published Media ID`; helper L552-553, L616-617 | VERIFIED + DELEGATED-TO-HELPER |
| C-16 | **Unbounded post-publish replay.** `Due Queue` rule A replays `published && !sourceSynced` every minute with no cap; if `Sync Posted to Customer Projects` keeps failing, Monday is rewritten each minute and the guard suppresses the review note (status Posted). | WF2 `Due Queue`, `Guard Publication Review` | VERIFIED |
| C-17 | **Idle Monday error call every minute** (`ids:[null]`). ≈1,440 failing calls/day. | WF2 `Publication Review Payload` | VERIFIED; seen in history |
| C-18 | **`Manual Audit` is not a dry run;** it performs the same writes. | WF3 `Manual Audit` | VERIFIED |
| C-19 | **WF3 block label unvalidated.** `status:{label:c.status}` comes straight from the helper. | WF3 `Build Block Update` | VERIFIED |
| C-20 | **Asset identities never released.** Once claimed, `account\|format\|contentHash` blocks the same file for every other item forever. | helper L546-549, L563 | DELEGATED-TO-HELPER |
| C-21 | **`prepare()` may return a cached success whose file is gone** (after maintenance or manual delete) → `/v1/media/delivered` fails "Verified file not found". | helper L361-364 | DELEGATED-TO-HELPER |
| C-22 | **`/v1/health` is never called;** pending `publish_requested` receipts are never surfaced. | helper L599-601; no caller | VERIFIED |
| C-23 | **Silent item exits.** Guard skips and guard-quartet errors go to `Item Finished`; `Schedule Allowed?` false cancels the slot with no note; `Save Item Note` errors are swallowed; WF3 loop errors skip items silently. | WF1, WF3 | VERIFIED |
| C-24 | **Folder/item mismatch masked** as an editor task: `Candidates` returns a mismatch note with `pick:null`, `Final File Found?` false goes to the editor branch, and the note is discarded. | WF1 `Candidates`, `Waiting for Editor` | VERIFIED |
| C-25 | **No WF2-level media checks** (Story < 60 s, ≥1080, size). WF2 relies on Verified media ID + claim's `verified_media`. | WF2; helper claim L531-564 | VERIFIED + DELEGATED-TO-HELPER |
| C-26 | **Stale UI/dead code:** sticky "Draft — DO NOT RUN" on active workflows; "Quality Rules" sticky contains no rule; unused `MIN_SIZE`, `styleWeekdays`, `protectedStatus`/`checkStamp` in WF2/WF3. | WF1, WF2, WF3 | VERIFIED |
| C-27 | **`Save Project Folder` bypasses the freshness guard.** | WF1 | VERIFIED |

---

## (d) POSSIBLE RISKS

Not proven at runtime. Each states why it may or may not apply.

| Topic | Risk | What limits it today | Label |
|---|---|---|---|
| Duplicate triggers | WF3 has no `misfirePolicy`; after downtime n8n's default may fire missed slots. WF2 every minute can start while the previous run is still polling (runs up to ~10+ min per item). | WF1/WF2 use `skip`; WF2 overlap is handled by the claim lease, not by n8n. | INFERRED |
| Duplicate Monday writes | WF2 post-publish replay (C-16); WF1 rewrites `Save Selected Version` / `Save Project Folder` for every queued item each run; WF3 Update posts once per applied repair. | Writes are mostly idempotent values; Monday Updates (comments) are not. | VERIFIED pattern / INFERRED volume |
| Conflicting schedulers | WF1 (`/v1/schedule/reserve`, with `styleWeekdays`) and WF3 (`/v1/monitor/reserve`, without `styleWeekdays`, from now+10 min) both choose slots and write `date4`/`hour`/Publish at. A human can also edit these columns. | Shared `preparation` lock serialises WF1/WF3; `UNIQUE(format,at)` in SQLite; `styleWeekdays` is `{}` today. Human edits are not locked. | DELEGATED-TO-HELPER (L580) + INFERRED |
| Multiple publication initiators | Only WF2 publishes (VERIFIED). Unknown: whether the old publisher ("Old publisher … stopped", `Configuration` comment) or any of the 15 inaccessible workflows can still post to the same IG account. | Asset dedupe per account in `assets`. | INFERRED |
| Repeated processing | WF1 reprocesses all Scheduled items every 10 min (Dropbox listing + preflight + invalidate). Items without a final file are picked every run (C-10). | 20-item cap; caching in `jobs`/`media`. | VERIFIED pattern |
| Idempotency gaps | No idempotency key on Graph calls; Monday Updates and subitem updates are append-only; `commit` returns ok on 0 rows (C-5); `create_subitem` de-dupe depends on name prefix `Social <itemId> — `, which breaks if a human renames the subitem. | Receipt stages; `retryOnFail:false` on publish calls. | INFERRED |
| Race conditions | (1) WF3 can reschedule/block an item WF2 has claimed (status still Scheduled, Post Link empty); `/v1/monitor/plan` skips items with a receipt and items due within 10 min, which narrows but does not close it. (2) WF1 runs concurrently with WF2 (no shared lock); WF1 can reset Topazed or clear Publish at mid-publish. (3) Window between fresh read and write in guards and in WF2 final gate → `media_publish`. | Claim re-checks committed reservation inside a transaction; WF2 final gate re-reads Monday. | DELEGATED-TO-HELPER (L391-439) + INFERRED |
| Retry duplication | n8n "retry execution" from the UI re-runs with a new execution id = new lock owner and new lease owner. A manual retry of a WF2 run that died after `media_publish` cannot double-post (the receipt is already `publish_requested`). A retry of WF1 repeats every non-idempotent write (Monday Updates, subitem status resets) for the items it reaches; only subitem creation and the upload path are de-duplicated. | `publish_requested` never repeated (helper L616-617). | INFERRED |
| Supervisor conflicts | WF3 blocked all 25 Scheduled items at once (HISTORICAL). WF1 then re-prepares them, possibly re-scheduling, then WF3 may block again — a WF1/WF3 loop if their criteria differ. WF3 skips slots whenever WF1 holds the lock at :05/:35. | Shared lock. | HISTORICAL OBSERVATION + INFERRED |
| Stale media validation | Topaz is a human toggle, not detected (helper `topazVerification='editor_confirmation_bound_to_asset'`, L247). WF2 checks Dropbox revision but not the prepared file content; preflight downloads up to 5 GB just for duration. | Asset key `id@rev` + content hash binding. | DELEGATED-TO-HELPER |
| Stale payloads | WF1 works from a run-start snapshot for up to ~7 min (C-11). WF2 resumes a stored `containerId`; if caption/format changed but claim still matches, an older container could be published. | Claim raises 'Claim content changed' if mediaId/sourceProjectId/format/at differ; Graph containers expire (~24 h). Caption change is **not** in the claim comparison. | DELEGATED-TO-HELPER + INFERRED |
| Lock expiry | Lock TTL 2100 s vs WF1 timeout 1800 s and WF3 600 s: a live run should finish or be killed before expiry, so two holders are unlikely — **if** n8n enforces the timeout during a long `executeCommand`. After a crash the lock blocks for up to 35 min. No renew endpoint. | executionTimeout settings. | VERIFIED settings + INFERRED |
| Reconcile deleting slots | See C-6. Also: the base64 `events` list (every Posted item ever) is passed on the command line; past ≈700–800 events Linux `MAX_ARG_STRLEN` (128 KiB) would make reconcile and reserve fail every run (no onError). | Board has 15 Posted today. | INFERRED |
| 145 MB real cap | See C-7. Posts longer than ~90 s are compressed hard; very long Posts are refused. Editor instructions still say "under 300 MB", so editors get no warning. | — | DELEGATED-TO-HELPER |
| Helper divergence | The server helper may differ from the local copy; every DELEGATED claim depends on it. Directly relevant to the outage. | — | INFERRED |
| Two Dropbox credentials | WF1 uses `dropboxOAuth2Api` for create/upload and a generic `oAuth2Api` ("Unnamed credential 2") for sharing/listing; WF2 uses the generic one. If they point to different accounts or one expires, paths diverge. | — | VERIFIED (credential types) / INFERRED (impact) |
| Public links | Prepared files are shared publicly (`raw=1`) for Instagram to fetch. | — | VERIFIED |
| Caption quality | LLM output is not validated for format, hashtag count or client-name leakage; brief = oldest ≥60-char update on the source item. `Write Caption` has no onError, so an OpenRouter failure aborts the whole WF1 run. | — | VERIFIED |
| Item re-creation | If a social item is deleted/archived, WF1 recreates it from the source on the next run. | — | INFERRED |

---

## (e) Missing or unverified functionality

| # | Item | Status |
|---|---|---|
| 1 | **15 workflows not accessible via MCP** (inventory §C), incl. `7D6DDdNoLDw3syFM`, `oR268O5OPIb0y0T0`, `Nb8fvRaLfumdkYT0`, `QUbrWrpK2WD9TS3P`. Not reviewed; they may write board 5105608159 or the source columns. | NOT VERIFIED |
| 2 | **helper.py on the server** (`/home/node/.n8n-files/waset-social/helper.py`) and its data dir / `state.sqlite`. Not compared with the local copy; its current error message is unknown (stderr not stored). | NOT VERIFIED |
| 3 | **How items enter board 5105608159.** WF1 imports from 5091110326; whether people, Monday automations or inaccessible workflows also create items is unknown. | NOT VERIFIED |
| 4 | **Facebook Page publishing.** No FB endpoints in any export. | NOT IMPLEMENTED |
| 5 | **Slack / email / Telegram notifications** for the social system. | NOT IMPLEMENTED |
| 6 | **Error workflow / alerting / health check** (`/v1/health` uncalled). | NOT IMPLEMENTED |
| 7 | **Paused / Skipped handling.** Protected in guards and excluded from queues (WF1 `protectedStatus`, WF2 `Due Queue`, WF3 `Repair Still Applies`); helper has no literal Paused/Skipped and frees their reservations via reconcile. No "resume from Paused" logic. WF2 guard does not protect other human statuses. | PARTIAL |
| 8 | **Group moves.** Only WF2 → `group_title` (Posted). No move to Skipped (`group_mm7y8mkr`), none on Post↔Story change. | NOT IMPLEMENTED (except Posted) |
| 9 | **Source → social updates after import** (format/link changes other than Canceled). | NOT IMPLEMENTED |
| 10 | **Automatic reconciliation with Instagram** for `publish_requested` receipts. | NOT IMPLEMENTED |
| 11 | **Real Topaz detection.** Human toggle only. | NOT IMPLEMENTED |
| 12 | **Image / carousel / collaborator posts.** Only REELS (Post) and STORIES video; IG Colab items go to manual review. | NOT IMPLEMENTED |
| 13 | **Style weekday map** (`styleWeekdays`). Code exists in helper, config is `{}`. | OFF |
| 14 | **Publishing outcome in practice.** No publish has ever run; Graph-side behaviour (container timing, errors, Story permalinks) is unobserved. | NOT OBSERVED |
| 15 | **Side effects of canceled WF1 runs 51699 and 51713** (empty runData). | UNKNOWN |
| 16 | **Wait unit in WF2 `Wait For Processing`** (amount 30, unit omitted; seconds assumed for v1.1). | INFERRED |
