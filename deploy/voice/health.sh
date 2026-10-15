#!/usr/bin/env bash
# Bondok voice health on the PBX host: bridge container, its listener, Bondok's session API, the route.
set -u
ok=1
st=$(docker inspect -f '{{.State.Status}}' bondok-voice-bridge-1 2>/dev/null || echo missing)
echo "bridge container: $st"; [[ $st == running ]] || ok=0
ss -ltn 'sport = :9020' | grep -q LISTEN && echo "listener 127.0.0.1:9020: up" || { echo "listener: down"; ok=0; }
url=$(grep -E '^BONDOK_SESSION_URL=' /etc/bondok-voice/bridge.env | cut -d= -f2-)
curl -fsS -m 5 "$url/health" >/dev/null && echo "bondok session api: up" || { echo "bondok session api: down"; ok=0; }
[[ -e /etc/bondok-voice/switch/disabled ]] && echo "switch: DISABLED" || echo "switch: enabled"
asterisk -rx 'dialplan show 111@office-internal' | grep -q bondok-voice && echo "route 111: present" || echo "route 111: absent"
exit $((1 - ok))
