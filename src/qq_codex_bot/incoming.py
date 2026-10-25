"""Download bounded QQ image attachments into the persistent Codex workspace."""

import asyncio
import contextlib
import io
import uuid
import warnings
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from PIL import Image, UnidentifiedImageError

from .config import Settings
from .messages import Message

MAX_IMAGES = 3
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_GIF_PIXELS = 10_000_000
QQ_IMAGE_DOMAINS = ("qq.com", "qq.com.cn", "qpic.cn", "qlogo.cn", "gtimg.cn", "gtimg.com",
                    "idqqimg.com")


class ImageInputError(ValueError):
    pass


def image_url(raw: str) -> str:
    if raw.startswith("//"):
        raw = "https:" + raw
    elif "://" not in raw:
        raw = "https://" + raw
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").lower()
        valid_host = any(host == domain or host.endswith("." + domain)
                         for domain in QQ_IMAGE_DOMAINS)
        if (parsed.scheme not in {"http", "https"} or not valid_host
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in {None, 443}):
            raise ValueError
    except ValueError:
        raise ImageInputError("图片地址不是受支持的 QQ 附件地址，请直接重新上传图片。") from None
    return urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))


def image_suffix(data: bytes | bytearray) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    raise ImageInputError("图片格式无法读取，请使用 PNG、JPEG、WebP 或 GIF 图片。")


def model_image(data: bytes | bytearray) -> tuple[bytes | bytearray, str]:
    suffix = image_suffix(data)
    if suffix != ".gif":
        return data, suffix
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                if source.format != "GIF" or source.width * source.height > MAX_GIF_PIXELS:
                    raise ImageInputError("GIF 首帧尺寸过大，请缩小后重新发送。")
                source.seek(0)
                with source.convert("RGBA") as frame:
                    output = io.BytesIO()
                    frame.save(output, format="PNG")
                    result = output.getvalue()
                    if len(result) > MAX_IMAGE_BYTES:
                        raise ImageInputError("GIF 首帧转换后过大，请缩小后重新发送。")
                    return result, ".png"
    except ImageInputError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, EOFError,
            Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise ImageInputError("GIF 首帧无法解码，请重新上传图片。") from None


async def download_image(client: httpx.AsyncClient, raw_url: str) -> bytearray:
    url = image_url(raw_url)
    for _ in range(4):
        # No QQBot authorization or cookies are attached to CDN downloads.
        request = client.build_request("GET", url, timeout=20)
        request.headers.pop("Authorization", None)
        request.headers.pop("Cookie", None)
        response = await client.send(request, auth=None, stream=True, follow_redirects=False)
        try:
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    break
                url = image_url(urljoin(url, location))
                continue
            response.raise_for_status()
            length = response.headers.get("content-length")
            if length and int(length) > MAX_IMAGE_BYTES:
                raise ImageInputError("单张图片不能超过 20 MiB，请压缩后重新发送。")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > MAX_IMAGE_BYTES:
                    raise ImageInputError("单张图片不能超过 20 MiB，请压缩后重新发送。")
                data.extend(chunk)
            return data
        finally:
            await response.aclose()
    raise ImageInputError("图片下载重定向异常，请重新上传图片。")


async def receive_images(settings: Settings, client: httpx.AsyncClient,
                         message: Message) -> tuple[Path, ...]:
    if not message.images:
        return ()
    if len(message.images) > MAX_IMAGES:
        raise ImageInputError("一次最多读取 3 张图片，请分开发送。")
    for url in message.images:
        image_url(url)
    root = settings.workspace.resolve()
    directory = (root / ".qq-inbox" / uuid.uuid4().hex).resolve()
    if not directory.is_relative_to(root):
        raise ImageInputError("图片保存目录不可用，请检查工作目录后重试。")
    paths = []
    succeeded = False
    try:
        directory.mkdir(parents=True, exist_ok=False)
        for url in message.images:
            data = await asyncio.wait_for(download_image(client, url), timeout=30)
            data, suffix = await asyncio.to_thread(model_image, data)
            path = directory / f"image-{len(paths) + 1}{suffix}"
            paths.append(path)
            await asyncio.to_thread(path.write_bytes, data)
        succeeded = True
        return tuple(paths)
    except ImageInputError:
        raise
    except (httpx.HTTPError, OSError, ValueError, TimeoutError):
        # Signed attachment URLs and their query parameters must not reach replies or logs.
        raise ImageInputError("图片下载失败或已过期，请直接重新上传图片。") from None
    finally:
        if not succeeded:
            for path in paths:
                with contextlib.suppress(OSError):
                    path.unlink()
            with contextlib.suppress(OSError):
                directory.rmdir()
