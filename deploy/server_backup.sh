#!/usr/bin/env bash
# Retired (2026-10-15): the pre-cutover backup read the n8n container. The daily backup is deploy/state_backup.sh
# (installed as /usr/local/sbin/waset-state-backup, timer deploy/waset-backup.timer).
exec /usr/local/sbin/waset-state-backup "$@"
