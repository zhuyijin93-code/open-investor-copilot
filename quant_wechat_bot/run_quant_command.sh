#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [[ $# -eq 0 ]]; then
  echo "用法: /bin/zsh quant_wechat_bot/run_quant_command.sh '选股 质量'" >&2
  exit 1
fi

MESSAGE="$*"

cd "$ROOT_DIR"
exec python3 -m quant_wechat_bot.bot_service chat "$MESSAGE"
