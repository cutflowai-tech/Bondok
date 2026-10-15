# INTEGRATION_DEPENDENCIES

| Integration | Used by | Credential (name/type only) | Verified access from this session | Notes |
|---|---|---|---|---|
| monday.com | n8n V2 + Office flows; Bondok (target) | n8n: "Monday.com account" (`mondayComApi`) | Read via connector (VERIFIED) | Same identity as human owner → no writer separation. Bondok must be scoped to board 5105608159 in code. |
| Dropbox | n8n (prep, archive cleanup, office) | "Dropbox account", "Dropbox account 2" (OAuth2), "Dropbox account 3" (API) | Connector authenticated (VERIFIED) | Three Dropbox credentials — which one V2 uses is NOT VERIFIED. Archive cleanup deletes old versions weekly. |
| Instagram / Meta Graph | V2-2 publisher | not visible by name; candidates: "Bearer Auth account", "Header Auth account", "Simplified Custom Auth account", "Unnamed credential 2" ×2 (OAuth2) | BLOCKED | Owner-stated token expiry 2026-12-07 (from earlier notes) — expiry risk before refactor completes. |
| OpenRouter | n8n Telegram priority agent; caption generation | "OpenRouter account" | Name only | Bondok's provider unknown. |
| Telegram | n8n "My workflow" | "Telegram account" | Name only | Existing chat-command entry point (not Slack). |
| SSH | unknown workflow(s) | "SSH Private Key account" | Name only | Likely server resize/processing step. |
| Slack | Bondok (target) | **none found** | BLOCKED | No Slack connector, no n8n Slack credential. |
| Topaz | editor (manual) | — | — | Confirmation is a human-set `Topazed` column; not tied to file revision in a verifiable way yet (Source asset version column exists, filled on 101 items). |
| Bondok DB | Bondok | unknown | BLOCKED | — |

## Dependency risks

1. Meta token expiry 2026-12-07 — renew before migration cut-over.
2. Archive cleanup can delete a file version referenced by an older verification → stale `Publish video` links.
3. Multiple ambiguous credentials (3× Dropbox, 2× "Unnamed credential 2") — ownership must be clarified before rerouting.
