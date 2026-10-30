# Team publishing cutover — pending deployment

This replaces only the publishing source, not the historical journal. The Python runtime owns scheduling;
archived n8n publishers remain archived and the independent caption workflow remains unchanged. Employee
preparation stays local. No new Monday columns are required.

## Configuration and behavior

Set `TEAM_PUBLISH_BOARD_ID` in the private runtime environment to select `TeamRunner` / `TeamOps`.
Leave import/preparation settings unchanged: Team mode does not invoke those cycles or the caption writer.
`INSTAGRAM_ACCOUNT_ID` must equal the account injected into this release by `deploy/build_release.py`.
Validate that account is the intended company profile before activation. Never commit the private environment.

Team keys are `board_id:item_id`; old numeric keys and receipts remain unchanged. `ops_team` is an additive
table. `ops_meta.publishing_source=team` permits only Team claims/commits; `team_paused` refuses all new
claims/commits while result recording and reconciliation continue. Removing the environment switch alone
cannot restore old publication authority because the database guard remains. Do not roll back to a binary
that predates that guard.

The runtime polls every minute. The first observation of an already-ready item is not approval. An observed
transition into Ready to Publish binds Code, Format, Caption, Collab, Ready File, Topaz and 1080 approval.
An unchanged approved item can move time without another visual review. A file revision or content change
revokes approval. Clearing time or leaving Ready cancels precommit reservations. After media_publish has
started, cancellation is not guaranteed. A status toggle between two polls is not observed: let In Progress
be observed before selecting Ready again, or wait for the system's In Progress projection.

Monday's date-column API stores date/time in UTC; retain that existing adapter contract. The UI and the
planning instructions use Africa/Cairo with date-specific DST. Do not reinterpret UTC API time as local time.
No automatic scheduling fallback is used. Same-type collisions block the later item; Post and Story may
share a slot with different approved files. A container can be prepared 15 minutes early; commit cannot occur
early and expires after 15 minutes late. Fresh Monday and Dropbox reads are mandatory before commit.

The verified download is anonymous, with Dropbox block content hash, SHA256, id, revision and measured media
metadata retained internally. It bypasses the old 145 MB prepared-upload path. No transcoding, trimming,
uploading or source selection runs on the server. Only one new full-file QA is started per sync cycle;
verified items are checked first. A slow QA does not stop the separate publishing cycle. Changed data is
re-read after QA; superseded sync results are discarded by the run fence.

## Media requirements and reference review

Company gate: 1080×1920 or 1920×1080 display dimensions, SDR H.264 4:2:0 MOV/MP4, 23–60 FPS,
AAC mono/stereo at 48 kHz when audio exists, video bitrate at most 25 Mbps. Silent video is allowed.
Story is 3 to strictly less than 60 seconds and strictly below 100,000,000 bytes. Post is 3 to strictly less
than 900 seconds and below 300,000,000 bytes. The latter duration is conservatively below the API maximum.
Local profiles target 95 MB Story / 290 MB Post, with a 25 Mbps ceiling. A small file may still be incompatible:
the tool skips it by size, and the server returns it for an employee re-export rather than altering it.

Checked 2026-10-31: [Meta's official Postman collection](https://www.postman.com/meta/instagram/documentation/6yqw8pt/instagram-api?entity=request-23987686-3fe78620-2258-44b6-893f-42d76c7200d7)
confirms the Reel codec/container, frame rate, audio sampling, bitrate and duration constraints. It advertises
a larger Reel file allowance; company policy remains below 300 MB. It also limits Stories publishing to
Business accounts. The [IG User Media reference](https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media/)
initially returned HTTP 429 via the browsing tool, but a direct HTTPS retrieval succeeded on 2026-10-31.
It confirms Story 3–60 seconds / 100 MB, and faststart MOV/MP4 without edit lists; these are checked locally.
The company gate remains strictly below 60 seconds and 100 MB. The compressor now disables edit lists.
Progressive scan is checked; provider FINISHED status remains mandatory for remaining codec/GOP compatibility.
No test container or post was created during development.

## Approval gates and commands

Run from the deployed package with the private runtime environment path. Commands below require the
specific owner's activation/containment authorization. Read-only inventory uses a disposable database copy.

```sh
python3 -m waset_runtime.service --env PRIVATE_ENV --team-source inventory
python3 -m waset_runtime.service --env PRIVATE_ENV --dry-run --once sync
python3 -m waset_runtime.service --env PRIVATE_ENV --dry-run --team-source enable --authorized-by DRY_RUN_REFERENCE
```

1. Review and merge the code and operator-package PRs. Confirm production baseline against the release SHA;
   use the existing `deploy/build_release.py` and `deploy/deploy.py` code deployment gates, backup and drain.
   Do not import/activate workflows or run the older n8n rollback procedure.
2. Confirm account identity/Business capability, board labels/columns, and
   UTC date-column behavior against a known Cairo appointment. Inspect next old reservations again immediately
   before the window; reconcile any committed/unknown attempts first. Never deploy over an active publisher.
3. Announce the new meaning of Ready to Publish to the employee. Obtain specific owner approval of the release,
   cancellation inventory and source switch. This document is not that approval.
4. Drain/stop old runtime before changing the source environment. Deploy reviewed code, set Team board config,
   and run the enable command while workers are stopped:

```sh
python3 -m waset_runtime.service --env PRIVATE_ENV --team-source enable --authorized-by OWNER_APPROVAL_REFERENCE
```

Enable refuses committed/unknown or live-leased attempts, cancels old reservations with an audit report, and
revokes all cached Team approvals. On first scan uncached Ready items also require a fresh transition.
Start the runtime after the switch, verify source flag, cadence and readback. Do not move old reservations
into Team automatically. First live acceptance is an actual employee-approved work item, never a test post.

## Monitoring and containment

Check runtime heartbeat/cycle errors, `team_read_failure`, `team_publish_error`, `ops_team.desired_status`,
expired reservations, outbox pending/retry/escalated states, and `ops_attempts` committed/outcome_unknown.
Verify confirmed results reach Monday and Updates retain the actual media/container evidence. The recorded
reconciliation time is not represented as the actual publication time. Receipt Updates have durable markers;
repeated polls and retries do not re-post an Update. Notes use a delimited Bondok section, preserving employee
text outside it. No Slack notification is introduced by this adapter.

Duplicate checks include legacy identity receipts, SHA/content hash and original file revision across formats.
Historical matching Code without verifiable identities blocks review; matching Code within the same format
also blocks conservatively, even if bytes differ. A project may have a Post and a Story with distinct reviewed
files; identical bytes are blocked. No automatic waiver or duplicate-release command is added here.

To contain while keeping receipts and reconciliation live:

```sh
python3 -m waset_runtime.service --env PRIVATE_ENV --team-source pause --authorized-by OWNER_APPROVAL_REFERENCE
```

Do not restore an older database or re-enable the old board. Keep the Team runtime running to reconcile and
flush receipts; pause clears uncommitted Team reservations and approval. A publication request committed just
before pause may still finish; record its actual result. Restarting requires another reviewed enable step and
employee reapproval. Never retry an unknown media_publish result blindly.

## Validation record

The automated suite uses real SQLite Ops with fake providers: early prepare vs due commit, 15-minute expiry,
content/time/status/version edits, initial Ready reapproval, duplicate/collision gates, Post/Story/Collab payloads,
Monday/Dropbox outages, restart lease reuse, unknown outcome reconciliation, and receipt-write retries.
Production-copy dry-run (2026-10-31): 102 Team items, all New; zero active attempts; 48 old reservations.
The simulation canceled 48 reservations, retained 48 historical attempts, 48 publication rows and 81 identity
rows, and produced zero approved Team candidates. External writes were denied by read-only adapters and the
publisher's dry-run gate. The raw snapshot is private and outside this repository. This is not live acceptance.

Production history in the inspected snapshot contains 45 confirmed Story and 3 confirmed Post attempts.
The account-type field is unavailable on this Facebook-login object; historical Story success is evidence of
past capability, not a substitute for checking the current token/account at activation.
