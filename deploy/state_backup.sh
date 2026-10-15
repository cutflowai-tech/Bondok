#!/usr/bin/env bash
# Daily consistent backup of Bondok's stores ON THE SERVER (root; installed as waset-backup.timer).
# SQLite online backup (WAL-consistent) of the operational store and Bondok's conversation store, integrity-checked,
# kept 14 days under /root/bondok-backups (0700, stays on the host). No n8n dependency.
set -euo pipefail
KEEP_DAYS=${KEEP_DAYS:-14}
ROOT=/root/bondok-backups
TS=$(date -u +%Y%m%dT%H%M%SZ)
DEST=$ROOT/daily-$TS
install -d -m 0700 "$ROOT" "$DEST"
python3 - "$DEST" <<'PY'
import sqlite3, sys
dest = sys.argv[1]
for name, src in (('state.sqlite', '/var/lib/bondok/pipeline/state.sqlite'),
                  ('bondok.sqlite', '/var/lib/bondok/bondok.sqlite')):
    s = sqlite3.connect(f'file:{src}?mode=ro', uri=True, timeout=60)
    d = sqlite3.connect(f'{dest}/{name}')
    s.backup(d)
    ok = d.execute('PRAGMA integrity_check').fetchone()[0]
    d.close(); s.close()
    if ok != 'ok':
        sys.exit(f'BACKUP INVALID {name}: {ok}')
print('ok')
PY
cp -p /etc/systemd/system/bondok.service /etc/systemd/system/waset-runtime.service "$DEST/"
[ -f /etc/systemd/system/bondok-voice.service ] && cp -p /etc/systemd/system/bondok-voice.service "$DEST/"
# Configuration needed to restore (secrets: the folder is root-only and never leaves the host).
for f in /opt/waset-bondok/.env /opt/waset-bondok/runtime.env /var/lib/bondok/pipeline/instagram-token.json \
         /etc/bondok-voice/voice.env /etc/bondok-voice/voice-access.json; do
  if [ -f "$f" ]; then install -m 0600 "$f" "$DEST/$(basename "$f")"; fi
done
( cd "$DEST" && sha256sum * > SHA256SUMS )
find "$ROOT" -maxdepth 1 -name 'daily-*' -type d -mtime +"$KEEP_DAYS" -exec rm -rf {} +
echo "$DEST"
