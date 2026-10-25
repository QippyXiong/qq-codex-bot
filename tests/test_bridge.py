import asyncio
import json
import time
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from qq_codex_bot.app import create_app
from qq_codex_bot.codex import Codex, CodexError
from qq_codex_bot.config import Settings
from qq_codex_bot.messages import Message
from qq_codex_bot.qq import QQClient, QQError
from qq_codex_bot.signing import QQSignature
from qq_codex_bot.store import Store
from qq_codex_bot.worker import Worker


@pytest.fixture
def settings(tmp_path):
    return Settings(app_id="test-app", app_secret="test-secret",
                    data_dir=tmp_path, workspace=tmp_path / "workspace", transport="webhook")


def message(text="hello", target="alice", kind="c2c", sender="alice", msg_id="m1"):
    return Message("test-app", kind, target, sender, msg_id, text, time.time())


class FakeCodex:
    def __init__(self):
        self.calls = []

    async def run(self, prompt, thread_id, on_thread):
        self.calls.append((prompt, thread_id))
        thread = thread_id or f"thread-{len(self.calls)}"
        on_thread(thread)
        return "reply:" + prompt


class FakeQQ:
    def __init__(self):
        self.sent = []
        self.fail = False

    async def prepare_reply(self, msg, text, **kwargs):
        pass

    async def reply(self, msg, text, **kwargs):
        if self.fail:
            raise QQError(503)
        self.sent.append((msg, text))


def test_official_signature_vectors():
    signing = QQSignature("naOC0ocQE3shWLAfffVLB1rhYPG7")
    from cryptography.hazmat.primitives import serialization
    public_key = signing.key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    assert list(public_key) == [
        215, 195, 98, 254, 120, 174, 248, 31, 242, 50, 135, 180, 147, 98, 139, 93,
        176, 42, 60, 79, 227, 11, 33, 94, 77, 25, 96, 155, 93, 118, 103, 58,
    ]
    body = b'{ "op": 0,"d": {}, "t": "GATEWAY_EVENT_NAME"}'
    sig = signing.key.sign(b"1725442341" + body).hex()
    assert signing.verify("1725442341", body, sig)
    assert not signing.verify("1725442341", body + b" ", sig)
    assert not signing.verify("1725442341", body, "invalid")
    challenge = QQSignature("DG5g3B4j9X2KOErG").challenge("Arq0D5A61EgUu4OxUvOp", "1725442341")
    assert challenge["signature"] == (
        "87befc99c42c651b3aac0278e71ada338433ae26fcb24307bdc5ad38c1adc2d01b"
        "cfcadc0842edac85e85205028a1132afe09280305f13aa6909ffc2d652c706"
    )


def test_session_reuse_isolation_new_reset_and_restart(settings):
    async def scenario():
        store = Store(settings.data_dir / "test.sqlite3")
        codex, qq = FakeCodex(), FakeQQ()
        worker = Worker(store, codex, qq)

        async def send(msg):
            assert store.enqueue(msg)
            await worker.process(store.claim())

        await send(message())
        await send(message("again", msg_id="m2"))
        assert codex.calls[:2] == [("hello", None), ("again", "thread-1")]
        await send(message(target="bob", sender="bob", msg_id="m3"))
        assert codex.calls[2][1] is None
        await send(message(kind="group", target="group1", msg_id="m4"))
        await send(message(kind="group", target="group1", sender="bob", msg_id="m5"))
        assert codex.calls[3:5] == [
            ("[群成员 alice]\nhello", None), ("[群成员 bob]\nhello", "thread-4"),
        ]
        await send(message("/new", msg_id="m6"))
        assert codex.calls[5][1] is None
        await send(message("after new", msg_id="m7"))
        assert codex.calls[6][1] == "thread-6"
        await send(message("/reset", msg_id="m8"))
        assert store.current_session(message().chat_key) is None
        await send(message("after reset", msg_id="m9"))
        assert codex.calls[7][1] is None
        store.close()
        reopened = Store(settings.data_dir / "test.sqlite3")
        assert reopened.current_session(message().chat_key)["thread_id"] == "thread-8"
        assert not reopened.enqueue(message())
        reopened.close()

    asyncio.run(scenario())


def test_claim_preserves_chat_order_but_allows_other_chats(settings):
    store = Store(settings.data_dir / "test.sqlite3")
    for msg in (message(), message(msg_id="m2"), message(target="bob", msg_id="m3")):
        store.enqueue(msg)
    first = store.claim()
    assert first["id"] == 1
    assert store.claim()["id"] == 3
    assert store.claim() is None
    store.finish(1)
    assert store.claim()["id"] == 2
    store.close()


def test_delivery_retry_does_not_repeat_codex(settings):
    async def scenario():
        store = Store(settings.data_dir / "test.sqlite3")
        codex, qq = FakeCodex(), FakeQQ()
        worker = Worker(store, codex, qq)
        store.enqueue(message())
        qq.fail = True
        await worker.process(store.claim())
        with store.db:
            store.db.execute("UPDATE jobs SET next_attempt=0")
        qq.fail = False
        await worker.process(store.claim())
        assert len(codex.calls) == 1
        assert len(qq.sent) == 1
        assert store.db.execute("SELECT status FROM jobs").fetchone()[0] == "done"
        store.close()

    asyncio.run(scenario())


def test_crash_recovery_does_not_replay_codex(settings):
    store = Store(settings.data_dir / "test.sqlite3")
    store.enqueue(message())
    store.claim()
    store.recover()
    job = store.claim()
    assert job["reply"] and "重启" in job["reply"]
    store.close()


def test_expired_reply_window_still_runs_codex_and_saves_result(settings):
    async def scenario():
        store = Store(settings.data_dir / "test.sqlite3")
        codex, qq = FakeCodex(), FakeQQ()
        msg = Message("test-app", "c2c", "u", "u", "old", "hello", time.time() - 400)
        store.enqueue(msg)
        await Worker(store, codex, qq).process(store.claim())
        assert len(codex.calls) == 1 and not qq.sent
        assert store.latest_result(msg.chat_key)["reply"] == "reply:hello"
        assert store.db.execute("SELECT error FROM jobs").fetchone()[0] == "reply_window_expired"
        store.close()

    asyncio.run(scenario())


def test_webhook_verification_and_dedup(settings, monkeypatch):
    async def idle(self, **kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(Worker, "run", idle)
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 200
        challenge = {"op": 13, "d": {"plain_token": "token", "event_ts": "123"}}
        assert "signature" in client.post("/qq/webhook", json=challenge).json()
        event = {"op": 0, "t": "C2C_MESSAGE_CREATE", "d": {
            "id": "m1", "author": {"user_openid": "alice"}, "content": "hello",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }}
        raw = json.dumps(event).encode()
        timestamp = str(int(time.time()))
        sig = QQSignature(settings.app_secret).key.sign(timestamp.encode() + raw).hex()
        headers = {"X-Signature-Timestamp": timestamp, "X-Signature-Ed25519": sig}
        assert client.post("/qq/webhook", content=raw).status_code == 401
        assert client.post("/qq/webhook", content=raw + b" ", headers=headers).status_code == 401
        for _ in range(2):
            assert client.post("/qq/webhook", content=raw, headers=headers).json() == {"op": 12}
        # Query through a separate connection: TestClient owns the service connection's thread.
        import sqlite3
        with sqlite3.connect(settings.data_dir / "sessions.sqlite3") as db:
            assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_qq_token_refresh_payload_and_business_error(settings):
    async def scenario():
        requests = []

        def handle(request):
            requests.append(request)
            if request.url.path.endswith("getAppAccessToken"):
                return httpx.Response(200, json={"access_token": "test-token", "expires_in": 7200})
            assert request.headers["Authorization"] == "QQBot test-token"
            payload = json.loads(request.content)
            assert payload["msg_seq"] == 1 and payload["msg_id"] == "m1"
            return httpx.Response(200, json={"id": "reply-id"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            qq = QQClient(settings, client)
            await qq.reply(message(), "hello")
            await qq.reply(message(kind="group", target="g"), "hello")
            assert len(requests) == 3
            assert requests[-1].url.path == "/v2/groups/g/messages"
        with pytest.raises(QQError):
            QQClient.checked(httpx.Response(200, json={"code": 100016}))
        with pytest.raises(QQError):
            QQClient.checked(httpx.Response(200, json={"err_code": 304103}))

    asyncio.run(scenario())


@pytest.mark.parametrize("thread_id", [None, "saved-thread"])
def test_codex_subprocess_protocol_and_secret_filter(settings, tmp_path, monkeypatch, thread_id):
    fake = tmp_path / "fake_codex.py"
    fake.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
assert "QQ_BOT_APP_SECRET" not in os.environ
assert sys.argv[1:3] == ["--approve-for-me", "exec"]
assert "never" not in sys.argv and "-s" not in sys.argv
prompt = sys.stdin.read()
assert prompt == "hello; $(echo no-shell)"
instructions = Path("AGENTS.md").read_text()
assert "图片与文件交付" in instructions and "最终回复" in instructions
print(json.dumps({"type":"thread.started", "thread_id":"test-thread"}), flush=True)
print(json.dumps({"type":"item.completed", "item":{"type":"agent_message", "text":"ok"}}))
print(json.dumps({"type":"turn.completed"}))
''')
    fake.chmod(0o755)
    from dataclasses import replace
    codex = Codex(replace(settings, codex_bin=str(fake)))
    wait_timeouts = []
    original_wait_for = asyncio.wait_for

    async def record_wait_for(awaitable, timeout):
        wait_timeouts.append(timeout)
        return await original_wait_for(awaitable, timeout)

    monkeypatch.setattr(asyncio, "wait_for", record_wait_for)
    monkeypatch.setenv("QQ_BOT_APP_SECRET", "must-not-inherit")
    threads = []
    assert asyncio.run(codex.run("hello; $(echo no-shell)", thread_id, threads.append)) == "ok"
    assert threads == ["test-thread"]
    assert wait_timeouts[0] is None
    args = codex.command("saved-thread")
    assert args[-3:] == ["resume", "saved-thread", "-"]


def test_codex_timeout_terminates_subprocess(settings, tmp_path):
    from dataclasses import replace
    fake = tmp_path / "slow.py"
    fake.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(60)\n")
    fake.chmod(0o755)
    codex = Codex(replace(settings, codex_bin=str(fake), timeout=1))
    with pytest.raises(CodexError, match="超时"):
        asyncio.run(codex.run("hello", None, lambda _: None))


@pytest.mark.parametrize("host_sandbox", ["seatbelt", "approve-for-me", "unexpected-host-value"])
def test_config_ignores_host_codex_sandbox(monkeypatch, host_sandbox):
    monkeypatch.setenv("QQ_BOT_APP_ID", "test-app")
    monkeypatch.setenv("QQ_BOT_APP_SECRET", "test-secret")
    monkeypatch.setenv("CODEX_SANDBOX", host_sandbox)
    monkeypatch.delenv("QQ_BOT_CODEX_SANDBOX", raising=False)
    monkeypatch.delenv("QQ_BOT_CODEX_TIMEOUT", raising=False)
    monkeypatch.setattr("qq_codex_bot.config.shutil.which", lambda _: "/fake/codex")
    settings = Settings.from_env()
    assert settings.sandbox == "approve-for-me"
    assert settings.timeout == 0
    assert "test-secret" not in repr(settings)


@pytest.mark.parametrize("thread_id", [None, "saved-thread"])
@pytest.mark.parametrize("mode", ["approve-for-me", "read-only", "workspace-write"])
def test_codex_permission_modes(settings, thread_id, mode):
    from dataclasses import replace

    args = Codex(replace(settings, sandbox=mode, model="test-model")).command(thread_id)
    assert args[args.index("-m") + 1] == "test-model"
    if mode == "approve-for-me":
        assert args[1:3] == ["--approve-for-me", "exec"]
        assert "-a" not in args and "-s" not in args
    else:
        assert args[1:5] == ["-a", "never", "-s", mode]
        assert "--approve-for-me" not in args
    if thread_id:
        assert args[-3:] == ["resume", thread_id, "-"]


def test_group_events_share_key_and_ignore_bot():
    event = {"op": 0, "t": "GROUP_MESSAGE_CREATE", "d": {
        "id": "msg", "group_openid": "group", "author": {"member_openid": "alice"},
        "content": "hello", "timestamp": "2026-10-21T10:00:00+08:00",
    }}
    first = Message.from_event("app", event)
    event["d"]["author"]["member_openid"] = "bob"
    event["t"] = "GROUP_AT_MESSAGE_CREATE"
    second = Message.from_event("app", event)
    assert first.chat_key == second.chat_key
    assert first.dedup_key == second.dedup_key
    event["d"]["author"]["bot"] = True
    assert Message.from_event("app", event) is None


def test_webhook_allowlist_and_malformed_body(settings, monkeypatch):
    from dataclasses import replace
    import sqlite3

    async def idle(self, **kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(Worker, "run", idle)
    settings = replace(settings, allowed_groups=frozenset({"allowed-group"}))
    with TestClient(create_app(settings)) as client:
        for target in ("allowed-group", "other-group"):
            event = {"op": 0, "t": "GROUP_AT_MESSAGE_CREATE", "d": {
                "id": "msg", "author": {"member_openid": "alice"},
                "group_openid": target, "content": "hello",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }}
            raw = json.dumps(event).encode()
            signature = QQSignature(settings.app_secret).key.sign(b"123" + raw).hex()
            response = client.post("/qq/webhook", content=raw, headers={
                "X-Signature-Timestamp": "123", "X-Signature-Ed25519": signature})
            assert response.status_code == 200
        assert client.post("/qq/webhook", content=b"[]").status_code == 400
        assert client.post("/qq/webhook", json={"op": 13, "d": None}).status_code == 400
        with sqlite3.connect(settings.data_dir / "sessions.sqlite3") as db:
            assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


@pytest.mark.parametrize("timeout", [0, 3600, 86400])
def test_config_accepts_unlimited_and_long_timeouts(monkeypatch, timeout):
    monkeypatch.setenv("QQ_BOT_APP_ID", "test-app")
    monkeypatch.setenv("QQ_BOT_APP_SECRET", "test-secret")
    monkeypatch.setenv("QQ_BOT_CODEX_TIMEOUT", str(timeout))
    monkeypatch.setattr("qq_codex_bot.config.shutil.which", lambda _: "/fake/codex")
    assert Settings.from_env().timeout == timeout


def test_long_task_survives_reply_expiry_and_result_is_retrievable(settings, monkeypatch):
    from types import SimpleNamespace

    async def scenario():
        clock = [time.time()]
        monkeypatch.setattr("qq_codex_bot.worker.time", SimpleNamespace(time=lambda: clock[0]))
        started, release = asyncio.Event(), asyncio.Event()

        class LongCodex(FakeCodex):
            async def run(self, prompt, thread_id, on_thread):
                on_thread("long-thread")
                started.set()
                await release.wait()
                clock[0] += 1000  # Simulate a task outlasting both old timeout and reply window.
                return "long task result"

        store = Store(settings.data_dir / "long.sqlite3")
        qq = FakeQQ()
        worker = Worker(store, LongCodex(), qq)
        store.enqueue(message())
        task = asyncio.create_task(worker.process(store.claim()))
        await started.wait()
        try:
            # A normal follow-up must wait, but queries must work while the task is running.
            store.enqueue(message("follow-up", msg_id="m2"))
            store.enqueue(message("/status", msg_id="status"))
            assert store.claim() is None
            await worker.process(store.claim(control_only=True))
            assert "处理中" in qq.sent[-1][1]
            assert not task.done()
            release.set()
            await task
            assert len(qq.sent) == 1  # No attempt to reply against an expired message.
            result = store.latest_result(message().chat_key)
            assert result["reply"] == "long task result"
            store.close()

            store = Store(settings.data_dir / "long.sqlite3")
            worker = Worker(store, FakeCodex(), qq)
            query = Message("test-app", "c2c", "alice", "alice", "result", "/result", clock[0])
            store.enqueue(query)
            await worker.process(store.claim(control_only=True))
            assert "long task result" in qq.sent[-1][1]
            assert qq.sent[-1][0].message_id == "result"
            # Results cannot cross chat boundaries or overwrite themselves with query replies.
            assert store.latest_result(message(target="bob").chat_key) is None
            assert store.latest_result(message().chat_key)["id"] == result["id"]
            store.close()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_store_migrates_existing_jobs_schema(settings):
    import sqlite3

    path = settings.data_dir / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, dedup_key TEXT UNIQUE NOT NULL,
            chat_key TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            reply TEXT, session_id TEXT, attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt REAL NOT NULL DEFAULT 0, created REAL NOT NULL, error TEXT
        )""")
        msg = message("/status")
        db.execute("INSERT INTO jobs(dedup_key,chat_key,payload,created) VALUES(?,?,?,?)",
                   (msg.dedup_key, msg.chat_key, json.dumps(msg.as_dict()), time.time()))
    store = Store(path)
    assert store.claim() is None
    assert store.claim(control_only=True)["is_control"] == 1
    store.close()
