"""QQ gateway: outbound WSS, protocol heartbeat, resumable reconnect and event dispatch."""

import asyncio
import json
import logging
import random
import time
from collections.abc import Callable

import aiohttp
import httpx

from .qq import QQClient, QQError

log = logging.getLogger(__name__)
GROUP_AND_C2C_EVENT = 1 << 25


class GatewayError(RuntimeError):
    pass


class Gateway:
    def __init__(self, qq: QQClient, on_event: Callable[[dict], None]):
        self.qq = qq
        self.on_event = on_event
        self.session_id: str | None = None
        self.sequence: int | None = None
        self.self_ids: frozenset[str] = frozenset()
        self.ready = asyncio.Event()
        self.status = "starting"
        self.last_error: str | None = None
        self.awaiting_ack = False
        self.events_received = 0
        self.messages_received = 0
        self.last_event: str | None = None
        self.last_message_at: float | None = None
        self.heartbeats_sent = 0
        self.heartbeat_acks = 0

    def health(self) -> dict:
        return {"status": self.status, "ready": self.ready.is_set(), "error": self.last_error,
                "events_received": self.events_received, "messages_received": self.messages_received,
                "last_event": self.last_event, "last_message_at": self.last_message_at,
                "heartbeats_sent": self.heartbeats_sent, "heartbeat_acks": self.heartbeat_acks}

    def clear_session(self):
        self.session_id = None
        self.sequence = None

    async def identify(self, ws, token: str):
        if self.session_id is not None and self.sequence is not None:
            await ws.send_json({"op": 6, "d": {
                "token": f"QQBot {token}", "session_id": self.session_id, "seq": self.sequence}})
            log.info("QQ WebSocket reconnect: requesting session resume")
        else:
            await ws.send_json({"op": 2, "d": {
                "token": f"QQBot {token}", "intents": GROUP_AND_C2C_EVENT,
                "shard": [0, 1], "properties": {"$os": "python", "$browser": "qq-codex-bot",
                                               "$device": "qq-codex-bot"}}})
            log.info("QQ WebSocket authentication sent (group + C2C events)")

    async def dispatch(self, ws, event: dict):
        op = event.get("op")
        if op == 11:
            self.awaiting_ack = False
            self.heartbeat_acks += 1
        elif op == 1:
            await ws.send_json({"op": 1, "d": self.sequence})
        elif op == 7:
            raise GatewayError("server_reconnect")
        elif op == 9:
            # Invalid session: re-identify with fresh credentials instead of looping on resume.
            self.clear_session()
            self.qq.token = ""
            raise GatewayError("invalid_session")
        elif op == 0:
            event_type = event.get("t")
            self.events_received += 1
            # Persist only protocol event names in health/logs, never arbitrary payload text.
            if isinstance(event_type, str) and len(event_type) <= 80 and all(
                c.isupper() or c.isdigit() or c == "_" for c in event_type
            ):
                self.last_event = event_type
                log.info("QQ WebSocket event=%s", event_type)
            if event_type in {"C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"}:
                self.messages_received += 1
                self.last_message_at = time.time()
            if event_type == "READY":
                data = event.get("d")
                if not isinstance(data, dict) or not isinstance(data.get("session_id"), str):
                    raise GatewayError("invalid_ready_payload")
                self.session_id = data["session_id"]
                user = data.get("user")
                if isinstance(user, dict):
                    self.self_ids = frozenset(
                        str(user[field]) for field in ("id", "openid", "union_openid")
                        if isinstance(user.get(field), (str, int))
                        and not isinstance(user[field], bool) and str(user[field])
                    )
                self.status = "ready"
                self.last_error = None
                self.ready.set()
                log.info("QQ WebSocket READY: authenticated; waiting for private/group messages")
            elif event_type == "RESUMED":
                self.status = "ready"
                self.last_error = None
                self.ready.set()
                log.info("QQ WebSocket RESUMED: connection restored")
            else:
                try:
                    self.on_event(event)
                except (ValueError, TypeError, OverflowError):
                    log.warning("QQ WebSocket skipped malformed message")
            # Commit messages before advancing the resume cursor; redelivery is deduplicated.
            if isinstance(event.get("s"), int):
                self.sequence = event["s"]

    async def heartbeat(self, ws, interval: float):
        # QQ starts heartbeat handling after READY/RESUMED. Sending before authentication
        # can leave an unanswered initial heartbeat and force a reconnect every interval.
        await self.ready.wait()
        while True:
            if self.awaiting_ack:
                raise GatewayError("heartbeat_ack_timeout")
            self.awaiting_ack = True
            await ws.send_json({"op": 1, "d": self.sequence})
            self.heartbeats_sent += 1
            await asyncio.sleep(interval)

    async def receive(self, ws):
        while True:
            # READY/RESUMED must arrive promptly; once ready, heartbeats detect stalled links.
            packet = await asyncio.wait_for(ws.receive(), timeout=None if self.ready.is_set() else 20)
            if packet.type == aiohttp.WSMsgType.TEXT:
                try:
                    event = json.loads(packet.data)
                except ValueError:
                    raise GatewayError("invalid_gateway_json") from None
                if not isinstance(event, dict):
                    raise GatewayError("invalid_gateway_payload")
                await self.dispatch(ws, event)
            elif packet.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                                 aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSING}:
                code = ws.close_code
                if code in {4004, 4006, 4007, 4009}:
                    self.clear_session()
                if code == 4004:
                    self.qq.token = ""
                raise GatewayError(f"connection_closed_{code}")

    async def connection(self, session):
        self.ready.clear()
        self.status = "connecting"
        url = await self.qq.gateway_url()
        token = await self.qq.access_token()
        async with session.ws_connect(url, autoping=True, max_msg_size=4 * 1024 * 1024) as ws:
            hello = await asyncio.wait_for(ws.receive_json(), 15)
            if not isinstance(hello, dict) or hello.get("op") != 10:
                raise GatewayError("expected_hello")
            data = hello.get("d")
            interval_ms = data.get("heartbeat_interval") if isinstance(data, dict) else None
            if not isinstance(interval_ms, (int, float)) or not 0 < interval_ms <= 300000:
                raise GatewayError("invalid_heartbeat_interval")
            self.status = "authenticating"
            await self.identify(ws, token)
            self.awaiting_ack = False
            tasks = [asyncio.create_task(self.receive(ws)),
                     asyncio.create_task(self.heartbeat(ws, interval_ms / 1000))]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def run(self):
        delay = 1
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                while True:
                    try:
                        await self.connection(session)
                    except (GatewayError, QQError, aiohttp.ClientError, httpx.HTTPError,
                            TimeoutError, ValueError, TypeError, OSError) as exc:
                        was_ready = self.ready.is_set()
                        self.ready.clear()
                        self.status = "reconnecting"
                        # Never log raw exception strings; URLs and server bodies can hold credentials.
                        if isinstance(exc, QQError):
                            self.last_error = f"QQ_API_HTTP_{exc.status}_code_{exc.code}"
                        elif isinstance(exc, GatewayError):
                            self.last_error = str(exc)
                        else:
                            self.last_error = type(exc).__name__
                        log.warning("QQ WebSocket disconnected/error=%s; reconnecting", self.last_error)
                        if was_ready:
                            delay = 1
                        await asyncio.sleep(delay + random.random())
                        delay = min(delay * 2, 30)
        finally:
            self.ready.clear()
            self.status = "stopped"


async def probe(settings, timeout: float = 30) -> bool:
    """Authenticate one real gateway connection without invoking Codex or replying to users."""
    async with httpx.AsyncClient(timeout=15) as client:
        gateway = Gateway(QQClient(settings, client), lambda _: None)
        task = asyncio.create_task(gateway.run())
        try:
            await asyncio.wait_for(gateway.ready.wait(), timeout)
            print("QQ WebSocket READY：鉴权成功，可以接收私聊和群聊消息。")
            return True
        except TimeoutError:
            print(f"QQ WebSocket 尚未就绪，最后状态：{gateway.last_error or gateway.status}")
            return False
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    from .config import Settings

    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    raise SystemExit(0 if asyncio.run(probe(Settings.from_env())) else 1)
