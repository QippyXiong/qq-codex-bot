import asyncio
import base64
import json
import time
from dataclasses import replace
from datetime import datetime, timezone

import httpx
import pytest

from qq_codex_bot import incoming
from qq_codex_bot.codex import Codex
from qq_codex_bot.config import Settings
from qq_codex_bot.incoming import ImageInputError, image_url, receive_images
from qq_codex_bot.ingress import accept_event
from qq_codex_bot.messages import Message
from qq_codex_bot.qq import QQClient
from qq_codex_bot.store import Store
from qq_codex_bot.worker import Worker

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jWZkAAAAASUVORK5CYII="
)
IMAGE_URL = "https://multimedia.nt.qq.com.cn/download?appid=app&token=private-query"


@pytest.fixture
def settings(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return Settings("app", "secret", data_dir=tmp_path, workspace=workspace)


def message(text="", *, images=(IMAGE_URL,), kind="group", message_id="m1"):
    return Message("app", kind, "target", "user", message_id, text, time.time(),
                   mentioned=True, images=images)


def test_parser_preserves_images_in_durable_payload_and_supports_old_messages():
    event = {"op": 0, "t": "GROUP_AT_MESSAGE_CREATE", "d": {
        "id": "image-message", "group_openid": "target", "content": "",
        "author": {"member_openid": "user"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "attachments": [
            {"content_type": "image/jpeg", "url": IMAGE_URL},
            {"content_type": "audio/ogg", "url": "https://example.com/audio"},
            None, {"content_type": "image/png", "url": None},
        ],
    }}
    parsed = Message.from_event("app", event)
    assert parsed.images == (IMAGE_URL,) and parsed.text == ""
    assert Message(**json.loads(json.dumps(parsed.as_dict()))) == parsed
    old = parsed.as_dict()
    old.pop("images")
    assert Message(**old).images == ()


@pytest.mark.parametrize("kind", ["group", "c2c"])
def test_image_only_text_with_images_and_new_session_reach_model(settings, kind):
    async def scenario():
        calls, replies = [], []

        class VisionCodex:
            async def run(self, prompt, thread_id, on_thread, *, images=()):
                assert images and all(path.read_bytes() == PNG for path in images)
                assert all(path.is_relative_to(settings.workspace) for path in images)
                calls.append((prompt, thread_id, images))
                on_thread(f"vision-{len(calls)}")
                return "图片已读取"

        def handle(request):
            if request.method == "GET":
                assert request.url.host == "multimedia.nt.qq.com.cn"
                assert "authorization" not in request.headers and "cookie" not in request.headers
                return httpx.Response(200, content=PNG)
            if request.url.path.endswith("getAppAccessToken"):
                return httpx.Response(200, json={"access_token": "token", "expires_in": 7200})
            replies.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "reply"})

        store = Store(settings.data_dir / "images.sqlite3")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            worker = Worker(store, VisionCodex(), QQClient(settings, client, store=store))
            for index, text in enumerate(("", "比较一下这张图", "/new")):
                packet = {"op": 0, "t": "GROUP_AT_MESSAGE_CREATE" if kind == "group" else
                          "C2C_MESSAGE_CREATE", "d": {
                    "id": str(index), "group_openid": "target", "content": text,
                    "author": {"member_openid": "user", "user_openid": "target"},
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "attachments": [{"content_type": "image/png", "url": IMAGE_URL}],
                }}
                accept_event(settings, store, packet)
                await worker.process(store.claim())
        assert [call[1] for call in calls] == [None, "vision-1", None]
        assert "附带的图片" in calls[0][0] and "比较一下这张图" in calls[1][0]
        assert "附带的图片" in calls[2][0] and "新会话已创建" not in calls[2][0]
        assert len(replies) == 3
        assert len(list(settings.workspace.rglob("image-*.png"))) == 3
        store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("thread_id", [None, "saved-thread"])
def test_cli_receives_image_files_for_new_and_resumed_turns(settings, thread_id):
    image = settings.workspace / "incoming.png"
    image.write_bytes(PNG)
    record = settings.data_dir / "cli-args.json"
    script = settings.data_dir / "fake_codex.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\nfrom pathlib import Path\n"
        f"Path({str(record)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        "assert sys.argv[-2:] == ['--', '-']\n"
        f"assert Path(sys.argv[sys.argv.index('--image') + 1]).read_bytes() == {PNG!r}\n"
        "assert '看图' in sys.stdin.read()\n"
        "print(json.dumps({'type': 'thread.started', 'thread_id': 'image-thread'}))\n"
        "print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '图片已读取'}}))\n"
        "print(json.dumps({'type': 'turn.completed'}))\n"
    )
    script.chmod(0o755)
    codex = Codex(replace(settings, codex_bin=str(script)))
    assert asyncio.run(codex.run("看图", thread_id, lambda _: None, images=(image,))) == "图片已读取"
    args = json.loads(record.read_text())
    assert ("resume" in args) == bool(thread_id)
    if thread_id:
        assert args[args.index("resume") + 1] == thread_id
        assert args.index("--image") > args.index("resume")


@pytest.mark.parametrize("raw", [
    "http://127.0.0.1/internal", "file:///etc/passwd", "https://qq.com.evil.test/image",
    "https://user:secret@qpic.cn/image", "https://qpic.cn:8080/image",
])
def test_untrusted_image_urls_are_rejected(raw):
    with pytest.raises(ImageInputError):
        image_url(raw)


def test_qq_urls_without_scheme_are_upgraded_to_https():
    assert image_url("//gchat.qpic.cn/image") == "https://gchat.qpic.cn/image"
    assert image_url("multimedia.nt.qq.com.cn/download?token=x") == (
        "https://multimedia.nt.qq.com.cn/download?token=x"
    )


def test_download_redirects_are_checked_and_credentials_are_not_forwarded(settings):
    async def scenario():
        requests = []

        def handle(request):
            requests.append(request)
            assert "authorization" not in request.headers and "cookie" not in request.headers
            if request.url.host == "multimedia.nt.qq.com.cn":
                return httpx.Response(302, headers={"location": "https://gchat.qpic.cn/image"})
            return httpx.Response(200, content=PNG)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle),
                                     headers={"Authorization": "QQBot private", "Cookie": "secret=x"}) as client:
            paths = await receive_images(settings, client, message())
        assert len(requests) == 2 and paths[0].read_bytes() == PNG

    asyncio.run(scenario())


@pytest.mark.parametrize("response", [
    httpx.Response(404),
    httpx.Response(200, content=b"<html>not an image</html>"),
    httpx.Response(200, headers={"content-length": str(incoming.MAX_IMAGE_BYTES + 1)}),
    httpx.Response(302, headers={"location": "http://127.0.0.1/private"}),
])
def test_bad_images_do_not_call_model_or_disclose_signed_url(settings, response):
    async def scenario():
        class UnusedCodex:
            async def run(self, *args, **kwargs):
                pytest.fail("Unreadable images must not invoke the model")

        store = Store(settings.data_dir / "bad-image.sqlite3")
        msg = message()
        store.enqueue(msg)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
            worker = Worker(store, UnusedCodex(), QQClient(settings, client, store=store))
            answer = await worker.answer(store.claim(), msg)
        assert "图片" in answer and "private-query" not in answer and IMAGE_URL not in answer
        assert not list(settings.workspace.rglob("image-*"))
        store.close()

    asyncio.run(scenario())


def test_stream_size_limit_and_failed_batch_cleanup(settings, monkeypatch):
    class OversizedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield PNG
            yield PNG

    async def scenario():
        calls = []

        def handle(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(200, content=PNG)
            return httpx.Response(200, stream=OversizedStream())

        monkeypatch.setattr(incoming, "MAX_IMAGE_BYTES", len(PNG) + 1)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            # Exercise a streaming response without Content-Length.
            with pytest.raises(ImageInputError):
                await receive_images(settings, client, message(images=(IMAGE_URL, IMAGE_URL)))
        assert not list(settings.workspace.rglob("image-*"))
        assert len(calls) == 2

    asyncio.run(scenario())


def test_disabled_group_does_not_download_images(settings):
    async def scenario():
        class UnusedCodex:
            async def run(self, *args, **kwargs):
                pytest.fail("A disabled unmentioned image must not invoke the model")

        def unexpected_request(_):
            pytest.fail("A disabled unmentioned image must not be downloaded or replied to")

        store = Store(settings.data_dir / "disabled.sqlite3")
        msg = replace(message(), mentioned=False)
        store.set_all_message(msg.chat_key, False)
        store.enqueue(msg)
        async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request)) as client:
            await Worker(store, UnusedCodex(), QQClient(settings, client, store=store)).process(store.claim())
        assert store.db.execute("SELECT status FROM jobs").fetchone()[0] == "done"
        store.close()

    asyncio.run(scenario())


def test_image_count_limit_does_not_make_requests(settings):
    async def scenario():
        def unexpected_request(_):
            pytest.fail("Too many images must be rejected before any downloads")

        async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request)) as client:
            with pytest.raises(ImageInputError, match="最多读取 3 张"):
                await receive_images(settings, client, message(images=(IMAGE_URL,) * 4))

    asyncio.run(scenario())
