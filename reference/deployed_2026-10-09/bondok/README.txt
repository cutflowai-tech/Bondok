Bondok — Waset Co Studio
Python service on <server-ip>

Runtime: /opt/waset-bondok
Protected credentials: /opt/waset-bondok/.env (0600, bondok user)
Persistent memory, event deduplication and approval audit: /var/lib/bondok/bondok.sqlite
Model: openai/gpt-6.1-sol through OpenRouter Responses API, medium reasoning.
Service: bondok.service, enabled on boot, Restart=always, non-root user.
Slack app: <bondok-app-id> / bot <bondok-bot-user-id> / workspace <slack-workspace-id>.
Monday board: For Social Media / 5105608159.

Use
Mention @Bondok in the configured private channel, then continue within that thread.
Examples: راجع الجدولة — ليه الفيديو ده متوقف؟ — اقترح كابشن — حوّل الفيديو ده لبوست.
Bondok reads the live board and prepares a concrete change. The configured owner replies
اعتمد B-XXXXXXXX
in the same thread within 30 minutes. Stale proposals and replayed approvals are rejected.
The owner Slack ID is <owner-slack-id>. Other members may read/ask but cannot approve writes.

Operational scope
Read board schema, all items (paginated internally), recent item updates; audit scheduling.
Prepare approved caption/notes/code/owner/collaboration/variety edits, format/source/Topaz
changes, pause, resume, recheck, verified reschedule, add or archive items and comments.
Publication receipts, technical verification fields, schema deletion and changing the
publishing rules are intentionally not model tools. No shell, arbitrary GraphQL or URL tool.
Board restriction is enforced by application code. The reused Monday credential may have
broader account permissions; for credential-level isolation replace it with a dedicated
Monday account/service identity granted access only to this board.

Existing n8n preparation, due-time publisher and automatic schedule supervisor stay active.
Bondok's 30-minute audit alerts only when findings change. It uses the model for explanation;
existing n8n owns automatic repairs. Bondok does not independently post to Instagram.
Python board writes acquire the same preparation lease used by n8n. Publishing receipts
and the near-due window prevent unsafe concurrent editing. Changed media/type invalidates
prior schedule and QA; rescheduling requires stored verified media and a free approved slot.

Service operations (server)
systemctl status bondok.service
systemctl restart bondok.service
journalctl -u bondok.service --since '1 hour ago'
To stop Bondok only: systemctl disable --now bondok.service
This does not stop the existing n8n automations.

Secure Slack setup fallback
Run bash /opt/waset-bondok/connect-slack.sh in a human terminal (not chat).
It uses hidden token entry, validates workspace/bot/private membership and Socket Mode,
then atomically installs the protected env and restarts Bondok.
Never copy the actual .env into this source package. .env.example contains placeholders.

Validation
30 behavior/security tests passed in isolated SQLite fixtures, including exact 60-second
Story rejection, long Post acceptance, size/resolution boundaries, owner/channel/thread
checks, stale approvals, duplicate approvals, ambiguous API results, scope enforcement,
shared locks and publication receipt protection. Read-only live model function call +
continuation succeeded against the real board: 164 items, 31 columns. No test publications
or production board writes were used. Live Slack conversation is verified separately
once Socket Mode token and private channel membership are configured.

Dependencies
Install with venv/bin/pip install -r requirements.lock to reproduce the deployed versions.
