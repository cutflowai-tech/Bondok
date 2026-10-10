# Bug register — Bondok V2 deep audit (2026-10-09/10)

Branch `bondok/deep-audit` (base `45b6418` = deployed `452da98` + approved, undeployed Story trimming
`271e1d0` and Slack model-credit fix `45b6418`). **Nothing below is deployed.** Implementation status and
deployment status are separate columns.

Severity: CRITICAL = data loss, duplicate/unsafe publication, unauthorized action, security. HIGH = broken
core function, scheduling failure, stuck workflow, incorrect approval. MEDIUM = recoverable error, repeated
work, wrong notification, unnecessary API use. LOW = minor.

Every fixed bug has a regression test that was run against the code before the fix (separate git worktree)
and failed there; reproductions came from scratch scripts against the real code (local only).

Production impact was checked read-only during rounds 1–3 (2026-10-09 evening to 2026-10-10 early): publishing
had not started and no bug had caused observable damage ("latent" = would occur once the conditions arise).
**Round 4 re-check (2026-10-10 09:34 UTC) found BV-16 live in production (see BV-77) and the operational
findings OPS-1/OPS-2**; the "Production impact" column of earlier rows reflects the round 1–3 check unless
marked otherwise.

## CRITICAL

| ID | Bug | Components | Fix | Test | Production impact | Deploy |
|---|---|---|---|---|---|---|
| BV-01 | Explicit-owner checks matched substrings anywhere: questions, negations and unrelated words ("Topaz is not done", "postpone", "مش موافق", "look") executed protected actions (Topaz, format, resume, skip, caption approval) | bondok/bridge.py | `1107c7a` | test_bondok `ExplicitIntentFailsClosed` | latent | not deployed |
| BV-02 | "هو 350 مش منشور؟" resolved an unknown publication as not published and rescheduled it (duplicate post) | bondok/bridge.py, publish.py | `1107c7a` (always an approval) | `UncertainOutcomeNeedsApproval` | latent (0 unknown) | not deployed |
| BV-03 | A board snapshot read before a WF2 write landed was taken as human edits: reservation released, owner reschedule/caption reverted (old caption marked approved), Dropbox link re-pinned | items._diff, core.outbox_ack | `d18f498`, refined `8a1457d` | `StaleBoardSnapshot`, `RoundTwoRegressions.test_genuine_pause_after_recent_write_is_applied` | latent | not deployed |
| BV-04 | Board Status=Posted / typed post link mapped to a hold Monday was not permitted to issue: refused, reservation kept, item published again | core.PERMISSIONS, items | `6eb2748` | `PublishingAuditCritical` | latent; **confirmed end-to-end on the deployed release (round 4)**: board "Posted" or a typed post link before the slot → published again | not deployed |
| BV-05 | resolve_outcome(not_published) picked an old failed attempt of a published item, reset it to not_started and rescheduled it | publish.op_resolve_outcome | `6eb2748` | `PublishingAuditCritical` | latent | not deployed |

## HIGH

| ID | Bug | Components | Fix | Test | Deploy |
|---|---|---|---|---|---|
| BV-06 | Items imported as outcome_unknown (board said Publishing) could never be resolved and were invisible to health/WF3 | publish, monitor | `6eb2748` | `PublishingAuditCritical` | no |
| BV-07 | v1 publication receipts ignored at claim and overwritten at commit (re-publication of v1 posts) — production has 0 such rows | publish.claim | `496255b` | `test_v1_publication_receipt_holds_until_owner_answers` | no |
| BV-08 | Container ERROR kept the slot: a new Instagram container every minute for 2 h (120 in repro), then repeated at the next slot | publish | `77b363f` | `PublishingAuditHigh` | no; **confirmed end-to-end on the deployed release (round 4)**: 13 containers in 14 minutes |
| BV-09 | A ready item stayed unscheduled forever after an abandoned pre-commit attempt | publish.due/abandon | `77b363f` | `PublishingAuditHigh` | no |
| BV-10 | Dropbox unreadable at commit ('unverifiable') handled as "file changed": slot released, verification dropped, misleading notice | publish.commit, WF2 | `77b363f`, `8a1457d` | `PublishingAuditHigh`, `RoundTwoRegressions` | no |
| BV-11 | rejected_edit / retry holds on active items could only be cleared by pause+resume | items.op_resume | `77b363f` | `PublishingAuditHigh` | no |
| BV-12 | New time after "Publish at" was cleared raised TypeError (escaped submit, stopped the whole board poll) | sched | `d49d1bf` | `SchedulingAudit` | no |
| BV-13 | Clearing Publish at was replaced by the legacy Post Date/Time v2 wrote itself: item still published | items._apply_edit | `d49d1bf` | `SchedulingAudit` | no |
| BV-14 | Pause turned an automatic slot into a sticky owner request; resume could wait forever on an occupied slot | items.op_pause, sched | `d49d1bf` | `SchedulingAudit` | no |
| BV-15 | Swap proposal from an item with the 'unscheduled' hold could never be approved | sched | `d49d1bf` | `SchedulingAudit` | no |
| BV-16 | Requested time for a not-ready item re-submitted every poll (version churn, every bound proposal went stale) | items._diff | `d49d1bf`, `8a1457d` | `SchedulingAudit` | **observed from 2026-10-10 00:21 UTC (BV-77)** |
| BV-17 | A not-ready item could take a reserved slot through an approved swap and give it away | sched | `d49d1bf` | `SchedulingAudit` | no |
| BV-18 | Rejected board edits (off-grid time, paused item, invalid link, invalid format) were neither reverted nor explained (revert ran inside the rolled-back command) | items._apply_edit | `d49d1bf`, `8a1457d` | `SchedulingAudit`, `RoundTwoRegressions` | no |
| BV-19 | WF2 overwrote a person's status (e.g. Paused) typed between WF1 observations: the pause was lost and the item could publish | WF2, core.project | `190de50` (compare-before-write), `8a1457d` | test_workflows `DisplaySyncGuard`, `DisplaySyncGuardCore`; e2e with real n8n | no (WF2 import) |
| BV-20 | Display jobs landed out of order (failed older job retried after a newer one; late success), and an A→B→A change reused a finished job's dedupe key and was dropped | core.outbox_ack, project | `85ec377`, `8a1457d` | `OutboxRecovery` | no |
| BV-21 | Escalated display/editor jobs were never sent again while their values blocked re-projection: board wrong after a >1 h Monday outage | core.outbox | `85ec377` | `OutboxRecovery` | no |
| BV-22 | Model failure after a tool ran: reply said "nothing was done", proposal ids lost | bondok/app, agent | `bbd0e3b` | `SlackAuditHigh` | no |
| BV-23 | More than 6 tool calls in one step left calls without output; next model call failed | bondok/agent | `bbd0e3b` | `SlackAuditHigh` | no |
| BV-24 | Replies in a proposal notification's thread were dropped | bondok/app | `bbd0e3b` | `SlackAuditHigh` | no |
| BV-25 | Unreadable database: notification delivery raised before the watchdog, so the "database not readable" alert never fired | bondok/app | `bbd0e3b` | `SlackAuditHigh` | no |
| BV-26 | Explicit recheck of a ready item never finished (check stuck 'running', reverify verdict never read) | items, media | `5e80ec0` | `MediaAudit` | no |
| BV-27 | Temporary media failures stopped retrying after 3 attempts (~15 min); those items then starved never-checked items | media._reuse, work_queue | `5e80ec0` | `MediaAudit` | no |
| BV-28 | Round-2: a board "Posted"/post link re-held and notified every cycle; WF2 conflicted forever; owner's "not published" overridden | items, core | `8a1457d` | `RoundTwoRegressions` | no |

## MEDIUM

| ID | Bug | Fix | Test |
|---|---|---|---|
| BV-29 | Swap approval bypassed the near-due guard or crashed with RuleError | `d49d1bf` | `SchedulingAudit` |
| BV-30 | Caption draft on a scheduled Post un-approved its approved caption (WF3 released the slot); rejecting a draft left board text 'missing'; WF3 release not projected | `29fcf78` | `CaptionDraftAndRepairAudit` |
| BV-31 | Storage failures inside commands (read-only DB = 2026-10-09 outage class) printed ok:true and were never logged | `29fcf78` | `StorageFailureIsReported` |
| BV-32 | WF3 repair acted on an earlier inspection and released a slot the owner had pinned meanwhile | `29fcf78` | `CaptionDraftAndRepairAudit` |
| BV-33 | Outbox leases: a stale worker's failure ack accepted; crashed workers never counted (poison job looped forever) | `85ec377` | `OutboxRecovery` |
| BV-34 | Prepared file missing from disk: old "ready" job result reused forever | `5e80ec0` | `MediaAudit` |
| BV-35 | WF1 Dropbox classification: 401 (expired token) or a number in free text ("Calli 403", ":443") blocked items as config and released slots | `655201b` | test_workflows `DropboxErrorText` (WF1 import) |
| BV-36 | Slack "Topaz done" bound to whatever file was selected at execution | `5e80ec0`, `8a1457d` | `TopazFromSlackBinding`, `RoundTwoRegressions` |
| BV-37 | Disk: superseded prepared files and partial ffmpeg outputs never removed (then every download fails the free-space guard) | `5e80ec0`, `873a730` | `MediaMaintenance` |
| BV-38 | One malformed errors.log line disabled the watchdog for good; undelivered alerts lost | `bbd0e3b`, `8a1457d` | `SlackAuditHigh` |
| BV-39 | Caption drafts (valid or invalid) shown as "✅ Done" | `7378d8d` | `SlackAuditMedium` |
| BV-40 | Partial name match acted on the wrong item ("Calli 3" → "Calli 30"); exact names never won | `7378d8d` | `SlackAuditMedium` |
| BV-41 | Approvals copied with backticks or a trailing period went to the model | `7378d8d` | `SlackAuditMedium` |
| BV-42 | Duplicate tool results rendered inconsistently; "approve all captions" created a proposal per call | `7378d8d` | `SlackAuditMedium` |
| BV-43 | Topaz label not reset when the file changed (editor's re-toggle invisible) | `5b05291` | `MondaySyncMedium` |
| BV-44 | Expired 24 h notice (Action required) stayed on the board | `5b05291`, `dc03a9f` | `MondaySyncMedium` |
| BV-45 | Item missing from the board re-notified after every WF3 run | `5b05291` | `MondaySyncMedium` |
| BV-46 | Meta throttling errors became terminal failures needing a manual retry (generic codes 1/2 deliberately excluded) | `7438dbf`, `8a1457d` | `TransientProviderErrors` |
| BV-47 | Client brief fetched from Monday every WF1 cycle after a rejected/failed caption draft | `2fe1d51` | `CaptionBriefRequests` |
| BV-48 | Round-2: Format write-back never landed; failed owner-approved writes discarded; project() dropped its own replacement job | `8a1457d` | `RoundTwoRegressions` |

## LOW

| ID | Bug | Fix |
|---|---|---|
| BV-49 | Legacy posted items placed by people moved between groups by unrelated edits | `5b05291` |
| BV-50 | Canceled source refused the claim but kept the reservation (WF2 re-read Monday every minute) | `7438dbf` |
| BV-51 | Skipped DST time reported as "ambiguous"; impossible dates raised a bare ValueError | `873a730` |
| BV-52 | Reject accepted from any thread | `7378d8d` |
| BV-53 | Arabic-Indic digits in item ids not resolved | `7378d8d` |
| BV-54 | server_backup.sh never checked integrity_check (corrupt backup got SHA256SUMS) | `873a730` |
| BV-55 | errors.log never rotated | `873a730` |

## Round 2 — review of the audit fixes and randomized sequence testing

An independent review of the fixes (diff 45b6418..8a1457d^) and a seeded fuzzer (~1 M steps, three code
snapshots, a simulated board honouring WF2's compare-before-write) found regressions introduced by round 1 and
a few older display-sync defects. No publication-safety invariant (duplicate, stale or unauthorized
publication, published reverted, unknown outcome claimed) failed in any fuzz run.

| ID | Sev | Bug | Fix | Test |
|---|---|---|---|---|
| BV-65 | HIGH | Round-1 stale-snapshot window (20 min) hid a genuine board Pause typed after a recent v2 write: the item published | `8a1457d` (snapshot floor = WF1 run start) | `RoundTwoRegressions.test_genuine_pause_after_recent_write_is_applied` |
| BV-66 | HIGH | Board "Posted"/post link re-held and notified every cycle; WF2 conflicted forever; owner answer undone (round-1 regression of BV-04) | `8a1457d` | `RoundTwoRegressions` |
| BV-67 | MEDIUM | Format write-back carried a guard and never landed; a failed owner-approved write was dropped by any newer display job; project() dropped its own identical replacement | `8a1457d` | `RoundTwoRegressions` |
| BV-68 | MEDIUM | Meta generic codes 1/2 auto-rescheduled (could duplicate if the post went through) | `8a1457d` (throttling codes only) | `RoundTwoRegressions` |
| BV-69 | LOW | First file selection counted as a recent change (Slack Topaz always a proposal); Dropbox outage at the slot put the item on an owner hold; intent phrasings 'خلاص', 'اتخطاه', 'زي ما' refused, 'نزلها' (imperative) taken as "published" | `8a1457d` | `RoundTwoRegressions` |
| BV-70 | MEDIUM | (fuzz F5) a status write already taken by WF2 landed before the next one, which then conflicted forever (board "Scheduled" for a paused item) | `be00eed` | `FuzzFindings` |
| BV-71 | MEDIUM | (fuzz F3) two owner-approved writes of the same column before a sync: second conflicted forever (board caption A, publish caption B) | `be00eed` | `FuzzFindings` |
| BV-72 | LOW–MED | (fuzz F7) caption typed while the item was a Story became approved, unvalidated, on conversion to Post | `be00eed` | `FuzzFindings` |
| BV-73 | LOW | (fuzz F8) rejected board edit's hold kept the reservation | `be00eed` | `FuzzFindings` |
| BV-75 | MEDIUM | (fuzz round 3) an owner-approved write taken by WF2 before a person edited the same column retried forever after its conflict | `efb72d0` | `FuzzRoundThree` |
| BV-76 | MEDIUM | (fuzz round 3) a consumed board value (person's "Posted") masked by an older pending value: corrective status write never queued | `efb72d0` | `FuzzRoundThree` |
| BV-74 | LOW (by design) | (fuzz F9) published / unknown items keep their past reservation row (table growth only) | open | — |

## Round 4 — production re-check, current-state replay, end-to-end publishing (2026-10-10)

Read-only production check at 09:34 UTC, a replay of the audit branch on a fresh copy of the production database
and board (real n8n, mocked services), and WF2 publishing driven end to end through real n8n against an Instagram
mock (both the deployed release and the audit branch; see REGRESSION_TEST_RESULTS.md).

| ID | Sev | Bug | Components | Fix | Test (fails before fix) | Production impact | Deploy |
|---|---|---|---|---|---|---|---|
| BV-77 | HIGH | Stories imported with a legacy Post Date/Time whose pair a person later changed or cleared on the board (no reservation) were re-submitted on every WF1 cycle: 2 `request_reschedule` commands and 2 version bumps per item per cycle, indefinitely | items._diff / _apply_edit | `8a1457d` (a handled board value is recorded); test `325f46e` | `LegacyDateLoopIncident` (fails on `45b6418` and every commit before `8a1457d`) | **live since 2026-10-10 00:21 UTC**: 28 Stories, ~330 commands/h, item versions up to 204; proposals bound to those items go stale | not deployed |
| BV-78 | LOW | System-column text v2 did not write was "reverted" every WF1 cycle without any write (v2 never clears values it does not own) | items._apply_edit | `8a1457d`; test `b2c4951` | `LegacyDateLoopIncident.test_foreign_system_text_is_handled_once` | **live**: one item, 6 audit rows/h | not deployed |
| BV-79 | HIGH | Bondok paused five Stories on an ambiguous "stop all this, it's a waste of time and resources" 50 s after the owner asked to publish them the next day; the owner's "I want you to publish them at their time tomorrow, nothing more" was not an explicit resume (publish verbs were not resume words; "مش اكتر" read as a negation), so it became approval requests that expired | bondok/bridge.py, policy.txt | `2ea6bb4` | test_bondok `OwnerPublishWordsResume` | **observed 2026-10-09 21:22 UTC**: the owner's five Sunday Stories are paused (the deployed Bondok also showed placeholder approval ids, fixed in `45b6418`) | not deployed |
| BV-80 | MEDIUM | A reserved item whose prepared file is missing at its slot (e.g. deleted to free disk space) lost the slot silently: claim refused, reservation released, nobody told | publish.claim | `2187315` | `MissingPreparedFileAtSlot` | latent; relevant while the disk is full | not deployed |
| BV-81 | MEDIUM | No alert when low disk space blocks media preparation: only a per-item board note, every cycle | monitor.inspect, rules | `8646598` | `LowDiskAlert` | **live**: preparation blocked since ≈03:00 UTC 2026-10-10, 16 Stories waiting, no Slack alert | not deployed |
| BV-82 | MEDIUM | `media_publish` answered 400 code 9007 / 2207027 ("The media is not ready for publishing, please wait for a moment") was recorded as a definitive rejection: item failed, slot lost until the owner asked for a retry (found by the end-to-end WF2 test; same in the deployed release) | publish.result | `78d9af1`, `6d92fa0` (committed attempts only) | `MediaNotReadyAtPublish`; end-to-end: retried in the same slot, 1 publication | latent (both releases) | not deployed |
| BV-83 | LOW | Repository hygiene: a Monday person id (the owner's fallback assignee) is committed in docs and in the archived v1 workflow export, against the public-repo rule; not a credential | docs/, workflows/original | open (removing it from history needs a force-push: owner decision) | — | none (public since the v2 handoff commit) | — |

### Operational findings (not code defects; owner action)

| ID | Sev | Finding | Recommended containment |
|---|---|---|---|
| OPS-1 | HIGH | Server disk 96% used (4.7 GB free of 96 GB at 10:13 UTC). Bondok refuses to start a media download below 6 GB, so no new item can be prepared. The space is used by other systems on the same disk (waset-atlas ≈43 GB, container images ≈18 GB, `/root` ≈11 GB). If the disk fills completely, every database write fails (n8n, Postgres, the operational store) | Free space now (waset-atlas retention, old builds, caches) or enlarge the disk; never delete Bondok's prepared media folder (scheduled videos). Preparation resumes automatically above 6 GB |
| OPS-2 | HIGH | Sunday 2026-10-11: the three Stories that will publish automatically come from board dates set at 03:21 Cairo; the five Stories the owner chose in Slack are paused (BV-79), unprepared (OPS-1), and three of their slots are taken | Owner decides which Stories publish; until the release is deployed, use Bondok or the board's Paused/Skipped status, never manual posting (BV-04 in the deployed release) |

## OPEN (not fixed; see POST_RELEASE_IMPROVEMENTS.md)

| ID | Severity | Item |
|---|---|---|
| BV-56 | MEDIUM (owner decision) | request_publish, request_reschedule, replace_source, request_recheck run on the model's initiative without explicit owner words (documented behaviour vs the policy sentence "only the owner's explicit words authorize…") |
| BV-57 | LOW | Per-job `.lock` files accumulate in the data directory (safe deletion needs a lock protocol change) |
| BV-58 | LOW | A Slack reply that fails to post stays `send_uncertain` and is never retried |
| BV-59 | LOW | `file_share` / `thread_broadcast` messages from the owner are ignored |
| BV-60 | LOW | Unknown outcome reconciled from container status keeps no media id/permalink |
| BV-61 | LOW | A failed "Record Publication Result" helper call loses the returned media id (recovered through the unknown-outcome path) |
| BV-62 | LOW | WF2 executionTimeout 1800 s can be exceeded by 5 due items polling containers (recovered by leases) |
| BV-63 | LOW | A swap proposal whose execution was refused stays pending until it expires (30 min) |
| BV-64 | LOW | Clearing Publish at within 20 minutes of v2 writing it (inside one WF1 run) is applied one cycle later |
