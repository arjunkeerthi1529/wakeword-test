"""Scam Guard simulator — test the phone page and warning logic on a PC, no Pi needed.

    python scripts/scam_sim.py                         # fake LLM
    python scripts/scam_sim.py --llm-url http://<pi-ip>:8080   # real LLM on the Pi
    python scripts/scam_sim.py --host 0.0.0.0                  # reachable from a phone/emulator

Open http://localhost:8765/sim — the phone page sits in a phone-sized frame,
and the panel beside it lets you "say" lines as if heard on the call.
Real code under test: rules, session ledger, LLM review scheduling/validation,
alert revisions, phone page. Faked: mic, speech-to-text, and (by default) the LLM.
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import uvicorn
from fastapi import HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from src.scam.config import SpamGuardConfig
from src.scam.monitor import ScamMonitor
from src.scam.server import ScamServer

PANEL = Path(__file__).with_name("scam_sim.html")


class SimMic:
    def start(self):
        import queue
        return queue.Queue()

    def stop(self):
        pass


class SimLED:
    def __init__(self):
        self.level = "off"

    def set(self, level):
        self.level = level


class SimLLM:
    """Stand-in for llama-server: flags obvious requests after a short delay."""
    _KEYWORDS = ("code", "otp", "pin", "password", "cvv", "anydesk", "teamviewer", "transfer", "qr")

    def review(self, system, user, max_tokens, timeout):
        time.sleep(2.0)
        lines = user.split("NEW segments:")[-1].splitlines()
        for line in lines:
            m = re.match(r'\s*(s\d+) \[[\d:]+\] "(.*)"', line)
            if m and any(k in m.group(2).lower() for k in self._KEYWORDS) \
                    and not re.search(r"\b(never|won't|don't|not)\b", m.group(2).lower()):
                return f"warn {m.group(1)} caller", {}
        return "none", {}


class SimMonitor(ScamMonitor):
    def say(self, text: str) -> None:
        session = self._session
        if session is None:
            raise RuntimeError("Tap Listen to this call on the phone first")
        now_ms = int((time.monotonic() - session.started_at) * 1000)
        self._commit(session, text, now_ms, now_ms)

    def _watchdog_loop(self, session, stop_evt):
        while not stop_evt.wait(1.0):          # no mic here, so no audio-gap check
            if time.monotonic() - session.started_at > self.cfg.max_session_s:
                self.stop("max_session")
                return


class SayBody(BaseModel):
    text: str


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to let a phone on your network reach it")
    ap.add_argument("--llm-url", default="", help="e.g. http://192.168.1.42:8080 (default: fake LLM)")
    args = ap.parse_args()

    cfg = SpamGuardConfig(port=args.port, host=args.host, llm_cadence_s=6.0, llm_timeout_s=60.0)
    if args.llm_url:
        from src.scam.llm import ScamLLM
        llm = ScamLLM(args.llm_url)
        print(f"LLM: real server at {args.llm_url}")
    else:
        llm = SimLLM()
        print("LLM: fake (pass --llm-url to use the Pi's llama-server)")

    led = SimLED()
    server = ScamServer(cfg)
    server.monitor = SimMonitor(cfg, SimMic(), None, llm, led, on_event=server.publish)

    @server.app.get("/sim")
    def sim_page():
        return FileResponse(PANEL)

    @server.app.get("/sim/state")
    def sim_state():
        return {"led": led.level}

    @server.app.post("/sim/say")
    def sim_say(body: SayBody):
        try:
            server.monitor.say(body.text.strip())
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"ok": True}

    print(f"\nOpen http://localhost:{args.port}/sim\n")
    uvicorn.run(server.app, host=cfg.host, port=cfg.port, log_level="warning")


if __name__ == "__main__":
    main()
