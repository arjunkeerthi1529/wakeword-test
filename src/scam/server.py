"""Scam Guard HTTP + WebSocket API (served from the Pi).

    GET  /              phone web page
    GET  /health        is the service up, and is the language model reachable?
    POST /start         begin monitoring a call through the microphone
    POST /stop          stop monitoring (idempotent)
    GET  /status        full state of the current call
    WS   /events        live events: snapshot, transcript, warning, ...
    GET  /events/schema JSON Schema of every WebSocket event
    POST /analyze       stateless: send conversation text, get a verdict (no microphone)
    GET  /docs          interactive API docs (OpenAPI spec at /openapi.json)

No authentication — use only on a private Wi-Fi or hotspot.
"""
import asyncio
import logging
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import TypeAdapter

from .analyze import analyze
from .api_models import (API_VERSION, AnalyzeRequest, AnalyzeResponse, ErrorResponse, Event,
                         HealthResponse, LLMHealth, Snapshot, StartResponse, StopResponse)
from .monitor import MonitorError

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
CONNECTION_GRACE_S = 60.0

_DESCRIPTION = """
On-device live scam-call warning. Audio and analysis stay on the Raspberry Pi.

**Live monitoring** — `POST /start`, then listen on the `/events` WebSocket (the first message is always a
full `snapshot`), then `POST /stop`. **One-off analysis** — `POST /analyze` with conversation text.

Risk levels: `none`, `watch` (be careful: pressure or impersonation), `warn` (a request for a code, PIN,
money or remote access). A result of `none` is not a guarantee a call is safe.
"""


class ScamServer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.monitor = None            # set after the monitor is built (it needs publish())
        self._clients = []             # [(loop, asyncio.Queue)]
        self._clients_lock = threading.Lock()
        self._no_client_since: Optional[float] = None
        self.app = self._build_app()

    # Called from monitor worker threads.
    def publish(self, event: dict) -> None:
        with self._clients_lock:
            clients = list(self._clients)
        for loop, q in clients:
            loop.call_soon_threadsafe(q.put_nowait, event)

    def _client_count(self) -> int:
        with self._clients_lock:
            return len(self._clients)

    async def _grace_watchdog(self) -> None:
        while True:
            await asyncio.sleep(5.0)
            if self.monitor is None or not self.monitor.active or self._client_count() > 0:
                self._no_client_since = None
                continue
            now = time.monotonic()
            if self._no_client_since is None:
                self._no_client_since = now
            elif now - self._no_client_since > CONNECTION_GRACE_S:
                logger.warning("Phone disconnected for %.0fs — stopping monitoring", CONNECTION_GRACE_S)
                self.monitor.stop("connection_lost")
                self._no_client_since = None

    def _build_app(self) -> FastAPI:
        @asynccontextmanager
        async def lifespan(_app: FastAPI):
            task = asyncio.create_task(self._grace_watchdog())
            yield
            task.cancel()

        app = FastAPI(title="Scam Guard API", version=API_VERSION, description=_DESCRIPTION,
                      lifespan=lifespan)
        app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

        @app.exception_handler(RequestValidationError)
        async def invalid_request(_request: Request, exc: RequestValidationError):
            """Same {detail, code} shape as every other error, instead of FastAPI's default list."""
            parts = [f"{'.'.join(str(p) for p in e['loc'][1:]) or 'body'}: {e['msg']}" for e in exc.errors()]
            return JSONResponse(status_code=422, content={"detail": "; ".join(parts), "code": "invalid_request"})

        @app.get("/", include_in_schema=False)
        async def index():
            return FileResponse(STATIC_DIR / "scam.html")

        @app.get("/health", response_model=HealthResponse, tags=["service"],
                 summary="Service and model status")
        def health():
            llm = self.monitor.llm
            ping = getattr(llm, "ping", None)
            reachable, detail = ping() if ping else (True, "no health check available")
            return HealthResponse(
                status="ok" if reachable else "degraded",
                api_version=API_VERSION,
                monitoring=self.monitor.active,
                stt_model=self.cfg.stt_model,
                rules_enabled=self.cfg.rules_enabled,
                analysis_log=bool(self.cfg.analysis_log),
                llm=LLMHealth(url=getattr(llm, "base_url", self.cfg.llm_base_url),
                              model=getattr(llm, "model", "local"), reachable=reachable,
                              mode=getattr(llm, "mode", None), detail=detail),
            )

        @app.get("/status", response_model=Snapshot, tags=["live monitoring"],
                 summary="Full state of the current call")
        def status():
            return self.monitor.snapshot()

        @app.post("/start", response_model=StartResponse, tags=["live monitoring"],
                  summary="Start monitoring a call through the microphone",
                  responses={409: {"model": ErrorResponse,
                                   "description": "already_monitoring, or mic_unavailable (busy/missing microphone)"}})
        def start():
            try:
                return StartResponse(call_id=self.monitor.start())
            except MonitorError as exc:
                return JSONResponse(status_code=409, content={"detail": str(exc), "code": exc.code})

        @app.post("/stop", response_model=StopResponse, tags=["live monitoring"],
                  summary="Stop monitoring (safe to call when nothing is running)")
        def stop():
            was_active = self.monitor.active
            self.monitor.stop("user")
            return StopResponse(was_active=was_active)

        @app.post("/analyze", response_model=AnalyzeResponse, tags=["analysis"],
                  summary="Analyze conversation text without the microphone",
                  responses={422: {"model": ErrorResponse, "description": "invalid_request: bad or missing input"}})
        def analyze_text(req: AnalyzeRequest):
            return analyze(req, self.monitor.llm, self.monitor.system_prompt, self.cfg)

        @app.get("/events/schema", tags=["live monitoring"],
                 summary="JSON Schema of every WebSocket event")
        def events_schema():
            return TypeAdapter(Event).json_schema()

        @app.websocket("/events")
        async def events(ws: WebSocket):
            """Live events. The first message is a `snapshot` of the whole current state; after that
            each message is one of: listening, transcript, processing, warning, checked, info, error,
            stopped. Send any text (e.g. "ping") every ~15 s as a heartbeat. Schemas: GET /events/schema."""
            await ws.accept()
            loop = asyncio.get_running_loop()
            q: asyncio.Queue = asyncio.Queue()
            entry = (loop, q)
            with self._clients_lock:
                self._clients.append(entry)
            try:
                await ws.send_json({"type": "snapshot", "event_id": 0, **self.monitor.snapshot()})

                async def sender():
                    while True:
                        await ws.send_json(await q.get())

                async def receiver():
                    while True:
                        await ws.receive_text()      # client heartbeats; also detects disconnect

                tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
                for t in done:
                    t.exception()      # mark retrieved; a disconnect is the normal exit
            except Exception:
                pass
            finally:
                with self._clients_lock:
                    if entry in self._clients:
                        self._clients.remove(entry)

        return app

    def run(self) -> None:
        """Serve in the foreground until interrupted."""
        import importlib.util

        import uvicorn

        if not (importlib.util.find_spec("websockets") or importlib.util.find_spec("wsproto")):
            logger.error(
                "No WebSocket library installed — the phone page will load but show 'Connection lost'. "
                "Run: pip install websockets   (then restart)"
            )
        logger.info("Scam Guard page: http://<pi-ip>:%d   API docs: http://<pi-ip>:%d/docs",
                    self.cfg.port, self.cfg.port)
        uvicorn.run(self.app, host=self.cfg.host, port=self.cfg.port, log_level="warning")
