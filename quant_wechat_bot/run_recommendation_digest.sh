#!/bin/zsh
set -euo pipefail

python3 -m quant_wechat_bot.recommendation_digest send "$@"
