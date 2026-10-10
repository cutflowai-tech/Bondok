# Agent instructions (Bondok)

Read `docs/HANDOFF_STATUS.md` first, then `docs/ARCHITECTURE_AND_OWNERSHIP.md`.

* Local root: `/Volumes/Zeno/Bondok`. Work on a branch; never force-push, reset others' work or merge to `main` unasked.
* The GitHub repo is intentionally **public**: never commit server addresses, Slack/user ids, credential ids, `.env`
  contents, database files or board snapshots. Raw evidence lives in gitignored `.local-snapshots/`.
* Production changes (server files, n8n workflow import/activation, service restarts, ACLs, Monday writes, posts)
  need explicit owner authorization for the specific change set. Read-only inspection is allowed. Never run a
  workflow to "test" it; never use helper write routes against production as an access check.
* All readiness/reservation/publication transitions go through `waset_ops` commands. Do not add direct writers.
* Workflow JSON is generated: edit `workflows/build.py`, then `python3 workflows/build.py`; never hand-edit `workflows/dist`.
* Keep tests green: `cd tests && python3 -m unittest discover -s . -p 'test_*.py'` (every module; a hand-picked
  module list silently left the R5 suites out). Report collected/run/skipped counts.
  Code must run on Python 3.12 (Bondok) and 3.14 (n8n image); stdlib only in `src/`.
* Business rules are owner policy (slots, 60 s Story, Topaz, 1080 px, 300 MB). Do not change them without a decision.
