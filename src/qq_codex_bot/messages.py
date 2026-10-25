import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from urllib.parse import urlsplit

EVENTS = {"C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"}
MENTION = re.compile(r"<@!?([^>\s]+)>")
LEADING_MENTIONS = re.compile(r"^(?:<@!?[^>\s]+>\s*)+")
LOCAL_COMMANDS = {"/new", "/reset", "/session", "/status", "/result", "/help", "/all_message"}
FACE_MARKUP = re.compile(r"<faceType\s*=.*?>", re.IGNORECASE)
IMAGE_EXTENSION = re.compile(r"\.(?:png|jpe?g|webp|gif)$", re.IGNORECASE)


def attachment_images(data: dict) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """MIME may be missing on file uploads; the downloader still verifies the bytes."""
    images, notices = [], []
    attachments = data.get("attachments")
    for item in attachments if isinstance(attachments, list) else ():
        if not isinstance(item, dict):
            continue
        mime = str(item.get("content_type") or "").split(";", 1)[0].strip().lower()
        url = item.get("url")
        filename = str(item.get("filename") or item.get("file_name") or "")
        try:
            url_path = urlsplit(url).path if isinstance(url, str) else ""
        except ValueError:
            url_path = ""
        fallback = mime in {"", "application/octet-stream", "binary/octet-stream"}
        is_image = mime.startswith("image/") or (fallback and (
            IMAGE_EXTENSION.search(filename) or IMAGE_EXTENSION.search(url_path)
        ))
        if not is_image:
            continue
        if isinstance(url, str) and url.strip():
            if url.strip() not in images:
                images.append(url.strip())
        else:
            notices.append("QQ 图片附件缺少可下载地址，本条未能取得该图片。")
    text = data.get("content", "")
    if isinstance(text, str) and FACE_MARKUP.search(text):
        notices.append("QQ 表情标签只提供了占位信息，没有对应图像；不要据标签猜画面。")
    return tuple(images), tuple(dict.fromkeys(notices))


def is_all_message_command(text: str) -> bool:
    return text.split(maxsplit=1)[:1] == ["/all_message"]


@dataclass(frozen=True)
class Message:
    app_id: str
    kind: str
    target: str
    sender: str
    message_id: str
    text: str
    sent_at: float
    mentioned: bool = False
    images: tuple[str, ...] = ()
    context: tuple[str, ...] = ()
    image_notices: tuple[str, ...] = ()

    def __post_init__(self):
        # SQLite payloads deserialize JSON arrays as lists, including after a restart.
        object.__setattr__(self, "images", tuple(self.images))
        object.__setattr__(self, "context", tuple(self.context or ()))
        object.__setattr__(self, "image_notices", tuple(self.image_notices or ()))

    @property
    def chat_key(self) -> str:
        return f"{self.app_id}:{self.kind}:{self.target}"

    @property
    def dedup_key(self) -> str:
        return f"{self.chat_key}:{self.message_id}"

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_event(cls, app_id: str, event: dict, *,
                   self_ids: frozenset[str] = frozenset(),
                   allow_bot_images: bool = False) -> "Message | None":
        if event.get("op") != 0 or event.get("t") not in EVENTS:
            return None
        data = event.get("d")
        if not isinstance(data, dict):
            raise ValueError("d must be an object")
        author = data.get("author")
        if not isinstance(author, dict):
            raise ValueError("author must be an object")
        if author.get("bot"):
            # 机器人之间默认可互刷，但"带名字叫它"的消息要放行，
            # 否则白饭/菲比发的数据交接它永远收不到。
            raw_text = data.get("content", "")
            names = [n.strip() for n in os.getenv("QQ_BOT_NICKNAMES", "astra,astra大人").split(",") if n.strip()]
            called = isinstance(raw_text, str) and any(
                n.lower() in MENTION.sub("", raw_text).lower() for n in names
            )
            if not called and not (allow_bot_images and attachment_images(data)[0]):
                return None
        group = event["t"] != "C2C_MESSAGE_CREATE"
        sender = author.get("member_openid" if group else "user_openid") or author.get("id")
        target = data.get("group_openid") if group else sender
        msg_id = data.get("id")
        if not all(isinstance(x, str) and x for x in (sender, target, msg_id)):
            raise ValueError("missing message/user/group ID")
        if author.get("bot") and sender in self_ids:
            return None
        text = data.get("content", "")
        if not isinstance(text, str):
            raise ValueError("content must be a string")
        images, image_notices = attachment_images(data)
        mentions = data.get("mentions")
        has_mention_markup = bool(MENTION.search(text)) or (
            isinstance(mentions, list) and any(isinstance(item, dict) for item in mentions)
        )
        mentioned = group and (
            event["t"] == "GROUP_AT_MESSAGE_CREATE"
            or any(match.group(1) in self_ids for match in MENTION.finditer(text))
            or (isinstance(mentions, list) and any(
                isinstance(item, dict) and any(
                    str(item.get(field, "")) in self_ids
                    for field in ("id", "openid", "member_openid", "union_openid")
                ) for item in mentions
            ))
            # 兜底：拿不到自身 id（READY 未带 user、也没配 QQ_BOT_SELF_ID）时，
            # 只要消息里出现 @ 标记就可能是叫我，先收进来，最后由 all_message 开关裁决。
            or (not self_ids and has_mention_markup)
        )
        if mentioned:
            # Strip only our known identity; preserve mentions addressed to other members.
            text = MENTION.sub(
                lambda match: "" if match.group(1) in self_ids else match.group(0), text
            ).strip()
            if event["t"] == "GROUP_AT_MESSAGE_CREATE":
                # Some QQ deliveries retain a leading @ even though the event identifies us.
                candidate = LEADING_MENTIONS.sub("", text).strip()
                command = candidate.split(maxsplit=1)[:1]
                if command and command[0] in LOCAL_COMMANDS:
                    text = candidate
        timestamp = data.get("timestamp")
        if not isinstance(timestamp, str):
            raise ValueError("missing message timestamp")
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("timestamp must include timezone")
        return cls(app_id, "group" if group else "c2c", target, sender, msg_id,
                   text.strip(), dt.timestamp(), mentioned, images,
                   image_notices=image_notices)
