# Personal Agent — Raspberry Pi Setup Guide

This document covers everything needed to run the three agents (Email Digest,
Meeting Notification, Reminder) on top of the existing voice assistant.

---

## What the agents do

| Agent | How it works |
|---|---|
| **Email Digest** | Every morning at `EMAIL_FETCH_TIME` it summarises your emails using the local LLM and stores them in SQLite. Currently uses mock data — swap for real IMAP later with no other changes. |
| **Reminder** | Say *"Remind me to drink water in 5 minutes"* — Jarvis confirms, a timer starts, and it speaks the reminder when time is up. Survives restarts (stored in SQLite). |
| **Meeting Notification** | *(Coming next)* — reads your calendar and announces meetings before they start. |

---

## Prerequisites

The voice assistant must already be working:
- `python -m src.main` starts without errors
- Saying "Hey Jarvis" gets a response
- `llama-server` is running on port 8080

If not, complete the main setup first with `bash setup_pi.sh`.

---

## Step 1 — Install agent dependencies

```bash
source ~/pi-va-env/bin/activate
cd ~/pi-voice-assistant

pip install -r requirements-agents.txt
```

This installs two packages:
- **`schedule`** — daily job runner (email digest at 08:00, etc.)
- **`python-dotenv`** — loads the `.env` file automatically

---

## Step 2 — Create your `.env` file

The `.env` file lives in the project root and is gitignored (never committed).

```bash
cd ~/pi-voice-assistant
cp .env.example .env
nano .env          # or: vim .env
```

### Key variables to check

```bash
# Must match your running llama-server port
LLM_BASE_URL=http://localhost:8080

# Time to fetch and summarise emails (24-hour format)
EMAIL_FETCH_TIME=08:00

# Set to true to have Jarvis read the digest aloud at EMAIL_FETCH_TIME
AGENT_READ_DIGEST=false

# SQLite database (created automatically in data/agent.db)
AGENT_DB_PATH=data/agent.db
```

Save and close. The app reads `.env` before anything else on startup.

---

## Step 3 — Verify the database directory

The SQLite file goes in `data/agent.db`. The code creates `data/` automatically,
but you can pre-create it if you prefer:

```bash
mkdir -p ~/pi-voice-assistant/data
```

---

## Step 4 — Run the assistant

```bash
source ~/pi-va-env/bin/activate
cd ~/pi-voice-assistant
python -m src.main
```

On startup you will see lines like:

```
2026-10-09 08:00:01  src.agent.db        INFO     SQLite opened: /home/pi/pi-voice-assistant/data/agent.db
2026-10-09 08:00:01  src.agent.db        INFO     DB schema ready
2026-10-09 08:00:01  src.agent.scheduler INFO     Scheduled daily job at 08:00 → email_agent_run
2026-10-09 08:00:01  src.agent.scheduler INFO     Agent scheduler started — 1 job(s) registered
2026-10-09 08:00:01  src.main            INFO     Agents ready — email digest at 08:00, reminders active
```

---

## Step 5 — Test the Email Agent manually

You do not need to wait until 08:00. Run this one-liner to trigger the email
fetch right now and print the stored summaries:

```bash
source ~/pi-va-env/bin/activate
cd ~/pi-voice-assistant

python3 -c "
from dotenv import load_dotenv; load_dotenv()
from src.agent.config import get_agent_config
from src.agent import db
from src.agent.email_agent import run, print_stored

cfg = get_agent_config()
conn = db.get_conn(cfg.agent_db_path)
db.init_schema(conn)
run('http://localhost:8080', conn, cfg)
print_stored(conn)
"
```

Expected output:

```
────────────────────────────────────────────────────────────
ID   IMP      SENDER                    SUMMARY
────────────────────────────────────────────────────────────
1    urgent   doctor@healthclinic.in    Reminder for appointment with Dr. Sharma tomorrow at 2 PM.
2    urgent   manager@company.com       Action required: review Q4 budget projections by Friday.
3    normal   mum@gmail.com             Mum asking if you are coming for Sunday biryani dinner.
...
────────────────────────────────────────────────────────────
```

---

## Step 6 — Test the Reminder Agent

With the assistant running, say:

> **"Hey Jarvis... remind me to drink water in 2 minutes"**

Jarvis will reply: *"Sure, I will remind you to drink water in 2 minutes."*

After 2 minutes you will hear: *"Reminder: drink water"*

Also works with absolute times:

> **"Hey Jarvis... remind me to call Mum at 6 PM"**

---

## Step 7 — Inspect the SQLite database directly

```bash
sqlite3 data/agent.db

# View email summaries
SELECT id, importance, sender, summary FROM email_summaries;

# View all reminders
SELECT id, message, fires_at, fired FROM reminders;

# Count by importance
SELECT importance, COUNT(*) FROM email_summaries GROUP BY importance;

.quit
```

---

## Step 8 — Auto-start on boot (optional)

If you want the assistant (and agents) to start automatically when the Pi
boots, use the existing systemd approach or add to `rc.local`:

```bash
sudo nano /etc/rc.local
```

Add before `exit 0`:

```bash
su - pi -c "cd /home/pi/pi-voice-assistant && source /home/pi/pi-va-env/bin/activate && python -m src.main >> /home/pi/pi-voice-assistant/logs/assistant.log 2>&1 &"
```

Or create a systemd service:

```bash
sudo nano /etc/systemd/system/jarvis.service
```

```ini
[Unit]
Description=Jarvis Voice Assistant
After=network.target sound.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/pi-voice-assistant
ExecStart=/home/pi/pi-va-env/bin/python -m src.main
Restart=on-failure
RestartSec=10
StandardOutput=append:/home/pi/pi-voice-assistant/logs/assistant.log
StandardError=append:/home/pi/pi-voice-assistant/logs/assistant.log

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable jarvis
sudo systemctl start jarvis

# Watch logs live
sudo journalctl -fu jarvis
# or
tail -f logs/assistant.log
```

---

## Troubleshooting

### "ModuleNotFoundError: No module named 'schedule'"
```bash
source ~/pi-va-env/bin/activate
pip install -r requirements-agents.txt
```

### "ModuleNotFoundError: No module named 'dotenv'"
```bash
pip install python-dotenv
```

### Email digest runs but all summaries say "low" or LLM fails
Check that `llama-server` is running and reachable:
```bash
curl http://localhost:8080/health
```
If it returns `{"status":"ok"}` the server is up. If not, start it:
```bash
~/llm/llama.cpp/build/bin/llama-server \
    -m ~/models/<your-model>.gguf \
    --host 0.0.0.0 --port 8080 -c 2048 -t 4
```

### Reminder fires but no audio
The reminder calls `tts.speak()` on a background thread. If the main TTS is
currently playing, there may be a brief audio overlap. This is expected for now.
If you hear nothing at all, test TTS directly:
```bash
python3 scripts/tts_test.py
```

### SQLite database locked error
This should not happen — the code uses WAL mode and a write lock. If you see
it, make sure you are not running two instances of the assistant at the same time:
```bash
ps aux | grep "src.main"
```

### Reminder was set but did not fire after restart
Only reminders with `delay > 30 seconds` are persisted in SQLite and recovered on
restart. Very short reminders (≤30 s) are in-memory only. Check the DB:
```bash
sqlite3 data/agent.db "SELECT * FROM reminders WHERE fired=0;"
```

---

## Environment variable reference

| Variable | Default | Description |
|---|---|---|
| `LLM_BASE_URL` | `http://localhost:8080` | llama.cpp server URL |
| `STT_MODEL_NAME` | `small.en` | faster-whisper model |
| `PIPER_VOICE` | `models/piper/en_US-lessac-low.onnx` | TTS model path |
| `WAKE_MODEL` | `hey_jarvis` | openWakeWord model |
| `WAKE_THRESHOLD` | `0.35` | Wake word sensitivity (0.0–1.0) |
| `TTS_APLAY_DEVICE` | `auto` | ALSA device for audio output |
| `EMAIL_FETCH_TIME` | `08:00` | Daily email digest time (HH:MM) |
| `AGENT_READ_DIGEST` | `false` | Speak digest aloud after fetching |
| `AGENT_DB_PATH` | `data/agent.db` | SQLite file path |

All variables can be set in `.env` (recommended) or exported in the shell.

---

## What's coming next

- **Real IMAP / Gmail** — swap the mock emails for live inbox in `src/agent/email_agent.py`
- **Google Calendar / iCal** — meeting notifications via `src/agent/calendar_agent.py`
- **Voice query** — "Hey Jarvis, read my emails" → speaks the stored digest on demand
