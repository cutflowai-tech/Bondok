# Repair traceability — Round 5 register → tests → commits

Generated 2026-10-10 from the R5.2 directive's register (§23) and the test tree of `bondok/r5-monday-authority`.
Source text and classifications stay in `AUDIT_ROUND5_2026-10-10.md` (unchanged); BV ids in `BUG_REGISTER.md`.
"Introduced in" is the commit that added the executable test; the fix is in the same commit or its merge.
Titles are cut where the register line breaks.

**Disposition for every row: IMPLEMENTED · TESTED (Python 3.12 / 3.13 / 3.14) · NOT DEPLOYED.**
Live exposure before release: see `HANDOFF_STATUS.md` (live baseline 2026-10-10). Nothing is LIVE PATH VERIFIED.

Component owners: Monday authority/approvals/language → `bondok/`, `items.py`, `record.py`; scheduling and
publication → `sched.py`, `publish.py`; media/n8n → `media.py`, `items.py` (prep), `workflows/build.py`;
infra/deployment → `core.py`, `monitor.py`, `helper.py`, `deploy/`.

Beyond the register: `test_r5_authority.py` (R2 rebuild from the board), `test_r5_campaign.py` (multi-cycle
invariants; soak finding "refused attempt blocks its old file" fixed in 391d2a2),
`test_r5_captions.py` (M18, LOW-13).

| R5 | Sev | Finding | Executable tests | Introduced in |
|---|---|---|---|---|
| B1 | CRITICAL | Same video published twice from a duplicated board item (Monday "Duplicate item" copies link, code, Topazed, asset). v2 removed v1's | `test_r5_safety.py::R5_B1_CrossItemDuplicateGuard`<br>`test_r5_safety.py::R5_B1_RefusedAttemptDoesNotBlockItsOldFileForever` | 391d2a2, 7ceb6f1 |
| B2 | HIGH | Paused/skipped item marked | `test_r5_safety.py::R5_B2_OwnerPostedIsTerminal` | 0719109 |
| B3 | HIGH | Replace source ignored | `test_r5_media.py::R5_B3_OwnerSelectedFileOutranksFolder` | 4156cd7 |
| B4 | HIGH | Board | `test_r5_board.py::R5_B4_TopazBoundToTheVersionShown` | 16ea777 |
| B5 | HIGH | Bondok explicit-intent false positives execute protected actions (policy tells the model to call the tool to | `test_r5_language.py::R5_B5_FalsePositivesNeverAct` | c89b9a1 |
| B6 | HIGH | Explicit check ignores | `test_r5_language.py::R5_B6_TargetsComeFromTheOwnersWords` | c89b9a1 |
| B7 | HIGH | Status write unguarded when the confirmed status is empty (cleared cell / first sight) → a Pause typed within ~1 min is overwritten by "Scheduled" → publishes | `test_r5_safety.py::R5_B7_StatusWriteGuardedWhenEmpty` | 0719109 |
| B8 | MEDIUM | 4xx with Meta generic code 1/2 still "definitive": attempt failed, rollback receipt deleted, reconcile impossible (BV-68 incomplete) | `test_r5_publishing.py::R5_B8_GenericProviderErrorsAreNotDefinitive` | 739f11b |
| B9 | MEDIUM | Owner's "not published" accepted without checking the container; a later PUBLISHED reconcile is rejected and the item is rescheduled | `test_r5_publishing.py::R5_B9_OwnerNotPublishedVersusLaterEvidence` | 739f11b |
| A1 | HIGH | Missed owner-pinned slot stuck for ever | `test_r5_scheduling.py::R5_A1_MissedOwnerTimeIsClosedAndAsked` | 1cb465a |
| A2 | HIGH | WF2 pre-commit failures (create container error e.g. expired token, no id, save/renew/check errors, Monday reads) go to "Item Finished": no abandon, retry limit never applies, ~1 create every 3 min for 2 h, then false "missed … without a publication attempt", item cycles slot to slot for ever (30 h: 246 creates, 7 slots) | `test_r5_publishing.py::R5_A2_PreCommitFailuresAreBounded`<br>`test_r5_publishing.py::R5_A2_WorkflowRoutesEveryPreCommitFailure` | 6421007, 739f11b |
| A3 | HIGH | Regression in 91ee8c5 (8a1457d): | `test_r5_safety.py::R5_A3_IncompleteValuesStayItemLocal` | 3b62eff |
| A4 | HIGH | Bondok sees every prepared file as missing ( | `test_r5_media.py::R5_A4_PortableCacheIdentity` | 4156cd7 |
| A5 | HIGH | Owner-requested time given to an automatic item ( | `test_r5_scheduling.py::R5_A5_OwnerRequestProtected` | 1cb465a |
| A6 | HIGH | Board Format change: slot released before approval; reject → never rescheduled; proposal expiry → hold for ever; undo on board refused | `test_r5_board.py::R5_A6_BoardFormatEditIsTheAuthorization` | 16ea777 |
| A7 | HIGH | Rejected board edit → silent | `test_r5_board.py::R5_A7_RejectedEditsDoNotLeaveStaleHolds` | 16ea777 |
| A8 | HIGH | Expired caption draft (30 min) strands the Post: never approvable, WF1 never re-drafts, Bondok | `test_r5_language.py::R5_A8_DraftApprovalInPlainWords`<br>`test_r5_scheduling.py::R5_A8_DraftApprovalDoesNotExpire` | 1cb465a, c89b9a1 |
| A9 | HIGH | Recheck of an item blocked by a media verdict never finishes; full re-download each visit (BV-26 incomplete) | `test_r5_media.py::R5_A9_RecheckTerminates` | 4156cd7 |
| A10 | HIGH | Defective sources (audio-only, truncated, no duration, >5 GB, ffmpeg timeout) are "temporary" for ever: never blocked, editor never told, re-downloaded hourly, occupy the queue | `test_r5_media.py::R5_A10_DefectiveSourcesArePermanent` | 4156cd7 |
| A11 | HIGH | One Monday error while creating social items aborts the whole WF1 run, every run (Record Created Items throws); team owners are sent as | `test_r5_n8n.py::R5_A11_ImportIsolation` | c731276 |
| A14 | HIGH | Pause runs for any owner message and any number of items; BV-79's pause half is fixed only in the prompt (the production thread still pauses all 5 Stories) | `test_r5_language.py::R5_A14_PauseNeedsResolvedScope` | c89b9a1 |
| A15 | HIGH | "انشرهم في موعدهم بكرا بس مش اكثر" → resume with the earliest free slot (today), not the owner's time | `test_r5_language.py::R5_A15_ConstraintsTravel`<br>`test_r5_scheduling.py::R5_A15_ConstraintsTravelWithTheInstruction` | 1cb465a, c89b9a1 |
| A16 | MEDIUM | Clearing Publish at → "unscheduled" owner hold with | `test_r5_scheduling.py::R5_A16_ClearedTimeIsVisibleAndRecoverable` | 1cb465a |
| A17 | MEDIUM | reauthorize() | `test_r5_scheduling.py::R5_A17_ReleasedReadyItemsAreRescheduled` | 1cb465a |
| A18 | MEDIUM | Late transient answer (4/17/32/613) on an | `test_r5_publishing.py::R5_A18_LateRefusalOnUnknownAttempt` | 739f11b |
| M1 | MEDIUM | Proposals go stale from unrelated version bumps (prep, infra flap +2/visit, Notes edits); legacy-caption batch voided by one Dropbox timeout | `test_r5_board.py::R5_M1_ApprovalsBindToTheirDependencies` | 16ea777 |
| M2 | MEDIUM | One Post Date+Time edit → 2 commands, 2 swap proposals, 2 Slack notices | `test_r5_board.py::R5_M2_DateAndTimeEditIsOneDecision` | 16ea777 |
| M3 | MEDIUM | Repeated identical write to a human column dropped (INSERT OR IGNORE): 2nd Topaz reset, caption A→X→A (board X, publishes A), 2nd format rejection | `test_r5_board.py::R5_M3_RepeatedOwnerValuesAreSeparateEvents` | 16ea777 |
| M4 | MEDIUM | Rejected time hidden when an Action is already shown; accepted-but-unreservable time vanishes from the board | `test_acceptance.py::SchedulingAudit.test_rejected_board_time_is_reverted_with_notice` | d49d1bf |
| M5 | MEDIUM | Swap is not a swap: displaced item goes to the earliest free slot and becomes owner-pinned; requester's old slot left empty | `test_r5_scheduling.py::R5_M5_TrueSwap` | 1cb465a |
| M6 | MEDIUM | Instagram container error text ( | `test_r5_publishing.py::R5_M6_ContainerContentErrors` | 739f11b |
| M7 | MEDIUM | Definitively rejected item shows "Redy For Scheduled"; no retry primitive except approving "not published" | `test_r5_publishing.py::R5_M7_FailedPublicationDisplayAndRetry` | 739f11b |
| M8 | MEDIUM | Canceled source projects applied to the first observation chunk only (~88% of items never canceled by WF1) | `test_r5_n8n.py::R5_M8_CancellationInEveryChunk` | c731276 |
| M9 | MEDIUM | WF1 upload/share/register failures silent; up to 145 MB re-uploaded every 10 min | `test_r5_media.py::R5_M9_DeliveryFailuresRecorded` | 4156cd7 |
| M10 | MEDIUM | Permanent Dropbox errors raised by WF1 code nodes classified | `test_r5_media.py::R5_M10_PermanentDropboxErrors` | 4156cd7 |
| M11 | MEDIUM | Deleted editor subitem: stored task id wins over lookup → all later editor tasks fail and escalate | `test_r5_n8n.py::R5_M11_DeletedEditorSubitem` | c731276 |
| M12 | MEDIUM | Code fixed on the board but item stays blocked "invalid code" | `test_r5_board.py::R5_M12_CorrectedCodeUnblocks` | 16ea777 |
| M13 | MEDIUM | Blocked backoff never grows for Topaz/duration/media blocks (143 Dropbox listings/day per item) | `test_r5_media.py::R5_M13_BlockedBackoffGrows` | 4156cd7 |
| M14 | MEDIUM | Low-disk alert: no hysteresis, frozen detail (says 5.9 GB at 0.3 GB), "0 items waiting" while preparing, needs SQLite so cannot fire when the disk is full (BV-81 incomplete) | `test_r5_infra.py::R5_M14_LowDiskAlertLifecycle` | a022886 |
| M15 | MEDIUM | Import findings (duplicate/invalid codes, two social items per source) dropped → projects silently never imported | `test_r5_n8n.py::R5_M15_ImportFindingsKept` | c731276 |
| M16 | MEDIUM | Media job killed mid-encode leaves source (≤5 GB), pass logs and partial output for ever | `test_r5_media.py::R5_M16_OrphanReaper` | 4156cd7 |
| M17 | MEDIUM | Delivered Dropbox copy (what Instagram fetches) never bound or rechecked | `test_r5_media.py::R5_M17_DeliveredIdentityBound` | 4156cd7 |
| M18 | MEDIUM | Post without a usable brief re-reads Monday every visit (BV-47 incomplete) | `test_r5_captions.py::R5_M18_NoBriefIsRememberedNotReRead` | 7348d98 |
| M19 | MEDIUM | Slack notices lost after ~1 h Slack outage (escalated slack jobs excluded, no alert) | `test_r5_infra.py::R5_M19_SlackOutageKeepsNotices` | a022886 |
| M20 | MEDIUM | Watchdog: one helper-error alert per clock hour, the rest consumed; alert lost if one Slack post fails; rotation drops unread lines | `test_r5_language.py::R5_M20_WatchdogKeepsDistinctErrors` | c89b9a1 |
| M21 | MEDIUM | Owner replies in alert threads (e.g. "published" under the unknown-outcome notice) ignored; only proposal threads are listened to | `test_r5_language.py::R5_M21_AlertThreadReplies` | c89b9a1 |
| M22 | MEDIUM | Approval reply drops "may already be in progress"; binding misses claim/commit; resume approval hides the new slot | `test_r5_language.py::R5_M22_ApprovalRepliesAreTruthful` | c89b9a1 |
| M23 | MEDIUM | Asking for the same caption draft again → | `test_r5_language.py::R5_M23_SameDraftAgain` | c89b9a1 |
| M24 | MEDIUM | Owner caption typed in Slack stored with Slack markup ( | `test_r5_language.py::R5_M24_SlackMarkupIsDecodedInCaptions` | c89b9a1 |
| M25 | MEDIUM | Clear owner instructions refused: "اه", "نزلهم بكره", "خليهم ينزلوا", "خلصنا التوباز", "عملنا توباز", "go ahead with them"; "مدهش"/"مشوش" read as negation | `test_r5_language.py::R5_M25_ClearInstructionsExecute` | c89b9a1 |
| M26 | MEDIUM | Approval near-miss (👍, | `test_r5_language.py::R5_M26_ApprovalNearMisses` | c89b9a1 |
| M27 | MEDIUM | Names with Arabic-Indic digits no longer resolvable (BV-53 regression); ى/ي, ة/ه not normalised | `test_r5_language.py::R5_M27_NamesResolveWithoutGuessing` | c89b9a1 |
| M28 | MEDIUM | Display-sync outbox lease 300 s < worst-case batch → duplicate writes, wrong escalation reason (BV-33 incomplete) | `test_r5_infra.py::R5_M28_SlowDisplayBatchKeepsItsLease` | a022886 |
| M29 | MEDIUM | Late window 2 h ignores the 1 h spacing of the 21:00/22:00 Stories | `test_r5_scheduling.py::R5_M29_LateWindowStopsAtTheNextSlot` | 1cb465a |
| R5-LOW-01 | LOW | WF1 chunk size measured in UTF-16 chars, guard in base64 bytes (Arabic-heavy chunk → every cycle throws) — build.py:457 vs 112. | `test_r5_n8n.py::R5_LOW01_ChunksSizedInBytes` | c731276 |
| R5-LOW-02 | LOW | Helper: non-object JSON payload exits 1 with no JSON/log; | `test_r5_n8n.py::R5_LOW02_HelperOutcomes` | c731276 |
| R5-LOW-03 | LOW | submit() | `test_r5_infra.py::R5_LOW03_HeldLockIsBoundedAndTruthful` | a022886 |
| R5-LOW-04 | LOW | Caption guard trims differently in JS and Python (U+FEFF) → endless "conflict" retries. | `test_r5_board.py::R5_LOW04_CaptionNormalisationMatchesJS` | 16ea777 |
| R5-LOW-05 | LOW | Display jobs of one batch may be written in parallel, not outbox order (NOT VERIFIED n8n behaviour). | `test_r5_infra.py::R5_LOW05_OneWriterPerColumnPerBatch` | a022886 |
| R5-LOW-06 | LOW | Items without a code share | `test_r5_media.py::R5_LOW06_MissingCodeNoNullFolder` | 4156cd7 |
| R5-LOW-07 | LOW | exitCode | `test_r5_n8n.py::R5_LOW07_ExecuteCommandFailureBoundary` | c731276 |
| R5-LOW-08 | LOW | get_schedule | `test_r5_scheduling.py::R5_LOW08_ScheduleListsOnlyPendingWork` | 1cb465a |
| R5-LOW-09 | LOW | BV-80 notice says "needs a new time from you", then the item is rescheduled automatically with a second notice. | `test_r5_scheduling.py::R5_A1_MissedOwnerTimeIsClosedAndAsked.test_missed_automatic_slot_is_rescheduled_with_a_truthful_notice` | 1cb465a |
| R5-LOW-10 | LOW | "Published at" = time the result was recorded (hours late for reconciled posts). | `test_r5_board.py::R5_LOW10_PublishedAtIsNotInvented` | 16ea777 |
| R5-LOW-11 | LOW | BV-50 partial: with a pre-commit attempt the canceled source is re-read every minute. | `test_r5_board.py::R5_LOW11_CanceledSourceWithPreCommitAttempt` | 16ea777 |
| R5-LOW-12 | LOW | Unknown outcome + board Posted needs two owner answers. | `test_r5_publishing.py::R5_LOW12_OneOwnerReportResolvesAnUnknownOutcome` | f79667e |
| R5-LOW-13 | LOW | Status order differs from the contract (paused + external-post hold shows Paused). | `test_r5_captions.py::R5_LOW13_PausedWithOwnerReportedPost` | 7348d98 |
| R5-LOW-14 | LOW | "This column is managed by the system" notice while the foreign text stays. | `test_r5_board.py::R5_LOW14_ForeignTextInASystemColumn` | 16ea777 |
| R5-LOW-15 | LOW | Prepared file not normalised (120 fps, 96 kHz 5.1, 2 s Story pass the gate); >20 @mentions not checked. | `test_r5_media.py::R5_LOW15_PlatformRequirements`<br>`test_r5_media.py::R5_LOW15_PreparedFileIsNormalised` | 4156cd7 |
| R5-LOW-16 | LOW | policy.txt | `test_r5_language.py::R5_LOW16_PolicyAndTestCollection` | c89b9a1 |
| R5-LOW-17 | LOW | Expired proposals stay | `test_r5_board.py::R5_LOW17_ExpiredProposalsReachATerminalState` | 16ea777 |
| R5-LOW-18 | LOW | Repo hygiene: Instagram business account id in | `test_deploy.py::R5_LOW19_ReleasePackaging.test_deployment_ids_are_placeholders_filled_only_in_the_release`<br>`test_deploy.py::R5_Deploy_Gates.test_release_for_another_instagram_account_refuses_before_any_change` | 10d11b0 |
| R5-LOW-19 | LOW | deploy/build_release.py | `test_deploy.py::R5_LOW19_ReleasePackaging` | 6d625dd |
| R5-LOW-20 | LOW | WF2 saves every successful per-minute execution ( | `test_r5_n8n.py::R5_LOW20_SuccessRetention` | c731276 |
