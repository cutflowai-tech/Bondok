# Round 5 audit — pipeline at `91ee8c5` (2026-10-10)

Independent re-audit of `bondok/deep-audit` at `91ee8c5`, after the 83-entry register (BV-01..BV-83).
Seven parallel read-only reviews (publishing, scheduling, board sync, media/WF1, Bondok, monitoring/infra,
n8n layer) plus a read of the production snapshot taken 2026-10-10 09:42 UTC (local copy, read-only).
Every entry below is **new** (not in BV-01..BV-83) or shows that a registered fix is incomplete. "REPRO" = reproduced
against the real code with a scratch script (scripts kept locally, not in git); "REASONED" = from code reading.
Nothing was changed in production or in the code. **This file is not committed.**

"Base" = `45b6418` (equivalent to the deployed release). Where the board-sync review checked the base, the
column says whether the defect is already live.

## 1. Design logic — why the pipeline keeps producing these bugs

| # | Logic flaw | Evidence | Simpler rule |
|---|---|---|---|
| L1 | **One Monday account for people and the system** → no edit attribution → v2 guesses "human vs ours" from equal values and time windows (`_prev`, run-start floor, 120 s, 20 min, 15 min, `_ours`, pending/confirmed projections, write guards) | Root of BV-03/16/19/20/21/28/48/65–67/70–78 and of B1, B5, B9, B13 below. Production 2026-10-10 00:12–00:21 UTC: a writer outside v2 changed Post Date/Time, Status and System text on ~52 items; v2 turned them into 23 owner-pinned reservations, 19 "unscheduled" holds and a 2,166-command loop | Give the integration its own Monday user/token (activity log shows who edited), or split input columns from display columns: Status is display only, a separate "Owner action" column is read and emptied |
| L2 | **Trust is inverted.** The owner's Slack words are second-guessed by a keyword regex and turned into `اعتمد B-XXXXXXXX` proposals (same thread, 30 min); meanwhile pause runs on any wording, publish/filler words count as "explicit", and unattributed board edits become owner-pinned | Production Fri 2026-10-09 21:11–21:26 UTC: ~25 owner instructions → 17 proposals → **0 executed**; the only effective action was an unintended pause of 5 Stories. 21/21 proposals in the DB expired unexecuted | Model only *proposes*; protected actions run on one tap (Slack button) for the named items; Bondok's own question + "yes" approves exactly that question |
| L3 | **Approvals bound to the whole item `version`** (bumps on every readiness/prep/infra change; max version 192 in production) + 30-min TTL + no re-issue + holds that outlive their proposal | Caption drafts 4/4 expired; Topaz proposals stale after one WF1 cycle; legacy-caption batch voided by any Dropbox timeout | Bind to the fields the action depends on (caption text, file version, format, slot); no timer; holds end when their proposal ends |
| L4 | **"Protective" defaults produce the main business failure: missed posts.** Pause cheap / resume expensive; unclear situations become owner holds, often with no Slack message; "temporary" problems retry forever | Production: 19 ready/checking Stories on a silent "unscheduled" hold, 16 blocked by disk, 33 waiting for Topaz, **0 Posts scheduled**, 7 Slack messages in 15 h | Every hold notifies; temporary problems escalate after N hours; undoing an edit undoes its hold |
| L5 | **Scheduling only reacts to events; no reconciliation loop.** Any path that leaves a ready item without a slot leaves it for ever; pins never expire | A1, A5, A6, A10, A11, A17 | Each cycle: `try_schedule` every eligible unreserved item; a pin protects future slots only |
| L6 | **Owner intent loses to automation.** Requested times are not protected from auto-allocation; "publish now" cannot move an auto item; the owner must approve a swap of their own request | A5 (REPRO) | Auto-allocation skips valid requested slots; owner request outranks unpinned auto slots without a proposal |
| L7 | **Publishing depends on things it does not use, and not on the thing it does.** Local prepared file must exist at claim/commit although Instagram fetches the Dropbox copy; a fresh Monday read is required; 6 GB free disk even for a duration check; the delivered Dropbox copy is never verified | BV-80, OPS-1, M17 | Verify and bind the delivered copy; local file is a cache; publish from durable state if Monday is down |
| L8 | **Failures are discovered at the slot.** Instagram container created only at slot time; 9007 creates a new container; content errors go to the owner, not the editor | B2, M6 | Create + verify the container 30–60 min before the slot; publish that container at the slot; content errors → editor task |
| L9 | **Preparation is not tied to the calendar.** Queue is fairness-ordered, not by slot/requested time; slots only after full readiness; a Story is downloaded up to 3 times; every file is 2-pass re-encoded | D1–D3 | Order by earliest requested/reserved slot; ffprobe over the link; remux conforming files |
| L10 | **Monolithic workflows, error outputs bypass the state machine.** WF1 (121 nodes) does import+observe+captions+media+upload+editor+maintenance; one bad cell or one failed create stops all of it; WF2 is publisher *and* display sync; error outputs go straight to "Item Finished"; heartbeat written at run start; helper input is a ≤120 KB base64 argument | A3, A11, A12, A13, B2, M8, M9 | Separate executions with their own heartbeat written on completion; every error output reports to the helper; per-item error isolation |
| L11 | **Monitoring checks "did it run", not "will the next slots publish"** | OPS-2 not detected; WF1 failing after `run_start` looks healthy | Hourly "next 24 h" check per slot (ready, file, caption, no hold, last provider call OK) |

## 2. Publication safety (wrong or duplicate publication)

| ID | Sev | Defect | Where | Status |
|---|---|---|---|---|
| B1 | CRITICAL | Same video published twice from a duplicated board item (Monday "Duplicate item" copies link, code, Topazed, asset). v2 removed v1's `duplicate_asset` guard; `assets` is written, never read | publish.py:88-189, 306-308 | REPRO (2 publications, same asset/hash) |
| B2 | HIGH | Paused/skipped item marked **Posted** on the board → resume *proposal*, not the external-post hold; Posted label overwritten; after the owner resumes, it is claimed and published again | items.py:320 | REPRO; **also on base** |
| B3 | HIGH | **Replace source ignored** when the item has a project folder: WF1 lists the folder and re-selects the old file; old Topaz still applies → rejected file is prepared and scheduled | build.py:481-483 (Item Context prefers `folder_url`) | REPRO |
| B4 | HIGH | Board **Topazed** bound to the file shown at the *next* board read, not the one the editor saw → a non-Topazed newer file is prepared | items.py:226,269,615-623 | REPRO |
| B5 | HIGH | Bondok explicit-intent false positives execute protected actions (policy tells the model to call the tool to *suggest*): "توباز هيبقى جاهز بكره", "Topaz will be done tomorrow", "هو التوباز خلص" → Topaz confirmed on the pre-Topaz file; "بطل تنشرهم", "استنى قبل ما تنشرهم", "ينفع انشرهم بكره ولا نستنى", "رجعها للمونتير" → resume; "كان المفروض يبقى منشور امبارح", "اتنشرت على الحساب التاني" → marked **published** (never posts); "تمام كمل" → resume; "تمام شكرا" → caption approved; "cancel that" → skip | bridge.py:61-124 | REPRO |
| B6 | HIGH | Explicit check ignores *which* items: "انشر LIP12" also resumes a deliberately skipped LIP13 named by the model (or by board Notes text the model read) | bridge.py:89-124,172-174 | REPRO |
| B7 | HIGH | Status write unguarded when the confirmed status is empty (cleared cell / first sight) → a Pause typed within ~1 min is overwritten by "Scheduled" → publishes | core.py:414 | REPRO (narrow window) |
| B8 | MEDIUM | 4xx with Meta generic code 1/2 still "definitive": attempt failed, rollback receipt deleted, reconcile impossible (BV-68 incomplete) | build.py:361, publish.py:333-355 | REPRO |
| B9 | MEDIUM | Owner's "not published" accepted without checking the container; a later PUBLISHED reconcile is rejected and the item is rescheduled | publish.py:395-400,453-459 | REPRO |

## 3. Stuck items, lost slots, stopped workflows

| ID | Sev | Defect | Where | Status |
|---|---|---|---|---|
| A1 | HIGH | **Missed owner-pinned slot stuck for ever**: WF3 says "will be rescheduled" then its release is refused; board shows Scheduled with a past time; owner reschedule refused as "within 10 minutes of publication"; "publish it" answers "Already scheduled". All 23 production reservations are pinned | monitor.py:35-38,102,135-136; sched.py:157-158,257-259 | REPRO (4 reviews) |
| A2 | HIGH | WF2 pre-commit failures (create container error e.g. expired token, no id, save/renew/check errors, Monday reads) go to "Item Finished": no abandon, retry limit never applies, ~1 create every 3 min for 2 h, then false "missed … without a publication attempt", item cycles slot to slot for ever (30 h: 246 creates, 7 slots) | build.py:286-287,314-316,332-333 | REPRO (simulated graph) |
| A3 | HIGH | **Regression in 91ee8c5 (8a1457d):** a date picked without a time in Publish at / Published at / Last checked raises `RuleError` in `observe()` → WF1 stops every cycle; the item's own WF2 claim crashes too. A malformed Dropbox/Folder link does the same (also on base) | items.py:290 → board.py:83-87,152 → rules.py:68-70; items.py:30 | REPRO (real helper); base OK for dates |
| A4 | HIGH | Bondok sees every prepared file as missing (`media.path` is the n8n container path `/home/node/...`; Bondok has `ProtectHome=true` and sees the volume elsewhere) → Slack "Topaz done"/collab runs `evaluate()` → ready item un-verified, slot released | items.py:725,742-746; bondok.service | REPRO (path emulation; production paths confirmed) |
| A5 | HIGH | Owner-requested time given to an automatic item (`taken()` ignores requests); the owner's item then waits "requested slot is occupied" for ever, even after that time passes | sched.py:44-46,105-109 | REPRO |
| A6 | HIGH | Board Format change: slot released before approval; reject → never rescheduled; proposal expiry → hold for ever; undo on board refused `invalid`; approval after undo still converts | items.py:507-511,529-553 | REPRO; also on base |
| A7 | HIGH | Rejected board edit → silent `rejected_edit` hold that the corrected edit does not clear (Topazed before file selection; caption with 31 hashtags) | items.py:363-367 | REPRO; also on base |
| A8 | HIGH | Expired caption draft (30 min) strands the Post: never approvable, WF1 never re-drafts, Bondok `approve_existing_caption` cannot reach the draft. Production: 4/4 drafts expired, 3 ready Posts stuck | captions.py:27; items.py:810-813; bridge.py:213 | REPRO |
| A9 | HIGH | Recheck of an item blocked by a media verdict never finishes; full re-download each visit (BV-26 incomplete) | media.py:216-221,253-260 | REPRO |
| A10 | HIGH | Defective sources (audio-only, truncated, no duration, >5 GB, ffmpeg timeout) are "temporary" for ever: never blocked, editor never told, re-downloaded hourly, occupy the queue | media.py:326-334,103-112 | REPRO |
| A11 | HIGH | One Monday error while creating social items aborts the whole WF1 run, every run (Record Created Items throws); team owners are sent as `kind: person` | build.py:430,443-446; items.py:86-91 | REPRO (JS/plan level) |
| A12 | HIGH (latent) | Import Plan payload is not chunked: 60 KB of the 120 KB limit today → WF1 stops completely when the board roughly doubles; `bodyFile` exists but is unused | build.py:422-432,25,112 | REPRO (real snapshot) |
| A13 | MED-HIGH | WF1 dying after `run_start` raises no alert (heartbeat written at start; no `errorWorkflow`) | core.py:228; build.py:140-147 | REPRO |
| A14 | HIGH | Pause runs for any owner message and any number of items; BV-79's pause half is fixed only in the prompt (the production thread still pauses all 5 Stories) | bridge.py:186-187 | REPRO |
| A15 | HIGH | "انشرهم في موعدهم بكرا بس مش اكثر" → resume with the earliest free slot (today), not the owner's time | bridge.py:191-192 → sched.py:86-104 | REPRO |
| A16 | MEDIUM | Clearing Publish at → "unscheduled" owner hold with **no Slack notice**; `request_publish` refused with the hold text; production: 19 Stories (11 ready) | sched.py:160-165,260-262 | REPRO + DB |
| A17 | MEDIUM | `reauthorize()` after a passed slot releases and never schedules again; nothing revisits ready items without a slot | sched.py:142-144 | REPRO |
| A18 | MEDIUM | Late transient answer (4/17/32/613) on an `outcome_unknown` attempt → stuck unknown, false "will be tried again" | publish.py:333-351 | REPRO |

## 4. Medium

| ID | Defect | Where | Status |
|---|---|---|---|
| M1 | Proposals go stale from unrelated version bumps (prep, infra flap +2/visit, Notes edits); legacy-caption batch voided by one Dropbox timeout | core.py:100-107,670-678; items.py:899-900,997 | REPRO |
| M2 | One Post Date+Time edit → 2 commands, 2 swap proposals, 2 Slack notices | items.py:253,329-338 | REPRO |
| M3 | Repeated identical write to a human column dropped (INSERT OR IGNORE): 2nd Topaz reset, caption A→X→A (board X, publishes A), 2nd format rejection | items.py:406 | REPRO; caption case on base |
| M4 | Rejected time hidden when an Action is already shown; accepted-but-unreservable time vanishes from the board | core.py:293,383 | REPRO |
| M5 | Swap is not a swap: displaced item goes to the earliest free slot and becomes owner-pinned; requester's old slot left empty | sched.py:199-250 | REPRO |
| M6 | Instagram container error text (`status`) dropped; content errors retried 3× then owner hold; editor never told | build.py:327-329 | REPRO |
| M7 | Definitively rejected item shows "Redy For Scheduled"; no retry primitive except approving "not published" | core.py:265-291; sched.py:260-262 | REPRO |
| M8 | Canceled source projects applied to the first observation chunk only (~88% of items never canceled by WF1) | build.py:455-459; items.py:140 | REPRO |
| M9 | WF1 upload/share/register failures silent; up to 145 MB re-uploaded every 10 min | build.py:626-628 | REPRO |
| M10 | Permanent Dropbox errors raised by WF1 code nodes classified `infra` → retried every 20 min for ever | build.py:560-580 | REPRO |
| M11 | Deleted editor subitem: stored task id wins over lookup → all later editor tasks fail and escalate | build.py:651 | REPRO (Monday response reasoned) |
| M12 | Code fixed on the board but item stays blocked "invalid code" | items.py:644-651,920-921 | REPRO |
| M13 | Blocked backoff never grows for Topaz/duration/media blocks (143 Dropbox listings/day per item) | items.py:875 | REPRO |
| M14 | Low-disk alert: no hysteresis, frozen detail (says 5.9 GB at 0.3 GB), "0 items waiting" while preparing, needs SQLite so cannot fire when the disk is full (BV-81 incomplete) | monitor.py:76-85 | REPRO |
| M15 | Import findings (duplicate/invalid codes, two social items per source) dropped → projects silently never imported | items.py:39-95; build.py | REPRO |
| M16 | Media job killed mid-encode leaves source (≤5 GB), pass logs and partial output for ever | media.py:239-294,344-378 | REPRO (SIGKILL) |
| M17 | Delivered Dropbox copy (what Instagram fetches) never bound or rechecked | items.py:1017-1030 | REASONED |
| M18 | Post without a usable brief re-reads Monday every visit (BV-47 incomplete) | items.py:808-813; captions.py:29-30 | REPRO |
| M19 | Slack notices lost after ~1 h Slack outage (escalated slack jobs excluded, no alert) | core.py:519,528,638 | REPRO |
| M20 | Watchdog: one helper-error alert per clock hour, the rest consumed; alert lost if one Slack post fails; rotation drops unread lines | bondok/app.py:205-229; helper.py:47-48 | REPRO |
| M21 | Owner replies in alert threads (e.g. "published" under the unknown-outcome notice) ignored; only proposal threads are listened to | bondok/app.py:42-49,144 | REPRO |
| M22 | Approval reply drops "may already be in progress"; binding misses claim/commit; resume approval hides the new slot | bondok/app.py:78-81 | REPRO |
| M23 | Asking for the same caption draft again → `KeyError 'proposal_id'` → "An error stopped this request" | bridge.py:244-253; agent.py:224 | REPRO |
| M24 | Owner caption typed in Slack stored with Slack markup (`&amp;`, `<url|text>`) and would be published | bridge.py update_caption path | REPRO |
| M25 | Clear owner instructions refused: "اه", "نزلهم بكره", "خليهم ينزلوا", "خلصنا التوباز", "عملنا توباز", "go ahead with them"; "مدهش"/"مشوش" read as negation | bridge.py:61-124 | REPRO |
| M26 | Approval near-miss (👍, *bold*, RLM/zero-width, quote, several ids, "اعتمدها") → model creates a new proposal (loop) | bondok/app.py:38 | REPRO |
| M27 | Names with Arabic-Indic digits no longer resolvable (BV-53 regression); ى/ي, ة/ه not normalised | bridge.py:132 | REPRO |
| M28 | Display-sync outbox lease 300 s < worst-case batch → duplicate writes, wrong escalation reason (BV-33 incomplete) | core.py:512-535 | REASONED |
| M29 | Late window 2 h ignores the 1 h spacing of the 21:00/22:00 Stories | rules.py LATE_WINDOW | REASONED |

## 5. Low

* WF1 chunk size measured in UTF-16 chars, guard in base64 bytes (Arabic-heavy chunk → every cycle throws) — build.py:457 vs 112.
* Helper: non-object JSON payload exits 1 with no JSON/log; `outbox_ack` failures unlogged; newer-schema refusal unreachable from the helper.
* `submit()` under a held lock: 62 s, records nothing; docstring claims "failed" is recorded.
* Caption guard trims differently in JS and Python (U+FEFF) → endless "conflict" retries.
* Display jobs of one batch may be written in parallel, not outbox order (NOT VERIFIED n8n behaviour).
* Items without a code share `/Social Media/Production/null/<format>`.
* `exitCode` checks after Execute Command are dead code (node fails first; NOT VERIFIED).
* `get_schedule`/`item_status` list published items' past slots as scheduled.
* BV-80 notice says "needs a new time from you", then the item is rescheduled automatically with a second notice.
* "Published at" = time the result was recorded (hours late for reconciled posts).
* BV-50 partial: with a pre-commit attempt the canceled source is re-read every minute.
* Unknown outcome + board Posted needs two owner answers.
* Status order differs from the contract (paused + external-post hold shows Paused).
* "This column is managed by the system" notice while the foreign text stays.
* Prepared file not normalised (120 fps, 96 kHz 5.1, 2 s Story pass the gate); >20 @mentions not checked.
* `policy.txt` still says Stories ≥60 s fail (trim exists); `tests/test_bondok.py:277` has `unittest.main()` mid-file (direct run executes 17/35 tests); WORKFLOW_CHANGES node counts outdated (121/71/9).
* Expired proposals stay `pending` in the DB (21 in production).
* Repo hygiene: Instagram business account id in `src/waset_ops/rules.py`, WF2 JSON and 8 docs (beyond BV-83).
* `deploy/build_release.py` marks releases with `<REDACTED>` credentials as clean and copies `bondok/` wholesale (a local `.env` would ship); `deploy/server_backup.sh` saves the v2 helper as `helper.v1.py`, omits `waset_ops/` and `bondok.sqlite`.
* WF2 saves every successful per-minute execution (`saveDataSuccessExecution: all`) on a 96 %-full disk.

## 6. Untracked deploy scripts found in the working tree (not written by this audit)

`deploy/deploy_91ee8c5.sh`, `deploy/import_workflows_91ee8c5.sh` (another session, 18:31 local). Read only:
the "quiet second" wait falls through after 240 s and swaps anyway; files are swapped one `mv` at a time; the hash
check runs after the swap and exits without rollback; the final health JSON is not checked for `ok:true`;
`import_workflows_91ee8c5.sh` contains an n8n project id (do not commit). Deploying `91ee8c5` as is would ship A3.

## 7. Checked and rejected (summary)

Cairo DST handling; Bondok time parsing incl. Arabic-Indic digits; slot double-booking and races; approving twice;
WF3 safety guards; ISO/Monday value round-trips; multi-process SQLite contention (10 processes × 20 s, 0 locked);
concurrent `migrate()`; errors.log rotation race; fences and leases (no double commit/publish); result() idempotency;
5xx/timeouts → unknown, never auto-retried; GraphQL escaping (variables); non-idempotent POSTs not retried;
`$('Node').item` pairing; Story 59.9–60.0 s re-encode boundaries; 145 MB overshoot; rotation/odd sizes; path
traversal; caption hash stability; `dist` is byte-identical to `build.py` output; current-state replay of
production converges by cycle 2 (BV-77 fix works on real data).
