# pi-voice-assistant

A fully offline voice assistant for **Raspberry Pi 4**. No cloud. No mocking. Every stage (wake word → STT → LLM → TTS) runs entirely on-device, with per-turn latency measurement printed after each interaction.

```
You: "Hey Jarvis ... what's the capital of France?"

──────────────────────────────────────────────────
  LATENCY REPORT
──────────────────────────────────────────────────
  STT                0.92 s
  LLM TTFT           0.47 s
  LLM total          2.31 s
  TTS synthesize     0.28 s
  TTS play           1.05 s
  End-to-end         5.12 s
  LLM speed         11.8 tok/s  (27 tok generated)
──────────────────────────────────────────────────
```

---

## Stack

| Stage | Tool | Model |
|---|---|---|
| Wake word | openWakeWord (tflite) | `hey_jarvis` |
| STT | whisper.cpp (subprocess) | `ggml-tiny.en-q5_1` (~40 MB) |
| LLM | Ollama (HTTP streaming) | `llama3.2:1b` (~700 MB) |
| TTS | Piper (ONNX) | `en_US-lessac-low` (~60 MB) |
| GPIO | gpiozero | Button + LED |

**Total RAM usage: ~970 MB** — fits on a 2 GB Pi 4.

---

## Hardware required

- Raspberry Pi 4 (2 GB or 4 GB)
- USB microphone (or USB soundcard + mic)
- Speaker or 3.5 mm audio out
- 1× LED on **GPIO 27** (listening indicator)
- 1× push button on **GPIO 17** (mute switch)
- 1× LED on **GPIO 22** (optional, online indicator, reserved)

### LED wiring

```
GPIO 27 ──[220Ω]── LED anode
                   LED cathode ── GND

GPIO 17 ──────────── Button pin 1
GND ──────────────── Button pin 2   (internal pull-up active)
```

---

## Quick start

### Step 1 — Clone the repo onto your Pi

```bash
git clone https://github.com/arjunkeerthi1529/wakeword-test.git ~/pi-voice-assistant
cd ~/pi-voice-assistant
```

### Step 2 — Run setup (once)

```bash
bash setup_pi.sh
```

This script automatically:
1. Installs system packages (`cmake`, `portaudio`, `libopenblas`, `gpiozero` dependencies)
2. Creates a Python virtualenv at `~/pi-va-env`
3. Installs all Python packages
4. Clones and compiles **whisper.cpp** from source (with OpenBLAS acceleration)
5. Downloads the `ggml-tiny.en-q5_1` whisper model
6. Installs **Ollama** and pulls `llama3.2:1b`
7. Downloads the **Piper** voice model
8. Pre-downloads the openWakeWord `hey_jarvis` tflite model
9. Adds you to the `gpio` group

> After setup, log out and back in so the `gpio` group takes effect.

### Step 3 — Run the assistant

```bash
source ~/pi-va-env/bin/activate
cd ~/pi-voice-assistant
python -m src.main
```

### Step 4 — Talk to it

1. Say **"Hey Jarvis"** → LED fast-blinks, assistant greets you
2. Ask your question → LED slow-blinks while processing
3. Assistant speaks the reply → LED solid while playing audio
4. After the reply, you have **15 seconds** to ask a follow-up without waking again
5. Press the **mute button** any time to mute/unmute — LED goes off when muted

---

## Configuration

Edit [`config/raspberrypi.yaml`](config/raspberrypi.yaml) to change models, GPIO pins, or thresholds:

```yaml
stt_binary: /home/pi/whisper.cpp/build/bin/whisper-cli
stt_model: models/ggml-tiny.en-q5_1.bin
stt_threads: 3

ollama_base_url: http://localhost:11434
ollama_model: llama3.2:1b          # swap to qwen2.5:0.5b if low on RAM

piper_voice: models/piper/en_US-lessac-low.onnx

wake_word:
  model: hey_jarvis
  threshold: 0.5                   # lower = more sensitive, higher = fewer false triggers
  backend: tflite

gpio:
  mute_button_pin: 17
  listening_led_pin: 27
  online_led_pin: 22

log_latency_csv: true              # set false to disable CSV logging
```

### Override via environment variables (no file edit needed)

```bash
OLLAMA_MODEL=qwen2.5:0.5b python -m src.main
WAKE_THRESHOLD=0.4 python -m src.main
STT_THREADS=4 python -m src.main
```

---

## Latency data

Every interaction appends a row to `latency_log.csv`:

```
wall_ts, stt_s, llm_ttft_s, llm_total_s, tts_synth_s, tts_play_s, e2e_s, tok_per_s, tok_count, prompt_tok
```

View it:
```bash
column -t -s, latency_log.csv
```

Or open in Excel / Google Sheets for analysis.

---

## Project structure

```
pi-voice-assistant/
├── setup_pi.sh                   # one-shot Pi setup script
├── requirements.txt              # Python dependencies
├── requirements-pi.txt           # Pi-specific additions (gpiozero, tflite-runtime)
├── config/
│   └── raspberrypi.yaml          # all configuration lives here
├── scripts/
│   └── download_models.sh        # downloads Piper voice model
├── models/                       # model files go here (gitignored)
└── src/
    ├── main.py                   # entry point — pipeline loop
    ├── config.py                 # loads raspberrypi.yaml + env overrides
    ├── latency.py                # LatencyTracker — per-stage timing + CSV log
    ├── audio/
    │   ├── capture.py            # mic → energy VAD → utterance generator
    │   ├── wake_word.py          # openWakeWord wrapper (tflite on Pi)
    │   └── wake_gate.py          # background thread — blocks mic until wake fires
    ├── stt/
    │   └── whisper_engine.py     # whisper.cpp subprocess wrapper
    ├── llm/
    │   └── ollama_client.py      # Ollama streaming HTTP client (measures TTFT)
    ├── tts/
    │   └── piper_engine.py       # Piper TTS — synthesize() and play() split
    └── io/
        ├── hardware_interface.py # abstract base class
        └── pi_gpio.py            # GPIO: LED stage indicators + mute button
```

---

## Troubleshooting

**Mic not detected**
```bash
arecord -l          # list capture devices
python3 -c "import sounddevice; print(sounddevice.query_devices())"
```
If your mic is not the default device, set it:
```bash
AUDIODEV=hw:1,0 python -m src.main
```

**Wake word never fires**
- Lower the threshold in `raspberrypi.yaml`: `threshold: 0.3`
- Watch the score log in the terminal — it prints the max score every 3 seconds
- Make sure you're saying "Hey Jarvis" clearly, pausing slightly after

**STT returns blank or garbage**
- Check whisper-cli path: `ls ~/whisper.cpp/build/bin/whisper-cli`
- Test manually:
  ```bash
  arecord -d 3 -r 16000 -c 1 -f S16_LE /tmp/test.wav
  ~/whisper.cpp/build/bin/whisper-cli -m models/ggml-tiny.en-q5_1.bin -f /tmp/test.wav -nt
  ```

**Ollama not responding**
```bash
systemctl status ollama
ollama list           # confirm llama3.2:1b is pulled
curl http://localhost:11434/api/tags
```

**GPIO permission denied**
```bash
groups              # check if 'gpio' is listed
sudo usermod -aG gpio $USER
newgrp gpio         # apply without logging out
```

**Out of memory**
Switch to a smaller LLM:
```bash
ollama pull qwen2.5:0.5b
OLLAMA_MODEL=qwen2.5:0.5b python -m src.main
```

---

## Expected latency (Pi 4, 4-core, llama3.2:1b)

| Stage | Typical |
|---|---|
| STT (tiny.en-q5_1, 5s audio) | 0.8 – 1.5 s |
| LLM first token (TTFT) | 0.3 – 0.8 s |
| LLM full reply (~30 tokens) | 2.0 – 4.0 s |
| TTS synthesize | 0.2 – 0.5 s |
| **End-to-end** | **4 – 7 s** |
