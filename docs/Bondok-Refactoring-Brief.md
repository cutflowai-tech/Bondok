# Refactor Bondok and the Waset Social Media Automation

## Request

Refactor the existing system so that Bondok, the n8n workflows, the schedule monitor, and manual Monday edits work through one consistent execution model. Implement the necessary changes; do not stop at recommendations or add another independent automation that edits the same data.

The purpose is reliable coordination and a simple user experience. Reuse the existing working components. Change the pipeline where necessary to remove conflicting ownership, but avoid an unnecessary rewrite or additional infrastructure.

This document describes the required target behavior. It does **not** certify that any of these protections already exist. Inspect the actual code, workflows, database, and board before deciding what must change.

## Existing context and scope

- Bondok is the Python AI assistant for Waset Co Studio, intended to run continuously on the existing server and communicate through the designated private Slack social media channel.
- The requested model is GPT-6.1 Sol. Verify the configured provider and model identifier. Preserve the requested model unless the owner approves a change.
- Bondok's business tools are restricted to the **For Social Media** Monday board, ID `5105608159`. Validate item membership in code before reading its contents or modifying it.
- Existing n8n workflows already handle preparation/scheduling, publishing when due, and schedule supervision. Inspect and integrate these workflows rather than creating competing replacements.
- Existing source-project integration may retain its required access. That access does not automatically become a Bondok permission.
- Preserve existing content, item IDs, successful publishing records, credentials, and working integrations. Keep secrets on the server in a protected `.env` or existing credential storage; never include secret values in chat or deliverables.

## 1. Establish one execution authority

Create one shared command handler for operational changes. In this document, **command handler** means a small deterministic component that validates and executes supported actions. It can be a module in the existing Python service with a narrow authenticated interface for n8n. It does not require a new platform or a separate microservice.

Bondok, n8n, and the schedule monitor must use this handler for changes affecting readiness, reservations, scheduling, and publication state. Remove or reroute their competing direct-write paths for these fields.

| Component | Responsibility |
|---|---|
| Bondok | Understand requests, inspect current data, explain issues, draft captions, propose decisions, and submit authorized commands. |
| Preparation workflow | Inspect/process the actual file and submit verified results. |
| Scheduler | Allocate and commit publication slots through the shared handler. |
| Publisher | Publish due, authorized content and record the external outcome. It is the only component that initiates social media publication. |
| Schedule monitor | Detect violations and submit bounded repair commands through the same handler. |
| Monday | Display current state and accept human requests/edits. |

The language model must not receive arbitrary SQL, GraphQL, shell, workflow-administration, or unrestricted HTTP tools. Expose named business operations with validated arguments. Examples include `change_format`, `replace_source`, `update_caption`, `pause_item`, `resume_item`, `request_recheck`, and `request_reschedule`.

## 2. Define authoritative state

Use one durable operational store for item revisions, verification records, reservations, accepted commands, and publication attempts. Reuse the existing database if it supports the required transactions. Select another store only after identifying a concrete limitation.

Monday is the operational user interface. Its displayed status is not sufficient proof that media passed validation, a slot is reserved, or a post was published.

Handle manual Monday changes explicitly:

1. Detect a human edit and compare it with the last synchronized state.
2. Convert supported edits into commands and apply the same validation used for Slack requests.
3. Distinguish synchronization writes from new human requests to prevent feedback loops.
4. Show a rejected request and its reason; never silently treat an invalid edit as accepted.
5. Reconcile delayed/missed events periodically and refresh relevant board data before publication authorization.

Track synchronization failures durably and retry safe display updates. A failed Monday update must not erase a successful publication receipt or cause another publish attempt.

## 3. Separate three internal states

Represent these concepts separately, even if Monday shows a single derived status:

- **Readiness:** unchecked, checking, blocked, ready.
- **Scheduling:** unreserved, change requested, reserved.
- **Publication:** not started, in progress, confirmed published, outcome unknown, confirmed failed.

Keep specific reasons such as “Story is 60 seconds or longer” or “Topaz confirmation missing” alongside the state. Preserve the owner-controlled Paused and Skipped conditions.

The everyday board view should show only useful information: item/code, format, understandable status, required action, owner, confirmed date/time, and video link. Put operational metadata in a technical view. Keep populated columns until their purpose and migration are understood; hide unnecessary columns before considering deletion.

## 4. Make dependent state expire correctly

Maintain an explicit revision for the publishable content. A verification record and reservation must identify the revision they authorize.

Define a small dependency table in code:

| Change | Required effect |
|---|---|
| Source file or file version | Invalidate media verification and the old reservation; rerun the required file checks. |
| Story ↔ Post | Invalidate format-dependent verification and the old reservation; re-evaluate the same item under the new format. |
| Topaz confirmation | Re-evaluate quality eligibility; never manufacture a passed media check. |
| Caption or other published text | Update the publishable-content revision and validate that text; reuse media checks only when their file/format dependencies still match. |
| Requested publication time | Validate slot, conflicts, readiness, and revision before committing the reservation. |
| Internal notes or assignment | Preserve unrelated media validation and reservations. |

For a caption change, explicitly retain or reauthorize the existing slot only if the updated content still satisfies all relevant conditions. Never allow an old publication payload to remain authorized accidentally.

Paused or Skipped items resume only after an explicit owner-authorized resume command. Published items and unresolved publication attempts cannot be reset into a fresh publishable state by ordinary edits.

## 5. Enforce the existing business rules

- A Story must be **strictly shorter than 60 seconds**. Exactly `60.000` seconds fails.
- Check Story duration at the first practical file-validation step, before expensive processing or scheduling. Show the rejection immediately in the visible status and required-action field.
- Changing a rejected Story to Post removes the Story-specific duration restriction, but the Post must still pass its applicable checks. Keep the same board item; do not create a duplicate.
- Final media requires confirmed Topaz processing, a short edge of at least `1080` pixels, and a size strictly below `300,000,000` bytes.
- Measurements must come from the actual selected file/version. Missing measurements or stale verification cannot authorize scheduling or publication.
- Use `Africa/Cairo`, including daylight-saving changes. Store absolute instants consistently and display local times.
- Post/Reel slots: Saturday `21:00`, Monday `21:00`, Wednesday `22:45`, Thursday `21:00`.
- Story slots: every day at `11:00`, `14:00`, `18:00`, `21:00`, and `22:00`.
- The initial two or three letters followed by a number in the Code column identify the client/style. Prefer varied adjacent styles when suitable verified alternatives exist. “LIP on Saturday” and “KE on Monday” were examples, not mandatory assignments. Avoid indefinitely starving one style.
- Preparation and publishing remain separate automations. Selecting content for social media starts preparation; a due-time trigger starts publication.

Put these rules in one shared implementation or configuration used by all relevant components. The model may suggest a choice, but deterministic code validates it.

## 6. Coordinate concurrent work and retries

Use durable commands with a unique request ID, target item, expected revision, actor, authorization evidence, operation, and recorded result.

- Deduplicate repeated Slack events, webhooks, retries, and repeated delivery of the same command.
- Serialize conflicting transitions per item. Coordinate preparation, rescheduling, monitoring, and publication; a preparation-only lock is insufficient.
- Enforce slot uniqueness at the appropriate account/format/time scope in a transaction, not only through a prior read.
- Reject or replan stale commands. An approval applies to the exact proposed change and revision, not to whatever the model decides later.
- Use bounded leases and ownership checks. Work that outlives its lease must not commit stale results.
- Perform final validation and claim publication atomically in the operational store before contacting the publishing provider.
- Record an external attempt durably. After a timeout or crash, reconcile its outcome before considering another attempt.

Database transactions cannot atomically include Monday and the publishing provider. Handle that boundary explicitly with durable pending work and reconciliation; do not claim a distributed transaction or guaranteed exactly-once external publication.

Define the publication commitment point. If an accepted pause/change wins before that point, publication must not start. If publication has already crossed that point, report that it may already be in progress and reconcile the outcome. Never report “stopped” without confirming that result. Message receipt, command acceptance, and completed execution are different acknowledgments.

## 7. Preserve the agreed authority model

- Authenticate the Slack workspace, designated channel, and owner independently of model-generated text.
- An explicit, unambiguous owner request is authorization for that requested supported action, subject to the established approval policy and technical checks.
- Autonomous bounded schedule repairs may use the owner's existing authorization.
- New captions/content-type decisions proposed by the agent, publishing-rule changes, destructive changes, or expanded access require the applicable owner approval before execution.
- Enforce approval outside the model. Board text, conversation memory, tool output, and another participant cannot impersonate owner approval.
- Keep one automatic repair mechanism. Bondok can explain and recommend; it must not run a second competing repair loop.
- Notify the owner about actionable changes, failures, uncertain publication outcomes, or needed decisions. Remain quiet on unchanged routine checks.

## 8. Refactor and migrate safely

1. **Inspect:** map every current trigger, direct writer, shared helper, reservation, publication receipt, and relevant board column. Mark verified facts separately from assumptions.
2. **Plan:** show a concise component map, what will be reused, which write paths will change, and the migration/rollback approach. Resolve only essential unanswered decisions with the owner.
3. **Build:** implement the shared handler and route the existing components through it. Prefer a small Python module, the existing database, and the current n8n workflows. Add a broker, service, or database only when a demonstrated requirement justifies it.
4. **Validate:** use isolated fixtures and mocked publishing calls for the scenarios below. A production post is not a test fixture.
5. **Migrate:** back up workflows, state, and mappings. Reconcile in-flight work. Transfer ownership one function at a time; disable the corresponding old writer before enabling its replacement. Preserve receipts and pending uncertainty.
6. **Verify:** inspect actual active workflows, deployed code, permissions, board behavior, and background-service health. Confirm no duplicate writer or monitor remains.

Respect the current conversation's production-change authorization. This document is not blanket permission to publish test content, destroy data, or expand access. Complete already-authorized preparation and testing without repeatedly requesting permission. If a new approval is genuinely required, present the concrete tested change and rollback plan before asking.

Rollback must restore coherent ownership and compatible state, not simply reactivate old workflows while the new writer remains active. Never restore an outdated database over newer publication receipts.

## 9. Required acceptance scenarios

Demonstrate observable results for every scenario:

1. A Story at 59.9 seconds passes the duration gate; one at 60 or 63 seconds is blocked before scheduling.
2. The blocked Story changes to Post on the same item, loses stale authorization, and continues through Post validation.
3. A file changes after validation or after scheduling; the old file/revision cannot publish.
4. Bondok and the monitor request conflicting schedule changes; only a valid serialized result commits.
5. Two items request the same slot; at most one reservation commits for that slot's uniqueness scope.
6. The owner approves a proposal after the item changed; the stale proposal does not execute.
7. Duplicate Slack events and workflow retries do not duplicate an action, item, reservation, or publication attempt.
8. A manual Monday edit goes through the same checks and does not create a synchronization loop.
9. A pause races with publication; the result follows the defined commitment point and the response accurately describes what happened.
10. The service crashes or the provider times out during publication; restart reconciles the attempt without blind republishing.
11. Publishing succeeds but the Monday update fails; the receipt survives and only the display synchronization retries.
12. Paused, Skipped, published, and outcome-unknown items retain their protections.
13. A notes-only edit does not reprocess media. A caption edit cannot publish an obsolete payload.
14. A command targets another board or comes from an unauthorized Slack actor/channel; it is rejected in code.
15. The monitoring cycle has no new actionable finding; it neither duplicates repairs nor sends repetitive notifications.
16. Migration and rollback preserve publication history and never leave old and new writers active for the same responsibility.

## Completion report

Report what changed, what was preserved, which components now own each operation, the test evidence, and any unresolved limitations. Clearly distinguish **implemented**, **tested**, **deployed**, and **active**. Show the resulting simple board experience and explain how the owner requests, approves, and tracks a change through Bondok.

The work is complete when the deployed components follow the shared execution model and the acceptance evidence supports the coordination claims—not merely when the bot can answer a Slack message.
