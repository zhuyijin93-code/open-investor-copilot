#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
STATE_DIR="${OPENCLAW_STATE_DIR:-$ROOT_DIR/quant_wechat_bot/.state/weixin-personal}"

mkdir -p "$STATE_DIR"

export OPENCLAW_STATE_DIR="$STATE_DIR"
export QUANT_WECHAT_PYTHON="${QUANT_WECHAT_PYTHON:-python3}"

exec node "$ROOT_DIR/quant_wechat_bot/weixin_personal_agent.mjs" "${@:-start}"
