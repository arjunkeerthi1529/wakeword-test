# Integrating Scam Guard into a mobile app

For developers building the phone app (Android, iOS, React Native/Flutter) that talks to the Raspberry Pi.
The endpoints and every field are specified in [scam_api.md](scam_api.md); this guide covers what that
reference doesn't: how a phone app should connect, stay alive during a call, recover, and present warnings.

## 1. How it fits together

```
 caller ──speakerphone──► [ Pi microphone ] ──► speech-to-text ──► rules + on-device model
                                                                          │
   Phone app  ◄──── WebSocket  /events  (transcript, warning, ...) ──────┘
              ────► HTTP  /start  /stop  /status  /health  /analyze
```

- The **Pi** listens to the call (phone on speakerphone next to it), transcribes, and decides. No audio ever
  reaches the phone or any cloud.
- The **app** is a remote control and display: it starts/stops monitoring, shows the live transcript, and turns
  `warning` events into alerts the user cannot miss.
- The app needs **no microphone permission**. It needs network access, notifications and vibration.
- Phone and Pi must be on the **same private Wi-Fi or hotspot**. There is no cloud relay.

What the app does **not** do: speech recognition, risk analysis, or reading the call audio.

## 2. Integration steps

1. **Connect:** the user enters or scans the Pi address (section 3). Verify with `GET /health`.
2. **Open the WebSocket first:** `ws://<pi-ip>:8000/events`. The first message is a `snapshot` of the whole
   current state. Render it.
3. **Start monitoring:** `POST /start` → `{"ok": true, "call_id": "..."}`. A `409` means `already_monitoring`
   (just keep listening to the socket) or `mic_unavailable` (show `detail`).
4. **Handle events** (section 6). Every alert arrives as a `warning` event.
5. **Stop:** `POST /stop` (idempotent), or react to a `stopped` event.

The full flow for a screen showing a live call:

```text
on screen open  : GET /health → connect WS → render snapshot
user taps Listen: POST /start
events          : transcript → append · warning → upsert card (+ vibrate/notify if vibrate) · checked → status line
user taps Stop  : POST /stop → keep showing the final transcript and alerts
```

## 3. Finding and addressing the Pi

| Approach | Notes |
|---|---|
| **Manual IP entry** | Simplest and always works: the user types the address shown in the Pi's log (`http://<pi-ip>:8000`) or from `hostname -I` on the Pi. |
| **QR code** | Show a QR of `http://<pi-ip>:8000` on the Pi's screen/terminal; the app scans and stores it. |
| **Hostname** | The Pi's hostname (`makeathon21`) is usually reachable as `makeathon21.local` through mDNS, but Android support is inconsistent. Offer it as an option, never the only one. |
| **Stable address** | Recommend a DHCP reservation for the Pi on the router so the saved address doesn't go stale. |

Store the address and port; let the user change it. On every launch call `GET /health` and show one of:
*Connected*, *Model unavailable* (`status: "degraded"`: rule-based warnings still work), or *Pi unreachable*.

Some phone hotspots and guest Wi-Fi networks block devices from reaching each other even though both are
"connected". If `/health` times out, tell the user to try a normal router.

## 4. Platform setup

### Android

Permissions (`AndroidManifest.xml`):

```xml
<uses-permission android:name="android.permission.INTERNET" />
<uses-permission android:name="android.permission.POST_NOTIFICATIONS" />   <!-- API 33+: ask at runtime -->
<uses-permission android:name="android.permission.VIBRATE" />
<uses-permission android:name="android.permission.FOREGROUND_SERVICE" />
<!-- API 34+: also the permission for the foreground service type you declare -->
```

**Plain HTTP / `ws://`.** The Pi serves unencrypted traffic on the local network, and Android 9+ blocks cleartext
by default. Because the address is typed in by the user (so it can't be listed at build time), the practical
setting is:

```xml
<application android:usesCleartextTraffic="true" ... >
```

This is acceptable only because the app talks solely to a device on the user's own network. Do not send anything
sensitive to other hosts, and don't ship this setting if you later add a cloud backend without scoping it.

**Staying alive during a call (important).** The user will be on a phone call, so the app is in the background.
Android will kill an idle background socket. Run the WebSocket inside a **foreground service** with a visible
notification ("Scam Guard is listening"), and choose the service type that fits (e.g. `connectedDevice` or
`dataSync`; check current Play Store policy for your target API level). If the socket dies and the app stays
away for 60 s, the Pi **stops monitoring on purpose** (`stopped` / `connection_lost`): a privacy safeguard.

**Alerts.** Create a notification channel with `IMPORTANCE_HIGH` (heads-up) for warnings and a separate low
importance one for the persistent "listening" notification. On `warning` with `vibrate: true`, post the
notification and vibrate. Don't re-notify for a higher `revision` of the same alert.

### iOS

`Info.plist`:

```xml
<key>NSAppTransportSecurity</key>
<dict><key>NSAllowsLocalNetworking</key><true/></dict>
<key>NSLocalNetworkUsageDescription</key>
<string>Connects to your Scam Guard device on this network.</string>
```

iOS 14+ shows a one-time **Local Network** permission prompt on the first connection; explain it in your UI first.

**Background limitation.** iOS suspends an app's network connections shortly after it moves to the background, so
a WebSocket cannot be kept open during a phone call. Options, in order of simplicity:

1. **Foreground use (recommended for v1):** the user keeps the app open while the call is on speaker; show the
   warning prominently and use haptics + sound. The Pi's LED is a second, always-on signal.
2. **Push notifications** would need a server and Apple's push service, which breaks the "nothing leaves the
   device" guarantee. Decide this deliberately before building it; it is not provided by the Pi API today.

Be upfront with users about the iOS limitation in the app's onboarding.

### React Native / Flutter

Both use the platform's networking stack, so the Android and iOS settings above still apply. The standard
`WebSocket` API is enough. For background operation on Android you still need a native foreground service
(for example via a community plugin); on iOS the same suspension limit applies.

## 5. Data models

Field definitions and enums are in [scam_api.md](scam_api.md); all JSON keys are `snake_case`. Always ignore
unknown keys so the API can add fields without breaking old app versions.

### Kotlin (kotlinx.serialization)

```kotlin
@Serializable data class Segment(
    @SerialName("segment_id") val segmentId: String,
    @SerialName("start_ms") val startMs: Long,
    @SerialName("end_ms") val endMs: Long,
    val text: String,
)

@Serializable data class Alert(
    @SerialName("segment_id") val segmentId: String,
    val risk: String,            // "watch" | "warn"
    val speaker: String,         // "caller" | "user" | "unknown"
    val evidence: String,
    val message: String,
    val source: String,          // "rule" | "llm" | "rule+llm"
    val revision: Int,
)

@Serializable data class LastCheck(
    @SerialName("segment_id") val segmentId: String,
    val outcome: String,         // none | watch | warn | advice | invalid
    @SerialName("took_s") val tookS: Double,
)

val json = Json { classDiscriminator = "type"; ignoreUnknownKeys = true }

@Serializable sealed interface Event

@Serializable @SerialName("snapshot") data class SnapshotEvent(
    val active: Boolean,
    @SerialName("call_id") val callId: String,
    val segments: List<Segment>,
    val alerts: List<Alert>,
    @SerialName("audio_gap") val audioGap: Boolean,
    @SerialName("llm_busy") val llmBusy: Boolean,
    @SerialName("last_check") val lastCheck: LastCheck? = null,
    @SerialName("event_id") val eventId: Int = 0,
) : Event

@Serializable @SerialName("transcript") data class TranscriptEvent(
    @SerialName("event_id") val eventId: Int,
    @SerialName("segment_id") val segmentId: String,
    @SerialName("start_ms") val startMs: Long,
    @SerialName("end_ms") val endMs: Long,
    val text: String,
) : Event

@Serializable @SerialName("warning") data class WarningEvent(
    @SerialName("event_id") val eventId: Int,
    val vibrate: Boolean,
    @SerialName("segment_id") val segmentId: String,
    val risk: String, val speaker: String, val evidence: String,
    val message: String, val source: String, val revision: Int,
) : Event

@Serializable @SerialName("checked") data class CheckedEvent(
    @SerialName("event_id") val eventId: Int,
    @SerialName("segment_id") val segmentId: String,
    val outcome: String,
    @SerialName("took_s") val tookS: Double,
) : Event

@Serializable @SerialName("processing") data class ProcessingEvent(
    @SerialName("event_id") val eventId: Int, val active: Boolean, val message: String,
) : Event

@Serializable @SerialName("listening") data class ListeningEvent(
    @SerialName("event_id") val eventId: Int, val message: String,
) : Event

@Serializable @SerialName("info") data class InfoEvent(
    @SerialName("event_id") val eventId: Int, val message: String,
) : Event

@Serializable @SerialName("error") data class ErrorEvent(
    @SerialName("event_id") val eventId: Int, val code: String, val message: String,
) : Event

@Serializable @SerialName("stopped") data class StoppedEvent(
    @SerialName("event_id") val eventId: Int, val reason: String, val message: String,
) : Event
```

### Swift (Codable)

```swift
struct Segment: Codable { let segmentId: String; let startMs: Int; let endMs: Int; let text: String }
struct Alert: Codable {
    let segmentId: String, risk: String, speaker: String, evidence: String
    let message: String, source: String, revision: Int
}
struct LastCheck: Codable { let segmentId: String; let outcome: String; let tookS: Double }
struct Snapshot: Codable {
    let active: Bool; let callId: String; let segments: [Segment]; let alerts: [Alert]
    let audioGap: Bool; let llmBusy: Bool; let lastCheck: LastCheck?
}
struct Envelope: Decodable { let type: String; let eventId: Int }
struct WarningEvent: Decodable {
    let eventId: Int; let vibrate: Bool; let segmentId: String; let risk: String
    let speaker: String; let evidence: String; let message: String; let source: String; let revision: Int
}
struct TranscriptEvent: Decodable { let eventId: Int; let segmentId: String; let startMs: Int; let endMs: Int; let text: String }

let decoder: JSONDecoder = { let d = JSONDecoder(); d.keyDecodingStrategy = .convertFromSnakeCase; return d }()
```

### TypeScript (React Native)

```ts
type Risk = "none" | "watch" | "warn";
type Alert = { segment_id: string; risk: "watch" | "warn"; speaker: "caller" | "user" | "unknown";
               evidence: string; message: string; source: "rule" | "llm" | "rule+llm"; revision: number };
type Segment = { segment_id: string; start_ms: number; end_ms: number; text: string };
type LastCheck = { segment_id: string; outcome: "none" | "watch" | "warn" | "advice" | "invalid"; took_s: number };
type Snapshot = { active: boolean; call_id: string; segments: Segment[]; alerts: Alert[];
                  audio_gap: boolean; llm_busy: boolean; last_check: LastCheck | null };

type Event =
  | ({ type: "snapshot"; event_id: number } & Snapshot)
  | { type: "listening"; event_id: number; call_id: string; at_ms: number; message: string }
  | ({ type: "transcript"; event_id: number } & Segment)
  | ({ type: "warning"; event_id: number; vibrate: boolean } & Alert)
  | { type: "processing"; event_id: number; active: boolean; message: string }
  | ({ type: "checked"; event_id: number } & LastCheck)
  | { type: "info"; event_id: number; message: string }
  | { type: "error"; event_id: number; code: "stt_failed" | "no_audio" | string; message: string }
  | { type: "stopped"; event_id: number; reason: string; message: string };
```

The live schemas are also published by the Pi at `GET /events/schema` and the REST schemas at `/openapi.json`;
you can generate typed clients from them instead of hand-writing the models above.

## 6. Handling events: the rules that matter

Keep one state object (`active`, `segments`, `alerts` keyed by `segment_id`, `lastCheck`, `audioGap`,
`processing`, `connection`) and apply every event to it:

| Event | Do this |
|---|---|
| `snapshot` | **Replace** all state with it, and reset your `lastEventId` to 0. It is authoritative (sent on every connect/reconnect). |
| `listening` | A new call began: clear transcript and alerts, set `active = true`. |
| `transcript` | Append the segment. |
| `warning` | **Upsert** the alert by `segment_id`. A higher `revision` updates the existing card; never add a second card for the same segment. If `vibrate` is `true`, vibrate/notify **once**. |
| `processing` | `active: true` → "analyzing"; `false` → clear (a non-empty `message` on failure, e.g. "Analysis delayed — rule-based warnings still active", should be shown). |
| `checked` | Update a "last analyzed" line (outcome `none` = analyzed, nothing found). |
| `error` | `no_audio` → banner "Microphone is silent — monitoring may be incomplete"; `stt_failed` → "May have missed some speech". |
| `info` | Optional toast/banner (e.g. "Audio resumed"). |
| `stopped` | `active = false`; keep the final transcript and alerts visible; show why from `reason`. |

**De-duplication.** Ignore any non-snapshot event whose `event_id` is `<= lastEventId`, otherwise update
`lastEventId`. Resetting to 0 on `snapshot` matters: if the Pi service restarts, ids start over.

**Vibration is driven only by `vibrate`.** It is `true` for a new `warn`, or a `watch` upgraded to `warn`, and
`false` for updates, so the phone never buzzes twice for the same alert.

## 7. Connection lifecycle and reconnecting

| Situation | App behaviour |
|---|---|
| Socket drops (Wi-Fi blip, Pi busy) | Show "Connection lost". Reconnect with exponential backoff (1 s, 2 s, 4 s ... capped at ~15 s, plus jitter). On reconnect, the `snapshot` restores the transcript and alerts. |
| Back within 60 s | Monitoring continued on the Pi. The snapshot may contain transcript and alerts that arrived while you were away: **tell the user** and show any `warn` alerts they missed. |
| Away more than 60 s during a call | The Pi stopped (`connection_lost`). The reconnect snapshot has `active: false`; show "Monitoring ended while the app was disconnected" and offer to start again. |
| `POST /start` fails with `409 mic_unavailable` | Show the `detail` text. Typically the Pi's microphone is held by another program. |
| `/health` says `degraded` | The model is down; the Pi still warns using its pattern rules. Show a non-blocking notice. |
| App moved to background | Android: foreground service keeps the socket (section 4). iOS: the socket will be suspended. Warn the user at the start. |
| User taps Stop, or app is closed deliberately | Call `POST /stop`. If the app can't (crash/kill), the Pi stops itself after 60 s. |

Use a client-side keep-alive too (OkHttp `pingInterval(15, SECONDS)`, `URLSessionWebSocketTask.sendPing`, or a
periodic text frame) so a half-dead connection is noticed within seconds.

## 8. Presenting warnings

- **Lead with the risk.** `warn`: "Possible scam request" in a strong colour; `watch`: "Be careful" in a milder one.
  Don't rely on colour alone: also use an icon, text and (for `warn`) haptics.
- **Show the evidence.** Display `evidence` as a quote, then `message` as the advice.
- **Speaker is a hint.** Label it "Likely the caller / likely you / speaker unclear". Never present it as fact.
- **Never show "safe".** No alerts only means nothing was flagged. Say "No warnings so far", and show
  `audio_gap`/connection problems prominently, because they mean monitoring may be incomplete.
- **Show progress.** "Analyzing on the Pi..." from `processing`; "Analyzed through s5 in 7 s" from `checked`.
- **Make it large and calm.** The user is mid-call and possibly stressed or older. Big text, one clear action,
  no jargon, no auto-dismiss for `warn`.
- **Localization.** `message` text is English and comes from the Pi. If you localize, key your own strings on
  `risk` and `source` today (the API doesn't expose an alert category yet; see section 10).

## 9. Testing without the Pi, and what to test

**Simulator.** On a computer on your network:

```bash
python scripts/scam_sim.py --host 0.0.0.0        # then open http://<computer-ip>:8765/sim
```

It serves the same API on port 8765 with a fake model, and its control page lets you "say" lines as if heard
on a call so you can watch your app react. From an Android emulator the host machine is `10.0.2.2`; from the iOS
simulator it is `localhost`. A phone needs the computer's LAN IP.

**Stateless checks with curl** (no microphone needed):

```bash
curl -s http://<pi-ip>:8000/health
curl -s http://<pi-ip>:8000/analyze -H 'Content-Type: application/json' \
  -d '{"segments": ["Please read that code to me."]}'
```

**Checklist**

- [ ] Fresh install: enter address, `/health` shows connected / degraded / unreachable correctly.
- [ ] Open the app mid-call: the `snapshot` renders the existing transcript and alerts.
- [ ] A clear request ("please read that code to me") produces one `warn` card and exactly one vibration.
- [ ] The model's later confirmation updates the **same** card (`revision` 2, `source` `rule+llm`) with no second vibration.
- [ ] "Don't share your OTP with anyone" produces no warning.
- [ ] Greetings ("Hello?") produce no warning.
- [ ] Wi-Fi off for 10 s, then on: reconnect, state restored, user told what happened.
- [ ] Wi-Fi off for more than 60 s mid-call: app shows monitoring ended on reconnect.
- [ ] `POST /start` twice: second call handled (`already_monitoring`) without breaking the UI.
- [ ] Microphone unavailable on the Pi (`mic_unavailable`) shows its `detail`.
- [ ] Android: app in the background with the screen off still raises the heads-up notification.
- [ ] iOS: behaviour on backgrounding is explained to the user.
- [ ] Pi service restarted while the app is connected: app reconnects and `event_id` reset doesn't drop events.

## 10. Known gaps and decisions for the app team

- **No authentication or encryption.** Anyone on the same network can call the API. Fine for a private home
  network; before wider use, add an API key (sent as a header) and TLS on the Pi, and update the platform settings in
  section 4 accordingly.
- **iOS background delivery** is not solved without push notifications (section 4).
- **No alert category field.** To localize or choose icons per type (code request, remote access, payment,
  QR code, gift card, pressure), the API would need to expose one. Ask for it if needed; it is a small change on
  the Pi side.
- **Speech recognition can mishear and one microphone hears both people**, so a missing warning is never proof a
  call is safe. Say so in the app's onboarding.
- **Model latency on the Pi** is seconds to tens of seconds. Pattern-rule warnings are much faster than
  model-confirmed ones, which is why a `warning` can be followed by a higher-`revision` update.
- The snippets above were written against the API contract and have not been compiled in a mobile project;
  treat them as a starting point.
