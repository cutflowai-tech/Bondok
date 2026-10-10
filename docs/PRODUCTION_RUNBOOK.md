# Production runbook — R5 release (bondok/r5-monday-authority)

Nothing here has been executed against production. Every step marked **[AUTH]** needs the owner's explicit
authorization for that change set (AGENTS.md). Read-only steps may run without it. Never run a workflow to "test"
it, never post test content, never use helper write routes against production as an access check.

Server paths, container names, the n8n project and credential ids live only in the private deploy config
(template: `deploy/deploy.example.json`) and in the private site file (`{"IG_ACCOUNT": "<IG user id>"}`).

## 0. Change sets

| Set | Content | Reversible by |
|---|---|---|
| C1 **[AUTH]** | Code: helper + `waset_ops` (n8n data volume) and Bondok (code dir), one release id | `deploy.py` automatic rollback; manual: the backup printed by `deploy.py` |
| C2 **[AUTH]** | Workflows WF1/WF2/WF3: import as inactive drafts, compare with the artifact | previous version kept as n8n history and in the workflow backup |
| C3 **[AUTH]** | Activate (publish) the imported WF1/WF2/WF3 versions in n8n | re-publish the previous version |
| C4 **[AUTH]** | Monday: create the long-text column `Bondok record` with column id `bondok_record` on the For Social Media board (R2) | delete the column (records stop; nothing else depends on it) |

C1 before C2/C3: the new workflows call helper routes that only the new helper has. C4 is independent and can
follow at any time after C1 (the code writes the record only once the board shows the column).

## 1. Read-only preflight (no authorization needed)

1. `git log -1` on the release commit; `cd tests && python3 -m unittest discover -s . -p 'test_*.py'` on
   Python 3.12 and 3.14 (the n8n image) — all pass, 0 skipped (node and FFmpeg installed).
2. Build: `python3 deploy/build_release.py --creds PRIVATE_CREDS.json --site PRIVATE_SITE.json`. The manifest
   must say `deployable: {code: true, workflows: true}`, no blockers, no unresolved placeholders.
3. On the server: `deploy.py status --config C` (no fence, no lock, drain state) and
   `deploy.py snapshot-live --config C > baseline.json`. Review the baseline against the last recorded release.
   **Known drift (2026-10-10):** the live helper's `rules.py` carries an unattributed edit ("TEMP owner slots"
   15:10/16:00/18:30 Cairo, not date-limited). The release does not carry it: owner-chosen off-grid times are
   honoured directly now, so the edit is no longer needed for owner times. If the owner wants those three
   times as permanent *automatic* Story slots, that is a policy change to `rules.py` (decision first).
4. Read the live board and store (read-only): pending publications, `outcome_unknown` items, holds, next
   deadlines. Do not deploy inside the 30 minutes before a reserved slot.

## 2. Deploy code (C1) **[AUTH]**

```bash
deploy.py backup --config C
deploy.py code --config C --release release/<id> --expect-live baseline.json
```

Gates (all automatic, any failure before the switch changes nothing): lock; manifest hashes; live code equals the
reviewed baseline; complete, restore-tested backup; staged copies + import tests + helper health on a copy of
the live database; the release's Instagram account equals the account of the live reservations (R5 LOW-18);
fence + drain (timeout = fail closed, exit 3); atomic switch; post-switch hashes and health; automatic rollback
(exit 4) or containment (exit 5: writers fenced, Bondok stopped, printed instructions).

Schema version is unchanged (1); no database migration runs. The previous code can read every row this release
writes (additive values only).

## 3. Workflows (C2, C3) **[AUTH]**

```bash
deploy.py workflows --config C --release release/<id>
```

Imports WF1/WF2/WF3 as drafts with credentials resolved from the private config, compares each import with the
artifact and refuses if a live workflow drifted since the last recorded import. Activation is not changed.
C3: publish the imported versions in n8n (WF2 first is not required; WF1, WF2, WF3 in any order after C1).

## 4. Monday record column (C4) **[AUTH]**

Create a long-text column titled `Bondok record` with the explicit column id `bondok_record` (monday
`create_column` accepts a user-specified id). From the next WF1 cycle each item gets its record with its next
display write (spread by the outbox; no burst beyond the normal display jobs). The record is technical: owners
should not edit it (an edit is restored and never obeyed). Rebuild-from-board is then available
(`tests/test_r5_authority.py`).

## 5. Live acceptance (Gate 7)

* `deploy.py status`: recorded release = `<id>`, no fence. Helper `/v2/health`: ok, schema 1, release id.
* WF1: next runs complete (`run_completed` heartbeat), several different eligible items progress.
* WF2: runs every minute, due queue matches the reservations, display jobs drain.
* WF3: `:05/:35` runs complete; findings are factual (no stale "outcome unknown" after an owner report).
* Slack: an authorized, non-effectful check only (e.g. ask Bondok to show an existing draft again); no command.
* Publication: verify the **next normal owner-approved publication** when it happens (attempt published,
  permalink on the board, record updated). Never create or publish a test item.

## 6. Rollback

* Automatic inside `deploy.py code` (hash-verified). Manual: `deploy.py code --release <previous release>`;
  databases are never restored over newer evidence.
* Workflows: re-publish the previous n8n version (kept in history and in the workflow backup folder).
* C4: deleting the column stops records; the store remains the cache it was before.

## 7. Containment while not deployed

See `HANDOFF_STATUS.md` (Slack wording to avoid, the two Post items sharing a video, Paused/Skipped → Posted).
