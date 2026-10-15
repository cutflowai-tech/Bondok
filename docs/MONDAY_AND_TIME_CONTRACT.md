# Monday and Time Contract

Owner policy for board edits: `OWNER_AUTHORITY_CONTRACT.md` (wins where this file is older). Monday is the
authoritative business-state database (owner decision 2026-10-12).

## Time
* Canonical storage: absolute UTC instant, seconds precision, `YYYY-MM-DDTHH:MM:SSZ` (`rules.iso`). All comparisons use this form, so `21:00+03:00` and `18:00Z` are the same slot.
* Zone: `Africa/Cairo` via tzdata (DST handled). Non-existent or ambiguous Cairo wall times (clock changes) are **refused**, never silently shifted.
* Board input:
  * **Publish at** (`date_mm7y8s9t`, date+time): Monday stores the value in UTC and shows it in the viewer's timezone (account zone `Africa/Cairo`, verified via API `me.time_zone_identifier`). Humans type Cairo time in the UI; the API value is UTC. Read as UTC.
  * **Legacy Post Date / Post Time** (`date4`, `hour_mm7xy9cf`): timezone-less; interpreted as Cairo wall time. Used only when Publish at is empty. A cleared Publish at is never replaced by a stale legacy time.
* Display: the handler writes the confirmed slot to Publish at (UTC value) **and** the legacy pair (Cairo wall time) for compatibility, plus "Confirmed slot … (Cairo)" in System update.
* Requested vs confirmed: `ops_items.requested_at` (+ `requested_by`) is a request; `ops_reservations` is the confirmed slot. The board shows the confirmed slot, else the owner's requested time (also while it cannot be reserved yet, and a passed one next to the missed-time question); a rejected request is reverted on the board with the reason in "Action required", shown next to any Action already there.
* Owner times may be off the slot grid (owner decision 2026-10-12); the grid is used for automatic allocation only.
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
(outcome unknown, or owner hold) → Skipped → Paused → ستوري طويل (Story > 65 s; 60–65 s is trimmed automatically, owner
decision 2026-10-12) / Waiting for Editor (file, Topaz,
quality) / Needs Review (config) → جاري فحص الفيديو (checking) → Scheduled (confirmed reservation) → Redy For Scheduled
(ready; action explains: caption approval, requested slot occupied). Labels are reused unchanged. Group moves follow:
Posted → `group_title`, Skipped → `group_mm7y8mkr`, otherwise Post → `topics`, Story → `group_mm7xagm`.

## Human edits → commands
| Board edit | Effect |
|---|---|
| Status → Paused / Skipped | pause / skip (protective, accepted) |
| Status from Paused/Skipped → anything | resume (the owner's board change is the decision; contract §3) |
| Status → Posted, or Post Link / IG media ID typed | owner-reported publication: terminal, no automatic publication |
| Status → Scheduled | order to publish: at the board time; no time at all → next free slot; date-only / half legacy pair → incomplete request; passed or < 1 min → late inside the window, else missed request + one question (contract §2, §5) |
| Other status changes | reverted with the reason (status is derived) |
| Publish at / Post Date / Post Time | request_reschedule (owner time, on or off the grid; occupied → proposal; invalid → revert + reason) |
| Publish at cleared | release reservation + hold "unscheduled" until the owner asks |
| Format | change_format: the owner's change is the authorization; same item rechecked (contract §3) |
| Caption | new content revision, approved human caption, reservation re-authorized |
| Topazed | bound to the Source asset version shown on the board at the time |
| Dropbox Link / Folder Link | replace_source / set_folder (recheck) |
| Notes, Social owner | stored; no reprocessing |
| Story variety / Code | stored; affects variety ordering only |
| IG Colab | blocks for review (publisher cannot add collaborators) |

Our own writes are recognised because the observed value equals the confirmed or pending projection; they never loop back as requests. Edit commands are idempotent per (item, column, item version, edit counter, value) and carry their board provenance (column, value), which the handler checks (contract §1).

## Latency boundary
An edit on the board is not an accepted command until the next observation (WF1 every 10 min for the whole board; WF2 re-reads an item right before claiming it). After the claim, the publication intent is written on Monday with a compare-before-write on Status immediately before Instagram is asked to publish (ADR decision 4): a Pause typed after the fresh read, also into an empty Status cell, refuses that write and nothing is published. The remaining window is the ~1 s between the guard read and the write (Monday has no compare-and-set) plus the publish call itself; after that the reply says publication may already be in progress.
