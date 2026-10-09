# Scam Guard API (v1)

On-device live scam-call warning. Audio and analysis stay on the Raspberry Pi; clients on the same
network talk to it over HTTP and a WebSocket.

- **Base URL:** `http://<pi-ip>:8000` (port is `port` in `config/scam_guard.yaml`)
- **Interactive docs:** `GET /docs` · OpenAPI spec: `GET /openapi.json` · event schemas: `GET /events/schema`
- **Auth:** none. Use only on a private Wi-Fi or hotspot. CORS is off (native apps are unaffected;
  a browser page served from another origin cannot call it).
- **Format:** JSON, UTF-8. Every error is `{"detail": "<text>", "code": "<machine code>"}`.

Start the service: `python -m src.scam`

## Two ways to use it

| You want | Use |
|---|---|
| Listen to a live call through the Pi's microphone | `POST /start` → watch the `/events` WebSocket → `POST /stop` |
| Check some text you already have (no microphone) | `POST /analyze` |

## Core concepts

**Risk level** (`risk`)

| Value | Meaning |
|---|---|
| `none` | Nothing flagged. **Not a guarantee a call is safe** — see `complete` on `/analyze`. |
| `watch` | Be careful: pressure, threats, or impersonation (bank, police, ...) but no request yet. |
| `warn` | Someone is asking for a code/OTP, PIN, password or card details, for money, or to install remote-access software. |

**Speaker** (`speaker`): `caller`, `user`, or `unknown`. One microphone hears both people, so this is a
model's suggestion. Never depend on it.

**Alert source** (`source`): `rule` = instant pattern match · `llm` = the on-device model · `rule+llm` =
raised by a rule and confirmed by the model.

**Segment**: one transcribed sentence or two. Ids are `s1`, `s2`, ... within a call.

### Objects

`Segment`

| Field | Type | Notes |
|---|---|---|
| `segment_id` | string | `s1`, `s2`, ... |
| `start_ms`, `end_ms` | int | milliseconds from the start of the call |
| `text` | string | committed transcript text |

`Alert`

| Field | Type | Notes |
|---|---|---|
| `segment_id` | string | the segment that triggered it |
| `risk` | `watch` \| `warn` | |
| `speaker` | `caller` \| `user` \| `unknown` | |
| `evidence` | string | the words that triggered it (rules: the matched phrase; model: the whole line) |
| `message` | string | plain-language advice to show the user |
| `source` | `rule` \| `llm` \| `rule+llm` | |
| `revision` | int | starts at 1; increases when the same alert is updated |

`Snapshot` (returned by `GET /status`, and sent first on every WebSocket connection)

| Field | Type | Notes |
|---|---|---|
| `active` | bool | a call is being monitored |
| `call_id` | string | empty when idle |
| `segments` | `Segment[]` | transcript so far |
| `alerts` | `Alert[]` | alerts so far |
| `audio_gap` | bool | the microphone stopped delivering audio — monitoring may be incomplete |
| `llm_busy` | bool | the model is analyzing right now |
| `last_check` | `{segment_id, outcome, took_s}` \| null | latest completed model review. `outcome`: `none`, `watch`, `warn`, `advice` (a "don't share your OTP" line, ignored), `invalid` |

---

## `GET /health`

Is the service up and is the language model reachable? Returns quickly (1.5 s model ping).

```json
{
  "status": "ok",
  "api_version": "1",
  "monitoring": false,
  "stt_model": "base.en",
  "rules_enabled": true,
  "analysis_log": false,
  "llm": { "url": "http://localhost:8080", "model": "local", "reachable": true, "mode": "grammar", "detail": "HTTP 200" }
}
```

`status` is `"degraded"` (HTTP 200) when the model is unreachable: rule-based warnings still work.
`llm.mode` is `grammar` (fast, reply format enforced by the server) or `schema` (JSON fallback, slower).
`rules_enabled: false` means the model alone raises alerts.

## `POST /start`

Start monitoring a call through the microphone. No body.

`200`
```json
{ "ok": true, "call_id": "c80f56e" }
```

`409` — `already_monitoring` or `mic_unavailable`
```json
{ "detail": "Could not open the microphone (Device busy)", "code": "mic_unavailable" }
```

Connect to the `/events` WebSocket **before** calling this so no early warning is missed.

## `POST /stop`

Stop monitoring. Safe to call when nothing is running.

```json
{ "ok": true, "was_active": true }
```

## `GET /status`

The full current state — a `Snapshot`. Idle example:

```json
{ "active": false, "call_id": "", "segments": [], "alerts": [], "audio_gap": false, "llm_busy": false, "last_check": null }
```

## `POST /analyze`

Analyze conversation text with the same pipeline as live monitoring (pattern rules, then one model review,
then the refusal/safety-advice check). No microphone, no call state. The call blocks until the model replies:
**use a client timeout of about 3 minutes**; typical on the Pi is a few seconds to tens of seconds, but an
overloaded model server can take longer (it may retry once with a different reply constraint). It shares the
single model with live monitoring, so it can be slower during a call.

**Request** — provide exactly one of `segments` or `text`

| Field | Type | Default | Notes |
|---|---|---|---|
| `segments` | string[] | — | lines in order; 1–40 lines, each 1–500 characters |
| `text` | string | — | free text (max 4000 chars), split into sentences |
| `context` | string[] | `[]` | earlier lines (max 20) read alongside the new ones; not judged themselves |
| `use_llm` | bool | `true` | `false` = rules only (instant, no model call) |

```bash
curl -s http://<pi-ip>:8000/analyze -H 'Content-Type: application/json' \
  -d '{"segments": ["Hello?", "Please read that code to me."]}'
```

**Response** `200`

```json
{
  "risk": "warn",
  "complete": true,
  "alerts": [
    {
      "segment_id": "s2",
      "risk": "warn",
      "speaker": "caller",
      "evidence": "read that code",
      "message": "A request to share a verification code, PIN or password was heard. Don't share it; verify through the official app or a number you looked up yourself.",
      "source": "rule+llm",
      "revision": 1
    }
  ],
  "segments": [
    { "segment_id": "s1", "text": "Hello?", "skipped_by_llm": true,
      "rule": { "level": "none", "label": "", "evidence": "", "shadow": false } },
    { "segment_id": "s2", "text": "Please read that code to me.", "skipped_by_llm": false,
      "rule": { "level": "warn", "label": "secret", "evidence": "read that code", "shadow": false } }
  ],
  "llm": {
    "used": true, "skipped_reason": null, "risk": "warn", "segment_id": "s2", "speaker": "caller",
    "vetoed": false, "reply": "warn s2 caller", "mode": "grammar",
    "duration_s": 2.1, "prompt_tokens": 38, "output_tokens": 5, "error": null
  },
  "rules_enabled": true,
  "took_s": 2.2
}
```

| Field | Notes |
|---|---|
| `risk` | highest risk among `alerts`, else `none` |
| `complete` | **`false` if the model was requested but failed** (timeout, server down, unreadable reply). `risk: "none"` with `complete: false` is *not* an all-clear. Rule alerts are still returned. |
| `alerts` | at most one per line; if rules and the model both flag a line, one alert with `source: "rule+llm"` and the higher risk |
| `segments[].skipped_by_llm` | one- or two-word lines ("Hello?") are never sent to the model |
| `segments[].rule` | what the pattern rules concluded for that line. `label`: `secret`, `remote`, `payment`, `qr`, `giftcard`, `pressure` or empty. `shadow: true` when rules are switched off — reported for comparison, but they raise no alert |
| `llm.used` | the model gave a usable reply |
| `llm.skipped_reason` | set when no model call was needed (e.g. every line too short) |
| `llm.vetoed` | the model flagged a "don't share your OTP" style line and it was ignored |
| `llm.error` | set on a transport or parse failure (HTTP is still `200`) |

Other results:

- A refusal/safety-advice line (`"Please don't share your OTP with anyone."`) → `risk: "none"`, `alerts: []`, `llm.vetoed: true`.
- Only trivial lines (`["Hello?"]`) → `risk: "none"`, `llm.used: false`, `llm.skipped_reason: "every line is too short to carry a request"`.
- Model down but a rule matched → `risk: "warn"`, `complete: false`, `llm.error: "Read timed out"`, alert `source: "rule"`.

**Invalid input** → `422`
```json
{ "detail": "body: Value error, provide exactly one of 'segments' or 'text'", "code": "invalid_request" }
```

---

## `WS /events` — live events

`ws://<pi-ip>:8000/events`

1. **The first message is always a `snapshot`** of the whole current state (idle or mid-call), so a client that
   connects late or reconnects catches up immediately.
2. After that, each message is one event below. All except `snapshot` carry `call_id`, `event_id` and `at_ms`.
3. **Keep-alive:** the server uses standard WebSocket ping/pong. Sending any text (e.g. `"ping"`) every ~15 s
   is optional, but helps your client notice a dead connection. If no client is connected for 60 s during a
   call, the Pi stops monitoring (`stopped`, `reason: "connection_lost"`).
4. **De-duplicate with `event_id`** (increases by 1 per event) after a reconnect.

| `type` | When | Extra fields |
|---|---|---|
| `snapshot` | first message on every connection (`event_id` is 0) | all `Snapshot` fields |
| `listening` | monitoring started | `message` |
| `transcript` | a sentence was transcribed | `segment_id`, `start_ms`, `end_ms`, `text` |
| `processing` | the model started (`active: true`) or finished/failed (`active: false`) | `active`, `message` |
| `warning` | an alert was raised **or updated** | all `Alert` fields + `vibrate` |
| `checked` | a model review completed, whatever the result | `segment_id`, `outcome`, `took_s` |
| `info` | informational, e.g. "Audio resumed" | `message` |
| `error` | `code`: `stt_failed` or `no_audio` (microphone silent — monitoring may be incomplete) | `code`, `message` |
| `stopped` | monitoring ended; `reason`: `user`, `max_session`, `connection_lost`, `service_exit` | `reason`, `message` |

Examples

```json
{"type": "snapshot", "event_id": 0, "active": false, "call_id": "", "segments": [], "alerts": [], "audio_gap": false, "llm_busy": false, "last_check": null}
{"call_id": "c6d23fd", "event_id": 1, "type": "listening", "at_ms": 0, "message": "Monitoring started"}
{"call_id": "c6d23fd", "event_id": 3, "type": "transcript", "at_ms": 750, "segment_id": "s2", "start_ms": 2100, "end_ms": 4300, "text": "Please read that code to me."}
{"call_id": "c6d23fd", "event_id": 4, "type": "warning", "at_ms": 750, "vibrate": true, "segment_id": "s2", "risk": "warn", "speaker": "unknown", "evidence": "read that code", "message": "A request to share a verification code, PIN or password was heard. Don't share it; verify through the official app or a number you looked up yourself.", "source": "rule", "revision": 1}
{"call_id": "c6d23fd", "event_id": 7, "type": "warning", "at_ms": 3100, "vibrate": false, "segment_id": "s2", "risk": "warn", "speaker": "caller", "evidence": "read that code", "message": "A request to share a verification code, PIN or password was heard. Don't share it; verify through the official app or a number you looked up yourself.", "source": "rule+llm", "revision": 2}
{"call_id": "c6d23fd", "event_id": 8, "type": "checked", "at_ms": 3100, "segment_id": "s2", "outcome": "warn", "took_s": 2.4}
{"call_id": "c6d23fd", "event_id": 9, "type": "stopped", "at_ms": 6200, "reason": "user", "message": "Monitoring stopped"}
```

**`warning` semantics.** Alerts are keyed by `segment_id`. The first event for a segment has `revision: 1`. If the
model later confirms or refines it (speaker, `source: "rule+llm"`), you get another `warning` for the same
`segment_id` with a higher `revision`: **update the existing card, don't add a second one.** Vibrate / sound only
when `vibrate` is `true` — a new `warn`, or a `watch` upgraded to `warn`. A result of nothing flagged is
reported by `checked` (`outcome: "none"`), never by a `warning`.

## Building a mobile app

See [mobile_integration.md](mobile_integration.md) for platform setup, background behaviour, reconnect rules,
data models and sample code for Android, iOS and React Native.

## Errors

| HTTP | `code` | When |
|---|---|---|
| 409 | `already_monitoring` | `POST /start` while a call is already being monitored |
| 409 | `mic_unavailable` | the microphone is busy or missing (e.g. the voice assistant holds it without a shared device) |
| 422 | `invalid_request` | bad or missing input to `/analyze` |

## Typical live client

```text
1. GET  /health                       → show service/model status
2. open WS /events                    → first message: snapshot (render it)
3. POST /start                        → 200 {call_id}   (409 → show detail)
4. on 'transcript'                    → append to the transcript
5. on 'warning'                       → upsert card by segment_id; vibrate if vibrate == true
6. on 'checked' / 'processing'        → optional "analysis running / last analyzed" status
7. on 'error' no_audio                → show "monitoring may be incomplete"
8. POST /stop  (or on 'stopped')      → show the final transcript and alerts
```

```javascript
const ws = new WebSocket("ws://PI_IP:8000/events");
const cards = new Map();                       // segment_id -> alert
setInterval(() => ws.readyState === 1 && ws.send("ping"), 15000);
ws.onmessage = (m) => {
  const e = JSON.parse(m.data);
  if (e.type === "snapshot") { cards.clear(); e.alerts.forEach(a => cards.set(a.segment_id, a)); }
  if (e.type === "warning") { cards.set(e.segment_id, e); if (e.vibrate) navigator.vibrate?.([400, 150, 400]); }
};
await fetch("http://PI_IP:8000/start", { method: "POST" });
```

## Limits and behaviour to know

- One call is monitored at a time; the model serves one request at a time (live reviews and `/analyze` share it).
- Analysis log: if `analysis_log` is set, transcripts (never audio) are written to a file on the Pi; `/health`
  reports `analysis_log: true`.
- A clean result is never proof a call is safe: speech recognition can mishear, and one microphone hears
  both people.
