# N8N_WORKFLOW_INVENTORY

Instance: single personal project (team projects not enabled). 28 workflows total. Read via n8n MCP connector; nothing executed or changed.

## A. Workflows in Bondok's scope (social media)

| ID | Name | Active | Created / updated (UTC) | Readable via MCP | Status |
|---|---|---|---|---|---|
| V2-1 | Waset Social V2 — 1 Prepare & Schedule | yes | 2026-10-08 / 2026-10-09 | Yes (since 15:35 UTC) | read pending |
| V2-2 | Waset Social V2 — 2 Publish When Due | yes | 2026-10-08 / 2026-10-09 | Yes | VERIFIED |
| V2-3 | Waset Social V2 — 3 Schedule Supervisor | yes | 2026-10-09 | Yes | read pending |

All three have 1 trigger each (VERIFIED). MCP access was enabled by the owner on 2026-10-09; V2-2 has been read in full below. V2-1 and V2-3 internals: pending read (next step).

### V2-2 Publish When Due — now readable (VERIFIED 2026-10-09 15:40 UTC)

- **Trigger:** schedule, every 1 minute, misfire policy `skip`. 78 nodes. `Configuration` node has an `armed` switch (currently armed) and a note that the old publisher was stopped at cut-over on 2026-10-08.
- **Reads:** the monday board (paged snapshot). Due time = `Publish at` if set, otherwise legacy `Post Date` + `Post Time` (Africa/Cairo). It does **not** read the legacy n8n data table.
- **Due gate:** only items with Status `Scheduled` and due time ≤ now. Protected statuses: Posted, Skipped, Paused, Publishing.
- **Final gate before publishing** (re-reads the board): still `Scheduled`, `Topazed`, `Verified media ID` matches, `Source asset version` matches, no IG collab handle, no existing Post Link, due time unchanged, source project not Canceled; then re-verifies the Dropbox source revision.
- **Durable state:** a local Python helper on the n8n host (called through Execute Command nodes) with operations: publish snapshot, claim, renew lease, save container receipt, record publish intent, persist published receipt, persist permalink, complete receipt. This is an existing operational store with claims, leases and receipts — **the primary reuse candidate**. Its storage engine and schema are NOT VERIFIED (needs server access).
- **Publishing:** Instagram Graph API (container → poll status → `media_publish`) for one IG business account, via a generic templated-auth credential. `retryOnFail` is off on the publish call; errors route to a review branch.
- **After publish:** writes Posted state + media ID + permalink to the board, syncs Posted back to the source project board (Customer Projects), then completes the receipt. Monday-sync failure is handled after the receipt is persisted (receipt first) — INFERRED from node order.
- **Writes outside board 5105608159:** yes — Customer Projects (source sync).
- **Consequence:** with 0 items `Scheduled`, nothing can publish now. The 28 future legacy-table rows are inert for this publisher.


### Behaviour inferred from board activity (INFERRED, 2026-10-09 10:00–15:00 UTC)

Workflow 1 (Prepare & Schedule) is the most likely writer of: `Status`, `System update`, `المطلوب منك`, `Last checked`, `Video measurements`, `Processed format`, `Social owner`, `Folder Link`, `Dropbox Link`, `Version Check`, `Style`, `Story variety`, `Source item ID`, `Source asset version`. Evidence: these columns change together in bursts on the same minute across many items, with machine-generated text (e.g. "Topaz version confirmation is required", editor task IDs).

Observed during the window:
- 784 column writes across 72 items; one item's `System update` rewritten 29 times (high write churn).
- 24 transitions `Scheduled → Needs Review` and 0 items currently `Scheduled` — the new pipeline appears to have de-scheduled everything on 2026-10-09.
- Editor tasks are created on another board (subitem/task IDs quoted in System update) — workflow 1 writes outside board 5105608159.

## B. n8n data table (persistent state)

`IG Publish Schedule` (`<id>`): columns `itemId`, `token` (sequence number, not a secret), `publishAt` (UTC). 37 rows, last updated 2026-10-08 15:08 UTC.

- 28 rows are in the future, **all pointing at items that are not ready** (13 Waiting for Editor, 7 Skipped, 6 Needs Review, 2 long Story).
- Duplicate instant: 2 items reserved for 2026-10-09 19:00 UTC (22:00 Cairo tonight).
- 2 rows off the Story slot grid (Thu 18:09, Thu 20:00 Cairo).
- V2-2 does not read this table (VERIFIED). It is most likely a leftover of the stopped V1 publisher — candidate for retirement after owner confirmation.

## C. Other workflows touching the same systems (outside Bondok scope but relevant to conflicts)

| ID | Name | Active | Trigger | Notes |
|---|---|---|---|---|
| `<id>` | My workflow | yes | Telegram | AI agent (OpenRouter, `google/gemini-2.0-flash-lite-001`) parses priority-change messages → calls `<id>`. Existing chat-command pattern (VERIFIED). |
| `<id>` | My Sub-Workflow 1 | yes | sub-workflow | Applies priority changes (not inspected in full). |
| `<id>` | CEO notifications | no | Schedule | Inactive. |
| `<id>` | Weekly Dropbox Archive Cleanup | yes | Schedule (Fri 04:00 Cairo) | Deletes old Archive videos — can affect files referenced by board links if versions move. |
| `<id>`, `<id>`, `<id>`, `<id>` | Captions workflows | yes | webhook/board | Captions board pipeline; source of "latest version" files. |
| `<id>`, `<id>`, `<id>`, `<id>`, `<id>`, `<id>`, `<id>` | "(Office)" source-project workflows | yes | board webhooks | Customer Projects board; not readable via MCP. Source-project access — must not transfer to Bondok. |
| `<id>`, `<id>`, `<id>` | Dropbox / Office download helpers | yes | various | Not readable via MCP. |
| `<id>`, `<id>`, `<id>`, `<id>` | Editor/priority/revision workflows | yes | webhook | Source-project scope. |
| `<id>`, `<id>`, `<id>` | Drafts | no | — | Inactive. |

No workflow named for Slack, Instagram, Topaz, or error handling was found. No n8n Slack credential exists.

## D. Ownership conflicts (identified)

1. **Single shared Monday identity**: n8n and humans write as the same Monday account → writers cannot be distinguished by actor (see MONDAY_BOARD_MAPPING.md).
2. **Two reservation stores**: legacy data-table reservations vs. V2 `Publish at` column (empty for all items). Stale reservations persist.
3. **Schedule Supervisor + Prepare & Schedule** both appear to own scheduling repair (NOT VERIFIED — hidden).
4. **Monday board automations** (move on Status/Format change) overlap with n8n group moves — currently all three monday automations are inactive.

## E. API permissions for the implementation phase

- MCP connector scopes on visible workflows include `workflow:update/publish/unpublish/execute` — sufficient for implementation **once the three V2 workflows are made available in MCP**.
- Credential scopes include create/update/delete (owner-level). Least-privilege review recommended (PERMISSIONS_AND_SECURITY.md).
