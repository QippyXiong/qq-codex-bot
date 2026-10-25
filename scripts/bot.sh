#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${QQ_BOT_PYTHON:-$ROOT/.venv/bin/python}"
usage() {
    echo "Usage: bash scripts/bot.sh start [--model MODEL] [--transport websocket|webhook] [--no-tunnel]"
    echo "       bash scripts/bot.sh {stop|status|logs|probe}"
}

ACTION="${1:-status}"
WITH_TUNNEL=1
if [[ $# -gt 0 ]]; then shift; fi
while [[ $# -gt 0 ]]; do
    if [[ "$ACTION" != start ]]; then usage >&2; exit 2; fi
    case "$1" in
        --transport)
            if [[ $# -lt 2 ]]; then usage >&2; exit 2; fi
            export QQ_BOT_TRANSPORT="$2"
            shift 2
            ;;
        --transport=*) export QQ_BOT_TRANSPORT="${1#--transport=}"; shift ;;
        --no-tunnel) WITH_TUNNEL=0; shift ;;
        --model)
            if [[ $# -lt 2 || -z "$2" || "$2" == -* ]]; then
                echo "--model requires a model name" >&2; exit 2
            fi
            export QQ_BOT_CODEX_MODEL="$2"
            shift 2
            ;;
        --model=*)
            MODEL_VALUE="${1#--model=}"
            if [[ -z "$MODEL_VALUE" || "$MODEL_VALUE" == -* ]]; then
                echo "--model requires a model name" >&2; exit 2
            fi
            export QQ_BOT_CODEX_MODEL="$MODEL_VALUE"
            shift
            ;;
        *) usage >&2; exit 2 ;;
    esac
done
export QQ_BOT_TRANSPORT="${QQ_BOT_TRANSPORT:-websocket}"
export QQ_BOT_CODEX_MODEL="${QQ_BOT_CODEX_MODEL:-gpt-6.1-sol}"
export QQ_BOT_CODEX_EFFORT="${QQ_BOT_CODEX_EFFORT:-medium}"
if [[ "$QQ_BOT_TRANSPORT" != websocket && "$QQ_BOT_TRANSPORT" != webhook ]]; then
    echo "--transport must be websocket or webhook" >&2; exit 2
fi
if [[ "$QQ_BOT_TRANSPORT" == websocket ]]; then WITH_TUNNEL=0; fi
mkdir -p logs data
PIDFILE="$ROOT/data/bot.pid"
TUNNEL_PIDFILE="$ROOT/data/cpolar.pid"

pid_running() {
    [[ -f "$1" ]] || return 1
    local pid
    pid="$(cat "$1")"
    [[ "$pid" =~ ^[0-9]+$ && "$pid" -gt 1 ]] && kill -0 "$pid" 2>/dev/null
}

running() { pid_running "$PIDFILE"; }

start_tunnel() {
    if [[ "$QQ_BOT_TRANSPORT" == websocket ]]; then
        "$PYTHON" scripts/check_connection.py --port "${QQ_BOT_PORT:-8080}"
        return $?
    fi
    if [[ "$WITH_TUNNEL" == 0 ]]; then
        echo "Tunnel disabled; configure your existing HTTPS reverse proxy separately."
        return 0
    fi
    if ! pid_running "$TUNNEL_PIDFILE"; then
        if [[ -f logs/cpolar.log ]]; then
            mv logs/cpolar.log "logs/cpolar.$(date +%Y%m%d-%H%M%S).$$.log"
        fi
        nohup env -u QQ_BOT_APP_ID -u QQ_BOT_APP_SECRET bash scripts/tunnel.sh \
            >logs/cpolar.log 2>&1 </dev/null &
        echo "$!" > "$TUNNEL_PIDFILE"
    fi
    if "$PYTHON" scripts/check_tunnel.py --port "${QQ_BOT_PORT:-8080}"; then
        return 0
    fi
    echo "HTTPS tunnel check failed; local bot remains running. See logs/cpolar.log."
    # Stop only the cpolar process started and tracked by this project.
    if pid_running "$TUNNEL_PIDFILE"; then kill "$(cat "$TUNNEL_PIDFILE")"; fi
    rm -f "$TUNNEL_PIDFILE"
    return 1
}

case "$ACTION" in
    start)
        if [[ "$QQ_BOT_TRANSPORT" == websocket ]] && pid_running "$TUNNEL_PIDFILE"; then
            kill "$(cat "$TUNNEL_PIDFILE")"
            rm -f "$TUNNEL_PIDFILE"
            echo "Stopped this project's old cpolar tunnel; WebSocket needs outbound access only."
        fi
        if [[ "$WITH_TUNNEL" == 1 ]] && ! command -v "${QQ_BOT_CPOLAR_BIN:-cpolar}" >/dev/null; then
            echo "cpolar not found. Install it or use --no-tunnel for an existing HTTPS proxy."
            exit 1
        fi
        if running; then
            echo "Already running (PID $(cat "$PIDFILE")); stop before changing model"
            start_tunnel
            exit 0
        fi
        : "${QQ_BOT_APP_ID:?Set QQ_BOT_APP_ID}"
        : "${QQ_BOT_APP_SECRET:?Set QQ_BOT_APP_SECRET}"
        if [[ -f logs/bot.log ]]; then
            mv logs/bot.log "logs/bot.$(date +%Y%m%d-%H%M%S).$$.log"
        fi
        nohup "$PYTHON" -m qq_codex_bot >>logs/bot.log 2>&1 </dev/null &
        echo "$!" > "$PIDFILE"
        for _ in {1..30}; do
            if ! running; then
                rm -f "$PIDFILE"
                echo "Startup failed; see logs/bot.log"
                exit 1
            fi
            if curl --noproxy '*' -fsS "http://127.0.0.1:${QQ_BOT_PORT:-8080}/health" >/dev/null 2>&1; then
                echo "Local bot started (PID $(cat "$PIDFILE")); log: logs/bot.log"
                start_tunnel
                exit 0
            fi
            sleep 0.2
        done
        if running; then kill "$(cat "$PIDFILE")"; fi
        rm -f "$PIDFILE"
        echo "Health check failed; see logs/bot.log"; exit 1
        ;;
    stop)
        if pid_running "$TUNNEL_PIDFILE"; then
            kill "$(cat "$TUNNEL_PIDFILE")"
            echo "cpolar tunnel stop requested"
        fi
        rm -f "$TUNNEL_PIDFILE"
        if running; then
            kill "$(cat "$PIDFILE")"
            for _ in {1..50}; do
                if ! running; then break; fi
                sleep 0.2
            done
            if running; then echo "Bot is still shutting down; retry status shortly"; exit 1; fi
            echo "Stopped"
        else echo "Not running"; fi
        rm -f "$PIDFILE"
        ;;
    status)
        if running; then echo "Running (PID $(cat "$PIDFILE"))"; else echo "Not running"; fi
        if pid_running "$TUNNEL_PIDFILE"; then
            echo "Managed cpolar process running (PID $(cat "$TUNNEL_PIDFILE"))"
        else echo "Managed cpolar tunnel not running"; fi
        if [[ "$QQ_BOT_TRANSPORT" == webhook && -f data/callback_url ]]; then
            echo "Last verified callback (may be offline now): $(cat data/callback_url)"
        fi
        if running && [[ "$QQ_BOT_TRANSPORT" == websocket ]]; then
            "$PYTHON" scripts/check_connection.py --port "${QQ_BOT_PORT:-8080}" --timeout 0 || true
        fi
        ;;
    probe)
        # No inbound listener, no Codex execution, no QQ replies: just gateway authentication.
        "$PYTHON" -m qq_codex_bot.gateway
        ;;
    logs)
        if [[ ! -f logs/bot.log ]]; then echo "No log yet"; exit 0; fi
        # Older versions appended several launches to one log. Show the newest launch only.
        awk '
            /Started server process/ { count=0; delete lines }
            { lines[count % 80]=$0; count++ }
            END {
                first=(count > 80 ? count-80 : 0)
                for (i=first; i<count; i++) print lines[i % 80]
            }
        ' logs/bot.log
        tail -n 0 -F logs/bot.log
        ;;
    -h|--help) usage ;;
    *) usage >&2; exit 2 ;;
esac
