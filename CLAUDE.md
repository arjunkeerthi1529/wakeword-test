# pi-voice-assistant — project context

Read this first. It orients a fresh agent/session to the whole repo before touching code.
Full API references live in `docs/`; this file is the map, not the manual.

## What this is

A fully offline voice assistant + a set of independent "companion" services, all running on a
**Raspberry Pi 4** (dev/testing also happens on Windows, where some things need workarounds noted
below). Nothing talks to the cloud — wake word, STT, LLM and TTS are all on-device. GitHub repo:
`arjunkeerthi1529/wakeword-test`, branch `main`.

## The four services

Each is a **separate, independently-runnable process** (`python -m src.<name>`). They don't import
each other's internals — the only cross-service link is `src.main` making one HTTP POST to the work
service (`/remind`) when it detects a reminder intent in the LLM's reply. Otherwise each owns its own
SQLite file under `data/`, its own config, its own port.

| Service | Run with | Port | What it does | API doc |
|---|---|---|---|---|
| **Voice assistant** | `python -m src.main` | — (no HTTP server) | wake word ("Hey Jarvis") → streaming STT → LLM → sentence-streamed TTS. The main loop. | — |
| **Scam Guard** | `python -m src.scam` | 8000 | Listens to a phone call (phone on speakerphone near the Pi's mic), transcribes, flags scam patterns (rules + LLM), pushes live warnings over a WebSocket. | `docs/scam_api.md`, `docs/mobile_integration.md` |
| **Work** | `python -m src.work` | 8001 | Reminders (voice-triggered or API), daily email digest (currently 8 **mocked** emails, not real IMAP) with LLM-drafted reply/notify actions needing human approval and a synthesized **prioritized briefing** (`GET /digest` — not just a flat per-email list), and a start/stop meeting recorder (mic → faster-whisper transcript → LLM summary). | `docs/work_api.md` |
| **Financial** | `python -m src.financial` | 8002 | Expense tracker: accounts, transactions (exact integer-paise arithmetic), quick-add via natural language, CSV/PDF statement import with review, a locked 12-category system, and an Ask engine (LLM interprets the question, a fixed SQL query always computes the actual answer). | *(no dedicated doc yet — see "Financial service" section below)* |

Test dashboards (plain HTML, open directly in a browser, point the API field at the right host:port):
`tests/agent_dashboard.html` (work service). Financial has no dashboard yet — test via curl/`/docs`
(FastAPI's built-in Swagger UI) for now.

## Shared conventions (read before writing code here)

- **The LLM never does arithmetic, never writes SQL, never invents a date.** It only ever turns messy
  text into a few structured fields (extract an expense, categorize a merchant, interpret a question,
  draft a reply, summarize a transcript). Every number anyone sees comes from deterministic code. This
  is a hard line across all four services, not a style preference — breaking it undoes the main thing
  that makes any of this trustworthy enough to demo.
- **Structured LLM output must use strict `json_schema` response_format, not loose `json_object` mode.**
  Confirmed the hard way twice (financial service's `categorize_batch`, then work service's email
  `_analyze_email` — both Oct 2026): with loose `json_object` mode, a 3B model substituted its own
  field names (`"category"` for `"category_id"`, `"rows"` for `"results"`) despite the prompt spelling
  out the schema in words — silently defeating the task while still returning "valid" JSON. The same
  fix measurably improved email importance tagging too (urgent-tagging dropped from 5/8 emails to a
  sensible 1/8 once the model could no longer drift off-schema). Strict mode
  (`{"type": "json_schema", "json_schema": {"name": ..., "schema": ..., "strict": true}}`)
  on the OpenAI-compatible `/v1/chat/completions` endpoint fixes this and is confirmed working against
  **both** Ollama and llama.cpp. Always use it for anything beyond a trivial flat schema.
- **`chat_template_kwargs: {"enable_thinking": false}`** is sent on every structured call as a harmless
  no-op — without it, a Qwen3-style "thinking" model burns its whole context on invisible
  chain-of-thought and never emits the JSON answer (seen as 120s+ hangs during the financial profile's
  original build in a sibling project).
- **A model's own text is never trusted to decide a risky action.** Where "risky" is defined in code,
  not guessed by the model. Concretely: the work service's draft-approval split (a `reply` to an
  external sender always needs human approval; a `notify` to your own configured address can
  auto-send) is enforced by the backend, and the model is never even given a "to" field to fill in —
  closing off a prompt-injection path where an email body could try to redirect outbound mail.
- **`.env`** (gitignored, not committed) is the single source of config for all four services via
  `python-dotenv`. `LLM_BASE_URL` and `LLM_MODEL` are shared across services; `LLM_MODEL` matters more
  than it looks — llama.cpp ignores the `model` field (serves one loaded model) but Ollama's
  OpenAI-compatible endpoint validates it and 404s on anything not actually pulled. See `.env.example`
  for the full list (every var, every service).
- **Windows dev gotcha:** `zoneinfo.ZoneInfo("Asia/Kolkata")` raises `ZoneInfoNotFoundError` on Windows
  (no system IANA tz database) unless the `tzdata` pip package is installed. The Pi doesn't need it
  (Linux has the system tzdata), but it's harmless to install everywhere, so it's just in
  `requirements-agents.txt` unconditionally.
- **Requirements are split three ways**: `requirements.txt` (base — needed by `src.main` and
  transitively by the others: pyyaml, sounddevice, numpy, piper-tts, requests, openwakeword,
  faster-whisper), `requirements-pi.txt` (Pi-only: gpiozero, RPi.GPIO, onnxruntime), and
  `requirements-agents.txt` (work + financial: fastapi, uvicorn, schedule, python-dotenv,
  python-multipart, pymupdf, tzdata). Install all three on a fresh Pi via `setup_pi.sh`, or by hand:
  `pip install -r requirements.txt -r requirements-pi.txt -r requirements-agents.txt`.
- **No auth anywhere, CORS wide open.** Every service assumes a private Wi-Fi/hotspot. Prototype
  posture, deliberate — don't add auth unless explicitly asked.

## Financial service — more detail (newest piece, no API doc yet)

Ported from a separate reference project's "financial profile" (a richer, plug-and-play multi-profile
host) and **flattened** to match this repo's `src.work` convention: its own `config.py`/`db.py`/
`server.py`, direct synchronous LLM calls (`llm_client.py` — no shared scheduler/queue), and **blocking
endpoints** instead of a job-creation + polling pattern (`POST /entries/parse`, `POST /imports`, and
`POST /questions` all block until their LLM work finishes, then return the real result directly — same
tradeoff `src.work` already makes for `/emails/fetch` and `/meetings/stop`).

Files: `money.py` (paise arithmetic, enums), `categories.py` (12 locked categories + merchant aliases +
EMI detection), `periods.py` (date/period math + timezone), `parsers.py` (CSV canonical/mapped + PDF
single-line/block-format, via PyMuPDF), `llm_tasks.py` (prompts + JSON schemas for the 3 LLM tasks:
extract_expense(s), categorize_batch, interpret_question), `llm_client.py` (the actual HTTP calls),
`db.py` (schema + `FinanceRepository`), `services.py` (accounts/transactions/analytics/category
resolution), `entries.py` (quick-add parsing), `imports_service.py` (statement import pipeline),
`questions.py` (the Ask engine), `server.py` (all routes), `config.py` + `__main__.py`.

Categorization precedence (cheapest/most-certain first, LLM only as last resort): saved user merchant
rule → seeded alias (~50 known brands) → deterministic EMI-text detection (always `other`+needs_review,
zero LLM calls — an EMI line's principal/interest split is never invented) → per-import cache → LLM
`categorize_batch` (≤8 rows at a time).

Scope deliberately left out of this port (present in the reference project, not brought over): alerts,
coverage tracking, merchant-rule dedicated routes beyond the inline `remember_category` flag on
transaction create/patch. Add them later if actually needed — don't assume they exist.

**Known gap:** no `docs/financial_api.md` yet (unlike scam/work). Worth writing one in the same style
before handing this to a frontend team, mirroring how `docs/work_api.md` was written.

**Not yet verified on this Windows dev machine:** a real PDF statement import end-to-end (CSV import,
categorization, Ask engine, summary/comparisons, and quick-add are all confirmed working live against
Ollama). The PDF parser itself is an unmodified behavioral port of an already-tested parser from the
reference project, so risk is low, but it hasn't been exercised in *this* repo yet.

## Known stale docs / drift (fix opportunistically, not urgent)

- `docs/agents-setup.md` still imports from `src.agent.*` everywhere — the module was renamed to
  `src.work` after that doc was written. Anyone following its copy-paste commands verbatim will hit
  `ModuleNotFoundError`.
- Root `README.md`'s stack table says STT is "whisper.cpp (subprocess)" and LLM is "Ollama" — the
  actual current `src.main` uses `WhisperEngine` (faster-whisper, pure Python, not the whisper.cpp
  binary) and `LlamaCppClient` (llama.cpp's OpenAI-compatible endpoint). `setup_pi.sh` still builds
  whisper.cpp too, which appears to be a leftover from before that switch.

## Where things run / how to reach them

Pi hostname: `makeathon21` (usually reachable as `makeathon21.local` via mDNS, inconsistent on
Android — always offer manual IP entry too). Dev/testing has also happened directly on a Windows
laptop against local Ollama (`http://localhost:11434`) with `.env`'s `LLM_BASE_URL`/`LLM_MODEL`
pointed there instead of the Pi's llama.cpp server — flip those two vars to switch targets, no code
change needed either way (confirmed for work and financial; the main assistant's `LlamaCppClient` is
llama.cpp-endpoint-shaped specifically, so check `src/llm/llama_cpp_client.py` before assuming it also
free-floats to Ollama without changes).
