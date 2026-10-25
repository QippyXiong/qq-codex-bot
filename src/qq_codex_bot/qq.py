import asyncio
import base64
import hashlib
import json
import logging
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import httpx

from .config import Settings
from .messages import Message
from .outbound import build_plan, clipped

log = logging.getLogger(__name__)
MARKDOWN_UNAVAILABLE = {50056, 304036, 11253}


class QQError(RuntimeError):
    def __init__(self, status: int, code=0):
        self.status = status
        self.code = code
        super().__init__(f"QQ API HTTP={status} code={code}")

    @property
    def retryable(self):
        return self.status == 429 or self.status >= 500


class QQClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient, *, store=None):
        self.settings = settings
        self.client = client
        self.token = ""
        self.expires = 0.0
        self.lock = asyncio.Lock()
        self.store = store
        self.markdown_unavailable = set()

    @staticmethod
    def checked(response: httpx.Response) -> dict:
        try:
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError("not an object")
        except ValueError as exc:
            raise QQError(response.status_code, "invalid_json") from exc
        code = body.get("err_code") or body.get("code") or body.get("biz_code") or 0
        if not response.is_success or str(code) != "0":
            raise QQError(response.status_code, code)
        return body

    async def access_token(self) -> str:
        async with self.lock:
            if self.token and time.monotonic() < self.expires:
                return self.token
            response = await self.client.post(
                self.settings.api_base + "/app/getAppAccessToken",
                json={"appId": self.settings.app_id, "clientSecret": self.settings.app_secret},
            )
            body = self.checked(response)
            token = body.get("access_token")
            if not isinstance(token, str) or not token:
                raise QQError(response.status_code, "missing_access_token")
            self.token = token
            self.expires = time.monotonic() + max(0, int(body["expires_in"]) - 45)
            return self.token

    def message_path(self, message: Message) -> str:
        resource = "groups" if message.kind == "group" else "users"
        return f"/v2/{resource}/{quote(message.target, safe='')}"

    async def request(self, method: str, path: str, payload=None, *, timeout=15) -> dict:
        for attempt in range(2):
            token = await self.access_token()
            response = await self.client.request(
                method, self.settings.api_base + path,
                headers={"Authorization": f"QQBot {token}"}, json=payload, timeout=timeout,
            )
            if response.status_code == 401 and attempt == 0:
                self.token = ""
                continue
            return self.checked(response)
        raise QQError(401, "auth_failed")

    async def prepare_reply(self, message: Message, text: str, *, job_id: int | None = None):
        if not text.strip():
            return [], 0
        delivery = self.store.delivery(job_id) if self.store and job_id is not None else None
        delivered = delivery["delivered"] if delivery else 0
        if delivery and delivery["delivery_plan"]:
            plan = json.loads(delivery["delivery_plan"])
        else:
            previous = self.store.latest_result(message.chat_key) if self.store and message.text == "/result" else None
            if previous and previous["delivery_plan"]:
                # Re-deliver immutable snapshots, not files the next Codex turn may have changed.
                plan = json.loads(previous["delivery_plan"])
                if plan and plan[0]["kind"] == "text":
                    plan[0]["text"] = clipped(f"任务 #{previous['id']} 的结果：\n" + plan[0]["text"])
                for action in plan:
                    action.pop("delivery_error", None)
            else:
                directory = self.settings.data_dir / "artifacts" / str(job_id or uuid.uuid4().hex)
                plan = await asyncio.to_thread(build_plan, text, self.settings, directory)
            if delivery:
                self.store.save_delivery(job_id, plan, delivered)
        return plan, delivered

    async def reply(self, message: Message, text: str, *, job_id: int | None = None):
        if not text.strip():
            return
        plan, delivered = await self.prepare_reply(message, text, job_id=job_id)
        delivery = self.store is not None and job_id is not None
        for index in range(delivered, len(plan)):
            action = plan[index]
            seq = index + 1
            # Three attachments plus one text message fit the stricter C2C reply quota.
            if action["kind"] == "text":
                await self.send_text(message, action, seq)
            else:
                try:
                    if not action.get("file_info") or time.time() >= action.get("expires_at", 0):
                        uploaded = await self.upload(message, action)
                        action["file_info"] = uploaded["file_info"]
                        action["expires_at"] = time.time() + max(0, int(uploaded.get("ttl") or 3600) - 60)
                        if delivery:
                            self.store.save_delivery(job_id, plan, index)
                    await self.request("POST", self.message_path(message) + "/messages", {
                        "msg_type": 7, "media": {"file_info": action["file_info"]},
                        "msg_id": message.message_id, "msg_seq": seq,
                    })
                except QQError as exc:
                    if exc.retryable or exc.status == 401:
                        raise
                    # Report a platform rejection visibly instead of silently returning a local path.
                    action["delivery_error"] = str(exc.code)
                    log.warning("QQ attachment rejected HTTP=%s code=%s", exc.status, exc.code)
                    await self.send_text(message, {"kind": "text", "format": "plain", "text":
                        f"附件 {action['name']} 发送失败（QQ 错误码 {exc.code}）。"
                        "文件已保留，可在权限或限制问题解决后用 /result 重试。"}, seq)
                except (OSError, ValueError):
                    action["delivery_error"] = "local_file_error"
                    await self.send_text(message, {"kind": "text", "format": "plain", "text":
                        f"附件 {action['name']} 无法读取或上传，请重新生成后发送。"}, seq)
            if delivery:
                self.store.save_delivery(job_id, plan, index + 1)

    async def send_text(self, message: Message, action: dict, seq: int):
        path = self.message_path(message) + "/messages"
        scope = (message.kind, message.target)
        markdown = action.get("format") == "markdown" and scope not in self.markdown_unavailable
        payload = {"msg_id": message.message_id, "msg_seq": seq}
        if markdown:
            payload.update(msg_type=2, markdown={"content": clipped(action["text"])})
            try:
                await self.request("POST", path, payload)
                return
            except QQError as exc:
                if exc.status != 403 and str(exc.code) not in {str(c) for c in MARKDOWN_UNAVAILABLE}:
                    raise
                self.markdown_unavailable.add(scope)
                log.warning("QQ Markdown unavailable; falling back to text (code=%s)", exc.code)
                action["format"] = "plain"
                action["text"] = clipped("（QQ 未授予本场景 Markdown 权限，本次使用纯文本）\n" + action["text"])
        payload.update(msg_type=0, content=clipped(action["text"]))
        payload.pop("markdown", None)
        await self.request("POST", path, payload)

    async def upload(self, message: Message, action: dict) -> dict:
        path = self.message_path(message)
        payload = {"file_type": action["file_type"], "srv_send_msg": False, "file_name": action["name"]}
        if action.get("url"):
            payload["url"] = action["url"]
            result = await self.request("POST", path + "/files", payload, timeout=60)
        else:
            file = Path(action["path"])
            if file.stat().st_size > self.settings.max_attachment_bytes:
                raise ValueError("attachment too large")
            if file.stat().st_size <= 8 * 1024 * 1024:
                data = await asyncio.to_thread(file.read_bytes)
                if hashlib.sha256(data).hexdigest() != action["sha256"]:
                    raise ValueError("attachment snapshot changed")
                payload["file_data"] = base64.b64encode(data).decode("ascii")
                result = await self.request("POST", path + "/files", payload, timeout=60)
            else:
                result = await self.upload_chunked(path, file, action)
        if not isinstance(result.get("file_info"), str) or not result["file_info"]:
            raise QQError(502, "missing_file_info")
        return result

    async def upload_chunked(self, path: str, file: Path, action: dict) -> dict:
        def hashes():
            md5, sha1, first, sha256 = hashlib.md5(), hashlib.sha1(), hashlib.md5(), hashlib.sha256()
            count = 0
            with file.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    md5.update(chunk)
                    sha1.update(chunk)
                    sha256.update(chunk)
                    if count < 10_002_432:
                        first.update(chunk[:10_002_432 - count])
                    count += len(chunk)
            if sha256.hexdigest() != action["sha256"]:
                raise ValueError("attachment snapshot changed")
            return {"md5": md5.hexdigest(), "sha1": sha1.hexdigest(), "md5_10m": first.hexdigest()}

        prepared = await self.request("POST", path + "/upload_prepare", {
            "file_type": action["file_type"], "file_name": action["name"],
            "file_size": action["size"], **await asyncio.to_thread(hashes),
        }, timeout=60)
        upload_id = prepared.get("upload_id")
        block_size = int(prepared.get("block_size", 0))
        parts = prepared.get("parts")
        if not upload_id or block_size <= 0 or not isinstance(parts, list) or not parts:
            raise QQError(502, "invalid_upload_prepare")
        total = 0
        seen = set()
        for part in parts:
            index = int(part.get("index", 0))
            url = part.get("presigned_url", "")
            length = min(int(part.get("block_size") or block_size), action["size"] - (index - 1) * block_size)
            expected = min(block_size, action["size"] - (index - 1) * block_size)
            if index < 1 or index in seen or length <= 0 or length != expected or not url.startswith("https://"):
                raise QQError(502, "invalid_upload_part")
            seen.add(index)

            def read_part():
                with file.open("rb") as source:
                    source.seek((index - 1) * block_size)
                    return source.read(length)

            data = await asyncio.to_thread(read_part)
            if len(data) != length:
                raise ValueError("attachment snapshot truncated")
            # Presigned COS upload: do not forward QQ credentials to the storage server.
            response = await self.client.put(url, content=data,
                                             headers={"Content-Length": str(len(data))}, timeout=120)
            if not response.is_success:
                raise QQError(response.status_code, "storage_upload_failed")
            for attempt in range(5):
                try:
                    await self.request("POST", path + "/upload_part_finish", {
                        "upload_id": upload_id, "part_index": index, "block_size": len(data),
                        "md5": hashlib.md5(data).hexdigest(),
                    }, timeout=60)
                    break
                except QQError as exc:
                    if str(exc.code) != "40093001" or attempt == 4:
                        raise
                    await asyncio.sleep(1)
            total += len(data)
        if total != action["size"]:
            raise QQError(502, "incomplete_upload_parts")
        return await self.request("POST", path + "/files", {"upload_id": upload_id}, timeout=60)

    async def gateway_url(self) -> str:
        for attempt in range(2):
            token = await self.access_token()
            response = await self.client.get(
                self.settings.api_base + "/gateway",
                headers={"Authorization": f"QQBot {token}"},
            )
            if response.status_code == 401 and attempt == 0:
                self.token = ""
                continue
            body = self.checked(response)
            url = body.get("url")
            if not isinstance(url, str) or not url.startswith("wss://"):
                raise QQError(response.status_code, "invalid_gateway_url")
            return url
        raise QQError(401, "gateway_auth_failed")
