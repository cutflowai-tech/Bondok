# Owner authority contract (R5.2)

Executable rules for what the owner's Monday edits and Slack instructions do. Tests: `tests/test_r5_*.py`
(one class per rule, named after the R5 entry it closes). Supersedes the "unattributed board edit becomes a
proposal" rule of `ARCHITECTURE_AND_OWNERSHIP.md`.

## 1. Who is the owner

* **Monday.** Only the company owner edits the For Social Media board as a person. The automation uses the same
  Monday user (verified 2026-10-10: every activity-log entry, including WF2 display writes and agent writes, has
  one user id, and no source/app field exists). So a change is classified by *who did not write it*:
  * **automation write**: a value the handler queued, WF2 applied and acknowledged (outbox receipt);
  * **owner decision**: any other change to an owner column, observed in a snapshot that was read after
    every earlier automation write to that column was acknowledged (causal order, not a time window).
  Snapshot columns with an automation write acknowledged after the snapshot read started are ambiguous for that
  item and are re-read on the next cycle (never guessed).
* **Boundary (unresolved, owner action):** another automation using the same Monday token is indistinguishable
  from the owner. Mitigation: no other writer may use the token (inventory in `HANDOFF_STATUS.md`); the fix is
  a distinct integration identity (a separate Monday user or app), which needs the owner. Until then a
  non-automation change to an owner column is treated as the owner's decision. Results of model output, Notes,
  file names and source briefs are data and never stand in for an owner decision.
* **Slack.** The trusted adapter supplies the authenticated owner id. The model proposes structured meaning
  (speech act, polarity, targets, constraints, an evidence quote from the owner's own message); code decides
  whether it is an executable instruction (§4). The model never supplies authorization.

## 2. Board columns

| Column | Owner meaning | Automation writes |
|---|---|---|
| Status | `Paused`, `Skipped`, `Posted` are owner controls. Changing a Paused/Skipped item to any other label resumes it. | System labels only (Scheduled, Publishing, Waiting for Editor, ...), never over an owner value it has not observed (compare-before-write, including an empty cell) |
| Publish at | Requested publication time (Cairo display), on or off the slot grid | The confirmed slot; never over a newer owner value |
| Post Date / Post Time | Legacy request (used only when Publish at is empty) | Mirror of the confirmed slot |
| Topazed | `Topazed` = Topaz done for the file shown in *Source asset version* at the time of the change | `Not yet` when the confirmed file is no longer the selected one; `Topazed` to mirror a Slack confirmation |
| Format | Story/Post. An owner change *is* the authorization; the same item is rechecked under the new rules | Mirror of a Slack instruction |
| Caption | The owner's caption. An owner edit is approved text for that item | Mirror of an owner-approved Slack caption or draft |
| Dropbox Link | An owner-entered link = "use this file" (owner_selected_file); it outranks the project folder | The selected file link |
| Post Link / Instagram media ID | Owner-entered = owner-reported publication | Publication evidence |
| Code, Notes, Story variety, IG Colab, Social owner, Folder Link | Owner data | Never (Folder Link: only a folder it created when empty) |
| Action required, System update, Source asset version, Verified media ID, Publish video, Video measurements, Processed format, Version Check, Style, Source item ID, Published at, Last checked | Read-only results | Yes |

## 3. Owner decisions and their effect (board and Slack are equivalent)

| Decision | Effect | Never |
|---|---|---|
| Pause / Skip | Effective immediately before the commitment point; reservation released; owner time kept as the request. After commitment: "may already be in progress", reconciled | Silently claimed "stopped" |
| Resume (board label change away from Paused/Skipped, or Slack) | Back to normal checks and scheduling; owner constraints in the same instruction (e.g. "tomorrow") are kept | Clears terminal publication protection |
| Posted / typed post link / "I posted it" | Terminal: no automatic publication of the item or of the same video (same account and format) on any item; recorded as owner-reported, evidence reconciled later | Becomes a Resume proposal; requires a second confirmation |
| Topaz done (completed statement or board label) | Bound to the exact file version shown; a repeat for the same version is idempotent | Inferred from a future/question/conditional statement, a file name, or "ready" |
| Requested time (board or Slack) | Owner time, on or off the grid, if in the future: reserved when ready; kept visible while not reservable. Occupied slot: option B (§5) | Disappears; is moved to "earliest free" silently |
| Clear Publish at | Cancels the owner's time request; the item stays unscheduled (visible, notified) until a new time or "publish" | Recreated automatically |
| Format change | Same item rechecked under the new format; reservation released; undo before recheck restores | Duplicate item; conversion by the system on its own |
| Caption text / caption approval | The exact text is approved for that item | Approved by "thanks", a question or a plan |
| Use this file (Dropbox Link or Slack) | owner_selected_file: that exact file is prepared, not the folder's newest | Re-selecting the rejected file |
| Correct Code / link / caption / Topaz order | Clears only the rejection the earlier value caused | Leaves a stale rejected-edit hold |

## 4. Slack language

* Executable without a further question: an imperative or completed statement, positive, about named or
  unambiguously referenced items, whose evidence quote appears in the owner's message.
* Not executable (no effect): questions, plans/future ("will be done tomorrow"), predictions, conditionals
  ("if Topaz is done, prepare it" — kept only as a traceable condition), quotations, acknowledgments
  ("تمام شكرا"), missed-expectation remarks ("should have been posted yesterday"), other-destination reports.
* Unclear meaning, scope or target: one narrow question; nothing else changes. "اه"/"yes"/a button answers only
  the single open question in that thread. A vague "stop" does not pause the whole board.
* Targets come from the owner's words and the thread's named set ("them" = the items named in the thread),
  never from extra items the model or board Notes add.
* Constraints travel with the action: "tomorrow", "not before tomorrow", "at 4:17", "nothing more".
* Arabic-Indic digits, ى/ي, ة/ه, zero-width/RLM, Slack markup and emoji are normalised for matching only;
  stored captions are decoded from Slack markup (`&amp;`, `<url|text>`) and validated like board captions.

## 5. Scheduling

* Default grid (automatic allocation only): Story 11:00, 14:00, 18:00, 21:00, 22:00 daily; Post Sat 21:00,
  Mon 21:00, Wed 22:45, Thu 21:00 (Africa/Cairo). An owner time may be off-grid.
* Owner requests are protected: automatic allocation never takes a slot requested by the owner for an item that
  is still preparing.
* Option B: an occupied slot is never taken without one concrete approval naming every affected item and its
  resulting time; a true swap exchanges the two times; nothing else moves.
* A missed owner time is closed with its cause; the item is not left "Scheduled" in the past. If the owner gave
  no fallback, one question asks for a new time; "publish it" means the earliest free valid slot.
* Late publication: at most 2 hours after the slot and never at or after the next reserved slot of the same
  format.
* The supervisor's near-due protection restricts automation only. The owner may move or cancel an uncommitted
  item at any time, with a truthful "may already be in progress" once the commitment point is crossed.

## 6. Media and Topaz

* Story source < 60.000 s: no trim. 60.000–65.000 s inclusive: end trim to ~59.9 s in preparation, output
  verified < 60 s, original untouched, no approval. > 65.000 s: shorter edit requested. Unmeasurable: not
  eligible, exact cause.
* A trimmed or downscaled derivative inherits Topaz only from its exact confirmed source.
* Publication binds the delivered Dropbox copy (what Instagram fetches); a missing local cache is not a lost
  delivery.

## 7. Approvals and holds

* Approvals bind to their dependencies (caption text, file version, format, slots), not to a global version.
* A proposal's interface may expire; the decision it carries does not strand the item: asking again re-issues
  the same proposal; an expired proposal reaches a terminal state and its hold ends.
* Every hold has a reason code, origin, recovery condition and responsible party, and is notified once.
