import asyncio
import json
import time
from dataclasses import replace

import httpx
import pytest

from qq_codex_bot.codex import Codex, CodexError
from qq_codex_bot.config import Settings
from qq_codex_bot.messages import Message
from qq_codex_bot.outbound import build_plan
from qq_codex_bot.qq import QQClient
from qq_codex_bot.store import Store
from qq_codex_bot.worker import Worker


@pytest.fixture
def settings(tmp_path):
    return Settings("app", "secret", data_dir=tmp_path, workspace=tmp_path / "workspace")


def fake_codex(settings, events, exit_code=0):
    script = settings.data_dir / "fake_codex.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stdin.read()\n"
        f"print({events!r})\n"
        f"sys.exit({exit_code})\n"
    )
    script.chmod(0o755)
    return Codex(replace(settings, codex_bin=str(script)))


@pytest.mark.parametrize("answer", ["", " \n\t\u3000", None])
def test_successful_codex_empty_final_reply_is_silent(settings, answer):
    events = [
        {"type": "thread.started", "thread_id": "silent-thread"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "earlier"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": answer}},
        {"type": "turn.completed"},
    ]
    codex = fake_codex(settings, "\n".join(map(json.dumps, events)))
    threads = []
    assert asyncio.run(codex.run("please stay silent", None, threads.append)) == ""
    assert threads == ["silent-thread"]


@pytest.mark.parametrize("events", [
    "",
    " \n\t",
    "not a JSON event\nnull\n[]\n42",
    json.dumps({"type": "item.completed", "item": None}),
    json.dumps({"type": "turn.completed"}),
    json.dumps({"type": "item.completed", "item": {"type": "command_execution"}}),
])
def test_successful_codex_without_reply_events_is_silent(settings, events):
    codex = fake_codex(settings, events)
    assert asyncio.run(codex.run("hello", "existing-thread", lambda _: None)) == ""


@pytest.mark.parametrize("events,exit_code", [
    ("", 1),
    (json.dumps({"type": "turn.failed"}), 0),
])
def test_actual_codex_failures_still_report_errors(settings, events, exit_code):
    codex = fake_codex(settings, events, exit_code)
    with pytest.raises(CodexError):
        asyncio.run(codex.run("hello", None, lambda _: None))


@pytest.mark.parametrize("answer", ["", " \n\t\u3000", "a", "ab", "好", " \n你好\t\u3000"])
def test_silent_task_is_done_without_qq_calls_and_reuses_session_after_restart(settings, answer):
    async def scenario():
        calls = []

        class SilentCodex:
            async def run(self, prompt, thread_id, on_thread):
                calls.append(thread_id)
                on_thread(thread_id or "silent-thread")
                return answer

        def unexpected_http(request):
            pytest.fail("A silent reply must not call QQ, including token acquisition")

        msg = Message("app", "c2c", "user", "user", "m1", "hello", time.time())
        database = settings.data_dir / "silent.sqlite3"
        store = Store(database)
        async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_http)) as client:
            worker = Worker(store, SilentCodex(), QQClient(settings, client, store=store))
            store.enqueue(msg)
            await worker.process(store.claim())
            row = store.db.execute("SELECT status,error,reply FROM jobs WHERE id=1").fetchone()
            assert row["status"] == "done" and row["error"] is None
            assert row["reply"] == answer
            assert store.task_status(msg.chat_key) is None
            store.close()

            store = Store(database)
            store.recover()
            worker = Worker(store, SilentCodex(), QQClient(settings, client, store=store))
            # Retrieving an intentionally silent result must not add a visible prefix.
            store.enqueue(replace(msg, message_id="query", text="/result"))
            await worker.process(store.claim(control_only=True))
            assert calls == [None]
            store.enqueue(replace(msg, message_id="m2", text="continue"))
            await worker.process(store.claim())
            assert calls == [None, "silent-thread"]
            assert store.db.execute("SELECT COUNT(*) FROM jobs WHERE error IS NOT NULL").fetchone()[0] == 0
            store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("answer", ["", " \n\t\u3000", "a", "ab", "好", " \n你好\t\u3000"])
def test_short_outbound_reply_does_not_create_a_placeholder_or_send(settings, answer):
    async def scenario():
        def unexpected_http(request):
            pytest.fail("No message should be sent for a short reply")

        msg = Message("app", "group", "group", "user", "m1", "hello", time.time())
        directory = settings.data_dir / "artifacts"
        assert build_plan(answer, settings, directory) == []
        assert not directory.exists()
        async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_http)) as client:
            qq = QQClient(settings, client)
            assert await qq.prepare_reply(msg, answer) == ([], 0)
            await qq.reply(msg, answer)

    asyncio.run(scenario())


@pytest.mark.parametrize("answer", ["abc", "你好呀", " \nabc\t\u3000", " \n你好呀\t\u3000"])
def test_three_trimmed_characters_are_delivered_and_retrievable(settings, answer):
    async def scenario():
        sent = []

        class ShortCodex:
            async def run(self, prompt, thread_id, on_thread):
                on_thread("short-thread")
                return answer

        def handle(request):
            if request.url.path.endswith("getAppAccessToken"):
                return httpx.Response(200, json={"access_token": "token", "expires_in": 7200})
            payload = json.loads(request.content)
            sent.append(payload.get("markdown", {}).get("content", payload.get("content")))
            return httpx.Response(200, json={"id": "reply"})

        msg = Message("app", "group", "group", "user", "m1", "hello", time.time())
        store = Store(settings.data_dir / "boundary.sqlite3")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            worker = Worker(store, ShortCodex(), QQClient(settings, client, store=store))
            store.enqueue(msg)
            await worker.process(store.claim())
            assert sent == [answer.strip()]
            store.enqueue(replace(msg, message_id="query", text="/result"))
            await worker.process(store.claim(control_only=True))
            assert sent == [answer.strip(), f"任务 #1 的结果：\n{answer.strip()}"]
            assert store.db.execute("SELECT COUNT(*) FROM jobs WHERE error IS NOT NULL").fetchone()[0] == 0
        store.close()

    asyncio.run(scenario())
