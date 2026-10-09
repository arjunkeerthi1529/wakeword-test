"""Standalone scam-guard service.

    python -m src.scam

Independent of the voice assistant: its own process, models, config
(config/scam_guard.yaml) and web page. The microphone is opened only while a
call is being monitored.
"""
import logging
import time
from pathlib import Path

from .config import ROOT_DIR, load_config
from .datalog import DataLog
from .led import WarningLED
from .llm import ScamLLM
from .llm_review import SELF_TEST_PROMPT, SYSTEM_PROMPT
from .mic import MicSource
from .monitor import ScamMonitor
from .server import ScamServer
from .stt import ScamSTT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-22s %(levelname)-8s %(message)s",
)
logger = logging.getLogger("scam")


def check_llm(llm: ScamLLM, cfg) -> None:
    """One real review at startup: confirms the server follows the enforced reply
    format and loads the system prompt into its cache so the first live review is fast."""
    try:
        t0 = time.monotonic()
        text, info = llm.review(SYSTEM_PROMPT, SELF_TEST_PROMPT, cfg.llm_max_tokens, cfg.llm_timeout_s)
        logger.info("LLM check OK in %.1fs: reply=%r mode=%s prompt=%s tok/%.1fs decode=%s tok/%.1fs",
                    time.monotonic() - t0, text.strip(), info.get("mode"),
                    info.get("prompt_n"), info.get("prompt_ms", 0) / 1000,
                    info.get("predicted_n"), info.get("predicted_ms", 0) / 1000)
    except Exception as exc:
        logger.warning("LLM not reachable at %s (%s) — rule-based warnings only until it is",
                       cfg.llm_base_url, exc)


def main() -> None:
    cfg = load_config()
    logger.info("Scam Guard starting — stt=%s  llm=%s", cfg.stt_model, cfg.llm_base_url)

    stt = ScamSTT(cfg.stt_model, cfg.stt_threads)
    stt.warmup()
    log_path = ""
    if cfg.analysis_log:
        p = Path(cfg.analysis_log)
        log_path = str(p if p.is_absolute() else ROOT_DIR / p)
        logger.info("Analysis log (transcripts, not audio): %s", log_path)
    datalog = DataLog(log_path)

    llm = ScamLLM(cfg.llm_base_url, cfg.llm_model)
    check_llm(llm, cfg)

    server = ScamServer(cfg)
    server.monitor = ScamMonitor(
        cfg, MicSource(cfg.mic_device), stt, llm,
        WarningLED(cfg.led_pin), on_event=server.publish, datalog=datalog,
    )
    try:
        server.run()
    finally:
        server.monitor.stop("service_exit")
        datalog.close()


if __name__ == "__main__":
    main()
