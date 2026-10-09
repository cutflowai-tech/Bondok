# Monday and Time Contract

## Time
* Canonical storage: absolute UTC instant, seconds precision, `YYYY-MM-DDTHH:MM:SSZ` (`rules.iso`). All comparisons use this form, so `21:00+03:00` and `18:00Z` are the same slot.
* Zone: `Africa/Cairo` via tzdata (DST handled). Non-existent or ambiguous Cairo wall times (clock changes) are **refused**, never silently shifted.
* Board input:
  * **Publish at** (`date_mm7y8s9t`, date+time): Monday stores the value in UTC and shows it in the viewer's timezone (account zone `Africa/Cairo`, verified via API `me.time_zone_identifier`). Humans type Cairo time in the UI; the API value is UTC. Read as UTC.
  * **Legacy Post Date / Post Time** (`date4`, `hour_mm7xy9cf`): timezone-less; interpreted as Cairo wall time. Used only when Publish at is empty. A cleared Publish at is never replaced by a stale legacy time.
* Display: the handler writes the confirmed slot to Publish at (UTC value) **and** the legacy pair (Cairo wall time) for compatibility, plus "Confirmed slot … (Cairo)" in System update.
* Requested vs confirmed: `ops_items.requested_at` (+ `requested_by`) is a request; `ops_reservations` is the confirmed slot. The board shows the confirmed slot; a rejected request is reverted on the board with the reason in "Action required".
* Migration: no blanket offset is applied to historical rows. Future legacy dates are imported as unpinned preferences (`legacy-board`); invalid/occupied ones are dropped quietly to automatic allocation.

## Columns
Human-owned (never overwritten from snapshots; written only to apply an authorized command): Format, Caption, Code,
Story variety, IG Colab, Social owner, Notes, Topazed.
System display (projection of committed state): Status, Action required, System update, Publish at, Post Date/Time,
Verified media ID, Publish video, Video measurements, Processed format, Source asset version, Version Check,
Dropbox Link, Folder Link, Source item ID, Style, Published at, Instagram media ID, Post Link, Last checked.
Untouched: Numbers (`numeric_mm7xade9`) and every column not listed.

## Status derivation (display only)
Posted (confirmed publication or legacy Posted) → Publishing (attempt claimed/container/committed) → Needs Review
(outcome unknown, or owner hold) → Skipped → Paused → ستوري طويل (Story ≥ 60 s) / Waiting for Editor (file, Topaz,
quality) / Needs Review (config) → جاري فحص الفيديو (checking) → Scheduled (confirmed reservation) → Redy For Scheduled
(ready; action explains: caption approval, requested slot occupied). Labels are reused unchanged. Group moves follow:
Posted → `group_title`, Skipped → `group_mm7y8mkr`, otherwise Post → `topics`, Story → `group_mm7xagm`.

## Human edits → commands
| Board edit | Effect |
|---|---|
| Status → Paused / Skipped | pause / skip (protective, accepted) |
| Status from Paused/Skipped → anything | resume **proposal** in Slack (no attribution on the board) |
| Status → Posted, or Post Link / IG media ID typed | hold "external post?" + Slack question; publication blocked |
| Other status changes | reverted (status is derived) |
| Publish at / Post Date / Post Time | request_reschedule (validated; occupied → proposal; invalid → revert + reason) |
| Publish at cleared | release reservation + hold "unscheduled" until the owner asks |
| Format | hold + Slack approval proposal (owner-controlled) |
| Caption | new content revision, approved human caption, reservation re-authorized |
| Topazed | bound to the Source asset version shown on the board at the time |
| Dropbox Link / Folder Link | replace_source / set_folder (recheck) |
| Notes, Social owner | stored; no reprocessing |
| Story variety / Code | stored; affects variety ordering only |
| IG Colab | blocks for review (publisher cannot add collaborators) |

Our own writes are recognised because the observed value equals the confirmed or pending projection; they never loop back as requests. Edit commands are idempotent per (item, column, item version, value).

## Latency boundary
An edit on the board is not an accepted command until the next observation (WF1 every 10 min for the whole board; WF2 re-reads an item right before claiming it). A pause typed on the board seconds before a slot is honoured if WF2's fresh read sees it before the commitment point; otherwise publication may already be in progress and the reply says so.
