#!/usr/bin/env bash
# Consistent pre-cutover backup ON THE SERVER (run as root, only after authorization).
# Reads production; writes only to /root/waset-v2-backup-<UTC timestamp>/ (0700). Never copies secrets off the host.
set -euo pipefail
TS=$(date -u +%Y%m%dT%H%M%SZ)
DEST=/root/waset-v2-backup-$TS
DATA=/var/lib/docker/volumes/waset_social_data/_data
install -d -m 0700 "$DEST"
# SQLite online backup as the n8n user (same view the helper has; WAL-consistent).
docker exec -u node n8n-n8n-1 python3 -c "
import sqlite3
s=sqlite3.connect('file:/home/node/.n8n-files/waset-social/state.sqlite?mode=ro',uri=True,timeout=30)
d=sqlite3.connect('/tmp/state.backup.sqlite'); s.backup(d); d.close(); print('ok')"
docker cp n8n-n8n-1:/tmp/state.backup.sqlite "$DEST/state.sqlite"
docker exec -u node n8n-n8n-1 rm -f /tmp/state.backup.sqlite
cp -p "$DATA/helper.py" "$DEST/helper.v1.py"
getfacl -p "$DATA" "$DATA"/state.sqlite* > "$DEST/acl.txt" 2>/dev/null || true
tar --exclude=venv --exclude=__pycache__ -C /opt -czf "$DEST/waset-bondok.tgz" waset-bondok   # includes .env (stays on host, 0700 dir)
cp -p /etc/systemd/system/bondok.service "$DEST/"
sqlite3 "$DEST/state.sqlite" 'PRAGMA integrity_check;' 2>/dev/null || python3 -c "import sqlite3,sys;print(sqlite3.connect(sys.argv[1]).execute('PRAGMA integrity_check').fetchone()[0])" "$DEST/state.sqlite"
( cd "$DEST" && sha256sum * > SHA256SUMS )
echo "$DEST"
