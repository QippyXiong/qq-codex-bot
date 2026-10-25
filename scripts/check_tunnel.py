"""Verify the cpolar URL actually reaches this running bot, then print the callback URL."""

import argparse
import re
import time
from pathlib import Path

import httpx


def candidates(log: str) -> list[str]:
    # cpolar's INFO log includes the forwarding URL. Ignore paths and log quoting.
    urls = re.findall(r'https://[A-Za-z0-9.-]+(?::\d+)?', log)
    return list(dict.fromkeys(reversed(urls)))


def check_url(client, base: str, instance: str) -> bool:
    try:
        response = client.get(base + "/health", params={"probe": instance})
        response.raise_for_status()
        body = response.json()
        return isinstance(body, dict) and body.get("status") == "ok" and body.get("instance") == instance
    except (httpx.HTTPError, ValueError):
        return False


def announce(base: str, path: Path):
    callback = base.rstrip("/") + "/qq/webhook"
    previous = path.read_text().strip() if path.exists() else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(callback + "\n")
    print(f"QQ HTTPS 回调地址：{callback}")
    if previous and previous != callback:
        print(f"警告：回调地址已变化，旧地址为 {previous}")
        print("请更新 QQ 开放平台后台的回调地址，旧地址将无法接收消息。")
    elif not previous:
        print("请将以上地址填写到 QQ 开放平台后台的 Webhook 回调地址中。")
    print("公网转发已验证；这不代表 QQ 已连接。请在 QQ 后台保存并通过回调验证。")
    print("还需订阅 C2C_MESSAGE_CREATE（私聊）和 GROUP_AT_MESSAGE_CREATE（群内 @机器人）。")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--log", type=Path, default=Path("logs/cpolar.log"))
    parser.add_argument("--state", type=Path, default=Path("data/callback_url"))
    parser.add_argument("--timeout", type=float, default=40)
    args = parser.parse_args()
    # Do not route local probes through an inherited HTTP proxy.
    with httpx.Client(timeout=3, trust_env=False, follow_redirects=False) as client:
        try:
            health = client.get(f"http://127.0.0.1:{args.port}/health")
            health.raise_for_status()
            body = health.json()
            instance = body.get("instance") if isinstance(body, dict) else None
        except (httpx.HTTPError, ValueError):
            instance = None
        if not instance:
            print("Local bot health check failed or old bot version is running. Restart the bot first.")
            return 1
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            log = args.log.read_text(errors="replace") if args.log.exists() else ""
            for base in candidates(log):
                if check_url(client, base, instance):
                    announce(base, args.state)
                    return 0
            time.sleep(0.5)
    print("cpolar forwarding could not be verified. Check logs/cpolar.log for authentication/network/domain errors.")
    print("The local bot may be running, but QQ connectivity has NOT been confirmed.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
