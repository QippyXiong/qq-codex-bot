import importlib.util
import os
import signal
import subprocess
import sys
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("check_connection", ROOT / "scripts/check_connection.py")
connection = importlib.util.module_from_spec(spec)
spec.loader.exec_module(connection)


def test_readiness_does_not_confuse_local_health_with_qq_ready(monkeypatch, capsys):
    real_client = httpx.Client
    state = {"status": "ok", "transport": "websocket",
             "qq": {"ready": False, "status": "reconnecting", "error": "auth_failed"}}

    def handle(_):
        return httpx.Response(200, json=state)

    monkeypatch.setattr(connection.httpx, "Client", lambda **kw: real_client(
        transport=httpx.MockTransport(handle), **kw))
    assert not connection.wait_ready(8080, 0)
    assert "auth_failed" in capsys.readouterr().out
    state["qq"] = {"ready": True, "status": "ready", "error": None}
    assert connection.wait_ready(8080, 0)
    assert "READY" in capsys.readouterr().out
    state["transport"] = "webhook"
    assert not connection.wait_ready(8080, 0)
    assert "不是 WebSocket 模式" in capsys.readouterr().out


def test_default_start_does_not_require_or_launch_cpolar(tmp_path):
    import shutil

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(ROOT / "scripts/bot.sh", scripts / "bot.sh")
    # A fake executable checks the actual environment inherited by the long-running service.
    python = tmp_path / "fake-python"
    python.write_text(f'''#!{sys.executable}
import os, sys, time
from pathlib import Path
if sys.argv[1:3] == ["-m", "qq_codex_bot"]:
    Path("service-env").write_text(os.environ["QQ_BOT_TRANSPORT"] + ":" + os.environ["QQ_BOT_CODEX_MODEL"])
    time.sleep(60)
else:
    assert sys.argv[1] == "scripts/check_connection.py"
    print("QQ WebSocket READY")
''')
    python.chmod(0o755)
    curl = tmp_path / "curl"
    curl.write_text("#!/bin/sh\nexit 0\n")
    curl.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("QQ_BOT_")}
    env.update(QQ_BOT_PYTHON=str(python), QQ_BOT_APP_ID="app", QQ_BOT_APP_SECRET="secret",
               QQ_BOT_CPOLAR_BIN="does-not-exist", PATH=f"{tmp_path}:{os.environ['PATH']}")
    try:
        result = subprocess.run(["bash", str(scripts / "bot.sh"), "start", "--model", "test-model"],
                                env=env, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr + result.stdout
        assert "READY" in result.stdout
        assert (tmp_path / "service-env").read_text() == "websocket:test-model"
        assert not (tmp_path / "data/cpolar.pid").exists()
        assert not (tmp_path / "logs/cpolar.log").exists()
    finally:
        path = tmp_path / "data/bot.pid"
        if path.exists():
            try:
                os.kill(int(path.read_text()), signal.SIGTERM)
            except ProcessLookupError:
                pass
