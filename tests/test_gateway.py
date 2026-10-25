import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import aiohttp
import httpx
import pytest
from fastapi.testclient import TestClient

from qq_codex_bot.app import create_app
from qq_codex_bot.config import Settings
from qq_codex_bot.gateway import GROUP_AND_C2C_EVENT, Gateway, GatewayError
from qq_codex_bot.ingress import accept_event
from qq_codex_bot.qq import QQClient
from qq_codex_bot.store import Store
from qq_codex_bot.worker import Worker


def event(text="hello", message_id="msg1", sequence=2):
    return {"op": 0, "s": sequence, "t": "C2C_MESSAGE_CREATE", "d": {
        "id": message_id, "author": {"user_openid": "user1"}, "content": text,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }}


class FakeSocket:
    def __init__(self):
        self.sent = []
        self.queue = asyncio.Queue()
        self.close_code = None
        self.closed = False

    async def receive_json(self):
        return {"op": 10, "d": {"heartbeat_interval": 1000}}

    async def send_json(self, payload):
        self.sent.append(payload)
        if payload["op"] == 2:
            await self.push({"op": 0, "t": "READY", "s": 1,
                             "d": {"session_id": "gateway-session"}})
        elif payload["op"] == 6:
            await self.push({"op": 0, "t": "RESUMED", "s": 10, "d": {}})
        elif payload["op"] == 1:
            await self.push({"op": 11})

    async def push(self, payload):
        await self.queue.put(SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(payload)))

    async def receive(self):
        return await self.queue.get()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True


class FakeSession:
    def __init__(self, socket):
        self.socket = socket

    def ws_connect(self, url, **kwargs):
        assert url == "wss://gateway.example/ws"
        return self.socket


def test_gateway_message_to_codex_to_qq_and_heartbeat(tmp_path):
    async def scenario():
        settings = Settings("app", "secret", data_dir=tmp_path, workspace=tmp_path / "workspace")
        store = Store(tmp_path / "test.sqlite3")
        replies = []

        def handle(request):
            if request.url.path.endswith("getAppAccessToken"):
                return httpx.Response(200, json={"access_token": "token", "expires_in": 7200})
            if request.url.path == "/gateway":
                return httpx.Response(200, json={"url": "wss://gateway.example/ws"})
            replies.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "reply-id"})

        delivered = asyncio.Event()

        def ingest(payload):
            accept_event(settings, store, payload)
            if payload.get("t") == "C2C_MESSAGE_CREATE":
                delivered.set()

        class FakeCodex:
            async def run(self, prompt, thread_id, on_thread):
                assert prompt == "hello"
                on_thread("codex-thread")
                return "Codex response"

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            qq = QQClient(settings, client)
            gateway = Gateway(qq, ingest)
            socket = FakeSocket()
            task = asyncio.create_task(gateway.connection(FakeSession(socket)))
            try:
                await asyncio.wait_for(gateway.ready.wait(), 1)
                assert gateway.health()["ready"]
                identify = next(x for x in socket.sent if x["op"] == 2)
                assert identify["d"]["intents"] == GROUP_AND_C2C_EVENT
                assert identify["d"]["token"] == "QQBot token"
                await asyncio.sleep(0)
                assert any(x["op"] == 1 for x in socket.sent)
                await socket.push(event())
                await asyncio.wait_for(delivered.wait(), 1)
                assert gateway.sequence == 2
                assert gateway.health()["messages_received"] == 1
                assert gateway.health()["last_event"] == "C2C_MESSAGE_CREATE"
                # Websocket redelivery never invokes Codex twice.
                await gateway.dispatch(socket, event(sequence=3))
                assert store.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
                await Worker(store, FakeCodex(), qq).process(store.claim())
                assert replies[0]["msg_type"] == 2
                assert replies[0]["markdown"]["content"] == "Codex response"
                assert replies[0]["msg_id"] == "msg1"
                assert store.current_session("app:c2c:user1")["thread_id"] == "codex-thread"
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                assert socket.closed
                store.close()

    asyncio.run(scenario())


def test_gateway_resume_invalid_session_and_heartbeat_timeout():
    async def scenario():
        qq = SimpleNamespace(token="old-token")
        gateway = Gateway(qq, lambda _: None)
        gateway.session_id, gateway.sequence = "previous", 42
        socket = FakeSocket()
        await gateway.identify(socket, "fresh-token")
        assert socket.sent[0] == {"op": 6, "d": {
            "token": "QQBot fresh-token", "session_id": "previous", "seq": 42}}
        await gateway.dispatch(socket, {"op": 0, "t": "RESUMED", "s": 43})
        assert gateway.ready.is_set() and gateway.sequence == 43
        with pytest.raises(GatewayError, match="invalid_session"):
            await gateway.dispatch(socket, {"op": 9, "d": False})
        assert gateway.session_id is None and gateway.sequence is None and qq.token == ""
        await gateway.identify(socket, "new-token")
        assert socket.sent[-1]["op"] == 2
        # Without the receive loop, ACKs remain unread and the heartbeat must detect a dead link.
        gateway.ready.set()
        with pytest.raises(GatewayError, match="heartbeat_ack_timeout"):
            await gateway.heartbeat(socket, 0.001)

    asyncio.run(scenario())


def test_heartbeat_waits_for_ready_and_uses_authenticated_sequence():
    async def scenario():
        gateway = Gateway(SimpleNamespace(), lambda _: None)
        socket = FakeSocket()
        task = asyncio.create_task(gateway.heartbeat(socket, 1))
        try:
            await asyncio.sleep(0.01)
            assert socket.sent == []
            gateway.sequence = 7
            gateway.ready.set()
            await asyncio.sleep(0.01)
            assert socket.sent == [{"op": 1, "d": 7}]
            await gateway.dispatch(socket, {"op": 11})
            assert gateway.health()["heartbeat_acks"] == 1
            assert not gateway.awaiting_ack
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_resume_cursor_advances_only_after_durable_ingress():
    async def scenario():
        def fail(_):
            raise RuntimeError("database unavailable")

        gateway = Gateway(SimpleNamespace(), fail)
        gateway.sequence = 1
        with pytest.raises(RuntimeError):
            await gateway.dispatch(FakeSocket(), event(sequence=2))
        assert gateway.sequence == 1

    asyncio.run(scenario())


def test_gateway_url_refreshes_token_on_401():
    async def scenario():
        token_calls, gateway_calls = [], []

        def handle(request):
            if request.url.path.endswith("getAppAccessToken"):
                token_calls.append(request)
                return httpx.Response(200, json={"access_token": f"t{len(token_calls)}", "expires_in": 7200})
            gateway_calls.append(request)
            if len(gateway_calls) == 1:
                return httpx.Response(401, json={"code": 1})
            assert request.headers["Authorization"] == "QQBot t2"
            return httpx.Response(200, json={"url": "wss://gateway.example/ws"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            qq = QQClient(Settings("app", "secret"), client)
            assert await qq.gateway_url() == "wss://gateway.example/ws"
            assert len(token_calls) == 2

    asyncio.run(scenario())


def test_websocket_mode_disables_webhook_and_reports_real_ready(tmp_path, monkeypatch):
    async def idle(self, **kwargs):
        await asyncio.sleep(3600)

    async def ready(self):
        self.status = "ready"
        self.ready.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(Worker, "run", idle)
    monkeypatch.setattr(Gateway, "run", ready)
    settings = Settings("app", "secret", data_dir=tmp_path, workspace=tmp_path / "workspace")
    with TestClient(create_app(settings)) as client:
        body = client.get("/health").json()
        assert body["transport"] == "websocket" and body["qq"]["ready"]
        assert client.post("/qq/webhook", json={"op": 13}).status_code == 404


def test_config_defaults_to_websocket_and_allows_explicit_webhook(monkeypatch):
    monkeypatch.setenv("QQ_BOT_APP_ID", "app")
    monkeypatch.setenv("QQ_BOT_APP_SECRET", "secret")
    monkeypatch.delenv("QQ_BOT_TRANSPORT", raising=False)
    monkeypatch.setattr("qq_codex_bot.config.shutil.which", lambda _: "/fake/codex")
    assert Settings.from_env().transport == "websocket"
    monkeypatch.setenv("QQ_BOT_TRANSPORT", "webhook")
    assert Settings.from_env().transport == "webhook"
    monkeypatch.setenv("QQ_BOT_TRANSPORT", "invalid")
    with pytest.raises(ValueError, match="QQ_BOT_TRANSPORT"):
        Settings.from_env()
