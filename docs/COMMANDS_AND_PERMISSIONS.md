# Commands and Permissions

Every operational change is a durable command (`ops_commands`): stable id, op, item, expected version (optional),
actor, actor kind, authorization reference, recorded outcome. Same id + same payload returns the recorded result;
same id + different payload is rejected (`id_reuse`). Outcomes: `received`, `accepted` (will happen later),
`awaiting_approval`, `completed`, `rejected`, `failed`. "Accepted" is never reported as done.

Actor kinds are assigned by trusted adapters only: `owner` (authenticated Slack owner), `member` (other channel members),
`monday` (a board edit, which is the owner's decision; contract §1), `bondok` (the assistant itself),
`service:wf1|wf2|wf3|bondok`. A `monday` command must carry the board observation's provenance (column + value,
stored in `auth_ref`) and is accepted only for an operation that column can request (`CoreMixin.BOARD_COLUMNS`);
`request_publish` only from Status set to Scheduled (release review 2026-10-12).

| Operation | Allowed actor kinds | Extra condition |
|---|---|---|
| pause, skip | owner, monday | protective; after the commitment point the reply says "may already be in progress" |
| resume | owner, monday | Slack: explicit words or approval; board: a label change away from Paused/Skipped (Status column) |
| change_format | owner, monday | Slack: **explicit owner words** (deterministic, direction-aware) or approved proposal; board: the Format edit is the authorization |
| update_caption | owner, monday | owner text verbatim or approval; board caption edits are treated as human-authored |
| confirm_topaz | owner, monday | bound to the selected file version the person could see; Slack needs explicit words |
| replace_source, set_folder | owner, monday | invalidates media authorization and reservation |
| request_reschedule | owner, monday | owner time on or off the grid, future/horizon validation; occupied slot → proposal; a Scheduled order's passed time → late window or missed request |
| request_publish | owner, monday | earliest free valid slot through WF2; never a direct publish; board: Status → Scheduled with no time |
| request_recheck | owner, monday, bondok, wf3 | returns `accepted` or `already_running` |
| report_published | owner, monday | board Posted / typed post link or media id, Slack "I posted it": terminal |
| resolve_outcome | owner | manual Instagram verification of unknown/failed attempts |
| propose_legacy_captions | owner | one batch approval request for imported captions; nothing changes until approved |
| approve_proposal / reject_proposal | owner | same thread; 30-minute expiry; bound item versions and slots |
| set_notes, set_variety, set_code, set_collab | owner, monday | notes/variety never reprocess media; collab blocks for review |
| prep_source/preflight/media/delivered | service:wf1 | fenced by WF1 run id/fence |
| caption_draft | service:wf1, service:bondok | stored by input hash; creates approval proposal |
| hold | services, owner | protective; releases reservation |
| repair_release, repair_reauthorize | service:wf3 | never near-due, in-flight, unknown, published, paused or skipped |
| source_canceled | service:wf1 | protected states kept |

Publication (service:wf2, fenced by attempt + worker + fence, not commands): `claim` → `container` → `commit`
(checks; returns the Monday intent write) → `intent_ack` (only an acknowledged intent reaches `committed`) →
`result`. ADR decision 4.

Documented exception to "no direct writers": `deploy/rehearse.py` inserts synthetic attempts/media/reservations
into **isolated copies** of a snapshot to prove the rollback receipt guard; it never runs against the live store.

## Bondok tools (model-visible)
Reads: `find_items`, `get_item_status`, `get_schedule`, `get_system_health`, `get_operation_status`.
Requests: `pause_item`, `resume_item`, `skip_item`, `request_recheck`, `request_reschedule` (Cairo `YYYY-MM-DD HH:MM`),
`request_publish`, `change_format`, `replace_source`, `update_caption`, `approve_existing_caption`,
`approve_all_existing_captions`, `draft_caption`, `confirm_topaz`, `resolve_publication`.
Not available: SQL, GraphQL, shell, arbitrary HTTP, workflow execution, n8n administration, other boards.

Deterministic (no model): `اعتمد B-XXXXXXXX` / `approve B-…`, `ارفض B-…` / `reject B-…`, notifications, watchdog.
If the model is unavailable, nothing is guessed or executed; approvals and automation continue.

## Approval binding
A proposal stores kind, payload, payload hash, bound items with their `version` and current reservation slot, creator,
thread, expiry. Approval re-checks every binding; any change → `stale` (not executed). Executed/rejected proposals
cannot run again (`decided`).
