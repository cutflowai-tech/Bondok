# ACCESS_AUDIT — Bondok Preflight

Audit date: 2026-10-09 (Africa/Cairo). Mode: read-only. No production object was created, changed, executed, or deleted.

Legend: **VERIFIED** (observed directly), **INFERRED** (deduced from verified evidence), **NOT VERIFIED** (could not be checked), **BLOCKED** (access missing).

## Access checklist

| # | Dependency | Result | Evidence |
|---|---|---|---|
| 1 | GitHub repository management | **1 — Access confirmed (push, this repo only)** | Account linked 2026-10-09. Session cannot create repos ("sessions are bound to their configured repositories"); owner created `Bondok`, Claude has push access. |
| 2 | Existing Bondok server | **3 — Access unavailable (BLOCKED)** | Owner supplied host; SSH from both Claude environments fails with "Network is unreachable" (port 22 egress blocked). Password login is not used by Claude. An n8n credential of type `sshPrivateKey` exists (name only seen), suggesting n8n reaches some server over SSH — INFERRED. |
| 3 | Existing Bondok application code | **3 — BLOCKED** | No repo, no server, no local folder connected. |
| 4 | Server logs | **3 — BLOCKED** | Depends on #2. |
| 5 | Service/process configuration | **3 — BLOCKED** | Depends on #2. |
| 6 | Existing n8n instance | **2 — Read-only access confirmed (partial)** | Listed 28 workflows, 12 credential names, 1 project, 1 data table. Credential scopes include create/update/delete — write capability exists but was not used. |
| 7 | Relevant n8n workflows | **2 — Read-only access confirmed** | Owner enabled "Available in MCP" on all three V2 workflows. V2-2 read in full. |
| 8 | Monday.com board 5105608159 | **2 — Read-only access confirmed** | Board structure, 31 columns, 4 groups, 164 items, activity log read. Connector also has write tools (unused). |
| 9 | Relevant Monday.com integrations | **2 — Partial** | 3 board automations listed (all inactive). Webhooks/app integrations are not exposed by the connector — NOT VERIFIED. |
| 10 | Slack workspace | **3 — BLOCKED (in progress)** | Slack connector added but sign-in incomplete (`connect_incomplete`). No Slack credential found in n8n credential list. |
| 11 | Bondok Slack app configuration | **3 — BLOCKED** | Depends on #10 and Slack admin access. |
| 12 | Existing persistent database | **INFERRED exists; schema BLOCKED** | V2-2 uses a local Python helper on the n8n host for claims/leases/receipts. Previously: | No database connector. Only persistent store found: n8n data table `IG Publish Schedule` (3 columns). Bondok's own DB, if any, is on the unreachable server. |
| 13 | Media storage | **2 — Read-only access confirmed** | Dropbox connector authenticated as account "Waset Co Studio" (team: none, personal namespace). Not browsed in this phase. |
| 14 | Media processing pipeline | **4 — Additional permission required** | Lives inside hidden V2 workflow 1 and possibly the server (resize). Only its board outputs are visible. |
| 15 | Existing publishing integrations | **2 — Read-only confirmed** | V2-2 publishes to Instagram Graph API via the generic credential "Simplified Custom Auth account" (VERIFIED). |
| 16 | Publishing history and receipts | **2 — Verified absent on board** | All 15 `Posted` items have empty `Instagram media ID` and empty `Published at`. No receipt store found. |
| 17 | Existing scheduler | **4 — Additional permission required** | V2 workflow 1 (hidden). Legacy reservations exist in the n8n data table. |
| 18 | Existing schedule monitor | **4 — Additional permission required** | V2 workflow 3 "Schedule Supervisor" (hidden), created 2026-10-09 14:09 UTC. |
| 19 | Configuration and credential references | **2 — Names only** | 12 n8n credential names/types listed; no secret values were retrieved. Server `.env` BLOCKED. |
| 20 | AI provider/model configuration | **NOT VERIFIED for Bondok** | Bondok's provider/model is on the server (BLOCKED). In n8n, workflow "My workflow" uses OpenRouter with model `google/gemini-2.0-flash-lite-001` (VERIFIED). "GPT-6.1 Sol" is not verified anywhere. |

## What the owner must provide

See REFACTORING_READINESS.md → "Owner actions required" for exact steps per blocked item.
