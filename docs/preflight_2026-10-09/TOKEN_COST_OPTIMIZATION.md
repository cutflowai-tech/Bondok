# TOKEN_COST_OPTIMIZATION

Scope: within the existing social-media pipeline only.

| # | Opportunity | Evidence | Confidence |
|---|---|---|---|
| 1 | **Stop rewriting unchanged state.** One item's `System update` was rewritten 29× in ~5 h; 784 writes on 72 items. If any LLM call sits on this path, it repeats per cycle. Write (and reason) only on change, keyed on `Source asset version` + format + revision. | Activity log | VERIFIED churn; AI involvement NOT VERIFIED |
| 2 | Duration, resolution, size, Topaz flag, slot grid, style rotation are all deterministic — no model calls needed. | Business rules | VERIFIED rules |
| 3 | Event-driven instead of polling: run preparation on board/Dropbox change webhooks; supervisor only when a reservation, readiness or due time changes. | Supervisor/prepare cadence | INFERRED |
| 4 | Bondok answers status questions from the operational store/board snapshot via named read tools, not by re-fetching full board context per message. | Brief §1 | Design |
| 5 | Model only for: interpreting free-text owner requests, drafting captions, explaining blockers. Use a small model for intent parsing (precedent: Gemini Flash Lite via OpenRouter in the Telegram flow). | n8n "My workflow" | VERIFIED precedent |
| 6 | Quiet monitoring: no notification when nothing actionable changed. | Brief §7 | Design |
| 7 | "Version Check" stores a confidence score from an AI file-picker ("Jev") — replace with deterministic latest-version selection by Dropbox rev + naming rules where possible. | Column description | INFERRED |

Model costs per call cannot be measured until Bondok's server and the V2 workflow nodes are accessible.
