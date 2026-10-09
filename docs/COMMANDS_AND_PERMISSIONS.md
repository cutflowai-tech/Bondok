# Commands and Permissions

Every operational change is a durable command (`ops_commands`): stable id, op, item, expected version (optional),
actor, actor kind, authorization reference, recorded outcome. Same id + same payload returns the recorded result;
same id + different payload is rejected (`id_reuse`). Outcomes: `received`, `accepted` (will happen later),
`awaiting_approval`, `completed`, `rejected`, `failed`. "Accepted" is never reported as done.

Actor kinds are assigned by trusted adapters only: `owner` (authenticated Slack owner), `member` (other channel members),
`monday` (unattributed board edit), `bondok` (the assistant itself), `service:wf1|wf2|wf3|bondok`.

| Operation | Allowed actor kinds | Extra condition |
|---|---|---|
| pause, skip | owner, monday | protective; after the commitment point the reply says "may already be in progress" |
| resume | owner | Slack: explicit words or approval; board: becomes a proposal |
| change_format | owner | **explicit owner words** (deterministic check, direction-aware) or approved proposal; board edit → hold + proposal |
| update_caption | owner, monday | owner text verbatim or approval; board caption edits are treated as human-authored |
| confirm_topaz | owner, monday | bound to the selected file version the person could see; Slack needs explicit words |
| replace_source, set_folder | owner, monday | invalidates media authorization and reservation |
| request_reschedule | owner, monday | grid/future/horizon validation; occupied slot → proposal; invalid → alternatives |
| request_publish | owner | earliest free valid slot through WF2; never a direct publish |
| request_recheck | owner, monday, bondok, wf3 | returns `accepted` or `already_running` |
| resolve_outcome | owner | manual Instagram verification of unknown/failed attempts |
| approve_proposal / reject_proposal | owner | same thread; 30-minute expiry; bound item versions and slots |
| set_notes, set_variety, set_code, set_collab | owner, monday | notes/variety never reprocess media; collab blocks for review |
| prep_source/preflight/media/delivered | service:wf1 | fenced by WF1 run id/fence |
| caption_draft | service:wf1, service:bondok | stored by input hash; creates approval proposal |
| hold | services, owner | protective; releases reservation |
| repair_release, repair_reauthorize | service:wf3 | never near-due, in-flight, unknown, published, paused or skipped |
| source_canceled | service:wf1 | protected states kept |

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
