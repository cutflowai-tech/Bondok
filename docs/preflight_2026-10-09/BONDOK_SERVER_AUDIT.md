# BONDOK_SERVER_AUDIT

**Overall: BLOCKED.** The server hosting the Bondok Python assistant could not be located or accessed from this session.

| Item | Result | Evidence / note |
|---|---|---|
| Server identity & environment | BLOCKED | No hostname/IP provided; no SSH connector. |
| Deployment location | BLOCKED | — |
| Python application structure | BLOCKED | No repo or code access. |
| Service/process manager | BLOCKED | — |
| Running service status | BLOCKED | — |
| Configuration structure / env var names | BLOCKED | — |
| Logging | BLOCKED | — |
| Database connections | BLOCKED | — |
| Integrations | BLOCKED | — |
| AI provider & model identifier | **NOT VERIFIED** | Requested model "GPT-6.1 Sol" cannot be confirmed. n8n uses OpenRouter (`google/gemini-2.0-flash-lite-001`) in a separate Telegram flow — not Bondok. |
| Slack integration | BLOCKED | No Slack access. |
| Business tools | BLOCKED | — |
| Background jobs | BLOCKED | — |

## Leads (INFERRED, not verified)

- n8n stores a credential of type `sshPrivateKey` → n8n connects to at least one server over SSH (memory notes say resizing "on the server").
- The n8n host runs a Python helper (`waset-social/helper.py`) used by the publisher for claims, leases and receipts. If n8n and Bondok share the server, this is the existing operational store.
- SSH to the owner-supplied host fails from Claude's environments ("Network is unreachable"). Plan: owner runs a read-only discovery script and shares the output.

## Needed to unblock

One of:
1. SSH access (host, user, read-only key or a read-only account) to the Bondok server, or
2. Share the Bondok code folder on the linked Mac (desktop app → "+" → Add folder) if Bondok runs there, plus read access to its logs/service definition.

Requested read-only commands for the first pass: `systemctl status`/`pm2 ls`/`docker ps`, `ls` of the app directory, `.env` **key names only** (`cut -d= -f1`), DB schema dump (`\d` / `.schema`), last 500 log lines.
