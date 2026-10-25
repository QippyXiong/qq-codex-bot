"""Share validation, allowlists and durable enqueueing across event transports."""

import logging
import os
import time
from dataclasses import replace

from .config import Settings
from .incoming import MAX_IMAGES
from .messages import MENTION, Message
from .store import Store

log = logging.getLogger(__name__)


NICKNAMES_ENV = "QQ_BOT_NICKNAMES"


def _called_by_nickname(settings, text: str) -> bool:
    """手打 @ 不会转成真 @，QQ 只会下发纯文本；这里用昵称兜底。"""
    raw = os.getenv(NICKNAMES_ENV, "astra,astra大人")
    names = [n.strip() for n in raw.split(",") if n.strip()]
    bare = MENTION.sub("", text).lower()
    return any(n.lower() in bare for n in names)


RECENT_LIMIT = 20
RECENT_TTL = 1800.0
_RECENT: dict[str, list] = {}
IMAGE_FOLLOWUP_TTL = 60.0
_IMAGE_FOLLOWUPS: dict[tuple, float] = {}


def _remember(chat_key: str, text: str, sender: str, images=(), notices=()) -> None:
    """把没被唤醒的群聊记在本进程里，等有人叫它时一并带给模型。"""
    if not text.strip() and not images and not notices:
        return
    bucket = _RECENT.setdefault(chat_key, [])
    now = time.time()
    summary = text.strip()[:300]
    if images:
        summary += f" [附图 {len(images)} 张]"
    if notices:
        summary += " [图片提示：" + "；".join(notices) + "]"
    bucket.append((now, sender[-6:], summary, tuple(images)))
    cutoff = now - RECENT_TTL
    while bucket and (len(bucket) > RECENT_LIMIT or bucket[0][0] < cutoff):
        bucket.pop(0)


def _drain(chat_key: str, current_images=()) -> tuple[tuple[str, ...], tuple[str, ...]]:
    cutoff = time.time() - RECENT_TTL
    bucket = [entry for entry in _RECENT.pop(chat_key, []) if entry[0] >= cutoff]
    # Reserve slots for the current message, then take the newest cached images.
    slots = max(0, MAX_IMAGES - len(current_images))
    selected = []
    for _, _, _, images in reversed(bucket):
        for url in reversed(images):
            if url not in current_images and url not in selected and len(selected) < slots:
                selected.append(url)
    selected.reverse()
    history = []
    for _, who, what, images in bucket:
        if images:
            kept = sum(url in selected or url in current_images for url in images)
            what += f" [本轮实际附入 {kept} 张，其余未附入]"
        history.append(f"{who}: {what}")
    return tuple(history), tuple(selected)


def accept_event(settings: Settings, store: Store, event: dict, *,
                 self_ids: frozenset[str] = frozenset()):
    if settings.self_id:
        self_ids = self_ids | {settings.self_id}
    data = event.get("d") if isinstance(event.get("d"), dict) else {}
    author = data.get("author") if isinstance(data.get("author"), dict) else {}
    group = event.get("t") != "C2C_MESSAGE_CREATE"
    sender = author.get("member_openid" if group else "user_openid") or author.get("id")
    target = data.get("group_openid") if group else sender
    handoff_key = (settings.app_id, "group" if group else "c2c", target, sender)
    now = time.monotonic()
    for key, expiry in list(_IMAGE_FOLLOWUPS.items()):
        if expiry <= now:
            _IMAGE_FOLLOWUPS.pop(key, None)
    followup = _IMAGE_FOLLOWUPS.get(handoff_key, 0) > now
    message = Message.from_event(settings.app_id, event, self_ids=self_ids,
                                 allow_bot_images=followup)
    if message is None:
        data = event.get("d") if isinstance(event.get("d"), dict) else {}
        author = data.get("author") if isinstance(data.get("author"), dict) else {}
        log.info(
            "QQ event skipped t=%s op=%s has_content=%s has_mention_markup=%s "
            "mentions=%s author_bot=%s self_ids=%s",
            event.get("t"), event.get("op"),
            isinstance(data.get("content"), str),
            "<@" in str(data.get("content", "")),
            len(data.get("mentions") or []), bool(author.get("bot")), len(self_ids),
        )
        return
    if message:
        allowed = (message.kind != "group" or not settings.allowed_groups
                   or message.target in settings.allowed_groups)
        allowed = allowed and (not settings.allowed_users or message.sender in settings.allowed_users)
        if allowed:
            called = message.mentioned or _called_by_nickname(settings, message.text)
            # A named image handoff may arrive as two messages. It is scoped to the
            # same sender/chat, expires quickly, and admits one image message only.
            if followup and message.images and not called:
                message = replace(message, mentioned=True)
                _IMAGE_FOLLOWUPS.pop(handoff_key, None)
                log.info("QQ image followup accepted images=%d", len(message.images))
            elif called and not message.images and not message.text.startswith("/"):
                _IMAGE_FOLLOWUPS[handoff_key] = now + IMAGE_FOLLOWUP_TTL
            elif message.images:
                _IMAGE_FOLLOWUPS.pop(handoff_key, None)
            # 指令（/status、/reset、/all_message…）本来就是冲机器人来的，
            # 不该因为没带 @ 标记被当成闲聊丢掉。
            if (message.kind == "group" and not message.mentioned
                    and not MENTION.sub("", message.text).strip().startswith("/")
                    and not store.all_message_enabled(message.chat_key)
                    and not _called_by_nickname(settings, message.text)):
                # 静默丢弃不可查，这里至少留痕：能看到是"没被 @ 且全量接收关闭"才丢的
                log.info("QQ group message dropped chat=%s reason=no_mention "
                         "all_message=off text_has_markup=%s text_len=%d",
                         message.chat_key, "<@" in message.text, len(message.text))
                _remember(message.chat_key, message.text, message.sender,
                          message.images, message.image_notices)
                return
            if message.kind == "group" and not message.text.startswith("/"):
                history, previous_images = _drain(message.chat_key, message.images)
                if history:
                    message = replace(message, context=history,
                                      images=message.images + previous_images)
            inserted = store.enqueue(message)
            attachments = data.get("attachments")
            attachment_count = len(attachments) if isinstance(attachments, list) else 0
            log.info("QQ message received kind=%s queued=%s context=%d "
                     "attachments=%d images=%d image_notices=%d",
                     message.kind, inserted, len(message.context), attachment_count,
                     len(message.images), len(message.image_notices))
