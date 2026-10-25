import importlib.util
import os
import subprocess
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("check_tunnel", ROOT / "scripts/check_tunnel.py")
tunnel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tunnel)


def test_forwarding_check_rejects_other_bot_instances():
    def handle(request):
        instance = "current" if request.url.host == "ours.example" else "other"
        return httpx.Response(200, json={"status": "ok", "instance": instance})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert tunnel.check_url(client, "https://ours.example", "current")
        assert not tunnel.check_url(client, "https://other.example", "current")


def test_forwarding_check_rejects_network_errors():
    def handle(request):
        raise httpx.ConnectError("offline", request=request)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert not tunnel.check_url(client, "https://offline.example", "current")


def test_log_urls_and_changed_callback_warning(tmp_path, capsys):
    urls = tunnel.candidates(
        'url=https://old.cpolar.cn msg="https://new.cpolar.cn/health" https://new.cpolar.cn')
    assert urls == ["https://new.cpolar.cn", "https://old.cpolar.cn"]
    state = tmp_path / "callback_url"
    tunnel.announce("https://old.cpolar.cn", state)
    assert "填写到 QQ 开放平台" in capsys.readouterr().out
    tunnel.announce("https://new.cpolar.cn", state)
    assert "回调地址已变化" in capsys.readouterr().out
    assert state.read_text().strip() == "https://new.cpolar.cn/qq/webhook"
    tunnel.announce("https://new.cpolar.cn", state)
    assert "回调地址已变化" not in capsys.readouterr().out


def test_tunnel_script_passes_reserved_domain_and_region(tmp_path):
    fake = tmp_path / "cpolar"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    fake.chmod(0o755)
    env = dict(os.environ, QQ_BOT_CPOLAR_BIN=str(fake),
               QQ_BOT_CPOLAR_SUBDOMAIN="myqqbot", QQ_BOT_CPOLAR_REGION="cn_vip",
               QQ_BOT_PORT="8088")
    env.pop("QQ_BOT_CPOLAR_HOSTNAME", None)
    result = subprocess.run(["bash", str(ROOT / "scripts/tunnel.sh")], env=env,
                            capture_output=True, text=True, check=True)
    args = result.stdout.splitlines()
    assert args[0] == "http" and args[-1] == "8088"
    assert "-subdomain=myqqbot" in args
    assert "-region=cn_vip" in args
    assert "-proto=https" in args and "-daemon=off" in args
    assert "-inspect-addr=127.0.0.1:0" in args
    env["QQ_BOT_CPOLAR_HOSTNAME"] = "bot.example.com"
    result = subprocess.run(["bash", str(ROOT / "scripts/tunnel.sh")], env=env,
                            capture_output=True, text=True)
    assert result.returncode == 2


def test_tunnel_script_uses_project_reserved_domain_defaults(tmp_path):
    import shutil

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(ROOT / "scripts/tunnel.sh", scripts / "tunnel.sh")
    (tmp_path / "cpolar.env").write_text(
        ': "${QQ_BOT_CPOLAR_SUBDOMAIN:=test-reservation}"\n'
        ': "${QQ_BOT_CPOLAR_REGION:=cn}"\n'
        'export QQ_BOT_CPOLAR_SUBDOMAIN QQ_BOT_CPOLAR_REGION\n'
    )
    fake = tmp_path / "cpolar"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    fake.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("QQ_BOT_CPOLAR_")}
    env["QQ_BOT_CPOLAR_BIN"] = str(fake)
    result = subprocess.run(["bash", str(scripts / "tunnel.sh")], env=env,
                            capture_output=True, text=True, check=True)
    assert "-subdomain=test-reservation" in result.stdout.splitlines()
    assert "-region=cn" in result.stdout.splitlines()
