import asyncio
import base64
import io
import json
from datetime import datetime, timezone

import httpx
import pytest
from PIL import Image

from qq_codex_bot import incoming, ingress
from qq_codex_bot.config import Settings
from qq_codex_bot.incoming import ImageInputError, model_image, receive_images
from qq_codex_bot.messages import Message
from qq_codex_bot.qq import QQClient
from qq_codex_bot.store import Store
from qq_codex_bot.worker import Worker

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jWZkAAAAASUVORK5CYII="
)
URL = "https://gchat.qpic.cn/image.png?token=never-log-this"


def packet(text="", *, sender="user", group="group", bot=False, attachments=None, id="msg"):
    return {"op": 0, "t": "GROUP_MESSAGE_CREATE", "d": {
        "id": id, "group_openid": group, "content": text,
        "author": {"member_openid": sender, "bot": bot},
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "attachments": attachments or [],
    }}


def image_attachment(**kwargs):
    return {"content_type": "image/png", "url": URL, **kwargs}


@pytest.fixture
def setup(tmp_path):
    s = Settings("app", "secret", data_dir=tmp_path, workspace=tmp_path / "workspace")
    s.workspace.mkdir()
    store = Store(tmp_path / "sessions.sqlite3")
    store.set_all_message("app:group:group", False)
    yield s, store
    store.close()


@pytest.mark.parametrize("attachment", [
    {"url": URL, "filename": "cat.PNG"},
    {"url": URL},
    {"url": URL, "content_type": "application/octet-stream", "file_name": "cat.gif"},
    {"url": URL, "content_type": " IMAGE/PNG ; charset=binary"},
])
def test_image_file_without_standard_mime_is_preserved(attachment):
    message = Message.from_event("app", packet(attachments=[attachment]))
    assert message.images == (URL,)


def test_non_image_file_and_duplicate_url_do_not_become_extra_images():
    m = Message.from_event("app", packet(attachments=[
        image_attachment(), image_attachment(),
        {"url": URL, "filename": "cat.png", "content_type": "audio/ogg"},
    ]))
    assert m.images == (URL,)


def test_missing_url_and_face_placeholder_are_explicit_and_durable():
    m = Message.from_event("app", packet('Astra <faceType=6,faceId="0">', attachments=[
        {"content_type": "image/png"},
    ]))
    assert not m.images
    assert any("占位" in notice for notice in m.image_notices)
    assert any("缺少" in notice for notice in m.image_notices)
    assert Message(**json.loads(json.dumps(m.as_dict()))) == m
    old = m.as_dict()
    old.pop("image_notices")
    assert Message(**old).image_notices == ()


def test_cached_image_reaches_next_named_turn_and_control_does_not_consume_it(setup):
    s, store = setup
    ingress.accept_event(s, store, packet(attachments=[image_attachment()], id="image"))
    assert store.claim() is None
    ingress.accept_event(s, store, packet("/status", id="status"))
    assert store.claim(control_only=True) is not None
    ingress.accept_event(s, store, packet("Astra 看上面的图", id="wake"))
    m = Message(**json.loads(store.claim()["payload"]))
    assert m.images == (URL,)
    assert any("附图 1 张" in line for line in m.context)


def test_recent_image_expiry_and_current_image_priority(setup, monkeypatch):
    s, store = setup
    monkeypatch.setattr(ingress.time, "time", lambda: 1000)
    for i in range(5):
        ingress.accept_event(s, store, packet(id=str(i), attachments=[
            image_attachment(url=f"https://gchat.qpic.cn/{i}.png"),
        ]))
    ingress.accept_event(s, store, packet("Astra", id="wake", attachments=[image_attachment()]))
    job = store.claim()
    m = Message(**json.loads(job["payload"]))
    assert m.images == (URL, "https://gchat.qpic.cn/3.png", "https://gchat.qpic.cn/4.png")
    store.finish(job["id"])
    ingress._remember(m.chat_key, "old", "user", images=(URL,))
    monkeypatch.setattr(ingress.time, "time", lambda: 1000 + ingress.RECENT_TTL + 1)
    ingress.accept_event(s, store, packet("Astra", id="later"))
    m = Message(**json.loads(store.claim()["payload"]))
    assert not m.images and not m.context


def test_bot_split_image_handoff_is_one_shot_sender_and_chat_scoped(setup):
    s, store = setup
    ingress.accept_event(s, store, packet("Astra 看图", bot=True, sender="phoebe", id="call"))
    store.finish(store.claim()["id"])
    ingress.accept_event(s, store, packet(bot=True, sender="other", id="other",
                                         attachments=[image_attachment()]))
    ingress.accept_event(s, store, packet(bot=True, sender="phoebe", group="elsewhere", id="else",
                                         attachments=[image_attachment()]))
    assert store.claim() is None
    ingress.accept_event(s, store, packet(bot=True, sender="phoebe", id="followup",
                                         attachments=[image_attachment()]))
    job = store.claim()
    m = Message(**json.loads(job["payload"]))
    assert m.mentioned and m.images == (URL,)
    store.finish(job["id"])
    ingress.accept_event(s, store, packet(bot=True, sender="phoebe", id="again",
                                         attachments=[image_attachment()]))
    assert store.claim() is None


def test_handoff_expires_and_still_respects_allowlist(setup, monkeypatch):
    from dataclasses import replace
    s, store = setup
    monkeypatch.setattr(ingress.time, "monotonic", lambda: 10)
    ingress.accept_event(s, store, packet("Astra", bot=True, id="call"))
    store.claim()
    monkeypatch.setattr(ingress.time, "monotonic", lambda: 71)
    ingress.accept_event(s, store, packet(bot=True, id="expired", attachments=[image_attachment()]))
    assert store.claim() is None
    ingress.accept_event(replace(s, allowed_users=frozenset({"allowed"})), store,
                         packet("Astra", bot=True, id="denied"))
    assert not ingress._IMAGE_FOLLOWUPS


def gif_bytes():
    output = io.BytesIO()
    with Image.new("RGB", (2, 2), "red") as first, Image.new("RGB", (2, 2), "blue") as second:
        first.save(output, format="GIF", save_all=True, append_images=[second], duration=50)
    return output.getvalue()


def test_gif_converts_first_frame_and_rejects_oversize_or_corrupt_data(monkeypatch):
    data, suffix = model_image(gif_bytes())
    assert suffix == ".png"
    with Image.open(io.BytesIO(data)) as result:
        assert result.getpixel((0, 0)) == (255, 0, 0, 255)
    with pytest.raises(ImageInputError):
        model_image(b"GIF89a invalid bytes")
    monkeypatch.setattr(incoming, "MAX_GIF_PIXELS", 1)
    with pytest.raises(ImageInputError, match="尺寸过大"):
        model_image(gif_bytes())


def test_cached_gif_end_to_end_reaches_model_with_real_path(setup, caplog):
    s, store = setup
    ingress.accept_event(s, store, packet(attachments=[image_attachment(content_type="image/gif")],
                                         id="gif"))
    ingress.accept_event(s, store, packet("Astra 是什么图", id="wake"))
    calls = []

    class Vision:
        async def run(self, prompt, thread_id, on_thread, *, images=()):
            assert "GIF 只转换首帧" in prompt
            assert len(images) == 1 and str(images[0]) in prompt
            with Image.open(images[0]) as result:
                assert result.format == "PNG"
                assert result.getpixel((0, 0)) == (255, 0, 0, 255)
            calls.append(images)
            return "识别成功"

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=gif_bytes())
        )) as client:
            worker = Worker(store, Vision(), QQClient(s, client, store=store))
            job = store.claim()
            assert await worker.answer(job, Message(**json.loads(job["payload"]))) == "识别成功"
    asyncio.run(scenario())
    assert len(calls) == 1
    assert "never-log-this" not in caplog.text


def test_placeholder_notice_reaches_model_without_download(setup):
    s, store = setup
    ingress.accept_event(s, store, packet('Astra <faceType=6,faceId="0">'))

    class TextOnly:
        async def run(self, prompt, thread_id, on_thread):
            assert "占位信息" in prompt and "不要据标签猜画面" in prompt
            return "只收到表情占位"

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: pytest.fail("No URL means no image request")
        )) as client:
            worker = Worker(store, TextOnly(), QQClient(s, client, store=store))
            job = store.claim()
            return await worker.answer(job, Message(**json.loads(job["payload"])))
    assert asyncio.run(scenario()) == "只收到表情占位"


def test_self_bot_does_not_trigger_image_handoff(setup):
    s, store = setup
    ingress.accept_event(s, store, packet("Astra", bot=True, sender="self"),
                         self_ids=frozenset({"self"}))
    assert store.claim() is None and not ingress._IMAGE_FOLLOWUPS


def test_failed_gif_cleanup(setup):
    s, store = setup
    m = Message.from_event("app", packet(attachments=[image_attachment()]))
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=b"GIF89a broken")
        )) as client:
            with pytest.raises(ImageInputError):
                await receive_images(s, client, m)
    asyncio.run(scenario())
    assert not list(s.workspace.rglob("image-*"))
