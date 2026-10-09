# SYSTEM_ARCHITECTURE (as observed)

```
            humans (single monday identity)
                   │ edits
                   ▼
 Customer Projects ──(Office n8n flows)──► For Social Media board 5105608159 ◄── monday automations (3, inactive)
   board                                     ▲        ▲        ▲
                                             │writes  │writes  │writes?
                     ┌───────────────────────┘        │        │
          n8n V2-1 Prepare & Schedule      n8n V2-3 Supervisor  n8n V2-2 Publish When Due ──► Instagram (Meta app)
                │         │                                         │
                ▼         ▼                                         ▼ reads?
            Dropbox   editor tasks (other board)             n8n data table "IG Publish Schedule" (37 rows, stale)

          Bondok (Python, Slack)  ── location unknown; integrations unknown (BLOCKED)
```

## Components

| Component | Where | State | Confidence |
|---|---|---|---|
| Board "For Social Media" | monday.com | 164 items, 31 columns | VERIFIED |
| Prepare & Schedule | n8n `<id>` | active | VERIFIED exists; internals read pending |
| Publish When Due | n8n `<id>` | active, every 1 min | VERIFIED (reads board only; gated on Scheduled + Topazed + verified media/revision) |
| Schedule Supervisor | n8n `<id>` | active | VERIFIED exists; internals read pending |
| Legacy reservation table | n8n data table `IG Publish Schedule` | 28 stale future rows; not read by V2-2 | VERIFIED |
| Media storage | Dropbox (account "Waset Co Studio") | reachable | VERIFIED |
| Media processing (Topaz / resize) | editor manual Topaz + server/n8n resize | — | INFERRED |
| Publisher target | Instagram the company account via Meta app "Waset Auto Publisher" | — | INFERRED (owner-stated, not inspected) |
| Bondok assistant | unknown server | — | BLOCKED |
| Slack channel | unknown | — | BLOCKED |
| Operational store | Python helper on n8n host (claims, leases, receipts) | used by V2-2 | VERIFIED usage; schema BLOCKED |

## Key architectural observations

1. There is **no single execution authority** today: n8n workflows write Monday directly, humans write the same columns, and the reservation exists in two places (data table vs. `Publish at`).
2. A helper-based operational store already exists for publication (claims, leases, receipts), but readiness and scheduling state still live mainly on Monday. 15 older Posted items have no media ID on the board.
3. Readiness, scheduling and publication are collapsed into one `Status` column with 14 labels.
4. The V2 pipeline was built/edited on 2026-10-08/09 and is actively churning (hundreds of writes per hour).
