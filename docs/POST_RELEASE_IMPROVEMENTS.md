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
5. **Unknown outcome while the container stays FINISHED** (round 4, end-to-end test). After a publish request
   ends with HTTP 500 or no answer, only a PUBLISHED container status resolves the outcome automatically; a
   container that stays FINISHED (not published) keeps the item blocked until the owner answers, so a transient
   Instagram error costs the slot. Option: re-publish the *same* container after N FINISHED checks (Instagram
   publishes a container at most once), or keep today's rule (owner confirms).

## Performance / cost
6. **Second download for Stories.** The duration preflight downloads the source, deletes it, and preparation
   downloads it again. Keeping the preflight copy when Topaz is already confirmed saves one full download per
   Story (most Stories are measured before Topaz, so the saving is modest).
7. **WF2 compare-before-write reads** add one Monday query per guarded status job (≈10–15 per WF1 cycle in the
   replay). Batching the reads per WF2 run would cut that to one query.
8. Model calls on routine paths are zero: approvals, notifications, watchdog, WF1/WF2/WF3 make no model call.
   The only scheduled model use is one caption draft per (item, title, brief); Bondok replies in threads it is
   part of. Bondok's `max_output_tokens` (3000) with medium reasoning is the main cost driver per reply.

## Robustness
9. Pre-claim source-revision check in WF2 (avoid creating an Instagram container before the commit refusal).
10. Earlier detection of a replaced file for already scheduled items (WF3 or a cheap revisit).
11. `.lock` file cleanup (BV-57) with a lock protocol that tolerates deletion.
12. Retry Slack replies left `send_uncertain` (BV-58); accept `file_share` messages from the owner (BV-59).
13. Fetch the permalink after reconciling an unknown outcome from container status (BV-60).
14. Expire a refused swap proposal immediately (BV-63).

## Infrastructure (round 4)
15. **Shared server capacity.** Bondok shares its disk with other systems that grow by gigabytes per day; a
    separate volume (or retention on the other systems) keeps a full disk from stopping preparation and, at 0 bytes,
    every database write. The low-disk alert (BV-81) reports the condition; it does not prevent it.
16. **Prepared media and disk cleanup.** Prepared videos of reserved items live in the data directory's `media`
    folder; deleting them makes the item miss its slot (now reported, BV-80). Superseded files are cleaned
    automatically (audit `873a730`).

## Maintainability (release review 2026-10-12, standards axis; not done before release on purpose)
Judgement calls from the code review of `bondok/r5-monday-authority` (Fowler smells). None changes behaviour;
each is a separate, scoped refactor with the full suite as its safety net.

17. **One source for board and column ids** (duplicated values). `workflows/build.py` still writes the source
    board id and the Customer Projects status column id as literals in its JS, and `bondok/monday_read.py` /
    `bondok/app.py` repeat the social board id, while `rules.BOARD`, `rules.SOURCE_BOARD` and `board.COL` exist
    (the new intent node already uses `rules.BOARD`). Substitute them at build time like `__BOARD__`.
18. **`CODE_RE` copied into JavaScript** (`build.py`, "Same Code pattern as rules.CODE_RE"): emit it from
    `rules.CODE_RE.pattern` at build time, with a parity test.
19. **`bondok/policy.txt` restates owner-policy numbers** (slots, 60 s Story, 65 s trim limit, 1080 px, 300 MB):
    generate that paragraph from `rules` so a policy change is made once.
20. **Repeated tool switches in `bondok/bridge.py`** (24 `tool ==` branches in three chains; `confirm_topaz`
    handled twice): one table of tool → (op, scope check, protected?) used by all three places.
21. **Raw SQL reads outside `Ops`** (`bondok/app.py`, `src/helper.py` read `ops_proposals`, `ops_runs`,
    `ops_checks` through `ops.store.read()`): small query methods on `Ops` so the schema has one reader.
22. **`src/waset_ops/items.py` is 1,750 lines** for board observation, edit mapping, preparation and owner
    controls: split by concern (observation/edits vs preparation) behind the existing `Ops` mixin seams.
23. **Shared compare-before-write code.** The display sync and the publication intent now share `GUARD_JS`
    (`build.py`); the Python mirror in `tests/board_sim.py` should be generated from, or tested against, it.
