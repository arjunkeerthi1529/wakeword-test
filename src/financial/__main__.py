"""Financial Service entry point.

Run as a standalone service, completely independent of the voice assistant
and of src.work:

    python -m src.financial

Starts:
  - SQLite database (accounts, transactions, imports, categorization cache)
  - Idempotent seed of the 12 locked categories
  - FastAPI HTTP server on FINANCIAL_PORT (default 8002)

No job/polling layer: quick-add parsing, statement import, and Ask
questions each block their own HTTP request until the (one or few) LLM
calls they need finish -- see server.py's module docstring.
"""
import logging

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import uvicorn

from .server import app, create_app_state

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)-28s %(levelname)-8s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    create_app_state()
    from .config import get_financial_config
    cfg = get_financial_config()
    logger.info("Financial service ready -- http://0.0.0.0:%d  (LLM: %s, model=%s)", cfg.port, cfg.llm_base_url, cfg.llm_model)
    uvicorn.run(app, host="0.0.0.0", port=cfg.port, log_level="warning")


if __name__ == "__main__":
    main()
