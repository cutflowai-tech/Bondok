# Bondok on the telephone (extension 111)

The same Bondok — agent, tools, verifier, `waset_ops` owner-authority model, store and history — reached by phone.
Egyptian Arabic in, Egyptian Arabic out (young adult male voice). Addresses, ids and secrets are not in this repo.

## Architecture
```
office phone (PJSIP, digest auth) ─ Asterisk: 111 → [bondok-voice-agent]
  identity = ${CHANNEL(endpoint)} (authenticated endpoint, never caller ID) → last 12 digits of the AudioSocket UUID
  └─ AudioSocket 127.0.0.1:9020 ─ Bondok bridge (PBX host, container "bondok-voice", Sara's image and audio code)
       ├─ HMAC-signed POST /session over Tailscale → Bondok host (bondok-voice.service): role from the mapping,
       │    session token + greeting; /session/end at hangup (the call's words are dropped)
       └─ WebSocket (signed URL) ─ ElevenLabs agent "Bondok (111)": Scribe realtime ASR, turn-taking, barge-in,
            TTS eleven_v4_turbo, voice "Ahmad - Conversational AI Voice" (ihycSANIrpHfhWoaq1g3)
              └─ custom LLM (HTTPS, nginx → 127.0.0.1:8790, Bearer key) = bondok/voice.py
                   └─ Core.handle(voice=True): deterministic paths, model + verifier, bridge → waset_ops commands
```
Bondok decides every word; there is no hosted backup model. Replies are short spoken Arabic; what the tools did is
spoken by trusted code (`spoken_outcomes`): *اتنفذ* only for completed, *اتقبل ولسه بيتنفذ* for accepted,
*ماتنفذش* for refused, and a single question for confirmations ("قول اه … أو لا"). Times are Cairo time.

### Reused from Sara (voice-monday-mvp), unchanged
`voice_agent.telephony.el_bridge`: AudioSocket framing, 8↔16 kHz resampling, ringback while connecting, 20 ms pacer
and keep-alive, barge-in (`interruption` clears queued audio), ping/pong, signed-URL prefetch, hangup handling; the
image, the dialplan pattern (UUID tail, busy tone when the agent cannot start) and the ASR/TTS settings.
**Not reused:** Sara's prompt, tools, Redis, typing sound, outbound and prank features, port, env file and agent.
Sara's two words-carrying log events are suppressed for Bondok's calls.

## Identity and permissions
* Identity comes only from the authenticated PJSIP endpoint (endpoints use username digest auth; no IP identify
  sections). Spoken names and caller ID never grant anything.
* Roles (`/etc/bondok-voice/voice-access.json`, explicit, no default): `owner` acts as the Bondok owner; `member`
  asks questions, cannot change anything (same rule as Slack members); `test` acts as the owner on items isolated
  from Instagram only (no other item, no schedule/rules/global actions, no approval by id). An extension not listed
  has no access and never reaches the model.
* Mapped on the owner's instruction (2026-10-16): **101, 102 and 106 → owner** (102 is the owner's line; all three
  act with owner authority and every session records which extension called), plus the fixture line `99019`
  (role `test`, reachable only by an originate on the PBX host). Other extensions: no access.

## Turns, corrections, duplicates
* The ElevenLabs agent has `speculative_turn` off and normal eagerness: Bondok receives final transcripts only.
  That alone does not prove a correction cannot be overtaken; see the commit gate and the phone tests.
* Commit gate (telephone only): a change is submitted no earlier than 1.5 s after its turn arrived, and not at all
  if the bridge heard the caller speak again in that time or ElevenLabs dropped the request (barge-in). A held
  change is **not executed and not asked about**; the caller's next words, read with the earlier ones, decide.
  This narrows the window, it does not close it: a correction that comes after submission meets a change that is
  done; Bondok says so ("كان اتنفذ") and reverses it with the opposite action where one exists (pause/resume,
  schedule/cancel); some actions have no reversal (a publication that has started).
* Each turn's memory records what every action really did (completed, accepted, superseded, refused).
* One turn per call at a time, in order. Each turn has a stable key (call, position, text); its answer is stored,
  so a provider retry returns the same answer and runs nothing. Command ids derive from it (`voice:<call>:<key>`).
* Dropped call: if the line drops before the answer was streamed, the same request from the same line within
  10 minutes is answered from the record ("وصلني في المكالمة اللي فاتت… ومش هكرره"), not executed again.
* Slow tools: after 1.2 s a short buffer phrase is spoken ("ثانية واحدة... "), then the answer.
* Interrupting Bondok stops its speech; it cancels nothing by itself.

## Privacy and retention
* Bondok host: no audio, no transcript on disk. The call's words live in memory for the call (multi-turn context)
  and are dropped at hangup. Audit: `voice_session` rows (call id, endpoint, role, how verified) and the usual
  command/result records. Turn records hold the spoken answer for dedupe.
* PBX host: real calls are not recorded; bridge logs carry timings, endpoint and role, never words (filter checked
  by the self-test, 5 MB × 3 rotation). The originate-only fixture line records both directions for its own check
  and deletes the files right after.
* ElevenLabs, configured: audio saving off (`record_voice: false`), `delete_audio` and `delete_transcript_and_pii`
  on, retention **0 days** (ElevenLabs: scheduled deletion after the call); transcript events not sent to the
  bridge. Zero-retention mode is **not** available: ElevenLabs refuses it for an agent with a custom LLM. Configured
  deletion is not verified deletion: what the API still shows after the test calls is recorded in the test report.
  ElevenLabs keeps billing/usage records regardless. OpenRouter (Bondok's model) receives the turn text, as for
  Slack.

## Cost and budget
* Rate observed once on this account (a Sara call): 171 credits for 0.43 min, ≈ 400 credits per agent-minute.
  Bondok's own calls are billed per conversation; the bridge records the billed credits when ElevenLabs reports them.
* Approved budget (2026-10-16): test calls only, about 10 minutes. Two limits, measured separately in the bridge's
  ledger (`/var/lib/bondok-voice/ledger.json`, `bondok_bridge.py --budget`):
  * call time: measured seconds per call against 600 s; each call is granted only what remains and is cut off
    there (two calls never both get it). This is a duration limit, not a spending cap;
  * credits: billed credits per call + other billed usage (fixture TTS: 43 credits, from the ElevenLabs history)
    against a 4,000-credit ceiling, checked before each call; a call whose bill is unknown counts at an estimate
    (marked as such). One call can overshoot the ceiling by its own cost.
  * No call starts when the account's remaining credits would fall below 15,000 (kept for Sara).
* The agent is also capped at 2 concurrent calls, 40 calls/day, 10 minutes per call, 40 s of silence, no burst.
* Bondok's model turns are billed by OpenRouter (Bondok's existing key).

## Operations
| What | How |
|---|---|
| Bondok side | `bondok-voice.service` (`deploy/bondok-voice.service`), config `/etc/bondok-voice/voice.env` (0400 bondok), mapping `/etc/bondok-voice/voice-access.json` |
| Public endpoint | nginx vhost (`deploy/voice/nginx-bondok-voice.conf`), only `POST /v1/chat/completions`, no access log |
| PBX side | `docker compose -p bondok-voice -f deploy/voice/compose.yml up -d`, config `/etc/bondok-voice/bridge.env` |
| Route | `deploy/voice/pbx_route.sh apply|revert|status` (own include file + one marked block; dialplan reload only) |
| Agent | `deploy/voice/el_agent.py` (create/update; prints ids only) |
| Health | `GET /health` on both Bondok ports; `docker compose -p bondok-voice ps`; `pbx_route.sh status` |
| Disable | PBX: `touch /etc/bondok-voice/switch/disabled` (calls get the busy tone at once); Bondok: `VOICE_ENABLED=0` or `systemctl stop bondok-voice`. Slack, scheduling and publishing are unaffected |
| Rollback | `pbx_route.sh revert`, `docker compose -p bondok-voice down`, `systemctl disable --now bondok-voice`, remove the nginx vhost; Bondok code rolls back with `deploy.py` |
| Backup | the Bondok store backup covers voice audit/turn rows; `/etc/bondok-voice` is included in the host config backup |

## Tests
* `tests/test_voice.py`: identity (unmapped line, forged/expired session, spoken name), owner path and spoken
  outcomes, confirmation then "اه", model failure, member refusal, test-line isolation, no words on disk, dropped-call
  dedupe, deliberate repeat, two concurrent calls, HTTP auth and SSE format, signed fresh sessions.
* `tests/test_voice_deploy.py`: agent settings (final turns only, no backup model, privacy, limits, voice), dialplan
  identity rule, route scope, templates free of secrets and addresses.
* `deploy/voice/bridge_selftest.py` (inside Sara's image, no network): endpoint from UUID, signed session, token only
  in `custom_llm_extra_body`, greeting by role, audio both ways, barge-in stops speech, session end, disable switch,
  no words in logs.

## Limitations
* Some confirmation and refusal texts come from Bondok's English summaries; the voice reads them as they are.
* "Delivered" means the answer was streamed to ElevenLabs; a hangup during speech after that counts as delivered.
* The Starter quota is shared with Sara: heavy Bondok use would stop both until the reset (the 15,000 reserve
  stops Bondok first).
* Mutation tests use the dedicated isolated item ACC90 (`/root/acceptance/voice-fixtures.json`), never the
  acceptance suite's items; it is restored to its baseline after each test.
