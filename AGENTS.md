# Repository Guidelines

## Project Structure & Module Organization

`qq-codex-bot` bridges QQ official bot events (WebSocket/Webhook) to a persistent Codex CLI session.

- `src/qq_codex_bot/` — application package: `app.py` (FastAPI app), `gateway.py` / `qq.py` (QQ transport), `codex.py` (CLI process manager), `worker.py` (job queue), `store.py` (state), `ingress.py` / `incoming.py` / `outbound.py` / `messages.py` (message pipeline), `workspace.py` (per-chat workspace), `signing.py` (Ed25519 verification), `config.py` (settings from env).
- `tests/` — pytest suite, deterministic; no live QQ calls.
- `scripts/` — `bot.sh` (start/stop/tunnel), `check_connection.py`, `check_tunnel.py`, `tunnel.sh`.
- `docs/` — deployment examples such as `nginx.conf.example`.
- Root: `pyproject.toml`, `environment.yml`, `.env.example` (documentation only, never auto-loaded).

## Build, Test, and Development Commands

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest                       # full suite
pytest tests/test_gateway.py # single module
ruff check src tests         # lint
bash scripts/bot.sh start    # run bot (requires QQ_BOT_APP_ID / QQ_BOT_APP_SECRET)
```

All commands are run from the repository root.

## Coding Style & Naming Conventions

Python ≥ 3.11, 4-space indent, 100-character lines (`ruff` config in `pyproject.toml`).
Modules are lowercase with underscores; classes PascalCase; functions and variables snake_case.
Prefer small modules with one responsibility and explicit type hints on public functions.

## Testing Guidelines

Tests live in `tests/`, named `test_<behavior>.py`. Cover new behavior, failure paths, and regressions; mock all external I/O (`httpx` transports, subprocesses). No coverage gate, but new code should ship with tests.

## Commit & Pull Request Guidelines

Write concise imperative subjects (`Add config loader`), one logical change per commit.
PRs should state the problem, the approach, the validation commands run, and any behavior or configuration changes.

## Security & Configuration

Never commit credentials or private data: `.env`, `cpolar.env`, `data/`, `logs/`, and virtualenvs are gitignored.
Use `.env.example` for placeholders only. Report security issues privately rather than in public issues.
