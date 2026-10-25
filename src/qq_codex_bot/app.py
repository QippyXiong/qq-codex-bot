import asyncio
import fcntl
import json
import logging
import secrets
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request

from .codex import Codex
from .config import Settings
from .gateway import Gateway
from .ingress import accept_event
from .qq import QQClient
from .signing import QQSignature
from .store import Store
from .worker import Worker

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        s = settings or Settings.from_env()
        s.data_dir.mkdir(parents=True, exist_ok=True)
        lock = (s.data_dir / "service.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise RuntimeError("同一数据目录仅允许运行一个服务进程") from None
        store = Store(s.data_dir / "sessions.sqlite3")
        store.recover()
        app.state.settings = s
        app.state.instance = secrets.token_hex(12)
        app.state.store = store
        app.state.signing = QQSignature(s.app_secret)
        app.state.gateway = None
        async with httpx.AsyncClient(timeout=15) as client:
            qq = QQClient(s, client, store=store)
            worker = Worker(store, Codex(s), qq)
            tasks = [asyncio.create_task(worker.run()) for _ in range(s.workers)]
            # Read-only queries stay responsive even when all Codex workers are busy.
            tasks.append(asyncio.create_task(worker.run(control_only=True)))
            if s.transport == "websocket":
                gateway = Gateway(qq, lambda event: accept_event(
                    s, store, event, self_ids=gateway.self_ids
                ))
                app.state.gateway = gateway
                tasks.append(asyncio.create_task(gateway.run()))
                log.info("QQ transport=websocket: outbound WSS; webhook route disabled")
            else:
                log.info("QQ transport=webhook: waiting for callback requests")
            app.state.workers = tasks
            try:
                yield
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                store.close()
                lock.close()

    app = FastAPI(title="QQ Codex Bot", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def log_callback_requests(request: Request, call_next):
        response = await call_next(request)
        if request.url.path != "/health":
            # Do not log request bodies, headers or query strings (which can contain secrets).
            log.info("Callback HTTP method=%s status=%s webhook_path=%s",
                     request.method, response.status_code,
                     request.url.path == "/qq/webhook")
        return response

    @app.get("/health")
    async def health():
        healthy = all(not task.done() for task in app.state.workers)
        if not healthy:
            raise HTTPException(503, "worker stopped")
        gateway = app.state.gateway
        return {"status": "ok", "instance": app.state.instance,
                "transport": app.state.settings.transport,
                "qq": gateway.health() if gateway else {"status": "awaiting_webhook"}}

    @app.post("/qq/webhook")
    async def webhook(request: Request):
        if app.state.settings.transport != "webhook":
            raise HTTPException(404, "webhook disabled in websocket mode")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 1024 * 1024:
                raise HTTPException(413, "body too large")
        raw = bytes(body)
        s = app.state.settings
        bot_id = request.headers.get("X-Bot-Appid")
        if bot_id and bot_id != s.app_id:
            log.warning("QQ callback rejected: bot AppID mismatch")
            raise HTTPException(403, "wrong bot")
        try:
            event = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "invalid JSON") from None
        if not isinstance(event, dict):
            raise HTTPException(400, "expected object")
        # QQ's documented op=13 challenge has no signature headers.
        if event.get("op") == 13:
            data = event.get("d")
            if not isinstance(data, dict) or not all(
                isinstance(data.get(k), str) and 0 < len(data[k]) <= 1024
                for k in ("plain_token", "event_ts")
            ):
                raise HTTPException(400, "invalid challenge")
            log.info("QQ callback address validation received")
            return app.state.signing.challenge(data["plain_token"], data["event_ts"])
        if not app.state.signing.verify(
            request.headers.get("X-Signature-Timestamp", ""), raw,
            request.headers.get("X-Signature-Ed25519", ""),
        ):
            log.warning("QQ callback rejected: invalid or missing signature")
            raise HTTPException(401, "invalid signature")
        try:
            accept_event(s, app.state.store, event)
        except (ValueError, TypeError, OverflowError):
            raise HTTPException(400, "invalid message") from None
        return {"op": 12}

    return app
