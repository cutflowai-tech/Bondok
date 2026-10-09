# WASET SOCIAL MEDIA AUTOMATION — CURRENT SYSTEM TECHNICAL DOCUMENTATION

## 01 — System Overview

Investigation date: 2026-10-09. This was a **read-only** investigation. No workflow was executed, edited, activated or deactivated, and no Monday item or credential was touched.

### How to read this set

| File | Contents (sections of the requested report) |
|---|---|
| `01_SYSTEM_OVERVIEW.md` | 1 Executive overview · 2 n8n instance overview · 7 Trigger architecture (summary) |
| `02_WORKFLOW_INVENTORY.md` | 3 Complete workflow inventory, including workflows that could not be accessed |
| `03_WORKFLOW_DEEP_DIVES.md` | 4 Individual workflow documentation · 5 Node-by-node explanation · 6 Code node analysis · helper.py service |
| `04_MONDAY_DATA_MAPPING.md` | 9 Monday.com read/write mapping |
| `05_SCHEDULING_AND_PUBLISHING.md` | 8 Content lifecycle · 10 Media pipeline · 11 Scheduling · 12 Publishing · 13 Schedule monitoring · 15 Business rules |
| `06_DEPENDENCY_GRAPH.md` | 19 Dependency graph (Mermaid) |
| `07_ERRORS_AND_RISKS.md` | 16 Error handling · 17 Execution history · 18 Concurrency risks · 20 Missing/unverified functionality |
| `08_BONDOK_INTEGRATION.md` | 14 Slack & Bondok integration points · 21 Reusable components · 22 Owner questions |
| `workflow_exports/` | Sanitized JSON of the 3 social workflows. Credential IDs are replaced with `<REDACTED>`. A secret scan found no tokens inline. |
| `helper_reference/helper.py` | Local copy of the Python helper the workflows call (see the evidence note below) |

**Evidence labels used throughout:**
- **VERIFIED**: read in the live workflow JSON.
- **DELEGATED-TO-HELPER**: enforced in `helper.py`, local copy.
- **INFERRED**: a reasoned conclusion, not read directly.
- **NOT IMPLEMENTED**: no code does this.
- **HISTORICAL OBSERVATION**: from execution records, not from logic.

---

### 1. Executive overview

The social media system is **three active n8n workflows** plus a **Python helper service** that they call through `Execute Command` nodes. All four were built on 2026-10-08/09 ("Waset Social V2").

| # | Workflow | ID | Trigger | Role |
|---|---|---|---|---|
| 1 | Waset Social V2 — 1 Prepare & Schedule | `qI1N5VNgpRjnZAKH` | every 10 min | Syncs items from the source projects board, picks the final video from Dropbox, validates and prepares the media, writes Post captions with an LLM, reserves a slot, and writes the schedule to Monday |
| 2 | Waset Social V2 — 2 Publish When Due | `pUIshuf16zIYoYRz` | every 1 min | Finds `Scheduled` items whose `Publish at` has passed, claims them through a helper lease, re-validates them, publishes to Instagram (Reel or Story) through the Graph API, and writes the proof back to Monday |
| 3 | Waset Social V2 — 3 Schedule Supervisor | `WasetSocialScheduleGuard` | cron `0 5,35 * * * *` (:05 and :35) + manual | Audits the whole board with the helper, then reschedules or blocks items whose slot is invalid |

**`helper.py`** runs on the server at `/home/node/.n8n-files/waset-social/helper.py`. It holds all slot rules, locks, leases, reservations and media preparation (ffprobe/ffmpeg), with state kept in SQLite (`state.sqlite`). It exposes 19 `/v1/...` endpoints. Each call is a single command run whose argument is a base64 JSON payload.

**Evidence note on helper.py:** the server file cannot be read through the n8n MCP. A local copy was found in a Codex working folder, in the same bundle as three workflow JSON files that match the live n8n workflows **node for node, with 0 differences**. That copy is very probably the deployed version, but this is **not verified on the server**. Every rule attributed to the helper carries this caveat.

**Headline findings:**
1. **Production is currently failing.** Since 2026-10-09 15:00 UTC, every run of all three workflows fails at a helper call (exit code 1). Publish When Due had failed 62 times in a row by 16:01 UTC. **Nothing has ever been published by V2.** *(HISTORICAL OBSERVATION; the helper's error output is not stored in the execution records.)*
2. The brief's rules exist, but **mostly in `helper.py`, not in n8n**: Story < 60 s, short edge ≥ 1080, the Cairo timezone, the post slots (Sat/Mon 21:00, Wed 22:45, Thu 21:00), the story slots (11/14/18/21/22), style spreading and starvation fallback. The size rule reads `MAX_BYTES = 300_000_000`, but prepared files are **actually capped at 145 MB**.
3. "Topaz confirmation" is **a manual Monday toggle** tied to the Dropbox file id + revision. It does not technically check the file.
4. There is **no Slack, no Facebook publishing, no error workflow, no notification path, no group moves between Posts and Stories, and no Bondok/AI-assistant hook**.
5. Concurrency is handled by a helper `preparation` lock shared by workflows 1 and 3, plus a 180 s publish lease in workflow 2. Several confirmed gaps are listed in `07`: the lock is not released on a crash, reconcile can delete committed slots after a partial read, and human edits during publishing are overwritten to Needs Review.
6. 15 of the 28 workflows on the instance are **not readable through MCP**. Some of them, such as "When a Stat Changes In Main Projects (Office)", may feed the source board, so they could not be ruled out.

### 2. n8n instance overview

- Host: `https://n8n.wasetco.com` (from webhook URLs).
- 28 workflows: 13 readable through MCP, 15 not. See `02`.
- The social workflows' settings: `timezone = Africa/Cairo`, `saveDataErrorExecution = all`, `callerPolicy = workflowsFromSameOwner`, no `errorWorkflow`, `executionTimeout` 1800 s (WF1, WF2) and 600 s (WF3).
- Credentials used by the social workflows (names only, VERIFIED per node):
  - `Monday.com account` (mondayComApi).
  - `Dropbox account` (dropboxOAuth2Api): folder creation and the verified-video upload.
  - `Unnamed credential 2` (generic oAuth2Api) is a **second Dropbox credential**, used for listing, sharing, metadata and revision checks.
  - `Simplified Custom Auth account` (httpTemplatedCustomAuth) is used for the Instagram Graph API v26.0 calls on IG user `17841479950766455`: Create Container, Check Container, Publish To Instagram, Get Permalink.
  - `OpenRouter account`.
- LLMs used (OpenRouter, WF1 only): `openai/gpt-5.6-sol` writes Post captions (node `Write Caption`), and `openai/gpt-4.1-mini` reorders styles (node `Light AI — Diversify Styles`).
- Boards: **5105608159 For Social Media** (target). **5091110326** is the source projects board; WF1 reads it and WF2 writes `Posted` back to it.

### 7. Trigger and event architecture (summary)

- **Everything is polling.** There are no webhooks into the social workflows, and no workflow calls another through Execute Workflow. They coordinate only through:
  - the shared Monday columns, and
  - the helper's SQLite state (lock `preparation`, slot reservations/commits, publish leases, logs).
- WF1 and WF3 never run at the same time because of the `preparation` lock. WF2 runs every minute regardless of that lock.
- Manual edits on the board, for example a human changing Status, Format or `Publish at`, are picked up on the next poll. The workflows use guards (re-reading Monday before writing) instead of events. Details and conflicts are in `04` and `07`.

See `06_DEPENDENCY_GRAPH.md` for the diagram.
