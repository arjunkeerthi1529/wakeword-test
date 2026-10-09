"""pi-voice-assistant — main entry point.

Real-time conversation pipeline on Raspberry Pi 4:
  openWakeWord ("Hey Jarvis")
    → live streaming STT (tiny.en preview + small.en final)
    → llama.cpp LLM (streaming tokens)
    → sentence-by-sentence Piper TTS (plays while LLM still generating)

Per-stage latency printed after every turn and appended to latency_log.csv.

Run with:
    python -m src.main
"""
import logging
import queue as _queue
import re
import threading
import time
from pathlib import Path

import numpy as np

# Load .env before any config is read so env vars are available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass   # python-dotenv optional; env vars can be set in the shell instead

from .audio.capture import AudioCapture, SILENCE_TIMEOUT
from .audio.wake_gate import WakeGate
from .audio.wake_word import WakeWordDetector
from .config import get_config
from .io.pi_gpio import PiHardwareIO
from .io.null_hardware import NullHardwareIO
from .latency import LatencyTracker
from .llm.llama_cpp_client import LlamaCppClient
from .stt.whisper_engine import WhisperEngine
from .tts.piper_engine import PiperEngine
from .work.config import get_agent_config
from .work.reminder_agent import parse_tag

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-24s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)

CSV_LOG          = Path("latency_log.csv")
ECHO_FLUSH_S     = 2.0    # drain mic after TTS to suppress speaker echo
SILENCE_THRESH   = 0.012  # energy threshold for speech detection (above ambient noise floor)
PAUSE_SECS       = 1.3    # silence after speech → send to LLM
IDLE_TIMEOUT_S   = 30.0   # no speech in this window → close gate
# Sentence boundary: punctuation followed by space or end of string
_SENT_RE = re.compile(r'(?<=[.!?])(?:\s+|$)')
# Machine-readable reminder tag injected by the LLM — must not reach TTS
_REMINDER_TAG_RE = re.compile(r'\[REMINDER[^\]]*\]', re.IGNORECASE)


def main() -> None:
    cfg = get_config()
    logger.info("Config loaded — llm=%s  stt=%s  wake=%s",
                cfg.llm_base_url, cfg.stt_model_name, cfg.wake_model)

    # ── Hardware ──────────────────────────────────────────────────────────
    try:
        hardware = PiHardwareIO(
            mute_button_pin=cfg.gpio_mute_pin,
            listening_led_pin=cfg.gpio_listen_led_pin,
            online_led_pin=cfg.gpio_online_led_pin,
        )
    except Exception as e:
        logger.warning("GPIO unavailable (%s) — falling back to NullHardwareIO", e)
        hardware = NullHardwareIO()

    # ── Audio ─────────────────────────────────────────────────────────────
    audio = AudioCapture(
        hardware=hardware,
        block_duration=0.08,
        silence_threshold=SILENCE_THRESH,
        silence_duration_s=0.6,
    )

    # ── Wake word ─────────────────────────────────────────────────────────
    detector = WakeWordDetector(
        model_name=cfg.wake_model,
        threshold=cfg.wake_threshold,
        inference_framework=cfg.wake_backend,
    )

    tracker = LatencyTracker()

    def on_wake() -> None:
        tracker.mark("wake_fire")
        hardware.set_indicator("listening")

    gate = WakeGate(audio._q, detector, on_wake=on_wake)
    audio.set_wake_gate(gate)

    # ── STT / LLM / TTS ───────────────────────────────────────────────────
    stt = WhisperEngine(model_name=cfg.stt_model_name)
    llm = LlamaCppClient(base_url=cfg.llm_base_url)
    tts = PiperEngine(model_path=cfg.piper_voice, output_device=cfg.tts_output_device,
                      aplay_device=cfg.tts_aplay_device)

    # ── Agent service link ────────────────────────────────────────────────
    # Reminders and email digest run in a separate process (python -m src.work).
    # This service only forwards reminder intents via HTTP POST /remind.
    agent_service_url = get_agent_config().agent_service_url
    logger.info("Work service URL: %s  (start with: python -m src.work)", agent_service_url)

    # ── Start ─────────────────────────────────────────────────────────────
    hardware.start()
    gate.start()
    hardware.set_indicator("waiting_for_wake")
    logger.info("Ready — say '%s' to start.", cfg.wake_model.replace("_", " "))

    # ── Main loop ─────────────────────────────────────────────────────────
    for segment in audio.stream_utterances(follow_up_window_s=IDLE_TIMEOUT_S):

        if hardware.muted:
            continue

        if segment is SILENCE_TIMEOUT:
            gate.sleep()
            hardware.set_indicator("waiting_for_wake")
            tracker.reset()
            continue

        if segment is not None:
            # Should not reach here in streaming mode — handled inside wake block
            continue

        # ── Wake fired ────────────────────────────────────────────────────
        llm.reset_history()   # fresh conversation context on each wake
        gate.suppressed = True
        tts.speak("Hey, what's up?")
        _flush(audio, gate, ECHO_FLUSH_S)
        gate.suppressed = False
        gate.extend_timeout()

        # ── Conversation loop (follow-up without re-waking) ───────────────
        while True:
            hardware.set_indicator("listening")
            text = _listen_streaming(gate, stt, hardware, tracker)

            if not text or "[blank_audio]" in text.lower():
                logger.info("No speech detected — returning to wake detection")
                gate.sleep()
                hardware.set_indicator("waiting_for_wake")
                tracker.reset()
                break

            logger.info("STT final: %r", text)

            if not text.strip():
                tracker.reset()
                continue

            # ── LLM + sentence-streaming TTS ──────────────────────────────
            gate.suppressed = True
            hardware.set_indicator("thinking")
            reply, stats = _stream_reply_with_tts(llm, tts, text, tracker, hardware)

            # ── Forward reminder intent to agent service ───────────────────
            _, spec = parse_tag(reply)
            if spec:
                _forward_reminder(spec, agent_service_url)

            # ── Latency report ────────────────────────────────────────────
            tracker.report(
                tok_per_s=stats.get("tok_per_s", 0.0),
                tok_count=stats.get("tok_count", 0),
                prompt_tok=stats.get("prompt_tok", 0),
                prompt_per_s=stats.get("prompt_per_s", 0.0),
                log_csv=CSV_LOG if cfg.log_latency_csv else None,
            )
            tracker.reset()

            # Keep wake detection suppressed until echo is drained — clearing
            # it before the flush lets the speaker's own TTS tail re-trigger
            # the wake word (false wake on Jarvis's own voice).
            _flush(audio, gate, ECHO_FLUSH_S)
            gate.suppressed = False
            gate.reopen()   # ensure gate is open for follow-up even if timeout fired


# ── Agent service forwarder ───────────────────────────────────────────────────

def _forward_reminder(spec: dict, agent_service_url: str) -> None:
    """POST a reminder spec to the agent service. Fire-and-forget with timeout.
    If the agent service is not running the reminder is logged and dropped."""
    payload: dict = {"message": spec["message"]}
    if "delay_s" in spec:
        payload["delay_s"] = spec["delay_s"]
    else:
        payload["fires_at"] = spec["fires_at"].isoformat()
    try:
        import requests as _req
        _req.post(f"{agent_service_url}/remind", json=payload, timeout=3)
        logger.info("Reminder forwarded to agent service: %r", spec["message"])
    except Exception as exc:
        logger.warning("Agent service unreachable — reminder lost (%s). "
                       "Start it with: python -m src.work", exc)


# ── Streaming listen ──────────────────────────────────────────────────────────

def _listen_streaming(
    gate: WakeGate,
    stt: WhisperEngine,
    hardware,
    tracker: LatencyTracker,
) -> str:
    """Read audio chunks from gate, display live preview, return accurate text.

    Words appear in-place on terminal as the user speaks.
    After PAUSE_SECS of silence following speech, captures full audio and
    runs accurate small.en transcription for the LLM.
    Returns "" if no speech within IDLE_TIMEOUT_S.
    """
    blocks: list = []
    last_speech_t: float = 0.0
    started = False
    start_t = time.monotonic()
    dot_count = 0

    print("\r[listening] ", end="", flush=True)

    while True:
        # Gate closed internally (its own listen timeout fired) — stop
        # collecting immediately instead of silently spanning into a
        # later re-wake cycle's audio.
        if not gate._awake:
            if started:
                break
            print()
            return ""

        # Hard timeout — no speech at all
        if not started and time.monotonic() - start_t > IDLE_TIMEOUT_S:
            print()
            return ""

        chunk = gate.get_block(timeout=0.2)

        if chunk is None:
            if started and time.monotonic() - last_speech_t > PAUSE_SECS:
                break
            continue

        energy = float(np.abs(chunk).mean())
        blocks.append(chunk)

        if energy > SILENCE_THRESH:
            last_speech_t = time.monotonic()
            if not started:
                started = True
            # Animate dots to show voice is being captured
            dot_count = (dot_count % 5) + 1
            print(f"\r[listening] {'.' * dot_count}     ", end="", flush=True)

        if started and time.monotonic() - last_speech_t > PAUSE_SECS:
            break

    print()  # newline after live display

    if not blocks or not started:
        return ""

    tracker.mark("speech_end")
    hardware.set_indicator("thinking")

    full_audio = np.concatenate(blocks).flatten()
    logger.info("Transcribing %.2fs of audio…", len(full_audio) / 16_000)
    text = stt.transcribe(full_audio)
    tracker.mark("stt_done")
    return text


# ── Sentence-streaming TTS ────────────────────────────────────────────────────

def _split_sentence(buffer: str):
    """Return (first_complete_sentence_or_clause, remainder) or (None, buffer)."""
    m = re.search(r'[.!?](?:\s|$)', buffer)
    if m:
        end = m.end()
        return buffer[:end].strip(), buffer[end:].lstrip()
    # Long clause with comma — flush early so TTS doesn't lag
    if len(buffer) > 60:
        m2 = re.search(r'[,;]\s', buffer)
        if m2:
            end = m2.end()
            return buffer[:end].strip(), buffer[end:].lstrip()
    return None, buffer


def _stream_reply_with_tts(
    llm: LlamaCppClient,
    tts: PiperEngine,
    text: str,
    tracker: LatencyTracker,
    hardware,
):
    """Stream LLM tokens → detect sentence boundaries → play each sentence
    concurrently so TTS starts while LLM is still generating the rest."""

    tts_q: "_queue.Queue[str | None]" = _queue.Queue()
    first_synth = [True]

    def tts_worker():
        while True:
            item = tts_q.get()
            if item is None:
                break
            audio_data, sr = tts.synthesize(item)
            if first_synth[0]:
                first_synth[0] = False
                tracker.mark("tts_synth_done")
            hardware.set_indicator("speaking")
            tts.play(audio_data, sr)

    worker = threading.Thread(target=tts_worker, daemon=True)
    worker.start()

    buffer = ""
    printed_reply = []

    def on_first_token():
        tracker.mark("llm_first_token")

    def on_token(token: str):
        nonlocal buffer
        buffer += token
        sentence, buffer = _split_sentence(buffer)
        if sentence:
            printed_reply.append(sentence)
            print(f"\r[Jarvis] {sentence}", flush=True)
            tts_q.put(sentence)

    logger.info("Querying llama.cpp (%s)…", llm.base_url)
    reply, stats = llm.chat(text, on_first_token=on_first_token, on_token=on_token)
    tracker.mark("llm_done")

    # Strip machine-readable reminder tag so it is never spoken aloud
    buffer = _REMINDER_TAG_RE.sub("", buffer).strip()

    # Flush any trailing text that didn't hit a sentence boundary
    if buffer:
        printed_reply.append(buffer)
        print(f"\r[Jarvis] {buffer}", flush=True)
        tts_q.put(buffer)

    tts_q.put(None)   # stop TTS worker
    worker.join()     # wait for last sentence to finish playing
    tracker.mark("tts_play_done")

    full_reply = reply or " ".join(printed_reply)
    logger.info("Full reply: %r", full_reply)
    return full_reply, stats


# ── Echo flush ────────────────────────────────────────────────────────────────

def _flush(audio: AudioCapture, gate: WakeGate, duration_s: float) -> None:
    """Drain mic and gate queues for duration_s to suppress TTS echo."""
    deadline = time.monotonic() + duration_s
    while time.monotonic() < deadline:
        for q in (audio._q, gate._out_q):
            while True:
                try:
                    q.get_nowait()
                except _queue.Empty:
                    break
        time.sleep(0.02)


if __name__ == "__main__":
    main()
