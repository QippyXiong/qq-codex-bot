"""Convert final Markdown links into immutable, bounded outbound attachments."""

import hashlib
import os
import re
import stat
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .config import Settings

LINK = re.compile(r'(!?)\[([^\]\n]*)\]\((<[^>\n]+>|[^)\n]+)\)')
CODE = re.compile(r"```[\s\S]*?(?:```|$)|~~~[\s\S]*?(?:~~~|$)|`[^`\n]*`")

def clipped(text: str, limit: int = 12000) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit and len(text) <= 4000:
        return text
    return text[:3900].encode("utf-8")[:limit - 100].decode("utf-8", errors="ignore") + "\n（回复过长，已截断）"


def snapshot(raw_path: str, settings: Settings, directory: Path) -> dict:
    path = Path(raw_path)
    if not path.is_absolute():
        path = settings.workspace / path
    path = path.resolve()
    root = settings.workspace.resolve()
    if not path.is_relative_to(root):
        raise ValueError("附件不在 Codex 工作目录内")
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("附件必须是普通文件")
        if info.st_size == 0:
            raise ValueError("附件为空")
        if info.st_size > settings.max_attachment_bytes:
            raise ValueError(f"附件超过 {settings.max_attachment_bytes // (1024 * 1024)} MiB 限制")
        name = re.sub(r"[\x00-\x1f\x7f]", "_", path.name)[:200] or "attachment"
        target_dir = directory / uuid.uuid4().hex
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / name
        digest = hashlib.sha256()
        size = 0
        header = b""
        try:
            with target.open("xb") as output:
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    if size > settings.max_attachment_bytes:
                        raise ValueError("附件在复制过程中超出大小限制")
                    if not header:
                        header = chunk[:16]
                    digest.update(chunk)
                    output.write(chunk)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
    # Verify image signatures rather than silently calling arbitrary bytes an image.
    image = header.startswith(b"\x89PNG\r\n\x1a\n") or header.startswith(b"\xff\xd8\xff")
    return {"kind": "media", "path": str(target), "name": name,
            "file_type": 1 if image else 4, "size": size, "sha256": digest.hexdigest()}


def build_plan(text: str, settings: Settings, directory: Path) -> list[dict]:
    if len(text.strip()) < 3:
        return []
    protected = [(m.start(), m.end()) for m in CODE.finditer(text)]
    attachments, notices = [], []
    seen = set()

    def replace(match):
        if any(start <= match.start() < end for start, end in protected):
            return match.group(0)
        image, label, target = match.groups()
        target = target.strip().removeprefix("<").removesuffix(">")
        try:
            parsed = urlsplit(target)
        except ValueError:
            return match.group(0)
        if parsed.scheme in {"https", "http"}:
            if not image:
                return match.group(0)
            attachment = {"kind": "media", "url": target, "file_type": 1, "name": label or "图片"}
            key = target
        elif parsed.scheme in {"", "file", "sandbox"}:
            if parsed.netloc:
                notices.append("未发送附件：不支持远程文件路径")
                return label or "附件"
            path = unquote(parsed.path)
            if path.startswith("#") or not path:
                return match.group(0)
            # Leave documentation/anchor links intact unless they name an actual file.
            resolved = Path(path) if Path(path).is_absolute() else settings.workspace / path
            if not image and not resolved.is_file() and not Path(path).is_absolute():
                return match.group(0)
            key = str(resolved.resolve())
            if key in seen:
                return label or "附件"
            if len(attachments) >= 3:
                notices.append("本次超过 3 个附件，请让 Codex 将更多文件打包为 ZIP 后发送。")
                return label or "附件"
            try:
                attachment = snapshot(path, settings, directory)
            except (OSError, ValueError) as exc:
                reason = str(exc) if isinstance(exc, ValueError) else "文件不存在或无法读取"
                notices.append(f"未发送附件 {label or '文件'}：{reason}。")
                return label or "附件"
        else:
            return match.group(0)
        if key not in seen:
            if len(attachments) >= 3:
                notices.append("本次超过 3 个附件，请让 Codex 打包后发送。")
            else:
                attachments.append(attachment)
                seen.add(key)
        return f"{label or attachment['name']}（见附件）"

    rendered = LINK.sub(replace, text).strip()
    if notices:
        rendered += "\n\n" + "\n".join(dict.fromkeys(notices))
    plan = []
    if rendered:
        plan.append({"kind": "text", "text": clipped(rendered), "format": settings.message_format})
    plan.extend(attachments)
    return plan
