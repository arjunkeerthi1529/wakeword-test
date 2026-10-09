# Work Service API (v1)

Email digest, reminders, outbound drafts and meeting notes. Runs as its own process, independent of the
voice assistant and of Scam Guard — audio, transcription and summarisation all stay on the Raspberry Pi.

- **Base URL:** `http://<pi-ip>:8001` (port is `AGENT_PORT` in `.env`, default `8001`)
- **Interactive docs:** `GET /docs` · OpenAPI spec: `GET /openapi.json`
- **Auth:** none. Use only on a private Wi-Fi or hotspot. CORS is wide open (`*`).
- **Format:** JSON, UTF-8. Validation errors are FastAPI's standard `{"detail": [...]}`; app errors raised by
  this service are `{"detail": "<text>"}`.

Start the service: `python -m src.work`

## What's mocked right now

- **Email fetch is 8 hardcoded mock emails**, not a real inbox — `/emails/fetch` re-processes the same set
  (deduplicated, so re-fetching never creates duplicates). Swapping in real IMAP is planned with no API change.
- **Sending is mocked** — an approved/auto-sent draft is logged and marked `sent`, never actually delivered.
  There is no real SMTP yet.

## Core concepts

**Reminder.** Set with either a relative delay or an absolute time-of-day. Anything longer than 30s is
persisted (survives a restart); shorter ones are in-memory only.

**Email importance** (`importance`): `low` | `normal` | `urgent` — the model's own judgement per email.

**Draft action** (`action`): `reply` | `notify`, attached to an email that the model decided needs one (most
emails get neither and produce no draft at all).

| `action` | Goes to | Approval |
|---|---|---|
| `reply` | the **original sender** of that email | always `needs_approval: true` — nothing is sent until you call `/drafts/{id}/approve` |
| `notify` | the one address in `AGENT_NOTIFY_EMAIL` (your own) | `needs_approval: false` — sent automatically, no action needed |

This split is enforced by the backend, not the model: a `reply` is an external, visible action, so a human
always signs off on it; a `notify` only ever reaches your own configured address, so it's low-risk and
automatic. The model is never given a "to" address — it only ever supplies the content.

**Draft status** (`status`): `pending` → `approved`/`rejected` (your call) or `sent` (auto, for `notify`).

**Meeting.** One continuous recording from Start to Stop — no silence-based segmentation, so pauses in a
real meeting don't cut it short. Transcription is local (faster-whisper); the summary comes from the local LLM.

## Objects

`Reminder`

| Field | Type | Notes |
|---|---|---|
| `id` | int | |
| `message` | string | |
| `fires_at` | string | ISO-8601, local time |
| `fired` | int | `0` or `1` |
| `created_at` | string | |

`EmailSummary`

| Field | Type | Notes |
|---|---|---|
| `id` | int | |
| `sender`, `subject` | string | |
| `summary` | string | one-sentence LLM summary |
| `importance` | `low` \| `normal` \| `urgent` | |
| `raw_uid` | string | source email id, used for de-duplication |
| `fetched_at` | string | |

`Draft`

| Field | Type | Notes |
|---|---|---|
| `id` | int | |
| `source_email_uid` | string | which inbound email this came from |
| `action` | `reply` \| `notify` | |
| `to_addr` | string | decided by the backend, never the model (see above) |
| `subject`, `body` | string | drafted content |
| `reason` | string | short phrase, why the model chose this action |
| `needs_approval` | int | `0` or `1` |
| `status` | `pending` \| `approved` \| `rejected` \| `sent` | |
| `created_at`, `decided_at` | string \| null | |

`MeetingSummary` (list view — `GET /meetings`)

| Field | Type | Notes |
|---|---|---|
| `id` | int | |
| `started_at`, `ended_at` | string | ISO-8601 |
| `duration_s` | float | |
| `summary` | string | |
| `created_at` | string | |

`Meeting` (single view — `GET /meetings/{id}` and the `/meetings/stop` response) — all of the above, plus:

| Field | Type | Notes |
|---|---|---|
| `transcript` | string | full transcript; empty string if no speech was detected |

---

## `GET /health`

```json
{ "status": "ok", "reminders_pending": 2, "emails_stored": 8 }
```

## `POST /remind`

Schedule a reminder. Provide `delay_s`, or `fires_at` if `delay_s` is omitted — if both are given, `delay_s`
silently wins.

| Field | Type | Notes |
|---|---|---|
| `message` | string | required |
| `delay_s` | int | seconds from now |
| `fires_at` | string | ISO-8601 datetime, absolute — ignored if `delay_s` is also set |

```bash
curl -s http://<pi-ip>:8001/remind -H 'Content-Type: application/json' \
  -d '{"message": "drink water", "delay_s": 300}'
```

`200`
```json
{ "status": "scheduled", "message": "drink water" }
```

`400` — neither `delay_s` nor `fires_at` given, or `fires_at` isn't valid ISO-8601
```json
{ "detail": "Provide delay_s or fires_at" }
```

`503` — the reminder agent isn't initialised yet (startup race; shouldn't normally happen)
```json
{ "detail": "Agent service not initialised" }
```

## `GET /reminders`

Pending reminders only (not yet fired, still in the future). Returns `Reminder[]`. Note: a reminder set for
30s or less never appears here (in-memory only) — it still fires on time, it just has no persisted row to list.

## `GET /emails`

All stored email summaries, newest first. Returns `EmailSummary[]`.

## `POST /emails/fetch`

Triggers an immediate fetch + per-email LLM analysis (summary, importance, and a possible draft). Same job
the daily schedule (`EMAIL_FETCH_TIME`) runs. **Blocks until every email is processed** — budget a client
timeout of a few minutes; each email is one LLM call.

```json
{ "status": "done", "total_stored": 8 }
```

## `GET /drafts`

Returns `Draft[]`, newest first. Optional query param:

| Param | Notes |
|---|---|
| `status` | `pending` \| `approved` \| `rejected` \| `sent` — omit for all |

```bash
curl -s "http://<pi-ip>:8001/drafts?status=pending"
```

## `POST /drafts/{id}/approve`

Approve a pending draft and send it (mock). Only valid while `status == "pending"`.

`200`
```json
{ "status": "sent", "id": 1 }
```

`404` — no such draft · `409` — already `approved`/`rejected`/`sent`
```json
{ "detail": "Draft is already sent" }
```

## `POST /drafts/{id}/reject`

Discard a pending draft without sending it. Same `404`/`409` rules as approve.

```json
{ "status": "rejected", "id": 4 }
```

## `POST /meetings/start`

Begin recording from the Pi's microphone. Returns immediately; recording continues in the background.

`200`
```json
{ "status": "recording", "started_at": "2026-10-10T00:30:30" }
```

`409` — already recording
```json
{ "detail": "Already recording" }
```

`503` — faster-whisper isn't installed on this Pi (nothing to transcribe with)
```json
{ "detail": "Speech-to-text not available (faster-whisper not installed)" }
```

## `POST /meetings/stop`

Stop recording, transcribe locally, summarise via the local LLM, persist, and return the full `Meeting`.
**Blocks until transcription + summarisation finish** — budget a client timeout of a few minutes for a long
recording.

`200` — returns a `Meeting` (see Objects above)
```json
{
  "id": 3,
  "started_at": "2026-10-10T00:30:30",
  "ended_at": "2026-10-10T00:31:02",
  "duration_s": 32.1,
  "transcript": "Okay let's get started. First item is the budget review...",
  "summary": "The meeting covered the budget review and a vendor decision for new laptops.\n- Priya to send budget projections by Friday\n- Rahul to present vendor quotes next week"
}
```

If nobody spoke, `transcript` is `""` and `summary` is `"No speech was detected in this recording."` — not
an error, just an honest empty result.

`409` — not currently recording
```json
{ "detail": "Not recording" }
```

## `GET /meetings`

List past meeting notes, newest first. Returns `MeetingSummary[]` — **no `transcript` field**, to keep the
list payload light. Fetch `GET /meetings/{id}` for the full transcript of one entry.

## `GET /meetings/{id}`

One meeting note with its full transcript. Returns a `Meeting`. `404` if it doesn't exist.

---

## Errors

| HTTP | When |
|---|---|
| 400 | `/remind` given neither `delay_s` nor `fires_at`, or an invalid `fires_at` |
| 404 | `/drafts/{id}/...` or `/meetings/{id}` — id doesn't exist |
| 409 | `/drafts/{id}/approve\|reject` on a draft that's no longer `pending`; `/meetings/start` while already recording; `/meetings/stop` while not recording |
| 422 | malformed request body (FastAPI's own validation) |
| 503 | `/meetings/start` when faster-whisper isn't installed |

## Typical client flows

**Reminders**
```text
1. POST /remind  {message, delay_s}        → 200 {status: "scheduled"}
2. (later) GET /reminders                  → confirm it's still pending, or it's gone because it fired
```

**Email digest + approval**
```text
1. POST /emails/fetch                      → 200 {total_stored}         (blocks — show a spinner)
2. GET /emails                             → render the list (importance badge per row)
3. GET /drafts?status=pending               → anything here needs a human decision
4. user taps Approve/Reject                → POST /drafts/{id}/approve or /reject
5. GET /drafts                             → refresh to show updated statuses
```

**Meeting recording**
```text
1. POST /meetings/start                    → 200 {status: "recording"}   — show a recording indicator + timer
2. (user talks, then taps Stop)
3. POST /meetings/stop                     → 200 {transcript, summary}   (blocks — show "transcribing…")
4. GET /meetings                           → refresh the history list
```

```javascript
// Minimal fetch-based client for the meeting flow
const BASE = "http://PI_IP:8001";

async function startMeeting() {
  const r = await fetch(`${BASE}/meetings/start`, { method: "POST" });
  if (!r.ok) throw new Error((await r.json()).detail);
}

async function stopMeeting() {
  const r = await fetch(`${BASE}/meetings/stop`, { method: "POST" });
  if (!r.ok) throw new Error((await r.json()).detail);
  return r.json();   // { id, transcript, summary, ... }
}
```

## Limits and behaviour to know

- One meeting recording at a time; `/meetings/start` while already recording is a `409`, not a queue.
- `/emails/fetch` and `/meetings/stop` each **block until their own request finishes** (several sequential
  LLM calls for a fetch; one transcription + one summary for a stop) — `/health` and other endpoints stay
  responsive while one of these runs. There's no cross-request scheduler, though: if you trigger
  `/emails/fetch` while a meeting is being summarised (or vice versa), both calls compete for the same local
  model, so each takes longer than it would alone. Avoid firing both at once from the UI if you can.
- A `reply` draft's `to_addr` and a `notify` draft's `to_addr` are always backend-decided, never something
  the model supplied — don't expect (or allow a UI to show) an editable "to" field sourced from the draft
  itself without re-checking it server-side first, if this is ever extended to let users edit drafts before
  sending.
