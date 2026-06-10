# Quant WeChat Bot

Quant WeChat Bot is a lightweight, open-source MVP for people who want a
quant-style stock-picking project that feels usable from day one:

- interactive in the browser
- callable through a JSON API
- easy to front with a WeChat Official Account callback adapter
- simple enough to replace the sample universe with your own factor export

This repo intentionally ships with a small sample universe and transparent
scoring rules, so anyone can fork it and understand the whole stack.

It also supports a larger free A-share universe mode for everyday use on this machine.

## Product Idea

Most "AI stock bots" feel vague. This one is opinionated:

- expose a few concrete strategies instead of pretending to know everything
- show why a stock ranked high
- keep the command surface short enough for chat
- make the same commands work in web chat and WeChat callback flows

The current MVP is best thought of as:

- a quant stock screener
- wrapped in a chat interface
- with a WeChat-ready deployment path

## Strategies

Current built-in strategies:

- `质量动量`: high ROE, good revenue growth, positive medium-term trend
- `趋势增强`: strongest 20D and 60D momentum with basic fundamental support
- `低估价值`: lower PE/PB, decent dividend yield, still-profitable large caps
- `低波防守`: lower volatility, bigger market cap, steadier total-return profile

The ranking is intentionally simple and transparent:

1. load a CSV universe
2. compute percentile scores per factor
3. apply a weighted strategy score
4. filter obvious mismatches
5. return the top names with factor-based reasons

## Quick Start

No required third-party packages for the core MVP.

Run the local chat surface:

```bash
python3 -m quant_wechat_bot.bot_service serve
```

Then open:

```text
http://127.0.0.1:8790
```

Or use one-shot terminal commands:

```bash
python3 -m quant_wechat_bot.bot_service chat "策略列表"
python3 -m quant_wechat_bot.bot_service chat "选股 质量"
python3 -m quant_wechat_bot.bot_service chat "评分 NVDA"
```

## Commands

Try:

- `帮助`
- `策略列表`
- `选股 质量`
- `选股 动量`
- `选股 价值`
- `选股 低波`
- `评分 NVDA`
- `评分 600519 A股`
- `股票池`
- `股票池 A股`

Slash commands also work:

- `/help`
- `/strategies`
- `/pick quality`
- `/pick momentum 3`
- `/score NVDA`
- `/score 600519 A股`
- `/universe A股`

## Larger Universe

If the bundled sample universe feels too small, use the larger free A-share mode:

```bash
python3 -m quant_wechat_bot.bot_service chat "股票池 A股"
python3 -m quant_wechat_bot.bot_service chat "选股 质量 A股"
python3 -m quant_wechat_bot.bot_service chat "评分 600519 A股"
```

How it works:

- uses a free Eastmoney market snapshot
- filters out ST / delisted-like names / low-turnover names
- caches the local CSV under `quant_wechat_bot/.cache/a_share_universe.csv`
- default example config now points normal `选股 ...` commands to `A股`

Current trade-off:

- coverage is much larger
- quote and valuation fields are available
- deeper fundamental factors are still placeholder-quality until we plug in a richer provider

## CSV Universe Format

The project ships with:

```text
quant_wechat_bot/sample_universe.csv
```

Required columns:

- `ticker`
- `name`
- `sector`
- `price`
- `market_cap_b`
- `pe`
- `pb`
- `roe`
- `revenue_growth`
- `momentum_20d`
- `momentum_60d`
- `volatility_20d`
- `dividend_yield`

You can replace the sample file with your own export from any research stack
as long as you keep the same schema.

## Local Settings

Copy the example file:

```bash
mkdir -p quant_wechat_bot/.cache
cp quant_wechat_bot/local_settings.example.json quant_wechat_bot/.cache/local_settings.json
```

Example config:

```json
{
  "universe_csv": "sample_universe.csv",
  "default_market": "A股",
  "default_strategy": "quality",
  "a_share_universe_csv": ".cache/a_share_universe.csv",
  "a_share_limit": 800,
  "wechat_official_token": "replace-with-your-token",
  "wechat_menu_actions": {
    "MENU_PICK_QUALITY": "选股 质量 A股",
    "MENU_SCORE_NVDA": "评分 NVDA 样本"
  }
}
```

## WeChat Official Account Callback Adapter

Run the service:

```bash
python3 -m quant_wechat_bot.bot_service serve --host 0.0.0.0 --port 8790
```

Then point your callback URL to:

```text
https://your-domain.example.com/wechat/callback
```

Behavior today:

- `GET /wechat/callback` handles WeChat platform verification
- `POST /wechat/callback` verifies the signature and answers text messages
- `subscribe` events return a help message
- `CLICK` menu events route `EventKey` values into the same command core

Example menu:

```text
quant_wechat_bot/wechat_menu.example.json
```

Default menu ideas:

- `帮助`
- `策略列表`
- `选股 质量 A股`
- `选股 动量 A股`
- `选股 价值 A股`
- `评分 600519 A股`

## Personal WeChat Direct Chat

If you want to chat from a personal WeChat account instead of a public account,
this workspace can also reuse the local `weixin-agent-sdk` bridge already present
on this machine.

Local helper files:

- [weixin_personal_agent.mjs](/Users/admin/Documents/赚钱小能手/quant_wechat_bot/weixin_personal_agent.mjs)
- [run_personal_wechat_quant.sh](/Users/admin/Documents/赚钱小能手/quant_wechat_bot/run_personal_wechat_quant.sh)
- [run_quant_command.sh](/Users/admin/Documents/赚钱小能手/quant_wechat_bot/run_quant_command.sh)

Local debug:

```bash
node quant_wechat_bot/weixin_personal_agent.mjs chat "选股 质量"
```

Start the personal-WeChat bot:

```bash
/bin/zsh quant_wechat_bot/run_personal_wechat_quant.sh start
```

If you need a fresh QR login for the bridge:

```bash
/bin/zsh quant_wechat_bot/run_personal_wechat_quant.sh login
```

This route is intended for local testing and personal use first.

## Why This Can Be A Good Public Repo

It is easier to star a repo that feels like a product:

- obvious use case
- interactive demo surface
- deployable chat entrypoint
- understandable ranking logic
- easy extension path

Good next steps for the public version:

- plug in daily factor refresh from a real data source
- add portfolio tracking and user watchlists
- support richer backtest output and charts
- save per-user strategy presets
- add a better landing-page demo GIF

## Tests

Run:

```bash
python3 -m unittest discover -s quant_wechat_bot/tests
```

## Notes

- The bundled sample universe is illustrative, not real-time.
- This project is not investment advice.
- The MVP favors clarity and hackability over feature depth.
