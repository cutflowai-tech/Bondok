# REFACTORING_READINESS

## Verdict: **NOT READY — blocked on access.**

The board and the n8n inventory are understood well enough to plan. The core of the refactor — Bondok's code, its database, the three V2 workflows, the publisher, and Slack — cannot be inspected. Per the brief ("inspect actual code, workflows, database and board before deciding what must change"), implementation cannot safely start.

## Business-rule enforcement status

| Rule | Status | Evidence |
|---|---|---|
| Story < 60.000 s | Partially implemented, **inconsistent** | Owner notes describe a 60–62 s trim band and >62 s skip; brief requires strict <60 s. "ستوري طويل" label exists. Code BLOCKED. |
| Topaz confirmed | Implemented as human status (`Topazed`) | Not provably bound to file revision. |
| Short edge ≥ 1080 | Measured (`Video measurements`) | Enforcement logic BLOCKED. |
| Size < 300,000,000 B | Measured | Enforcement logic BLOCKED. |
| Africa/Cairo slots | Legacy data table: 1 duplicate slot + 2 off-grid rows | VERIFIED |
| Slot uniqueness | **Not enforced** (duplicate exists) | VERIFIED |
| Style rotation | `Style` column populated from Code prefix | Logic BLOCKED |
| Paused / Skipped protection | Labels exist; 26 Skipped items outside Skipped group | VERIFIED inconsistency |
| Publication receipt | Implemented in V2-2 helper store; **missing on board** for 15 older Posted items | V2-2 nodes; board |
| Format change only by owner | "مقبول كبوست" status suggests automation may propose Post | Needs confirmation |

## Components to reuse

monday board + existing columns (Source asset version, Video measurements, Processed format, Publish at, Published at, Instagram media ID, المطلوب منك), the three V2 workflows (as executors routed through the handler), Dropbox credential, Meta publisher app, OpenRouter credential, existing Python service and its DB (once verified).

## Owner actions required

| # | What is missing | Why | How to provide | Blocks |
|---|---|---|---|---|
| 1 | ~~GitHub account link~~ DONE | Create private repo `Bondok`, store these docs | claude.ai → Settings → Connectors → GitHub → connect the account/org that should own `Bondok` | Repo creation, all code work |
| 2 | Bondok server access | Read code, `.env` key names, service, logs, DB schema, model ID | SSH host + read-only user/key, or share the Bondok folder from the linked Mac via the desktop app's "+" → Add folder | Command handler, DB design, model verification, everything in brief §1–§6 |
| 3 | ~~MCP access to V2 workflows 1/2/3~~ DONE | Map triggers, Monday writes, publish calls, retries | n8n → each workflow → Settings → enable "Available in MCP" (read only is enough now) | Ownership map, publisher integration, migration plan |
| 4 | Slack access | Verify bot, channel ID, scopes, events, owner check | Install Slack connector in claude.ai **or** share the Slack app's manifest + channel ID + owner user ID | Approval flow, notifications |
| 5 | Database location/type | Decide whether it supports transactions, idempotency keys, leases | Comes with #2 | Authoritative state store |
| 6 | Model decision | "GPT-6.1 Sol" not verified anywhere | Confirm provider + exact model identifier configured on the server | Bondok runtime |
| 7 | Decision on legacy data table | 28 stale reservations; not read by the active publisher | Confirm it can be retired (no action until approved) | Clean cut-over |

## Resolved: legacy reservations

The legacy n8n table holds 28 future reservations (one duplicate slot). VERIFIED that the active publisher (V2-2) does not read it and only publishes board items in Status `Scheduled`; none are scheduled now, so these rows cannot trigger publication.

## Progress since first report

- n8n V2 workflows: MCP read access enabled by owner ✅
- GitHub: linked, repository created ✅ (public, by owner decision; documents sanitized accordingly)
- Slack: connector added, sign-in incomplete ⏳
- Server: unreachable from Claude's environments (port 22 blocked); awaiting owner-run read-only script ⏳
- Model: awaiting confirmation ⏳
