"""Local web server on the Pi: serves the phone page and streams call events.

    GET  /          phone page
    POST /start     begin monitoring one call
    POST /stop      stop monitoring
    GET  /status    current call snapshot
    WS   /events    transcript / processing / warning / stopped / error events

No authentication — use only on a private Wi-Fi or hotspot.
"""
import asyncio
import logging
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import FileResponse

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
CONNECTION_GRACE_S = 60.0


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

        app = FastAPI(title="Scam Guard", lifespan=lifespan)

        @app.get("/")
        async def index():
            return FileResponse(STATIC_DIR / "scam.html")

        @app.get("/status")
        async def status():
            return self.monitor.snapshot()

        @app.post("/start")
        async def start():
            try:
                call_id = self.monitor.start()
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc))
            return {"ok": True, "call_id": call_id}

        @app.post("/stop")
        async def stop():
            self.monitor.stop("user")
            return {"ok": True}

        @app.websocket("/events")
        async def events(ws: WebSocket):
            await ws.accept()
            loop = asyncio.get_running_loop()
            q: asyncio.Queue = asyncio.Queue()
            entry = (loop, q)
            with self._clients_lock:
                self._clients.append(entry)
            try:
                await ws.send_json({"type": "status", "event_id": 0, **self.monitor.snapshot()})

                async def sender():
                    while True:
                        await ws.send_json(await q.get())

                async def receiver():
                    while True:
                        await ws.receive_text()      # page pings; also detects disconnect

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
        logger.info("Scam Guard page: http://<pi-ip>:%d  (phone must be on the same Wi-Fi)", self.cfg.port)
        uvicorn.run(self.app, host=self.cfg.host, port=self.cfg.port, log_level="warning")
