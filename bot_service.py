#!/usr/bin/env python3
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import html
import json
import os
import re
import textwrap
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
import urllib.parse
import xml.etree.ElementTree as ET

import market_hub
import monitor


HOST = "127.0.0.1"
PORT = 8787
MAX_REPLY_CHARS = 3600
WECHAT_REPLY_CHARS = 1200
WECHAT_CALLBACK_PATH = "/wechat/callback"
SUPPORTED_WATCH_HINT = ", ".join(item["key"] for item in monitor.WATCHLIST)
DEFAULT_WECHAT_MENU_ACTIONS = {
    "MENU_HELP": "帮助",
    "MENU_MARKET": "市场简报",
    "MENU_FILINGS": "全部披露",
    "MENU_BUFFETT": "巴菲特最新披露",
    "MENU_WATCHLIST": "自选股快照",
}


@dataclasses.dataclass
class BotReply:
    text: str
    command: str


@dataclasses.dataclass
class WeChatCallbackConfig:
    token: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Interactive investing copilot with a web chat surface and a webhook-friendly API."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="Run the local web chat and JSON API.")
    serve_parser.add_argument("--host", default=HOST, help=f"Bind host. Default: {HOST}.")
    serve_parser.add_argument("--port", type=int, default=PORT, help=f"Bind port. Default: {PORT}.")

    chat_parser = subparsers.add_parser("chat", help="Run one chat turn from the terminal.")
    chat_parser.add_argument("message", help="User message to process.")

    return parser.parse_args()


def help_text() -> str:
    return textwrap.dedent(
        f"""
        Open Investor Copilot

        Try one of these:
        - 帮助
        - 全部披露
        - 巴菲特最新披露
        - 市场简报
        - 自选股快照
        - 个股 NVDA
        - 问一下：英伟达最近一个季度最重要的变化

        Slash commands also work:
        - /help
        - /watchlist
        - /filings [keys]
        - /market
        - /free-watchlist [tickers]
        - /stock <ticker>
        - /ask <question>

        Filing watch keys:
        - {SUPPORTED_WATCH_HINT}
        """
    ).strip()


def normalize_message(message: str) -> str:
    return re.sub(r"\s+", " ", message).strip()


def truncate_reply(text: str) -> str:
    if len(text) <= MAX_REPLY_CHARS:
        return text
    clipped = text[: MAX_REPLY_CHARS - 120].rstrip()
    return clipped + "\n\n[Truncated for chat delivery. Use the web view or CLI for the full result.]"


def csv_items(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def build_watchlist_text() -> str:
    rows = []
    for item in monitor.WATCHLIST:
        rows.append(f"- {item['key']}: {item['label']} ({item['kind']})")
    return "Current filing watchlist:\n" + "\n".join(rows)


def build_filing_preview(keys: str | None = None) -> str:
    watches = monitor.selected_watchlist(keys)
    snapshots = monitor.collect_snapshots(watches)
    rendered = "\n\n".join(monitor.format_snapshot(snapshot) for snapshot in snapshots)
    return truncate_reply(rendered)


def configured_fiscal_company_keys() -> list[str]:
    tickers = market_hub.setting_list("fiscal_watchlist")
    if not tickers:
        tickers = ["MSFT", "NVDA", "AMZN", "GOOG", "TSLA"]
    return [market_hub.normalize_fiscal_company_key(item) for item in tickers]


def build_free_watchlist_text(raw_tickers: str | None = None) -> str:
    company_keys = (
        [market_hub.normalize_fiscal_company_key(item) for item in csv_items(raw_tickers)]
        if raw_tickers
        else configured_fiscal_company_keys()
    )
    api_key = market_hub.resolve_fiscal_api_key()
    return truncate_reply(market_hub.fiscal_watchlist_text(api_key, company_keys).strip())


def build_free_market_text() -> str:
    api_key = market_hub.resolve_fiscal_api_key()
    company_keys = configured_fiscal_company_keys()
    top_news_items = market_hub.fiscal_top_news(api_key, 6, importance_max=2)
    top_news_rows = (
        [
            [
                market_hub.safe_date_text(market_hub.first_text(item, ["publishedAt", "date", "collectedAt"])),
                market_hub.first_text(item, ["companyKey", "ticker", "symbol"]) or "-",
                str(int(market_hub.first_number(item, ["importance"]) or 0))
                if market_hub.first_number(item, ["importance"]) is not None
                else "-",
                market_hub.first_text(item, ["eventType", "category"]) or "-",
                market_hub.first_text(item, ["headline", "title", "summary", "text"])[:80] or "-",
            ]
            for item in top_news_items
        ]
        if top_news_items
        else market_hub.fiscal_watchlist_headline_rows(api_key, company_keys, 6)
    )
    snapshots = market_hub.fiscal_watchlist_snapshot(api_key, company_keys)
    output = market_hub.fiscal_market_brief_text(
        top_news_items=top_news_items,
        top_news_rows=top_news_rows,
        watchlist_rows=market_hub.fiscal_watchlist_rows_from_snapshots(snapshots),
        snapshots=snapshots,
        watchlist_news_lines=market_hub.fiscal_watchlist_news_lines(api_key, company_keys, 3),
    )
    return truncate_reply(output.strip())


def build_free_stock_text(ticker: str) -> str:
    api_key = market_hub.resolve_fiscal_api_key()
    company_key = market_hub.normalize_fiscal_company_key(ticker)
    output = market_hub.fiscal_stock_brief_text(
        company_key,
        market_hub.fiscal_company_profile(api_key, company_key),
        market_hub.fiscal_company_prices(api_key, company_key),
        market_hub.fiscal_company_news(api_key, company_key),
        market_hub.fiscal_company_filings(api_key, company_key),
    )
    return truncate_reply(output.strip())


def build_finchat_answer(question: str) -> str:
    api_key = market_hub.resolve_finchat_api_key()
    response = market_hub.call_finchat_api(
        api_key,
        market_hub.finchat_payload(
            question,
            extra_rules=["Keep the answer concise enough for chat delivery."],
        ),
    )
    answer, title, follow_ups = market_hub.extract_assistant_message(response)
    rendered = market_hub.render_finchat_output(answer, title, follow_ups).strip()
    return truncate_reply(rendered)


def pick_watch_keys(message: str) -> str | None:
    aliases = {
        "buffett": ["buffett", "巴菲特", "berkshire", "13f"],
        "buffett_company": ["buffett_company", "伯克希尔", "公司层面", "10-q", "10-k"],
        "duan": ["duan", "段永平"],
        "ackman": ["ackman", "阿克曼"],
        "tepper": ["tepper", "泰珀"],
        "lilu": ["lilu", "李录"],
        "pelosi": ["pelosi", "佩洛西", "国会"],
        "huang": ["huang", "黄仁勋", "jensen"],
    }
    lowered = message.lower()
    matched = [key for key, words in aliases.items() if any(word.lower() in lowered for word in words)]
    if not matched:
        return None
    if "buffett" in matched and "buffett_company" not in matched and "公司" in message:
        matched.append("buffett_company")
    return ",".join(dict.fromkeys(matched))


def dispatch_message(message: str) -> BotReply:
    normalized = normalize_message(message)
    lowered = normalized.lower()

    if not normalized:
        return BotReply(help_text(), "help")

    if lowered in {"/help", "help", "帮助", "菜单", "menu"}:
        return BotReply(help_text(), "help")

    if lowered in {"/watchlist", "watchlist", "监控列表"}:
        return BotReply(build_watchlist_text(), "watchlist")

    if lowered in {"/market", "市场简报", "今日市场", "市场"}:
        return BotReply(build_free_market_text(), "market")

    if lowered in {"/free-watchlist", "自选股", "自选股快照", "watchlist-free"}:
        return BotReply(build_free_watchlist_text(), "free-watchlist")

    if lowered in {"/filings", "全部披露", "最新披露", "披露"}:
        return BotReply(build_filing_preview(), "filings")

    if lowered.startswith("/filings "):
        return BotReply(build_filing_preview(normalized.split(" ", 1)[1]), "filings")

    stock_match = re.match(r"^/(?:stock)\s+(.+)$", normalized, re.I)
    if stock_match:
        return BotReply(build_free_stock_text(stock_match.group(1).strip()), "stock")

    zh_stock_match = re.match(r"^(?:个股|股票)\s+(.+)$", normalized, re.I)
    if zh_stock_match:
        return BotReply(build_free_stock_text(zh_stock_match.group(1).strip()), "stock")

    ask_match = re.match(r"^/(?:ask)\s+(.+)$", normalized, re.I)
    if ask_match:
        return BotReply(build_finchat_answer(ask_match.group(1).strip()), "ask")

    zh_ask_match = re.match(r"^(?:问一下[:：]?|问[:：]?)(.+)$", normalized, re.I)
    if zh_ask_match:
        return BotReply(build_finchat_answer(zh_ask_match.group(1).strip()), "ask")

    watch_keys = pick_watch_keys(normalized)
    if watch_keys:
        return BotReply(build_filing_preview(watch_keys), "filings")

    bare_ticker = re.fullmatch(r"[A-Za-z.\-]{1,12}", normalized)
    if bare_ticker:
        return BotReply(build_free_stock_text(normalized), "stock")

    if "市场" in normalized or "热点" in normalized:
        return BotReply(build_free_market_text(), "market")

    if "自选" in normalized:
        return BotReply(build_free_watchlist_text(), "free-watchlist")

    return BotReply(
        truncate_reply(
            "I treated this as a research question.\n\n" + build_finchat_answer(normalized)
        ),
        "ask",
    )


def handle_message(message: str) -> BotReply:
    try:
        return dispatch_message(message)
    except Exception as exc:
        hint = ""
        if "Fiscal.ai" in str(exc):
            hint = "\n\nHint: configure `fiscal_api_key` in `.cache/local_settings.json`."
        elif "FinChat" in str(exc) or "finchat" in str(exc):
            hint = "\n\nHint: configure `finchat_api_key` in `.cache/local_settings.json`."
        elif "Unknown watch key" in str(exc):
            hint = f"\n\nAvailable keys: {SUPPORTED_WATCH_HINT}."
        elif "urlopen error" in str(exc).lower() or "nodename nor servname provided" in str(exc).lower():
            hint = "\n\nHint: this command needs network access to reach SEC or market-data APIs."
        return BotReply(
            f"Request failed: {exc}{hint}\n\nTry `帮助` to see supported commands.",
            "error",
        )


def resolve_wechat_config() -> WeChatCallbackConfig:
    settings = market_hub.load_local_settings()
    token = os.environ.get("WECHAT_OFFICIAL_TOKEN") or settings.get("wechat_official_token")
    if not isinstance(token, str) or not token.strip():
        raise RuntimeError(
            "Missing WeChat Official Account token. Set WECHAT_OFFICIAL_TOKEN or add "
            '{"wechat_official_token":"..."} to .cache/local_settings.json.'
        )
    return WeChatCallbackConfig(token=token.strip())


def normalize_wechat_event_key(event_key: str) -> str:
    return event_key.strip().upper()


def resolve_wechat_menu_actions() -> dict[str, str]:
    settings = market_hub.load_local_settings()
    merged = dict(DEFAULT_WECHAT_MENU_ACTIONS)
    raw_mapping = settings.get("wechat_menu_actions")
    if not isinstance(raw_mapping, dict):
        return merged
    for raw_key, raw_value in raw_mapping.items():
        if not isinstance(raw_key, str) or not isinstance(raw_value, str):
            continue
        normalized_key = normalize_wechat_event_key(raw_key)
        if normalized_key and raw_value.strip():
            merged[normalized_key] = raw_value.strip()
    return merged


def wechat_signature(token: str, timestamp: str, nonce: str) -> str:
    joined = "".join(sorted([token, timestamp, nonce]))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()


def verify_wechat_signature(token: str, signature: str, timestamp: str, nonce: str) -> bool:
    if not all([signature, timestamp, nonce]):
        return False
    return wechat_signature(token, timestamp, nonce) == signature


def wechat_safe_text(text: str) -> str:
    cleaned = text.replace("```text", "").replace("```", "").strip()
    cleaned = cleaned.replace("\r\n", "\n")
    if len(cleaned) <= WECHAT_REPLY_CHARS:
        return cleaned
    return cleaned[: WECHAT_REPLY_CHARS - 40].rstrip() + "\n\n[内容较长，请到网页端查看完整结果]"


def wechat_help_text() -> str:
    return textwrap.dedent(
        """
        Open Investor Copilot

        可直接发送：
        1. 市场简报
        2. 全部披露
        3. 巴菲特最新披露
        4. 自选股快照
        5. 个股 NVDA
        6. 问一下：英伟达最近一个季度最重要的变化

        推荐菜单：
        - 帮助
        - 市场
        - 披露
        """
    ).strip()


def parse_wechat_message(xml_text: str) -> dict[str, str]:
    root = ET.fromstring(xml_text)
    data: dict[str, str] = {}
    for child in root:
        data[child.tag] = (child.text or "").strip()
    return data


def render_wechat_text_reply(message: dict[str, str], content: str) -> str:
    to_user = message.get("FromUserName", "").replace("]]>", "]]]]><![CDATA[>")
    from_user = message.get("ToUserName", "").replace("]]>", "]]]]><![CDATA[>")
    safe_content = content.replace("]]>", "]]]]><![CDATA[>")
    create_time = message.get("CreateTime") or "0"
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{to_user}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
        f"<CreateTime>{create_time}</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{safe_content}]]></Content>"
        "</xml>"
    )


def dispatch_wechat_menu_event(event_key: str) -> BotReply:
    raw_key = event_key.strip()
    normalized_key = normalize_wechat_event_key(raw_key)
    menu_actions = resolve_wechat_menu_actions()
    mapped_command = menu_actions.get(normalized_key)
    if mapped_command:
        return handle_message(mapped_command)
    if ":" in raw_key:
        prefix, value = raw_key.split(":", 1)
        normalized_prefix = prefix.strip().upper()
        payload = value.strip()
        if normalized_prefix in {"ASK", "QUERY"} and payload:
            return handle_message(f"问一下：{payload}")
        if normalized_prefix in {"STOCK", "TICKER"} and payload:
            return handle_message(f"个股 {payload}")
        if normalized_prefix in {"CMD", "PROMPT"} and payload:
            return handle_message(payload)
    return handle_message(raw_key)


def build_wechat_event_reply(message: dict[str, str]) -> str | None:
    event = message.get("Event", "").lower()
    if event == "subscribe":
        return wechat_help_text()
    if event == "click":
        event_key = message.get("EventKey", "").strip()
        if event_key:
            return wechat_safe_text(dispatch_wechat_menu_event(event_key).text)
        return "菜单点击已收到。你也可以直接发送“市场简报”或“巴菲特最新披露”。"
    return None


def build_wechat_reply(xml_text: str) -> str | None:
    message = parse_wechat_message(xml_text)
    msg_type = message.get("MsgType", "").lower()
    if msg_type == "event":
        reply = build_wechat_event_reply(message)
        if not reply:
            return ""
        return render_wechat_text_reply(message, reply)
    if msg_type != "text":
        return render_wechat_text_reply(
            message,
            "当前先支持文本消息。你可以发送“帮助”“市场简报”或“巴菲特最新披露”。",
        )
    content = message.get("Content", "")
    reply = wechat_safe_text(handle_message(content).text)
    return render_wechat_text_reply(message, reply)


def read_json_request(handler: BaseHTTPRequestHandler) -> dict[str, Any] | None:
    length = int(handler.headers.get("Content-Length", "0"))
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return None


def write_response(
    handler: BaseHTTPRequestHandler,
    status: HTTPStatus,
    body: bytes,
    content_type: str,
) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def write_json(handler: BaseHTTPRequestHandler, status: HTTPStatus, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    write_response(handler, status, body, "application/json; charset=utf-8")


def handle_chat_api(handler: BaseHTTPRequestHandler) -> None:
    payload = read_json_request(handler)
    if payload is None:
        handler.send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON payload")
        return

    message = str(payload.get("message", ""))
    reply = handle_message(message)
    write_json(
        handler,
        HTTPStatus.OK,
        {
            "ok": reply.command != "error",
            "command": reply.command,
            "reply": reply.text,
        },
    )


def verify_wechat_request(query: dict[str, list[str]]) -> tuple[WeChatCallbackConfig | None, str | None]:
    try:
        config = resolve_wechat_config()
    except RuntimeError as exc:
        return None, str(exc)

    signature = query.get("signature", [""])[0]
    timestamp = query.get("timestamp", [""])[0]
    nonce = query.get("nonce", [""])[0]
    if not verify_wechat_signature(config.token, signature, timestamp, nonce):
        return None, "Invalid WeChat signature."
    return config, None


def handle_wechat_get(handler: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> None:
    _, error = verify_wechat_request(query)
    if error:
        handler.send_error(HTTPStatus.FORBIDDEN, error)
        return
    echostr = query.get("echostr", [""])[0]
    write_response(handler, HTTPStatus.OK, echostr.encode("utf-8"), "text/plain; charset=utf-8")


def handle_wechat_post(handler: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> None:
    _, error = verify_wechat_request(query)
    if error:
        handler.send_error(HTTPStatus.FORBIDDEN, error)
        return
    encrypt_type = query.get("encrypt_type", ["raw"])[0]
    if encrypt_type and encrypt_type != "raw":
        handler.send_error(
            HTTPStatus.NOT_IMPLEMENTED,
            "Only plaintext/raw mode is supported by this open-source adapter today.",
        )
        return

    length = int(handler.headers.get("Content-Length", "0"))
    raw = handler.rfile.read(length)
    try:
        reply_xml = build_wechat_reply(raw.decode("utf-8"))
    except ET.ParseError:
        handler.send_error(HTTPStatus.BAD_REQUEST, "Invalid WeChat XML payload")
        return

    if reply_xml is None:
        reply_xml = ""
    write_response(
        handler,
        HTTPStatus.OK,
        reply_xml.encode("utf-8"),
        "application/xml; charset=utf-8",
    )


def html_page() -> str:
    return """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Open Investor Copilot</title>
    <style>
      :root {
        --bg: #f4efe4;
        --panel: rgba(252, 248, 240, 0.9);
        --panel-strong: #fffaf0;
        --ink: #1f2a33;
        --muted: #6d6256;
        --accent: #0f766e;
        --accent-2: #b45309;
        --line: rgba(31, 42, 51, 0.12);
        --shadow: 0 24px 80px rgba(68, 49, 25, 0.14);
      }

      * {
        box-sizing: border-box;
      }

      body {
        margin: 0;
        min-height: 100vh;
        background:
          radial-gradient(circle at top left, rgba(180, 83, 9, 0.16), transparent 30%),
          radial-gradient(circle at bottom right, rgba(15, 118, 110, 0.18), transparent 28%),
          linear-gradient(135deg, #f7f2e8 0%, #efe4d2 100%);
        color: var(--ink);
        font-family: "Avenir Next", "Trebuchet MS", sans-serif;
      }

      .shell {
        width: min(1080px, calc(100vw - 32px));
        margin: 28px auto;
        display: grid;
        grid-template-columns: minmax(240px, 320px) minmax(0, 1fr);
        gap: 18px;
      }

      .panel {
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: 28px;
        box-shadow: var(--shadow);
        backdrop-filter: blur(14px);
      }

      .sidebar {
        padding: 24px;
        display: flex;
        flex-direction: column;
        gap: 18px;
      }

      .eyebrow {
        font-size: 12px;
        letter-spacing: 0.18em;
        text-transform: uppercase;
        color: var(--accent-2);
      }

      h1 {
        margin: 0;
        font-family: Georgia, "Times New Roman", serif;
        font-size: clamp(32px, 5vw, 52px);
        line-height: 0.95;
      }

      .lede {
        margin: 0;
        color: var(--muted);
        line-height: 1.55;
      }

      .pill-list {
        display: flex;
        flex-wrap: wrap;
        gap: 10px;
      }

      .pill {
        border: 0;
        border-radius: 999px;
        padding: 10px 14px;
        background: rgba(15, 118, 110, 0.1);
        color: var(--accent);
        font: inherit;
        cursor: pointer;
        transition: transform 180ms ease, background 180ms ease;
      }

      .pill:hover {
        transform: translateY(-1px);
        background: rgba(15, 118, 110, 0.16);
      }

      .chat {
        padding: 22px;
        display: flex;
        flex-direction: column;
        min-height: 80vh;
      }

      .chat-head {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 16px;
        margin-bottom: 18px;
      }

      .badge {
        border-radius: 999px;
        padding: 8px 12px;
        background: rgba(180, 83, 9, 0.12);
        color: var(--accent-2);
        font-size: 13px;
      }

      #messages {
        flex: 1;
        overflow: auto;
        display: flex;
        flex-direction: column;
        gap: 14px;
        padding-right: 6px;
      }

      .msg {
        max-width: min(720px, 100%);
        padding: 16px 18px;
        border-radius: 24px;
        white-space: pre-wrap;
        animation: rise 220ms ease;
        line-height: 1.5;
      }

      .msg.user {
        align-self: flex-end;
        background: linear-gradient(135deg, #0f766e, #115e59);
        color: #f5fffd;
        border-bottom-right-radius: 8px;
      }

      .msg.bot {
        align-self: flex-start;
        background: var(--panel-strong);
        border: 1px solid var(--line);
        border-bottom-left-radius: 8px;
      }

      .composer {
        margin-top: 18px;
        display: grid;
        grid-template-columns: minmax(0, 1fr) auto;
        gap: 12px;
      }

      textarea {
        resize: vertical;
        min-height: 92px;
        max-height: 200px;
        border-radius: 20px;
        border: 1px solid var(--line);
        background: rgba(255, 252, 245, 0.9);
        padding: 16px 18px;
        font: inherit;
        color: var(--ink);
      }

      button.primary {
        align-self: end;
        border: 0;
        border-radius: 18px;
        padding: 14px 18px;
        background: linear-gradient(135deg, #b45309, #92400e);
        color: #fff8ef;
        font: inherit;
        cursor: pointer;
      }

      .foot {
        color: var(--muted);
        font-size: 13px;
      }

      @keyframes rise {
        from {
          opacity: 0;
          transform: translateY(6px);
        }
        to {
          opacity: 1;
          transform: translateY(0);
        }
      }

      @media (max-width: 860px) {
        .shell {
          grid-template-columns: 1fr;
        }

        .chat {
          min-height: 60vh;
        }

        .composer {
          grid-template-columns: 1fr;
        }

        button.primary {
          width: 100%;
        }
      }
    </style>
  </head>
  <body>
    <main class="shell">
      <section class="panel sidebar">
        <div>
          <div class="eyebrow">Web + WeChat Ready</div>
          <h1>Open Investor Copilot</h1>
        </div>
        <p class="lede">
          A chat-first investing assistant built from official filings, free market snapshots, and
          API-friendly command routing. This web surface is the same core you can later wire into a
          WeChat-facing gateway.
        </p>
        <div>
          <div class="eyebrow">Quick Prompts</div>
          <div class="pill-list">
            <button class="pill" data-prompt="帮助">帮助</button>
            <button class="pill" data-prompt="全部披露">全部披露</button>
            <button class="pill" data-prompt="巴菲特最新披露">巴菲特最新披露</button>
            <button class="pill" data-prompt="市场简报">市场简报</button>
            <button class="pill" data-prompt="自选股快照">自选股快照</button>
            <button class="pill" data-prompt="个股 NVDA">个股 NVDA</button>
          </div>
        </div>
        <p class="foot">
          JSON endpoint: <code>POST /api/chat</code><br>
          Payload: <code>{"message":"市场简报"}</code>
        </p>
      </section>

      <section class="panel chat">
        <div class="chat-head">
          <div>
            <div class="eyebrow">Interactive Surface</div>
            <strong>Chat locally, then reuse the same command core in a bot adapter.</strong>
          </div>
          <span class="badge">Slash commands supported</span>
        </div>

        <div id="messages"></div>

        <form id="composer" class="composer">
          <textarea id="message" placeholder="Ask for filings, market briefs, stock snapshots, or a research question."></textarea>
          <button class="primary" type="submit">Send</button>
        </form>
      </section>
    </main>

    <script>
      const messages = document.getElementById("messages");
      const composer = document.getElementById("composer");
      const input = document.getElementById("message");

      function addMessage(role, text) {
        const el = document.createElement("div");
        el.className = `msg ${role}`;
        el.textContent = text;
        messages.appendChild(el);
        messages.scrollTop = messages.scrollHeight;
      }

      async function sendMessage(text) {
        addMessage("user", text);
        input.value = "";
        try {
          const response = await fetch("/api/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ message: text })
          });
          const payload = await response.json();
          addMessage("bot", payload.reply || "No reply returned.");
        } catch (error) {
          addMessage("bot", `Request failed: ${error.message}`);
        }
      }

      composer.addEventListener("submit", (event) => {
        event.preventDefault();
        const text = input.value.trim();
        if (!text) {
          return;
        }
        sendMessage(text);
      });

      document.querySelectorAll("[data-prompt]").forEach((button) => {
        button.addEventListener("click", () => {
          const text = button.getAttribute("data-prompt");
          input.value = text;
          sendMessage(text);
        });
      });

      addMessage(
        "bot",
        "Start with `帮助`, or try `市场简报` / `巴菲特最新披露` / `个股 NVDA`."
      );
    </script>
  </body>
</html>
"""


class BotHTTPRequestHandler(BaseHTTPRequestHandler):
    server_version = "OpenInvestorCopilot/0.1"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == WECHAT_CALLBACK_PATH:
            handle_wechat_get(self, urllib.parse.parse_qs(parsed.query))
            return
        if parsed.path not in {"/", "/index.html"}:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        body = html_page().encode("utf-8")
        write_response(self, HTTPStatus.OK, body, "text/html; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == WECHAT_CALLBACK_PATH:
            handle_wechat_post(self, urllib.parse.parse_qs(parsed.query))
            return
        if parsed.path != "/api/chat":
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        handle_chat_api(self)

    def log_message(self, format: str, *args: Any) -> None:
        return


def run_server(host: str, port: int) -> int:
    server = ThreadingHTTPServer((host, port), BotHTTPRequestHandler)
    print(f"Open Investor Copilot listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()
    return 0


def main() -> int:
    args = parse_args()
    if args.command == "chat":
        reply = handle_message(args.message)
        print(reply.text)
        return 0 if reply.command != "error" else 1
    if args.command == "serve":
        return run_server(args.host, args.port)
    raise RuntimeError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
