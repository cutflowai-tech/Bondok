# Architecture and Ownership

One deterministic execution authority — `waset_ops` — over the **existing** SQLite store (`state.sqlite`, WAL).
n8n and Bondok run on the same host and already share this file (n8n volume `waset_social_data`, bind-mounted into
Bondok's systemd sandbox), so a shared Python module is the smallest safe transport: no new service, port, broker or database.

```
Slack (owner) ─▶ Bondok app ─▶ bridge (trusted adapter: actor, explicit intent, idempotency)
                                   │                                   ┌──────────────┐
Monday board ─▶ WF1/WF2 snapshot ─▶ observe() (edit → command) ───────▶│              │
WF1 preparation results ─────────▶ prep_* commands ───────────────────▶│  waset_ops   │◀─▶ state.sqlite
WF3 supervisor ──────────────────▶ inspect() / repair commands ───────▶│ (handler)    │     (ops_* + legacy)
WF2 publisher ───────────────────▶ due / claim / commit / result ─────▶│              │
                                                                       └──────┬───────┘
                       ops_outbox (durable): monday · source_monday · editor · slack
                         WF2 applies display sync · WF1 applies editor tasks · Bondok posts Slack
```

## Who owns what (v2)

| Operation | Owner | Notes |
|---|---|---|
| Import source project → social item | WF1 creates the item; **handler decides** (`import_plan`, durable `ops_source_map`) | ambiguous codes and missing mapped items are reported, never recreated |
| Human board edits | handler via `observe()` (adapter in WF1/WF2) | edits become commands; own writes recognised by projection |
| File selection | WF1 (existing Dropbox rules) → `prep_source` | wrong folder = config, no file = editor, provider errors = infra |
| Story duration, media QA, encoding | helper media worker (detached ffprobe/ffmpeg, 2 slots) → `prep_preflight` / `prep_media` | immutable `media` rows reused; explicit recheck re-measures |
| Readiness | handler `evaluate()` | from durable facts only |
| Caption drafts | WF1 LLM node → `caption_draft` (stored by input hash) → owner approval proposal | never auto-applied |
| Slot allocation and reservations | handler (`try_schedule`, `ops_reservations` UNIQUE(account, format, slot)) | one implementation for every caller |
| Owner reschedule / occupied slot | handler → proposal → owner approval | option B: nothing moves before approval |
| Publication | **WF2 only** (claim → container → commit point → media_publish → result) | Bondok has no Instagram path |
| Outcome reconciliation | WF2 (`due` → container status) + owner `resolve_outcome` | unknown never becomes "not published" automatically |
| Schedule repair | WF3 only (`repair` → `repair_*` commands) | Bondok explains, never repairs |
| Monday display | handler projections → outbox → WF2 "Apply Display Sync" | committed state first, display second |
| Source "Posted" | outbox `source_monday` → WF2 | retried independently |
| Editor subitems | outbox `editor` → WF1 "Each Editor Job" | keyed by (item, issue); updates only on material change |
| Notifications | outbox `slack` → Bondok (templates, no model) | dedupe keys; quiet when unchanged |
| Health | `ops_heartbeat`, `errors.log` (no SQLite), Bondok watchdog | alerts on transitions only |

## Bypass paths

| Path | v1 | v2 |
|---|---|---|
| `/v1/*` helper routes | used by WF1/2/3 | **retired** in the new helper (refused with `retired`) |
| Reconcile deleting committed reservations from Monday status | yes | removed |
| Commit returning success after 0 rows | yes | no separate commit; reservation = UNIQUE insert or rejection |
| Old Bondok direct writes (Monday columns, `reservations`, `locks`) | yes | removed (Monday read-only; changes via handler) |
| Old Bondok 30-min LLM audit | yes | removed (deterministic notifications + watchdog) |
| WF1 "Light AI — Diversify Styles" | yes | removed (deterministic `fair_order`/`choose_slot`) |
| WF1 status/time writes from run-start snapshots | yes | removed (display only via projection) |
| WF3 writing Manual Audit | yes | manual trigger is read-only inspection |
| Native Monday board automations (3, all inactive) | inactive | leave inactive; group moves come from projection |
| 15 unread n8n workflows | NOT VERIFIED | still NOT VERIFIED; none reference board 5105608159 in readable form (see risks) |

## Key design decisions
* **Additive schema.** Legacy tables keep their exact column lists (the old helper inserts positionally), so rollback to the old helper works on a migrated DB (rehearsed).
* **Receipts mirrored into legacy `publications`/`assets`** at the commitment point, so a rolled-back publisher still refuses to republish.
* **Commitment point = `commit()`.** It re-validates lease/fence, content revision, payload fingerprint, owner state, hold, reservation, Dropbox revision, verification and due window in one transaction before `media_publish`.
* **Payload fingerprint** binds caption, media URL, video sha256, format, account and content revision; a changed caption abandons a pre-commit container.
* **Monday edit attribution is unavailable** (every board change is by the same account), so protected actions requested on the board (format change, resume) become Slack approval proposals instead of executing.
