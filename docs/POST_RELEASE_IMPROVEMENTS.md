# Post-release improvements (non-blocking)

Collected from the 452da98 activation review and the deep audit (2026-10-10). None of these blocks normal
operation; each needs either an owner decision or a separate, scoped change.

## Owner decisions
1. **Which Bondok actions may run on the model's initiative** (BV-56). Today `request_publish`,
   `request_reschedule`, `replace_source` and `request_recheck` execute when the model calls them, while the
   policy text says only the owner's explicit words authorize changes. Options: keep (documented), or route them
   through the same fail-closed explicit-intent check / proposal path as format, Topaz, skip and resume.
2. **Caption proposal expiry (30 min).** WF1 drafts often reach the owner later; a longer TTL or re-issue on
   request would reduce expired proposals.
3. **Restored v1 text when v2 clears its own value** (from the 452da98 review): v1 text is not brought back.
4. **Four board file links with audience "no one"**: downloads may fail; ask the editors to share them.

## Performance / cost
5. **Second download for Stories.** The duration preflight downloads the source, deletes it, and preparation
   downloads it again. Keeping the preflight copy when Topaz is already confirmed saves one full download per
   Story (most Stories are measured before Topaz, so the saving is modest).
6. **WF2 compare-before-write reads** add one Monday query per guarded status job (≈10–15 per WF1 cycle in the
   replay). Batching the reads per WF2 run would cut that to one query.
7. Model calls on routine paths are zero: approvals, notifications, watchdog, WF1/WF2/WF3 make no model call.
   The only scheduled model use is one caption draft per (item, title, brief); Bondok replies in threads it is
   part of. Bondok's `max_output_tokens` (3000) with medium reasoning is the main cost driver per reply.

## Robustness
8. Pre-claim source-revision check in WF2 (avoid creating an Instagram container before the commit refusal).
9. Earlier detection of a replaced file for already scheduled items (WF3 or a cheap revisit).
10. `.lock` file cleanup (BV-57) with a lock protocol that tolerates deletion.
11. Retry Slack replies left `send_uncertain` (BV-58); accept `file_share` messages from the owner (BV-59).
12. Fetch the permalink after reconciling an unknown outcome from container status (BV-60).
13. Expire a refused swap proposal immediately (BV-63).
