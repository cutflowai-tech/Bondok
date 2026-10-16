#!/usr/bin/env bash
# End-to-end fixture call on the PBX host (no phone rings): Local channel -> 111's agent route as line 99019 ->
# bridge -> ElevenLabs -> Bondok. Plays a scripted sequence of synthetic utterances and silences, then reports when
# the caller spoke and when Bondok spoke (both directions recorded for this check only, then deleted).
#   fixture_call.sh <seconds to wait> <Playback list, e.g. silence/6&bondok-fixture/q1&silence/10>
set -euo pipefail
MON=/var/spool/asterisk/monitor
wait_s=${1:?seconds}; seq=${2:?playback list}
before=$(date +%s.%N)
echo "originate_at=$before"
asterisk -rx "channel originate Local/s@bondok-voice-fixture application Playback $seq" >/dev/null
sleep "$wait_s"
f=$(ls -t $MON/bondok-fixture-*-bondok.wav 2>/dev/null | head -1)
c="${f%-bondok.wav}-caller.wav"
[[ -n "$f" && -f "$c" ]] || { echo "no fixture recording"; exit 1; }
python3 - "$c" "$f" <<'PY'
import sys, wave, array, json, os
def segs(path, thr=300, min_len=0.25, gap=0.35):
    w = wave.open(path); rate = w.getframerate(); a = array.array('h', w.readframes(w.getnframes()))
    win = rate // 50; out = []; cur = last = None
    for i in range(0, len(a) - win, win):
        t = i / rate
        if sum(abs(x) for x in a[i:i + win]) / win > thr:
            if cur is None: cur = t
            last = t + win / rate
        elif cur is not None and t - last > gap:
            if last - cur >= min_len: out.append([round(cur, 2), round(last, 2)])
            cur = None
    if cur is not None and last - cur >= min_len: out.append([round(cur, 2), round(last, 2)])
    return out, round(len(a) / rate, 1)
caller, n = segs(sys.argv[1]); bondok, _ = segs(sys.argv[2])
print(json.dumps({'recording_s': n, 'start_epoch': os.path.getctime(sys.argv[1]), 'caller': caller, 'bondok': bondok}))
PY
rm -f "${f%-bondok.wav}".wav "$c" "$f"
echo "recordings deleted"
