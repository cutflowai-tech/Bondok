#!/usr/bin/env bash
# End-to-end fixture call on the PBX host (no phone rings): Local channel -> 111's agent route as line 99019 ->
# bridge -> ElevenLabs -> Bondok. Plays synthetic questions, measures when Bondok speaks, deletes the recording.
set -euo pipefail
MON=/var/spool/asterisk/monitor
before=$(date +%s)
asterisk -rx "channel originate Local/s@bondok-voice-fixture application Playback silence/6&bondok-fixture/q1&silence/10&silence/4&bondok-fixture/q2&silence/10&silence/8" >/dev/null
sleep 52
f=$(ls -t $MON/bondok-fixture-*-bondok.wav 2>/dev/null | head -1)
[[ -n "$f" && $(stat -c %Y "$f") -ge $before ]] || { echo "no fixture recording"; exit 1; }
python3 - "$f" <<'PY'
import sys, wave, array, json
w = wave.open(sys.argv[1]); rate = w.getframerate(); a = array.array('h', w.readframes(w.getnframes()))
win = rate // 10; segs, cur = [], None
for i in range(0, len(a) - win, win):
    e = sum(abs(x) for x in a[i:i + win]) / win
    t = i / rate
    if e > 300 and cur is None: cur = t
    if e <= 300 and cur is not None:
        if t - cur > 0.3: segs.append((round(cur, 1), round(t, 1)))
        cur = None
print(json.dumps({'bondok_speech_segments_s': segs, 'seconds': round(len(a) / rate, 1)}))
PY
rm -f "${f%-bondok.wav}".wav "${f%-bondok.wav}"-caller.wav "$f"
echo "recordings deleted"
