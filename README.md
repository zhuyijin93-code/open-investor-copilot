# Open Investor Copilot

An open-source investing copilot built around official filings, lightweight
market briefs, and a chat-first command surface.

It is designed to feel like a small product, not just a collection of scripts:

- Track official disclosures from a curated smart-money watchlist
- Generate fast market snapshots and stock briefs
- Serve the same command core in terminal, web chat, JSON API, and WeChat callback flows
- Stay easy to fork, self-host, and extend

## What Makes It Interesting

Most investing tools are either closed products or one-off notebooks.
Open Investor Copilot tries to sit in the middle:

- opinionated enough to be immediately useful
- simple enough that an individual can run it alone
- structured enough that the same core can power future bots and channels

Today it ships with:

- Official disclosure monitoring for Buffett, Berkshire, Duan Yongping, Ackman, Tepper, Li Lu, Pelosi, and Jensen Huang
- Free market snapshots and stock briefs powered by Fiscal.ai
- FinChat-backed research Q&A when you add a paid API key
- A local web chat UI plus a JSON API that can later sit behind a WeChat-facing gateway

## Requirements

- Python `3.9+`
- No required third-party Python packages for the core repo
- Optional: `lark-cli` if you want Lark delivery
- Optional: Fiscal.ai and FinChat API keys if you want richer market responses

## Why This Direction

If you want people to star and reuse an open-source project, it helps when the repo
feels like a product instead of a pile of scripts. This repo is moving toward that:

- scriptable from the terminal
- interactive in the browser
- easy to wrap in chat channels
- grounded in official-source monitoring rather than generic commentary

## Quick Start

No package install is required for the core scripts.

Clone the repo, then copy the example config:

```bash
cp local_settings.example.json .cache/local_settings.json
```

Add any keys you have:

```json
{
  "finchat_api_key": "your_key_here",
  "fiscal_api_key": "your_free_fiscal_api_key_here",
  "market_brief_markets": ["US", "HK", "CN"],
  "watchlist": ["NVDA", "TSLA", "0700.HK"],
  "fiscal_watchlist": ["MSFT", "NVDA", "AMZN", "GOOG", "TSLA"],
  "lark_group_webhook": "https://..."
}
```

Core files:

- [bot_service.py](bot_service.py): chat router, web UI, JSON API, and公众号 callback adapter
- [monitor.py](monitor.py): official filing monitoring
- [market_hub.py](market_hub.py): market briefs, portal shortcuts, and API-backed research flows
- [wechat_menu.example.json](wechat_menu.example.json): ready-to-use公众号 menu example

## Interactive Web App

Run the local chat surface:

```bash
python3 bot_service.py serve
```

Then open `http://127.0.0.1:8787`.

Example prompts:

- `帮助`
- `全部披露`
- `巴菲特最新披露`
- `市场简报`
- `自选股快照`
- `个股 NVDA`
- `问一下：英伟达最近一个季度最重要的变化`

The same command core is also exposed as a JSON endpoint:

```bash
curl -s http://127.0.0.1:8787/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"市场简报"}'
```

## WeChat Official Account Callback Adapter

The same service can also act as a公众号 callback adapter:

```bash
python3 bot_service.py serve --host 0.0.0.0 --port 8787
```

Then point your callback URL at:

```text
https://your-domain.example.com/wechat/callback
```

Configuration:

- Set `wechat_official_token` in `.cache/local_settings.json`, or export `WECHAT_OFFICIAL_TOKEN`
- Keep the platform callback token identical to the one in your config
- This adapter currently supports plaintext callback mode first, which makes public open-source setup much easier
- `wechat_menu_actions` lets you map公众号 menu `EventKey` values to the same text commands the bot already understands

Current behavior:

- `GET /wechat/callback` handles platform verification
- `POST /wechat/callback` verifies the signature and answers text messages
- `subscribe` events return a help message
- `CLICK` menu events can route `EventKey` into the same bot command core

Recommended menu setup:

```json
{
  "button": [
    {
      "name": "开始",
      "sub_button": [
        { "type": "click", "name": "帮助", "key": "MENU_HELP" },
        { "type": "click", "name": "市场简报", "key": "MENU_MARKET" }
      ]
    },
    {
      "name": "披露",
      "sub_button": [
        { "type": "click", "name": "全部披露", "key": "MENU_FILINGS" },
        { "type": "click", "name": "巴菲特", "key": "MENU_BUFFETT" }
      ]
    },
    {
      "name": "自选",
      "sub_button": [
        { "type": "click", "name": "自选股快照", "key": "MENU_WATCHLIST" }
      ]
    }
  ]
}
```

You can also use the ready-made example file in this repo:

```text
wechat_menu.example.json
```

Default `EventKey` routing in this repo:

- `MENU_HELP` -> `帮助`
- `MENU_MARKET` -> `市场简报`
- `MENU_FILINGS` -> `全部披露`
- `MENU_BUFFETT` -> `巴菲特最新披露`
- `MENU_WATCHLIST` -> `自选股快照`

You can also define your own mapping in `.cache/local_settings.json`:

```json
{
  "wechat_menu_actions": {
    "MENU_TSLA": "个股 TSLA",
    "MENU_QA": "问一下：今天美股最大的风险点是什么",
    "MENU_NVDA": "个股 NVDA"
  }
}
```

Advanced shortcut forms for `EventKey` are also supported:

- `ASK:英伟达最近的风险`
- `STOCK:NVDA`
- `CMD:市场简报`

Example messages a follower can send:

- `帮助`
- `市场简报`
- `全部披露`
- `巴菲特最新披露`
- `个股 NVDA`

Current scope:

- Supported now: plaintext callback, text messages, subscribe events, menu click events
- Not yet implemented: AES encrypted callback mode, media messages, customer-service push, OAuth user binding

## Open-Source Setup Notes

This repository is intentionally lightweight:

- no framework lock-in
- no database requirement
- no background worker required for the local MVP
- no third-party Python dependency required to start hacking on it

That makes it a good candidate for a public GitHub repo, a weekend side project,
or a base for a more serious chat-first finance product.

## CLI Usage

Preview the latest filings without writing state:

```bash
python3 monitor.py --preview
```

Bootstrap local state on the first run:

```bash
python3 monitor.py
```

Print a normal filing alert without sending it:

```bash
python3 monitor.py --dry-run
```

Run only part of the watchlist:

```bash
python3 monitor.py --preview --keys buffett,duan
python3 monitor.py --preview --keys buffett_company
python3 monitor.py --preview --keys buffett,duan,ackman,tepper,lilu
python3 monitor.py --preview --keys pelosi,huang
```

Use the terminal chat surface for one-shot commands:

```bash
python3 bot_service.py chat "市场简报"
python3 bot_service.py chat "个股 NVDA"
python3 bot_service.py chat "巴菲特最新披露"
```

## Market Research Tools

Portal shortcuts:

```bash
python3 market_hub.py links
python3 market_hub.py open koyfin
python3 market_hub.py open koyfin --page movers
python3 market_hub.py open finchat --page api
```

Ask FinChat questions from the terminal:

```bash
FINCHAT_API_KEY=your_key_here python3 market_hub.py ask-finchat "英伟达最近一个季度最值得关注的三件事"
python3 market_hub.py ask-finchat "现在美股最强的热点板块是什么，驱动因素和风险分别是什么" --send-lark
```

Use the built-in presets:

```bash
python3 market_hub.py market-brief
python3 market_hub.py market-brief --markets US,HK,CN --send-lark
python3 market_hub.py stock-brief NVDA
python3 market_hub.py watchlist-brief --tickers NVDA,TSLA,0700.HK
```

Use the free Fiscal.ai entrypoints:

```bash
python3 market_hub.py top-news-free
python3 market_hub.py market-brief-free
python3 market_hub.py stock-brief-free NVDA
python3 market_hub.py watchlist-free
python3 market_hub.py market-brief-free --send-lark
```

## Delivery Channels

Current:

- Local web UI
- Terminal
- Lark private message and group webhook delivery

Planned:

- WeChat-friendly callback adapter
- WeCom app connector
- More natural conversational command routing
- Public deployment recipe
- Better demo assets for the GitHub landing page
- Scheduled daily digests

## Notes

- SEC requests use a polite `User-Agent`. Override with `SEC_USER_AGENT` if needed.
- Local state is stored in `.cache/watch_state.json`.
- Lark private messages default to the authenticated `lark-cli` user unless you set `LARK_USER_ID`.
- Group robot delivery is optional. Set `LARK_GROUP_WEBHOOK` or write `{"lark_group_webhook":"..."}` to `.cache/local_settings.json`.

## License

This project is released under the MIT License. See [LICENSE](LICENSE).
