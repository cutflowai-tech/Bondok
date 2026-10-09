# PERMISSIONS_AND_SECURITY

No secret value was read, printed, or stored during this audit. Only credential **names and types** were listed.

## Findings

| # | Finding | Severity | Status |
|---|---|---|---|
| 1 | n8n and humans share one monday.com identity (account owner). Any automation writes with full owner rights to every board; writes are unattributable. | High | VERIFIED |
| 2 | The n8n API/MCP identity has owner-level scopes on workflows and credentials (create/update/delete/share). Bondok must not inherit these. | High | VERIFIED |
| 3 | Bondok's Slack owner-identity check, channel restriction and duplicate-event protection cannot be inspected. | High | BLOCKED |
| 4 | Board 5105608159 is shareable with `permissions: everyone`. | Medium | VERIFIED |
| 5 | Publish receipts are not stored (0/15). Duplicate-publish prevention cannot be proven. | High | VERIFIED (absence on board) |
| 6 | Ambiguously named credentials ("Unnamed credential 2" ×2, generic Bearer/Header auth). | Medium | VERIFIED |
| 7 | Core V2 workflows are hidden from MCP — reduces exposure but blocks audit. | Info | VERIFIED |

## Recommendations for implementation (no action taken)

1. Create a dedicated monday user/API token for automations (and another for Bondok) with access limited to board 5105608159; enforce board membership in code as well.
2. Give Bondok a narrow authenticated interface to the command handler — no SQL, GraphQL, shell, workflow-admin or raw HTTP tools.
3. Store the Meta token in one place (server `.env` or n8n credential), named clearly; record expiry.
4. Slack: verify `team_id`, `channel_id`, owner `user_id` and request signature in code; dedupe on `event_id`.
