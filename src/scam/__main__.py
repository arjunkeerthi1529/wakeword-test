"""Standalone scam-guard service.

    python -m src.scam

Independent of the voice assistant: its own process, models, config
(config/scam_guard.yaml) and web page. The microphone is opened only while a
call is being monitored.
"""
import logging
from pathlib import Path

from .config import ROOT_DIR, load_config
from .datalog import DataLog
from .led import WarningLED
from .llm import ScamLLM
from .mic import MicSource
from .monitor import ScamMonitor
from .server import ScamServer
from .stt import ScamSTT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-22s %(levelname)-8s %(message)s",
)
logger = logging.getLogger("scam")


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

    server = ScamServer(cfg)
    server.monitor = ScamMonitor(
        cfg, MicSource(cfg.mic_device), stt, ScamLLM(cfg.llm_base_url),
        WarningLED(cfg.led_pin), on_event=server.publish, datalog=datalog,
    )
    try:
        server.run()
    finally:
        server.monitor.stop("service_exit")
        datalog.close()


if __name__ == "__main__":
    main()
