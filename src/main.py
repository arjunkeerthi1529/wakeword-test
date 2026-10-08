"""pi-voice-assistant — main entry point.

Full pipeline on Raspberry Pi 4:
    openWakeWord ("Hey Jarvis") -> whisper.cpp STT -> llama.cpp LLM -> Piper TTS

Per-stage latency is printed after every interaction and appended to
latency_log.csv for offline analysis.

Run with:
    python -m src.main
"""
import logging
import queue as _queue
import time
from pathlib import Path

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-24s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)

CSV_LOG = Path("latency_log.csv")
FOLLOW_UP_WINDOW_S = 30.0   # gate stays open this long after each reply
ECHO_FLUSH_S = 2.0          # drain mic after TTS to suppress speaker echo


def main() -> None:
    cfg = get_config()
    logger.info("Config loaded — llm=%s  stt=%s  wake=%s/%s",
                cfg.llm_base_url, Path(cfg.stt_model).name,
                cfg.wake_model, cfg.wake_backend)

    # ── Hardware ──────────────────────────────────────────────────────────
    try:
        hardware = PiHardwareIO(
            mute_button_pin=cfg.gpio_mute_pin,
            listening_led_pin=cfg.gpio_listen_led_pin,
            online_led_pin=cfg.gpio_online_led_pin,
        )
        logger.info("GPIO initialised — button on pin %d", cfg.gpio_mute_pin)
    except Exception as e:
        logger.warning("GPIO unavailable (%s) — falling back to NullHardwareIO", e)
        hardware = NullHardwareIO()

    # ── Audio (80 ms blocks = openWakeWord's native chunk size) ───────────
    audio = AudioCapture(
        hardware=hardware,
        block_duration=0.08,
        silence_threshold=0.012,
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
    stt = WhisperEngine(
        binary=cfg.stt_binary,
        model=cfg.stt_model,
        n_threads=cfg.stt_threads,
    )
    llm = LlamaCppClient(base_url=cfg.llm_base_url)
    tts = PiperEngine(model_path=cfg.piper_voice)   # warmup phrases pre-synthesized here

    # ── Start ─────────────────────────────────────────────────────────────
    hardware.start()
    gate.start()
    hardware.set_indicator("waiting_for_wake")
    logger.info("Ready — say '%s' to start.", cfg.wake_model.replace("_", " "))

    # ── Main loop ─────────────────────────────────────────────────────────
    for segment in audio.stream_utterances(follow_up_window_s=FOLLOW_UP_WINDOW_S):

        if hardware.muted:
            continue

        # Wake sentinel: gate just opened — play greeting and drain mic echo
        if segment is None:
            gate.suppressed = True
            tts.speak("Hey, what's up?")
            _flush(audio, gate, ECHO_FLUSH_S)
            gate.suppressed = False
            continue

        # Follow-up window expired with no speech — return to wake detection
        if segment is SILENCE_TIMEOUT:
            gate.sleep()
            _flush(audio, gate, ECHO_FLUSH_S)
            hardware.set_indicator("waiting_for_wake")
            tracker.reset()
            continue

        # ── STT ───────────────────────────────────────────────────────────
        tracker.mark("speech_end")
        hardware.set_indicator("thinking")
        dur_s = len(segment) / 16_000
        logger.info("Utterance captured (%.2fs audio) — transcribing…", dur_s)

        text = stt.transcribe(segment)
        tracker.mark("stt_done")
        logger.info("STT: %r", text)

        if not text.strip() or "[blank_audio]" in text.lower():
            logger.warning("STT returned empty/blank — skipping turn")
            hardware.set_indicator("listening")
            tracker.reset()
            continue

        # ── LLM (streaming) ───────────────────────────────────────────────
        logger.info("Querying llama.cpp (%s)…", cfg.llm_base_url)

        def _on_first_token() -> None:
            tracker.mark("llm_first_token")

        reply, stats = llm.chat(text, on_first_token=_on_first_token)
        tracker.mark("llm_done")
        logger.info("LLM reply: %r", reply)

        if not reply:
            reply = "I'm sorry, I couldn't form a response."

        # ── TTS synthesize ────────────────────────────────────────────────
        audio_data, sr = tts.synthesize(reply)
        tracker.mark("tts_synth_done")

        # ── TTS play ──────────────────────────────────────────────────────
        hardware.set_indicator("speaking")
        gate.suppressed = True   # block wake detector during playback
        tts.play(audio_data, sr)
        tracker.mark("tts_play_done")

        # ── Latency report ────────────────────────────────────────────────
        tracker.report(
            tok_per_s=stats.get("tok_per_s", 0.0),
            tok_count=stats.get("tok_count", 0),
            prompt_tok=stats.get("prompt_tok", 0),
            prompt_per_s=stats.get("prompt_per_s", 0.0),
            log_csv=CSV_LOG if cfg.log_latency_csv else None,
        )
        tracker.reset()

        # Drain mic so speaker output doesn't contaminate next VAD window
        _flush(audio, gate, ECHO_FLUSH_S)
        gate.suppressed = False  # re-enable wake detection after echo clears
        hardware.set_indicator("listening")  # gate still open for follow-up


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
