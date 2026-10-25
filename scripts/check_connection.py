"""Wait until the running bot actually authenticates with the QQ WebSocket gateway."""

import argparse
import time

import httpx


def wait_ready(port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    last = "local_service_unreachable"
    with httpx.Client(timeout=2, trust_env=False) as client:
        while True:
            try:
                response = client.get(f"http://127.0.0.1:{port}/health")
                response.raise_for_status()
                body = response.json()
                if body.get("transport") != "websocket":
                    print("运行中的服务不是 WebSocket 模式，请先 stop 再 start。")
                    return False
                state = body.get("qq", {})
                if state.get("ready"):
                    print("QQ WebSocket 已连接（READY）。无需公网地址或 cpolar。")
                    print(f"已接收消息事件：{state.get('messages_received', 0)}；"
                          f"最近事件：{state.get('last_event') or '(无)'}。")
                    print("请发送私聊 /help 或群内 @机器人 /help 测试回复。")
                    return True
                last = state.get("error") or state.get("status", "unknown")
            except (httpx.HTTPError, ValueError):
                pass
            if time.monotonic() >= deadline:
                print(f"QQ WebSocket 尚未就绪：{last}。服务将继续重连，详情见 logs/bot.log。")
                print("若 QQ 后台可选择接入方式，请切换为 WebSocket，并确认测试成员/测试群和接口权限。")
                return False
            time.sleep(0.5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--timeout", type=float, default=35)
    args = parser.parse_args()
    raise SystemExit(0 if wait_ready(args.port, args.timeout) else 1)
