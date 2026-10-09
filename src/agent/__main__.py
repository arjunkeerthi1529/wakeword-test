"""Agent Service entry point.

Run as a standalone service, completely independent of the voice assistant:

    python -m src.agent

Starts:
  - SQLite database (email summaries + reminders)
  - PiperEngine (own TTS instance for speaking reminders)
  - ReminderAgent (re-arms any pending reminders from DB)
  - Daily scheduler (email digest at EMAIL_FETCH_TIME)
  - FastAPI HTTP server on AGENT_PORT (default 8001)

The voice assistant calls POST /remind whenever it detects a reminder intent.
"""
import logging
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import uvicorn

from .config import get_agent_config
from .db import get_conn, init_schema
from .email_agent import run as email_run
from .reminder_agent import ReminderAgent
from .scheduler import start as scheduler_start
from .server import app, set_state

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-28s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    agent_cfg = get_agent_config()

    # Import voice assistant config for model paths (shared YAML + .env)
    from ..config import get_config
    main_cfg = get_config()

    # ── Database ──────────────────────────────────────────────────────────
    conn = get_conn(agent_cfg.agent_db_path)
    init_schema(conn)

    # ── TTS — own instance, independent of voice assistant process ─────────
    from ..tts.piper_engine import PiperEngine
    logger.info("Loading Piper TTS model for agent service…")
    tts = PiperEngine(
        model_path=main_cfg.piper_voice,
        aplay_device=main_cfg.tts_aplay_device,
        output_device=main_cfg.tts_output_device,
    )

    # ── Reminder agent ────────────────────────────────────────────────────
    reminder_agent = ReminderAgent(tts=tts, conn=conn)
    reminder_agent.recover_from_db()

    # ── Daily email scheduler ─────────────────────────────────────────────
    scheduler_start([
        (
            agent_cfg.email_fetch_time,
            lambda: email_run(
                llm_base_url=main_cfg.llm_base_url,
                conn=conn,
                cfg=agent_cfg,
                tts=tts if agent_cfg.agent_read_digest else None,
            ),
        ),
    ])

    # ── Hand state to the FastAPI app ─────────────────────────────────────
    set_state(
        conn=conn,
        tts=tts,
        reminder_agent=reminder_agent,
        agent_cfg=agent_cfg,
        main_cfg=main_cfg,
    )

    port = int(os.getenv("AGENT_PORT", "8001"))
    logger.info(
        "Agent service ready — http://0.0.0.0:%d  (email digest at %s)",
        port, agent_cfg.email_fetch_time,
    )
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    main()
