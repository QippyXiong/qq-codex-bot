import asyncio
import json
import os
from pathlib import Path
import logging
import time

import httpx

from .codex import Codex, CodexError
from .incoming import ImageInputError, receive_images
from .messages import MENTION, Message, is_all_message_command
from .qq import QQClient, QQError
from .store import Store

log = logging.getLogger(__name__)
HELP = ("/new：另开并切换到新 Codex 会话\n/reset：重置当前会话\n/session：查看会话\n"
        "/status：查看任务状态\n/result：领取最近一次任务结果\n"
        "/all_message [on|off|status]：切换或查看本群全量消息接收\n/help：帮助")


DATA_KEYWORDS = ("评估", "计划", "饮食", "体重", "训练", "营养", "蛋白", "减脂")


def _fitness_context(workspace) -> str:
    """数据类问题：直接把快照塞进去，不依赖模型自己想起来去读文件。"""
    base = Path(str(workspace)) / "fitness"
    for name in ("weekly-2026-10-23.md", "log.csv"):
        f = base / name
        if f.is_file():
            try:
                return f.read_text(encoding="utf-8")[:4000]
            except OSError:
                return ""
    return ""


class Worker:
    def __init__(self, store: Store, codex: Codex, qq: QQClient):
        self.store, self.codex, self.qq = store, codex, qq

    async def answer(self, job, message: Message) -> str:
        text = message.text
        if is_all_message_command(text):
            if message.kind != "group":
                return "/all_message 仅用于群聊，请在目标群内操作。"
            parts = text.split()
            option = parts[1].lower() if len(parts) == 2 else ""
            if len(parts) > 2 or (len(parts) == 2 and option not in {"on", "off", "status"}):
                return "用法：/all_message 切换；/all_message on、off 或 status。"
            enabled = self.store.all_message_enabled(message.chat_key)
            if not option:
                enabled = not enabled
            elif option != "status":
                enabled = option == "on"
            if option != "status":
                self.store.set_all_message(message.chat_key, enabled)
            if enabled:
                return "本群全量消息接收已开启：普通群消息也会触发处理。"
            return "本群全量消息接收已关闭：仅 @ 当前机器人时处理；恢复时请 @ 我发送 /all_message on。"
        if text == "/help":
            return HELP
        if text == "/status":
            task = self.store.task_status(message.chat_key)
            if not task:
                return "当前没有待处理任务，使用 /result 领取最近一次结果。"
            state = {"pending": "排队中", "running": "处理中", "ready": "等待发送回复"}
            return f"任务 #{task['id']}：{state[task['status']]}。完成后可用 /result 领取结果。"
        if text == "/result":
            result = self.store.latest_result(message.chat_key)
            if result:
                if len(result["reply"].strip()) < 3:
                    return ""
                return f"任务 #{result['id']} 的结果：\n{result['reply']}"
            return "暂时没有已保存的任务结果，可使用 /status 查询进度。"
        if text == "/session":
            session = self.store.current_session(message.chat_key)
            if not session:
                return "当前没有会话，发送消息即可新建。"
            return f"会话：{session['id']}\nCodex：{session['thread_id'] or '待首次消息创建'}"
        if text == "/reset":
            self.store.reset(message.chat_key)
            return "当前会话已重置。下一条消息将创建新 Codex 会话。"
        if text == "/new" or text.startswith("/new "):
            session = self.store.new_session(message.chat_key)
            self.store.bind_job(job["id"], session["id"])
            prompt = text[4:].strip()
            if not prompt:
                prompt = ("请查看这条消息附带的图片，并结合当前对话回应。" if message.images else
                          "开始一个全新的对话。请仅回复：新会话已创建，请发送你的问题。")
        else:
            if not text and not message.images:
                return "这条消息里没有可读取的文字或图片，请发送文字或直接上传图片。"
            session = self.store.current_session(message.chat_key)
            if session is None:
                session = self.store.new_session(message.chat_key)
            self.store.bind_job(job["id"], session["id"])
            prompt = text or "请查看这条消息附带的图片，并结合当前对话回应。"
        images = ()
        if message.images:
            try:
                images = await receive_images(self.qq.settings, self.qq.client, message)
            except ImageInputError as exc:
                return str(exc)
        if message.image_notices:
            prompt += "\n\n[图片接收状态]\n" + "\n".join(message.image_notices)
        if images:
            prompt += (f"\n\n[本轮实际附图：{len(images)} 张]\n"
                       "图片可能来自当前消息或下列最近群聊，结合发送者与附图标记判断归属。"
                       "GIF 只转换首帧，不代表看过整段动画。"
                       "若图片未在界面显示，可用图片查看工具读取这些本地文件：\n"
                       + "\n".join(str(path) for path in images))
        if any(k in (text or "") for k in DATA_KEYWORDS):
            snapshot = _fitness_context(self.qq.settings.workspace)
            if snapshot:
                prompt = (f"[机主训练与饮食数据，可直接引用]\n{snapshot}\n\n"
                          f"[当前问题]\n{prompt}")
        if message.kind == "group":
            prompt = f"[群成员 {message.sender}]\n{prompt}"
            if message.context and not (text or "").startswith("/"):
                history = "\n".join(message.context)
                prompt = (f"[最近群聊，未点名时的记录]\n{history}\n\n"
                          f"[当前消息]\n{prompt}")
        def on_thread(thread):
            self.store.save_thread(session["id"], thread)

        if images:
            return await self.codex.run(prompt, session["thread_id"], on_thread, images=images)
        return await self.codex.run(prompt, session["thread_id"], on_thread)

    async def process(self, job):
        message = Message(**json.loads(job["payload"]))
        bare = MENTION.sub("", message.text or "").strip()
        directed = bare.startswith("/") or any(
            n in bare.lower() for n in os.getenv("QQ_BOT_NICKNAMES", "astra,astra大人").split(",") if n.strip()
        )
        if (job["reply"] is None and message.kind == "group" and not message.mentioned
                and not directed
                and not self.store.all_message_enabled(message.chat_key)):
            # A switch-off may overtake ordinary messages already waiting in the model queue.
            self.store.save_reply(job["id"], "")
            self.store.finish(job["id"])
            return
        # Conservative 5-minute deadline for both scenarios; docs disagree on C2C duration.
        deadline = message.sent_at + 290
        if job["is_control"] and time.time() >= deadline:
            self.store.finish(job["id"], "reply_window_expired")
            return
        text = job["reply"]
        if text is None:
            try:
                # QQ's delivery window must not cancel a long-running Codex turn.
                text = await self.answer(job, message)
            except (CodexError, TimeoutError):
                log.warning("Codex turn failed for job=%s", job["id"])
                text = "本次 Codex 处理失败或超时，会话已保留。请重试；必要时使用 /reset。"
            except Exception:
                # Avoid exception messages that may contain user content or credentials.
                log.error("Internal processing failure for job=%s", job["id"])
                text = "本次处理出错，请重新发送消息。"
            self.store.save_reply(job["id"], text)
        if len(text.strip()) < 3:
            # Treat replies shorter than three trimmed characters as intentional silence.
            self.store.finish(job["id"])
            return
        # Freeze generated artifacts even when the original QQ message has expired.
        await self.qq.prepare_reply(message, text, job_id=job["id"])
        if time.time() >= deadline:
            # Result stays durable and can be delivered against a fresh /result message.
            self.store.finish(job["id"], "reply_window_expired")
            return
        try:
            await self.qq.reply(message, text, job_id=job["id"])
        except (QQError, httpx.HTTPError) as exc:
            retryable = isinstance(exc, httpx.HTTPError) or exc.retryable
            attempts = job["attempts"] + 1
            if retryable and attempts < 4 and time.time() + 20 < deadline:
                self.store.retry_delivery(job["id"], attempts)
            else:
                self.store.finish(job["id"], "qq_delivery_failed")
            code = exc.code if isinstance(exc, QQError) else type(exc).__name__
            log.warning("QQ delivery failed job=%s code=%s", job["id"], code)
        else:
            self.store.finish(job["id"])

    async def run(self, *, control_only: bool = False):
        while True:
            job = self.store.claim(control_only=control_only)
            if job is None:
                await asyncio.sleep(0.2)
            else:
                await self.process(job)
