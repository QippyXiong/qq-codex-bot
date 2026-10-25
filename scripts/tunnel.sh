#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [[ -f "$ROOT/cpolar.env" ]]; then source "$ROOT/cpolar.env"; fi
mkdir -p logs
# Use an already-reserved subdomain/region to keep the callback URL stable.
args=(http -proto=https -log=stdout -log-level=INFO
      -daemon=off -processMode=single -inspect-addr=127.0.0.1:0 -tunnelName=qq-codex)
if [[ -n "${QQ_BOT_CPOLAR_SUBDOMAIN:-}" && -n "${QQ_BOT_CPOLAR_HOSTNAME:-}" ]]; then
    echo "Set either QQ_BOT_CPOLAR_SUBDOMAIN or QQ_BOT_CPOLAR_HOSTNAME" >&2
    exit 2
fi
if [[ -n "${QQ_BOT_CPOLAR_SUBDOMAIN:-}" ]]; then
    args+=("-subdomain=$QQ_BOT_CPOLAR_SUBDOMAIN")
fi
if [[ -n "${QQ_BOT_CPOLAR_HOSTNAME:-}" ]]; then
    args+=("-hostname=$QQ_BOT_CPOLAR_HOSTNAME")
fi
if [[ -n "${QQ_BOT_CPOLAR_REGION:-}" ]]; then
    args+=("-region=$QQ_BOT_CPOLAR_REGION")
fi
args+=("${QQ_BOT_PORT:-8080}")
exec "${QQ_BOT_CPOLAR_BIN:-cpolar}" "${args[@]}"
