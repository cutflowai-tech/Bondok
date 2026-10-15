#!/usr/bin/env bash
# Bondok voice route on the office PBX: apply | revert | status. Touches only Bondok's own include file and one
# marked block in extensions.conf; reloads the dialplan only (no channel is touched, no PBX restart).
set -euo pipefail
DIR=/etc/asterisk
SRC="$(dirname "$(readlink -f "$0")")/extensions_bondok_voice.conf"
DST=$DIR/extensions_bondok_voice.conf
BEGIN='; >>> bondok-voice (deploy/voice/pbx_route.sh; revert removes this block)'
END='; <<< bondok-voice'
reload() { asterisk -rx 'dialplan reload' >/dev/null; }
case "${1:-status}" in
  apply)
    if asterisk -rx 'dialplan show 111@office-internal' | grep -q "'111' =>" && \
       ! grep -qF "$BEGIN" $DIR/extensions.conf; then
      echo "refused: 111 already routed in office-internal by something else" >&2; exit 2
    fi
    ts=$(date -u +%Y%m%dT%H%M%SZ)
    cp -p $DIR/extensions.conf "$DIR/extensions.conf.bak-bondok-$ts"
    install -m 0640 -o root -g asterisk "$SRC" "$DST"
    if ! grep -qF "$BEGIN" $DIR/extensions.conf; then
      printf '%s\n[office-internal](+)\ninclude => bondok-voice\n#include extensions_bondok_voice.conf\n%s\n' \
        "$BEGIN" "$END" >> $DIR/extensions.conf
    fi
    reload
    asterisk -rx 'dialplan show 111@office-internal' | grep -q 'bondok-voice' || { echo "route not visible" >&2; exit 3; }
    echo applied ;;
  revert)
    ts=$(date -u +%Y%m%dT%H%M%SZ)
    cp -p $DIR/extensions.conf "$DIR/extensions.conf.bak-bondok-$ts"
    python3 - "$DIR/extensions.conf" "$BEGIN" "$END" <<'PY'
import sys
p, b, e = sys.argv[1:]
lines, out, skip = open(p).read().split('\n'), [], False
for l in lines:
    if l == b: skip = True; continue
    if skip and l == e: skip = False; continue
    if not skip: out.append(l)
open(p, 'w').write('\n'.join(out))
PY
    rm -f "$DST"
    reload
    echo reverted ;;
  status)
    grep -qF "$BEGIN" $DIR/extensions.conf && echo "block: present" || echo "block: absent"
    asterisk -rx 'dialplan show 111@office-internal' | grep -E "111|bondok" || true ;;
  *) echo "usage: $0 apply|revert|status" >&2; exit 64 ;;
esac
