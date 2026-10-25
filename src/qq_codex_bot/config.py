import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path


def allowlist(name: str) -> frozenset[str]:
    return frozenset(x.strip() for x in os.getenv(name, "").split(",") if x.strip())


@dataclass(frozen=True)
class Settings:
    app_id: str
    app_secret: str = field(repr=False)
    data_dir: Path = Path("data")
    workspace: Path = Path("data/workspace")
    codex_bin: str = "codex"
    model: str = ""
    sandbox: str = "approve-for-me"
    timeout: int = 0
    workers: int = 2
    api_base: str = "https://api.bot.qq.com"
    allowed_users: frozenset[str] = frozenset()
    allowed_groups: frozenset[str] = frozenset()
    transport: str = "websocket"
    message_format: str = "markdown"
    max_attachment_bytes: int = 50 * 1024 * 1024
    self_id: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        app_id = os.getenv("QQ_BOT_APP_ID", "").strip()
        secret = os.getenv("QQ_BOT_APP_SECRET", "").strip()
        if not app_id or not secret:
            raise ValueError("请设置 QQ_BOT_APP_ID 和 QQ_BOT_APP_SECRET 环境变量")
        binary = shutil.which(os.getenv("QQ_BOT_CODEX_BIN", "codex"))
        if not binary:
            raise ValueError("找不到 Codex CLI；请安装并运行 codex login")
        data = Path(os.getenv("QQ_BOT_DATA_DIR", "data")).resolve()
        transport = os.getenv("QQ_BOT_TRANSPORT", "websocket").strip().lower()
        if transport not in {"websocket", "webhook"}:
            raise ValueError("QQ_BOT_TRANSPORT 必须是 websocket 或 webhook")
        message_format = os.getenv("QQ_BOT_MESSAGE_FORMAT", "markdown").strip().lower()
        if message_format not in {"markdown", "plain"}:
            raise ValueError("QQ_BOT_MESSAGE_FORMAT 必须是 markdown 或 plain")
        max_attachment_mb = int(os.getenv("QQ_BOT_MAX_ATTACHMENT_MB", "50"))
        if not 1 <= max_attachment_mb <= 200:
            raise ValueError("QQ_BOT_MAX_ATTACHMENT_MB 范围 1..200")
        sandbox = os.getenv("QQ_BOT_CODEX_SANDBOX", "approve-for-me")
        if sandbox not in {"approve-for-me", "read-only", "workspace-write"}:
            raise ValueError(
                "QQ_BOT_CODEX_SANDBOX 必须是 approve-for-me、read-only 或 workspace-write"
            )
        timeout = int(os.getenv("QQ_BOT_CODEX_TIMEOUT", "0"))
        workers = int(os.getenv("QQ_BOT_WORKERS", "2"))
        if timeout < 0 or not 1 <= workers <= 16:
            raise ValueError("QQ_BOT_CODEX_TIMEOUT 必须 >= 0（0 表示不限时）；QQ_BOT_WORKERS 范围 1..16")
        return cls(
            app_id=app_id, app_secret=secret, data_dir=data,
            workspace=Path(os.getenv("QQ_BOT_CODEX_WORKSPACE", str(data / "workspace"))).resolve(),
            codex_bin=binary, model=os.getenv("QQ_BOT_CODEX_MODEL", ""), sandbox=sandbox,
            timeout=timeout, workers=workers,
            api_base=os.getenv("QQ_BOT_API_BASE", "https://api.bot.qq.com").rstrip("/"),
            allowed_users=allowlist("QQ_BOT_ALLOWED_USERS"),
            allowed_groups=allowlist("QQ_BOT_ALLOWED_GROUPS"),
            transport=transport,
            message_format=message_format,
            max_attachment_bytes=max_attachment_mb * 1024 * 1024,
            self_id=os.getenv("QQ_BOT_SELF_ID", "").strip(),
        )
