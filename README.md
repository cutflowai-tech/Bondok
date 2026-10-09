# Bondok

Bondok is Waset Co Studio's AI Head of Social Media for one operational scope: preparing, verifying, scheduling and
publishing selected real-estate videos to Instagram from the Monday board "For Social Media".

**Principle:** automation first → deterministic rules → AI only when needed.

This repository holds the v2 refactor: one deterministic execution authority shared by the n8n workflows, the schedule
supervisor, Monday edits and the Bondok Slack assistant. Status: **implemented and tested, awaiting deployment
approval** — see [docs/HANDOFF_STATUS.md](docs/HANDOFF_STATUS.md).

| Path | Contents |
|---|---|
| `src/waset_ops/` | Command handler: rules, state, scheduling, publication lifecycle, supervisor, outbox |
| `src/helper.py` | n8n command-line dispatcher (`/v2/*` routes) |
| `bondok/` | Bondok Slack service (extends the deployed assistant) |
| `workflows/` | Original exports, deterministic v2 builder, built exports |
| `tests/` | Acceptance scenarios 1–32, workflow contracts, helper CLI, Bondok integration |
| `deploy/` | Release builder (hash manifest), server backup script, migration/rollback rehearsal |
| `docs/` | Baseline, architecture, commands, Monday/time contract, workflow changes, migration, evidence, handoff |
| `reference/` | Deployed code as found on 2026-10-09 (sanitized), for comparison only |

Run the tests: `cd tests && python3 -m unittest test_acceptance test_workflows test_helper_cli test_bondok`
(stdlib only; Node.js optional for Code-node checks).

No secrets, credential ids, database dumps or board snapshots are stored here; the repository is public.
