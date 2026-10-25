import asyncio
import json
import sqlite3
import time
from dataclasses import replace
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from qq_codex_bot.app import create_app
from qq_codex_bot.config import Settings
from qq_codex_bot.gateway import Gateway
from qq_codex_bot.ingress import accept_event
from qq_codex_bot.messages import Message
from qq_codex_bot.qq import QQClient, QQError
from qq_codex_bot.signing import QQSignature
from qq_codex_bot.store import Store, is_control_command
from qq_codex_bot.worker import Worker


@pytest.fixture
def settings(tmp_path):
    return Settings("app", "secret", data_dir=tmp_path, workspace=tmp_path / "workspace",
                    self_id="our-bot")


def event(text="hello", message_id="m1", kind="GROUP_MESSAGE_CREATE", group="group"):
    return {"op": 0, "t": kind, "d": {
        "id": message_id, "content": text, "group_openid": group,
        "author": {"member_openid": "user", "user_openid": "user"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }}


def group_message(text="hello", message_id="m1", mentioned=False):
    return Message("app", "group", "group", "user", message_id, text, time.time(), mentioned)


class EchoCodex:
    def __init__(self):
        self.prompts = []

    async def run(self, prompt, thread_id, on_thread):
        self.prompts.append(prompt)
        on_thread("thread")
        return "normal reply"


class RecordingQQ:
    def __init__(self):
        self.sent = []
        self.fail = False

    async def prepare_reply(self, message, text, **kwargs):
        pass

    async def reply(self, message, text, **kwargs):
        if self.fail:
            raise QQError(503)
        self.sent.append(text)


def test_group_setting_persists_and_isolated_from_sessions_and_other_bots(settings):
    database = settings.data_dir / "settings.sqlite3"
    store = Store(database)
    key = group_message().chat_key
    assert store.all_message_enabled(key)
    store.set_all_message(key, False)
    store.new_session(key)
    store.reset(key)
    store.close()
    store = Store(database)
    assert not store.all_message_enabled(key)
    assert store.all_message_enabled(replace(group_message(), target="other").chat_key)
    assert store.all_message_enabled(replace(group_message(), app_id="other-bot").chat_key)
    store.close()


@pytest.mark.parametrize("text,mentions,accepted", [
    ("hello", None, False),
    ("<@other-bot> hello", None, False),
    ("/all_message on", None, True),   # 指令不走 @ 门槛，见 ingress 的 is_control_command 判断
    ("<@our-bot> hello", None, True),
    ("<@!our-bot> /all_message on", None, True),
    ("hello", [{"id": "our-bot"}], True),
    ("hello", [{"id": "other-bot", "bot": True}], False),
    ("hello", {"id": "our-bot"}, False),
])
def test_disabled_group_only_enqueues_mentions_of_this_bot(settings, text, mentions, accepted):
    store = Store(settings.data_dir / "filter.sqlite3")
    store.set_all_message(group_message().chat_key, False)
    packet = event(text)
    if mentions is not None:
        packet["d"]["mentions"] = mentions
    accept_event(settings, store, packet)
    assert store.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == int(accepted)
    if accepted:
        job = store.db.execute("SELECT payload FROM jobs").fetchone()
        payload = json.loads(job["payload"])
        # 指令即使不带 @ 也放行，所以不要求 mentioned 为真
        assert payload["mentioned"] is True or is_control_command(payload["text"])
    store.close()


def test_explicit_at_events_work_without_identity_and_strip_command_prefix(settings):
    settings = replace(settings, self_id="")
    store = Store(settings.data_dir / "at.sqlite3")
    store.set_all_message(group_message().chat_key, False)
    accept_event(settings, store,
                 event("<@unknown-bot> /all_message on", kind="GROUP_AT_MESSAGE_CREATE"))
    job = store.claim(control_only=True)
    assert job is not None
    assert Message(**json.loads(job["payload"])).text == "/all_message on"
    store.close()


def test_default_full_mode_private_chat_and_other_groups_are_unaffected(settings):
    store = Store(settings.data_dir / "scope.sqlite3")
    accept_event(settings, store, event())
    store.set_all_message(group_message().chat_key, False)
    accept_event(settings, store, event(message_id="blocked"))
    accept_event(settings, store, event(message_id="other", group="other"))
    accept_event(settings, store, event(message_id="private", kind="C2C_MESSAGE_CREATE"))
    assert store.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3
    store.close()


def test_allowlists_still_reject_switches_and_mentions(settings):
    store = Store(settings.data_dir / "allowlists.sqlite3")
    packet = event("/all_message off", kind="GROUP_AT_MESSAGE_CREATE")
    accept_event(replace(settings, allowed_users=frozenset({"someone-else"})), store, packet)
    accept_event(replace(settings, allowed_groups=frozenset({"elsewhere"})), store, packet)
    assert store.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    store.close()


def test_switch_is_immediate_idempotent_and_does_not_call_codex(settings):
    async def scenario():
        store = Store(settings.data_dir / "commands.sqlite3")
        codex, qq = EchoCodex(), RecordingQQ()
        worker = Worker(store, codex, qq)
        store.enqueue(group_message(message_id="busy"))
        busy = store.claim()
        packet = event("/all_message off", message_id="off", kind="GROUP_AT_MESSAGE_CREATE")
        accept_event(settings, store, packet)
        accept_event(settings, store, packet)
        command = store.claim(control_only=True)
        assert command is not None
        await worker.process(command)
        assert not store.all_message_enabled(group_message().chat_key)
        assert len(qq.sent) == 1 and "已关闭" in qq.sent[0]
        assert store.claim(control_only=True) is None
        assert not codex.prompts
        assert store.db.execute("SELECT status FROM jobs WHERE id=?", (busy["id"],)).fetchone()[0] == "running"

        for index, text in enumerate(("/all_message status", "/all_message off", "/all_message", "/all_message on")):
            accept_event(settings, store, event(text, f"control-{index}", "GROUP_AT_MESSAGE_CREATE"))
            await worker.process(store.claim(control_only=True))
        assert store.all_message_enabled(group_message().chat_key)
        assert ["已关闭" in reply for reply in qq.sent] == [True, True, True, False, False]
        assert not codex.prompts
        store.close()

    asyncio.run(scenario())


def test_switch_retry_does_not_toggle_again(settings):
    async def scenario():
        store = Store(settings.data_dir / "retry.sqlite3")
        codex, qq = EchoCodex(), RecordingQQ()
        worker = Worker(store, codex, qq)
        store.enqueue(group_message("/all_message"))
        qq.fail = True
        await worker.process(store.claim(control_only=True))
        assert not store.all_message_enabled(group_message().chat_key)
        with store.db:
            store.db.execute("UPDATE jobs SET next_attempt=0")
        qq.fail = False
        await worker.process(store.claim(control_only=True))
        assert not store.all_message_enabled(group_message().chat_key)
        assert len(qq.sent) == 1 and "已关闭" in qq.sent[0]
        store.close()

    asyncio.run(scenario())


def test_switch_off_skips_waiting_messages_but_still_processes_at_messages(settings):
    async def scenario():
        store = Store(settings.data_dir / "waiting.sqlite3")
        codex, qq = EchoCodex(), RecordingQQ()
        worker = Worker(store, codex, qq)
        store.enqueue(group_message())
        store.enqueue(group_message("<@other-bot> hello", message_id="other"))
        store.set_all_message(group_message().chat_key, False)
        for _ in range(2):
            await worker.process(store.claim())
        assert not codex.prompts and not qq.sent
        assert store.db.execute("SELECT COUNT(*) FROM jobs WHERE status='done' AND reply=''").fetchone()[0] == 2
        accept_event(settings, store, event("<@our-bot> <@friend> hello", "mentioned"))
        await worker.process(store.claim())
        assert codex.prompts == ["[群成员 user]\n<@friend> hello"]
        assert qq.sent == ["normal reply"]
        store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("text,kind", [
    ("/all_message off", "c2c"),
    ("/all_message invalid", "group"),
    ("/all_message on extra", "group"),
])
def test_invalid_or_private_commands_do_not_change_group_mode(settings, text, kind):
    async def scenario():
        store = Store(settings.data_dir / "invalid.sqlite3")
        codex, qq = EchoCodex(), RecordingQQ()
        worker = Worker(store, codex, qq)
        store.enqueue(replace(group_message(text), kind=kind))
        await worker.process(store.claim(control_only=True))
        assert store.all_message_enabled(group_message().chat_key)
        assert len(qq.sent) == 1 and not codex.prompts
        store.close()

    asyncio.run(scenario())


def test_old_payloads_and_queued_commands_are_compatible(settings):
    database = settings.data_dir / "upgrade.sqlite3"
    store = Store(database)
    store.enqueue(group_message("/all_message status"))
    with store.db:
        store.db.execute("UPDATE jobs SET is_control=0")
    store.close()
    store = Store(database)
    assert store.claim(control_only=True) is not None
    old_payload = group_message().as_dict()
    old_payload.pop("mentioned")
    assert Message(**old_payload).mentioned is False
    store.close()


def test_ready_identity_recognizes_self_in_full_message_events(settings):
    async def scenario():
        store = Store(settings.data_dir / "ready.sqlite3")
        store.set_all_message(group_message().chat_key, False)
        settings_without_id = replace(settings, self_id="")
        async with httpx.AsyncClient() as client:
            gateway = Gateway(QQClient(settings, client), lambda packet: accept_event(
                settings_without_id, store, packet, self_ids=gateway.self_ids
            ))
            await gateway.dispatch(None, {"op": 0, "t": "READY", "d": {
                "session_id": "session", "user": {"id": "numeric-id", "union_openid": "our-bot"},
            }})
            await gateway.dispatch(None, event("<@our-bot> hello"))
            assert store.claim() is not None
        store.close()

    asyncio.run(scenario())


def test_webhook_uses_same_mention_filter(settings, monkeypatch):
    async def idle(self, **kwargs):
        await asyncio.Future()

    monkeypatch.setattr(Worker, "run", idle)
    database = settings.data_dir / "sessions.sqlite3"
    store = Store(database)
    store.set_all_message(group_message().chat_key, False)
    store.close()
    with TestClient(create_app(replace(settings, transport="webhook"))) as client:
        for index, text in enumerate(("ignored", "<@other-bot> ignored", "<@our-bot> hello")):
            body = json.dumps(event(text, str(index))).encode()
            timestamp = str(int(time.time()))
            signature = QQSignature(settings.app_secret).key.sign(timestamp.encode() + body).hex()
            response = client.post("/qq/webhook", content=body, headers={
                "X-Signature-Timestamp": timestamp, "X-Signature-Ed25519": signature,
            })
            assert response.status_code == 200
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
