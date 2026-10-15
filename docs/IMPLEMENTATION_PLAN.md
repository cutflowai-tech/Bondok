# Implementation Plan (as built)

| Area | Reused | Changed | Removed from active ownership | Migrated |
|---|---|---|---|---|
| Store | `state.sqlite`, legacy tables, `media`/`jobs` as immutable verification store | additive `ops_*` schema (`src/waset_ops/db.py`) | — | schema v1 on first v2 call |
| Rules | slot grid, 84-day horizon, 5-min lead, 10-min near-due, 2-h window, variety fallback, QA policy 3 (helper defaults) | one module `rules.py`; strict DST handling; transfer cap named separately from the 300 MB business rule | LLM style ranking | — |
| Media | helper download/ffprobe/ffmpeg, capacity locks, hash checks | `media.py`: honest caps, recheck re-measure, retryable infra errors | — | existing media rows reused when identity matches |
| Handler | — | `core.py` (commands, permissions, runs, projections, outbox, proposals), `items.py`, `sched.py`, `publish.py`, `monitor.py`, `captions.py` | v1 routes | — |
| Helper CLI | invocation pattern | `src/helper.py` v2 dispatcher, structured errors, `errors.log` | `/v1/*` | — |
| WF1/WF2/WF3 | integration nodes, IDs, credentials | `workflows/build.py` → `workflows/dist` | direct Monday decision writes, LLM ranking, Manual Audit writer | same IDs, imported over v1 |
| Bondok | Slack app, identity checks, dedupe, reply states, model | `bondok/` bridge, tools, notifier, watchdog | Monday/pipeline writes, LLM audit | same service/paths |
| Deploy | — | `deploy/build_release.py`, `deploy/server_backup.sh`, `deploy/rehearse.py` | — | — |

Owner-policy items deliberately unchanged: slot grid, quality thresholds, the 145 MB transfer path (see HANDOFF limitations),
caption house style, labels and columns (none renamed or deleted).
