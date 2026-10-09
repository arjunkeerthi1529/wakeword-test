"""Lightweight daily job scheduler.

Runs schedule.run_pending() every 30 s in a single daemon thread.
All daily triggers (email digest) register here.
Per-event timers (reminders) use threading.Timer directly.
"""
import logging
import threading
import time
from typing import Callable

import schedule

logger = logging.getLogger(__name__)

_started = False


def start(jobs: list[tuple[str, Callable]]) -> None:
    """Register daily jobs and start the scheduler daemon thread.

    jobs: list of ("HH:MM", callable) in 24-hour format.
    """
    global _started
    if _started:
        logger.warning("Scheduler already running — ignoring duplicate start")
        return

    for time_str, fn in jobs:
        schedule.every().day.at(time_str).do(fn)
        logger.info("Scheduled daily job at %s → %s", time_str, fn.__qualname__)

    t = threading.Thread(target=_loop, daemon=True, name="agent-scheduler")
    t.start()
    _started = True
    logger.info("Agent scheduler started — %d job(s) registered", len(jobs))


def _loop() -> None:
    while True:
        try:
            schedule.run_pending()
        except Exception as exc:
            logger.error("Scheduler job raised an exception: %s", exc)
        time.sleep(30)
