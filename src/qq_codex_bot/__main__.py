import logging
import os

import uvicorn

from .app import create_app


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    transport = os.getenv("QQ_BOT_TRANSPORT", "websocket").strip().lower()
    host = os.getenv("QQ_BOT_HOST", "127.0.0.1")
    if transport == "websocket" and host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("WebSocket 模式的健康检查仅允许监听本地回环地址")
    uvicorn.run(create_app(), host=host,
                port=int(os.getenv("QQ_BOT_PORT", "8080")), workers=1, access_log=False)


if __name__ == "__main__":
    main()
