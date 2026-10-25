import asyncio
import base64
import hashlib
import json
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from qq_codex_bot.config import Settings
from qq_codex_bot.messages import Message
from qq_codex_bot.outbound import build_plan, clipped
from qq_codex_bot.qq import QQClient, QQError
from qq_codex_bot.store import Store
from qq_codex_bot.worker import Worker

PNG = b"\x89PNG\r\n\x1a\n" + b"test-image"


@pytest.fixture
def settings(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return Settings("app", "secret", data_dir=tmp_path / "data", workspace=workspace)


def message(text="hello", kind="c2c", target="user", msg_id="m1", sent_at=None):
    return Message("app", kind, target, "sender", msg_id, text, sent_at or time.time())


def test_markdown_payload_and_permission_fallback(settings):
    async def scenario():
        requests = []

        def handle(request):
            if request.url.path.endswith("getAppAccessToken"):
                return httpx.Response(200, json={"access_token": "token", "expires_in": 7200})
            data = json.loads(request.content)
            requests.append(data)
            if data["msg_type"] == 2:
                assert "content" not in data
                assert data["markdown"]["content"] == "# 标题\n\n**加粗**"
                return httpx.Response(200, json={"code": 50056})
            assert "markdown" not in data
            return httpx.Response(200, json={"id": "reply"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            qq = QQClient(settings, client)
            await qq.reply(message(), "# 标题\n\n**加粗**")
        assert [x["msg_type"] for x in requests] == [2, 0]
        assert [x["msg_seq"] for x in requests] == [1, 1]
        assert "Markdown 权限" in requests[-1]["content"]

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["c2c", "group"])
def test_upload_local_image_and_file_then_send_media(settings, kind):
    async def scenario():
        image = settings.workspace / "图 1.png"
        document = settings.workspace / "报告.csv"
        image.write_bytes(PNG)
        document.write_text("a,b\n1,2\n")
        uploads, replies = [], []

        def handle(request):
            assert request.url.host == "api.bot.qq.com"
            if request.url.path.endswith("getAppAccessToken"):
                return httpx.Response(200, json={"access_token": "token", "expires_in": 7200})
            payload = json.loads(request.content)
            resource = "groups" if kind == "group" else "users"
            assert request.url.path.startswith(f"/v2/{resource}/user/")
            if request.url.path.endswith("/files"):
                uploads.append(payload)
                assert payload["srv_send_msg"] is False and "url" not in payload
                return httpx.Response(200, json={"file_info": f"media-{len(uploads)}", "ttl": 3600})
            replies.append(payload)
            return httpx.Response(200, json={"id": "reply"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            await QQClient(settings, client).reply(message(kind=kind),
                f"# 已完成\n![预览](<{image}>)\n[下载报告](<{document}>)")
        assert [x["file_type"] for x in uploads] == [1, 4]
        assert uploads[1]["file_name"] == "报告.csv"
        assert base64.b64decode(uploads[0]["file_data"]) == PNG
        assert [x["msg_type"] for x in replies] == [2, 7, 7]
        assert [x["msg_seq"] for x in replies] == [1, 2, 3]
        assert str(settings.workspace) not in replies[0]["markdown"]["content"]
        assert replies[-1]["media"] == {"file_info": "media-2"}

    asyncio.run(scenario())


def test_attachment_paths_are_bounded_and_code_examples_are_not_sent(settings, tmp_path):
    image = settings.workspace / "chart.png"
    image.write_bytes(PNG)
    outside = tmp_path / "private.txt"
    outside.write_text("secret")
    (settings.workspace / "escape.txt").symlink_to(outside)
    text = (f"示例：`[例子](<{image}>)`\n```markdown\n![代码示例](<{image}>)\n```\n"
            f"![图](<{image}>)\n[越界](<{outside}>)\n[软链接](escape.txt)")
    plan = build_plan(text, settings, settings.data_dir / "artifacts")
    media = [x for x in plan if x["kind"] == "media"]
    assert len(media) == 1 and media[0]["file_type"] == 1
    assert Path(media[0]["path"]).read_bytes() == PNG
    assert "不在 Codex 工作目录" in plan[0]["text"]
    assert "代码示例" in plan[0]["text"]


def test_reply_quota_and_size_limit_are_reported(settings):
    links = []
    for index in range(5):
        path = settings.workspace / f"file{index}.txt"
        path.write_text("file")
        links.append(f"[文件](<{path}>)")
    plan = build_plan("\n".join(links), settings, settings.data_dir / "artifacts")
    assert len(plan) == 4  # one text + three attachments fits C2C quota
    assert "超过 3 个附件" in plan[0]["text"]
    settings = replace(settings, max_attachment_bytes=2)
    plan = build_plan(links[0], settings, settings.data_dir / "small")
    assert len(plan) == 1 and "超过" in plan[0]["text"]
    assert len(clipped("a" * 20000)) <= 4000


def test_retry_resumes_at_unsent_attachment_after_restart(settings):
    async def scenario():
        image = settings.workspace / "result.png"
        image.write_bytes(PNG)
        store = Store(settings.data_dir / "db.sqlite3")
        msg = message()
        store.enqueue(msg)
        job = store.claim()
        sent = []
        fail = [True]

        def handle(request):
            if request.url.path.endswith("getAppAccessToken"):
                return httpx.Response(200, json={"access_token": "token", "expires_in": 7200})
            payload = json.loads(request.content)
            if request.url.path.endswith("/files"):
                assert base64.b64decode(payload["file_data"]) == PNG
                return httpx.Response(200, json={"file_info": "info", "ttl": 3600})
            if payload["msg_type"] == 7 and fail[0]:
                return httpx.Response(503, json={"code": 1})
            sent.append(payload)
            return httpx.Response(200, json={"id": "reply"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            qq = QQClient(settings, client, store=store)
            text = f"![结果](<{image}>)"
            with pytest.raises(QQError):
                await qq.reply(msg, text, job_id=job["id"])
            assert store.delivery(job["id"])["delivered"] == 1
            store.close()
            image.unlink()
            store = Store(settings.data_dir / "db.sqlite3")
            fail[0] = False
            await QQClient(settings, client, store=store).reply(msg, text, job_id=job["id"])
            assert [x["msg_seq"] for x in sent] == [1, 2]
            assert store.delivery(job["id"])["delivered"] == 2
            store.close()

    asyncio.run(scenario())


def test_expired_long_task_snapshots_files_and_result_delivers_them(settings):
    async def scenario():
        file = settings.workspace / "report.txt"
        file.write_text("original result")

        class FakeCodex:
            async def run(self, prompt, thread_id, on_thread):
                on_thread("thread")
                return f"[报告](<{file}>)"

        uploads, replies = [], []

        def handle(request):
            if request.url.path.endswith("getAppAccessToken"):
                return httpx.Response(200, json={"access_token": "token", "expires_in": 7200})
            payload = json.loads(request.content)
            if request.url.path.endswith("/files"):
                uploads.append(base64.b64decode(payload["file_data"]))
                return httpx.Response(200, json={"file_info": "info", "ttl": 3600})
            replies.append(payload)
            return httpx.Response(200, json={"id": "reply"})

        store = Store(settings.data_dir / "db.sqlite3")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            qq = QQClient(settings, client, store=store)
            worker = Worker(store, FakeCodex(), qq)
            old = message(sent_at=time.time() - 1000)
            store.enqueue(old)
            await worker.process(store.claim())
            assert not replies and store.latest_result(old.chat_key)["delivery_plan"]
            file.write_text("overwritten by later turn")
            query = message("/result", msg_id="result-msg")
            store.enqueue(query)
            await worker.process(store.claim(control_only=True))
            assert uploads == [b"original result"]
            assert all(x["msg_id"] == "result-msg" for x in replies)
            assert replies[-1]["msg_type"] == 7
        store.close()

    asyncio.run(scenario())


def test_large_file_chunked_upload_does_not_leak_auth_to_storage(settings):
    async def scenario():
        data = b"x" * (8 * 1024 * 1024 + 1)
        file = settings.workspace / "large.zip"
        file.write_bytes(data)
        chunks = []
        finished = []
        completed = []

        def handle(request):
            if request.url.host == "storage.example":
                assert "authorization" not in request.headers
                chunks.append(request.content)
                return httpx.Response(200)
            if request.url.path.endswith("getAppAccessToken"):
                return httpx.Response(200, json={"access_token": "token", "expires_in": 7200})
            payload = json.loads(request.content)
            if request.url.path.endswith("/upload_prepare"):
                assert payload["file_type"] == 4 and payload["file_size"] == len(data)
                assert payload["md5"] == hashlib.md5(data).hexdigest()
                return httpx.Response(200, json={"upload_id": "upload", "block_size": 8 * 1024 * 1024,
                    "parts": [{"index": 1, "presigned_url": "https://storage.example/one"},
                              {"index": 2, "presigned_url": "https://storage.example/two"}]})
            if request.url.path.endswith("/upload_part_finish"):
                finished.append(payload)
                return httpx.Response(200, json={})
            if request.url.path.endswith("/files"):
                completed.append(payload)
                return httpx.Response(200, json={"file_info": "large-info", "ttl": 3600})
            return httpx.Response(200, json={"id": "reply"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            await QQClient(settings, client).reply(message(), f"[压缩包](<{file}>)")
        assert b"".join(chunks) == data
        assert [x["part_index"] for x in finished] == [1, 2]
        assert completed == [{"upload_id": "upload"}]

    asyncio.run(scenario())


def test_platform_file_rejection_is_visible_to_user(settings):
    async def scenario():
        file = settings.workspace / "report.pdf"
        file.write_bytes(b"%PDF-1.7 test")
        replies = []

        def handle(request):
            if request.url.path.endswith("getAppAccessToken"):
                return httpx.Response(200, json={"access_token": "token", "expires_in": 7200})
            payload = json.loads(request.content)
            if request.url.path.endswith("/files"):
                return httpx.Response(200, json={"biz_code": 11253})
            replies.append(payload)
            return httpx.Response(200, json={"id": "reply"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            await QQClient(settings, client).reply(message(kind="group"), f"[报告](<{file}>)")
        assert replies[-1]["msg_type"] == 0
        assert "11253" in replies[-1]["content"] and "发送失败" in replies[-1]["content"]

    asyncio.run(scenario())
