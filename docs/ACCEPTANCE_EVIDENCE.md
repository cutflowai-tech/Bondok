# Acceptance Evidence

Status: **IMPLEMENTED and TESTED (locally / isolated). Not DEPLOYED, not ACTIVE.**

## How to run
```
cd tests && python3 -m unittest test_acceptance test_workflows test_helper_cli test_bondok
python3 workflows/build.py
python3 deploy/rehearse.py <state.sqlite snapshot> <board snapshot json> reference/deployed_2026-10-09/helper/helper.py
```
Synthetic fixtures only; mocked providers; no network, no paid model calls, no production database.

## Results (2026-10-09)
| Run | Interpreter | Result |
|---|---|---|
| Full suite (84 tests: acceptance 50, workflow contracts 16, helper CLI 6, Bondok 12) | macOS Python 3.13.5 | 84/84 OK |
| acceptance + helper CLI + Bondok (68) in a throwaway `/tmp` dir on the server host | Python 3.12.3 (Bondok's runtime) | 68/68 OK |
| acceptance + helper CLI (56) in a disposable `--network none` container of the n8n image | Python 3.14.8 (helper runtime) | 56/56 OK |

## Scenario → test map
| # | Scenario | Test(s) |
|---|---|---|
| 1 | Story 59.9 s passes duration only; 60.0/63.0 blocked before scheduling/encoding | `Scenario01StoryDuration` |
| 2 | Story→Post keeps item, invalidates, runs Post checks | `Scenario02StoryToPost` |
| 3 | File change after validation/scheduling blocks old version (also at commit) | `Scenario03FileChange` |
| 4 | Bondok vs monitor concurrent schedule commands serialize | `Scenario04ConflictingSchedulers` (threads) |
| 5 | Slot uniqueness; equivalent instants collide | `Scenario05SlotUniqueness` |
| 6 | Stale/expired approval does not execute | `Scenario06StaleApproval` |
| 7 | Duplicate Slack events / command ids / workflow retries | `Scenario07Duplicates`, `Scenario07SlackDuplicates` |
| 8 | Board edits go through checks, no sync loop | `Scenario08MondayEdits`, rehearsal (2nd observation: 0 edits) |
| 9 | Pause vs publish race at the commitment point | `Scenario09PauseRace` |
| 10 | Crash/timeout → unknown → reconcile, no republish | `Scenario10CrashReconcile` |
| 11 | Receipt survives Monday failure; only sync retries; one escalation | `Scenario11SyncFailure` |
| 12 | Paused/Skipped/published/unknown protections | `Scenario12Protections`, `Scenario31HistoricalPosted` |
| 13 | Notes don't reprocess; caption can't publish obsolete payload | `Scenario13NotesAndCaption` |
| 14 | Unauthorized actor/channel/workspace/item rejected in code | `Scenario14Unauthorized`, `Scenario14SlackAuthentication`, `test_member_cannot_change_or_approve` |
| 15 | Unchanged monitor cycles quiet | `Scenario15QuietMonitor` |
| 16 | Migration/rollback preserve history; no competing writers | `Scenario16MigrationHistory`, rehearsal (`legacy_rows_preserved`, old helper on migrated DB, old claim refused by mirrored receipt) |
| 17 | Model-claimed format permission rejected | `Scenario17AutonomousFormat`, `Scenario17ModelCannotChangeFormat`, `ExplicitIntent` |
| 18 | Occupied slot: nothing moves before approval; change invalidates | `Scenario18OccupiedSlot`, `Scenario06StaleApproval` |
| 19 | Zero model calls on routine paths | `Scenario19ZeroModelRoutine`; `test_llm_only_on_caption_draft_branch` (WF2/WF3 have no LLM nodes) |
| 20 | Unchanged blocked items: one editor task | `Scenario20EditorSpam` |
| 21 | Empty due queue: no null-item call | `Scenario21EmptyQueue`, `test_empty_due_queue_emits_nothing` |
| 22 | Outage observable; valid schedule/evidence kept | `Scenario22Outage` (incl. errors.log written with a corrupt DB), `test_helper_error_log_alert_without_database` |
| 23 | No zero-row success; instant normalisation | `Scenario23ZeroRow` |
| 24 | Stale worker fenced; late valid result kept | `Scenario24StaleWorker` |
| 25 | Human caption/Topaz/variety never overwritten | `Scenario25HumanEditsDuringRun` |
| 26 | Cairo round-trip, DST gap refused, legacy dates, off-grid alternatives | `Scenario26TimeContract`, `LegacyMigrationBehaviour` |
| 27 | Caption change re-binds payload without re-encoding | `Scenario27CaptionNoReencode` |
| 28 | Only registered social-board items reachable | `Scenario28BoardBoundary`; Bondok Monday reads check board membership first |
| 29 | Model outage: no guessed commands; publishing continues | `Scenario29ModelOutage` |
| 30 | IG Collab blocked, request kept | `Scenario30Collab` |
| 31 | Historical Posted / Publishing imported protected, not due | `Scenario31HistoricalPosted`, rehearsal (15 Posted, 0 due) |
| 32 | Explicit recheck really checks or reports already running | `Scenario32Recheck`; media `reverify` job re-probes and re-hashes |

Workflow contracts: `test_workflows.py` (graph integrity, reachability, references, v2-only routes, single publisher,
write ownership, LLM placement, read-only manual trigger, payload guard, no secrets, all Code nodes parse, behavioural
harness). Helper CLI: real `helper.py` subprocess through due → claim → container → renew → commit → result →
evidence → display sync ack; fresh board pause blocks a claim; retired `/v1` routes and errors keep diagnostics; health
is read-only; WF3 lock contention is `deferred`.

## Migration rehearsal on copies of live data (2026-10-09)
Production DB snapshot (SQLite backup API, read-only) + live board snapshot (164 items):
legacy rows preserved; schema 1; old helper on migrated DB: snapshot/lock/unlock OK; bootstrap 164 items in 0.05 s,
**0 edits, 0 Monday writes queued, 0 notifications**; publication: 15 published / 149 not started; owner state:
44 skipped / 120 active; captions: 35 legacy unapproved, 6 missing; 59 legacy future dates as preferences; second
observation 0 edits; due now: none; supervisor: 0 findings; old v1 claim against a v2 receipt → refused (`stage: published`).

## Limitations of the evidence
* No live n8n import or execution of the v2 workflows (needs authorization). n8n runtime semantics are inferred from the existing workflows' usage.
* No live Slack round-trip with the new Bondok code; Slack transport is unchanged from the working deployment, and Core logic is tested offline.
* Instagram behaviours (container `PUBLISHED` status, 4xx semantics) follow Graph API documentation and the existing workflow; not exercised live.
* Exactly-once publication across SQLite, Monday and Instagram is not claimed.
