import asyncio
import contextlib
import json
import os
import signal
from collections.abc import Callable
from pathlib import Path

from .config import Settings
from .workspace import ensure_workspace


class CodexError(RuntimeError):
    pass


class Codex:
    def __init__(self, settings: Settings):
        self.settings = settings
        ensure_workspace(settings.workspace)

    def command(self, thread_id: str | None, *, images: tuple[Path, ...] = ()) -> list[str]:
        s = self.settings
        # Apply permissions globally so they also govern exec resume.
        args = [s.codex_bin]
        if s.sandbox == "approve-for-me":
            args += ["--approve-for-me"]
        else:
            args += ["-a", "never", "-s", s.sandbox]
        effort = os.getenv("QQ_BOT_CODEX_EFFORT", "").strip()
        if effort:
            # 推理强度走 -c 覆盖，不动全局 Codex 配置
            args += ["-c", f"model_reasoning_effort={effort}"]
        args += ["exec", "--json", "--skip-git-repo-check"]
        if s.model:
            args += ["-m", s.model]
        if thread_id:
            args += ["resume", thread_id]
        if images:
            for image in images:
                args += ["--image", str(image)]
            # Delimit stdin's '-' prompt from the initial command's variadic image option.
            args += ["--"]
        return args + ["-"]

    async def run(self, prompt: str, thread_id: str | None,
                  on_thread: Callable[[str], None], *, images: tuple[Path, ...] = ()) -> str:
        s = self.settings
        # QQ credentials are for the bridge, not for the agent's child commands.
        env = {k: v for k, v in os.environ.items() if not k.startswith("QQ_BOT_")}
        proc = await asyncio.create_subprocess_exec(
            *self.command(thread_id, images=images), cwd=s.workspace, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=4 * 1024 * 1024,
        )
        stderr_tail: list[str] = []
        stderr_task = asyncio.create_task(self._discard_stderr(proc.stderr, stderr_tail))

        async def consume():
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
            completed = False
            seen_thread = thread_id
            answer = ""
            failed = False
            async for line in proc.stdout:
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "thread.started":
                    seen_thread = event.get("thread_id")
                    if isinstance(seen_thread, str) and seen_thread:
                        on_thread(seen_thread)
                elif event.get("type") == "item.completed":
                    item = event.get("item", {})
                    if isinstance(item, dict) and item.get("type") == "agent_message":
                        value = item.get("text", "")
                        answer = value if isinstance(value, str) else ""
                elif event.get("type") == "turn.completed":
                    completed = True
                elif event.get("type") == "turn.failed":
                    failed = True
            code = await proc.wait()
            if code or failed:
                detail = " / ".join(stderr_tail[-3:])
                raise CodexError(
                    f"Codex 未完成本轮处理（exit={code}，failed={failed}）"
                    + (f"：{detail}" if detail else "")
                )
            # Successful CLI runs may intentionally produce no reply, including no usable events.
            if not answer.strip():
                return ""
            if not completed or not seen_thread:
                raise CodexError(f"Codex 未完成本轮处理（exit={code}），请检查登录与服务日志")
            return answer.strip()

        try:
            return await asyncio.wait_for(consume(), timeout=s.timeout or None)
        except TimeoutError as exc:
            raise CodexError("Codex 处理超时，会话已保留；请缩短问题后重试") from exc
        finally:
            # Clean up descendants even when the CLI exited before its children.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            if proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), 3)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(proc.pid, signal.SIGKILL)
                    await proc.wait()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stderr_task, 3)

    @staticmethod
    async def _discard_stderr(stream, tail):
        # 只保留尾部少量行用于定位失败原因；不落 prompt、不落凭据。
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                break
            for line in chunk.decode("utf-8", "ignore").splitlines():
                line = line.strip()
                if line:
                    tail.append(line[:200])
                    if len(tail) > 5:
                        tail.pop(0)
