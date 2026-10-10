# Isolated WF1/WF2 replay — results (2026-10-09)

Purpose: validate WF1 before re-activation against a realistic, isolated copy of production
(owner request after the cutover incident). Raw evidence is local only (`.local-snapshots/`, gitignored).

## Environment
* Real n8n 2.39.8 (same version as production), running locally; WF1 and WF2 built from this branch.
  Only replay-copy changes: URLs point at a local mock, fake credentials, a Manual Trigger wired like the
  schedule trigger (the CLI cannot start schedule-only workflows).
* Data: production database copy (SQLite backup API) and a full read of the social board, both 19:14 UTC;
  projects board (1,348 items), the 72 editor subitems with their real statuses and updates.
  Dropbox: real responses recorded by runs 52060/52081 plus a read-only listing of the 65 never-processed
  folders/file links.
* Mock: Monday GraphQL (column semantics, label and group validation, all-or-nothing writes), Dropbox
  (links, listings, share creation, uploads, revisions, fault injection), caption model, Instagram
  (container create/status allowed, `media_publish`/permalink = violation, refused).
* Helper: candidate code with a simulated clock (10 minutes per cycle, production cadence) and local test
  videos instead of downloads. Every cycle = WF1 once, wait for media jobs, WF2 twice.

## Run A — three pure cycles (no injected events)
| | cycle 1 | cycle 2 | cycle 3 |
|---|---|---|---|
| items checked (all different) | 20 | 20 | 20 |
| board values cleared | 0 | 0 | 0 |
| group moves / items created or deleted | 0 | 0 | 0 |
| duplicate editor subitems | 0 | 0 | 0 |
| editor instructions posted | 0 (all already on the subitem) | 3 (new subitems) | 1 (new subitem) |
| Instagram calls / Slack notifications | 0 / 0 | 0 / 0 | 0 / 0 |
| Monday / Dropbox / mock errors | 0 | 0 | 0 |

Board writes are status/action/selected-file values for the items checked that cycle. Expected v2
overwrites of v1 text: the v1 "Dropbox" link column gets the link of the file v2 selected (sometimes
replacing a folder link) and v1 "Version Check" text gets v2's file/revision. Story items show
"Checking video" once while v2 measures the real file, then return to the correct state.

## Run B — seven cycles with scenarios
1. Owner marks an item Topazed on the board; Dropbox 503 for one item; revoked folder link for another.
2. Faults cleared. 3. — 4. Editor replaces the scheduled item's file in Dropbox. 5. —
6. Clock jumps to the item's slot (WF2 due). 7. Ten minutes later.

| Check | Result |
|---|---|
| Owner Topaz confirmation bound to the shown file version, picked up the same cycle | pass |
| Verified 1080p media uploaded, linked, item ready, reservation 11:00 Cairo, board "Scheduled" | pass (cycle 2) |
| Dropbox 503: temporary issue, content not rejected, recovered automatically | pass |
| Revoked link: readable message, item blocked (config), no editor spam | pass |
| Replaced file at publish time: commit refused, nothing published, authorization dropped, one owner note, no further claims | pass (cycles 6–7) |
| Cleared board values | only v2's own outdated values (stale message, resolved issue note, withdrawn slot/publish-at/prepared video) |
| `media_publish` / permalink calls | 0 |

## Defects found and fixed during the replay (all with regression tests, 110 passing)
| # | Defect | Severity |
|---|---|---|
| 1 | Share bodies ended in `}}` inside `{{ }}`: n8n "invalid syntax", new Dropbox links never created (v1 bug) | critical |
| 2 | Internal errors classified as content problems (blocked items) | high |
| 3 | Source/share problem blocks never cleared after the file resolved | high |
| 4 | Caption draft approvals bound to an item version the system bumped itself: approval impossible | critical (approvals) |
| 5 | Same editor instruction re-posted to subitems that already had it (15 subitems in production) | medium |
| 6 | Preparation queue re-picked the same 20 items; others never checked | high |
| 7 | Unchanged problem re-applied every cycle (version bump, writes) | medium |
| 8 | Editor jobs for items without a projects item never completed (re-taken forever) | medium |
| 9 | v2 messages written before ownership tracking could never be cleared (stale Action texts) | medium |
| 10 | Temporary-issue items waited behind every other item | low |
| 11 | Raw Dropbox JSON shown as Action text | low |
| 12 | Commit refused a replaced file but kept the reservation: a new Instagram container every minute | critical (publishing) |

## Follow-up (not implemented, not blocking)
* Pre-claim source-revision check in WF2 (avoid creating one Instagram container before the commit refusal).
* When v2 clears its own value in a column that held restored v1 text, the v1 text is not brought back.
* Four file-only links on the board have audience "no one"; a public download of them may fail.
* Caption proposals expire after 30 minutes; WF1 drafts often reach the owner later (owner decision).
* Detecting a replaced file for an already scheduled item happens at the commit point; earlier
  detection (WF3 or a cheaper revisit) would release the slot sooner.
* Replay harness limits: mock Dropbox cannot know links created outside the recorded data, so some
  items receive a new share link where production may reuse an existing one (same file, harmless).

## Round 5 replay on the 2026-10-10 production snapshot (local copy, read-only inputs)

Inputs: store copy and board snapshot taken 2026-10-10 09:48 UTC (`.local-snapshots/2026-10-10`, private).
Clock frozen at the snapshot time. Code: `bondok/r5-monday-authority` at 14f2233.

* First observation by the new code: 164 items, 0 imported, 22 edits — 19 "Publish at cleared" (the 00:12 agent
  writes, R5 A16) now notified, 1 owner date accepted as a request, 1 past date rejected with a notice, 1 System
  column edit. Owner facts changed for exactly one item (the accepted request). No reservation, pause, skip,
  publication or Topaz binding was lost.
* The 19 cleared-time notices go to Slack as **one** message (14f2233); before, 19 separate messages.
* First WF3 repair: changes nothing on owner facts. 23 "prepared file missing" findings are a replay artifact
  (the prepared media exist only on the server) and are grouped into one message as well.
* `due`: empty (no publication would start from the replay state).
* Rollback compatibility: the live helper 452da98 run against the database written by the new code — `/v2/health`
  ok (schema 1), `/v2/publish/due`, `/v2/prep/queue`, `/v2/maintenance` all succeed.
* `deploy/rehearse.py` is the v1→v2 cutover rehearsal: its `/v1/...` probes do not apply to a v2→v2 rollout
  (452da98 has no v1 routes); its bootstrap numbers matched the replay (164 items, 15 published, 19 unscheduled).
