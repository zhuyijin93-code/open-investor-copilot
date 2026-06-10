#!/usr/bin/env python3
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import textwrap
import urllib.parse
import xml.etree.ElementTree as ET
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    from . import data_sources, quant_engine
except ImportError:  # pragma: no cover - allows `python3 quant_wechat_bot/bot_service.py serve`
    import data_sources  # type: ignore
    import quant_engine  # type: ignore


HOST = "127.0.0.1"
PORT = 8790
MAX_REPLY_CHARS = 3600
WECHAT_REPLY_CHARS = 1200
WECHAT_CALLBACK_PATH = "/wechat/callback"
PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_SETTINGS_PATH = PROJECT_ROOT / ".cache" / "local_settings.json"
DEFAULT_WECHAT_MENU_ACTIONS = {
    "MENU_HELP": "帮助",
    "MENU_PICK_QUALITY": "选股 质量",
    "MENU_PICK_MOMENTUM": "选股 动量",
    "MENU_PICK_VALUE": "选股 价值",
    "MENU_SCORE_NVDA": "评分 NVDA",
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
        description="Interactive quant stock-picking bot with web chat and WeChat callback support."
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
        """
        Quant WeChat Bot

        可直接发送:
        1. 帮助
        2. 策略列表
        3. 选股 质量
        4. 选股 动量
        5. 选股 价值
        6. 选股 低波
        7. 选股 质量 A股
        8. 评分 NVDA
        9. 评分 600519 A股
        10. 股票池
        11. 股票池 A股

        Slash commands:
        - /help
        - /strategies
        - /pick <quality|momentum|value|defensive> [market] [top_n]
        - /score <ticker> [market]
        - /universe [market]

        提醒:
        - `样本池` 是仓库自带的小样本
        - `A股` 默认拉取全量免费行情股票池
        - 这不是投资建议
        """
    ).strip()


def normalize_message(message: str) -> str:
    return re.sub(r"\s+", " ", message).strip()


def truncate_reply(text: str) -> str:
    if len(text) <= MAX_REPLY_CHARS:
        return text
    clipped = text[: MAX_REPLY_CHARS - 120].rstrip()
    return clipped + "\n\n[Truncated for chat delivery. Open the web view for the full result.]"


def load_local_settings() -> dict[str, Any]:
    env_path = os.environ.get("QUANT_WECHAT_SETTINGS")
    candidate = Path(env_path).expanduser() if env_path else DEFAULT_SETTINGS_PATH
    if not candidate.exists():
        return {}
    return json.loads(candidate.read_text(encoding="utf-8"))


def resolve_universe_path(market: str | None = None) -> Path:
    settings = load_local_settings()
    requested_market = market if market is not None else resolve_default_market()
    normalized_market = normalize_market(requested_market)
    if normalized_market == "a":
        configured_a = settings.get("a_share_universe_csv") if isinstance(settings, dict) else None
        if isinstance(configured_a, str) and configured_a.strip():
            candidate = Path(configured_a.strip()).expanduser()
            if not candidate.is_absolute():
                candidate = PROJECT_ROOT / configured_a.strip()
        else:
            candidate = PROJECT_ROOT / ".cache" / "a_share_universe.csv"
        limit = int(settings.get("a_share_limit", 0)) if isinstance(settings, dict) else 0
        min_amount_yuan = float(settings.get("a_share_min_amount_yuan", 0)) if isinstance(settings, dict) else 0.0
        max_age_seconds = int(settings.get("a_share_cache_seconds", 900)) if isinstance(settings, dict) else 900
        return data_sources.refresh_a_share_universe(
            candidate,
            limit=limit,
            min_amount_yuan=min_amount_yuan,
            max_age_seconds=max_age_seconds,
        )

    configured = settings.get("universe_csv") if isinstance(settings, dict) else None
    if isinstance(configured, str) and configured.strip():
        candidate = Path(configured.strip()).expanduser()
        if not candidate.is_absolute():
            candidate = PROJECT_ROOT / configured.strip()
        return candidate
    return PROJECT_ROOT / "sample_universe.csv"


def normalize_market(value: str | None) -> str:
    if value is None:
        return "sample"
    normalized = value.strip().lower()
    if normalized in {"a", "a股", "ashare", "a-share", "cn", "china", "沪深", "中国"}:
        return "a"
    if normalized in {"sample", "样本", "美股", "us", "usa"}:
        return "sample"
    return normalized


def resolve_default_strategy() -> str:
    settings = load_local_settings()
    value = settings.get("default_strategy") if isinstance(settings, dict) else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "quality"


def resolve_default_market() -> str:
    settings = load_local_settings()
    value = settings.get("default_market") if isinstance(settings, dict) else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "sample"


def build_strategy_list_text() -> str:
    return truncate_reply(
        quant_engine.format_strategy_catalog()
        + "\n\n市场用法:\n- 选股 质量 A股\n- 评分 600519 A股\n- 股票池 A股"
    )


def build_screen_text(strategy_name: str | None = None, top_n: int = 5, market: str | None = None) -> str:
    chosen = strategy_name or resolve_default_strategy()
    effective_market = market if market is not None else resolve_default_market()
    return truncate_reply(
        quant_engine.format_screen_output(
            resolve_universe_path(effective_market),
            chosen,
            top_n=top_n,
            market=effective_market,
        )
    )


def build_stock_report_text(ticker: str, market: str | None = None) -> str:
    effective_market = market if market is not None else resolve_default_market()
    return truncate_reply(
        quant_engine.format_stock_report(
            resolve_universe_path(effective_market),
            ticker,
            market=effective_market,
        )
    )


def build_universe_overview_text(market: str | None = None) -> str:
    effective_market = market if market is not None else resolve_default_market()
    return truncate_reply(
        quant_engine.format_universe_overview(
            resolve_universe_path(effective_market),
            market=effective_market,
        )
    )


def dispatch_message(message: str) -> BotReply:
    normalized = normalize_message(message)
    lowered = normalized.lower()

    if not normalized:
        return BotReply(help_text(), "help")

    if lowered in {"/help", "help", "帮助", "菜单", "menu"}:
        return BotReply(help_text(), "help")

    if lowered in {"/strategies", "策略列表", "策略", "选股策略"}:
        return BotReply(build_strategy_list_text(), "strategies")

    universe_match = re.match(r"^(?:/universe|股票池|样本池)(?:\s+([^\s]+))?$", normalized, re.I)
    if universe_match:
        return BotReply(build_universe_overview_text(universe_match.group(1)), "universe")

    pick_match = re.match(r"^/(?:pick)\s+([^\s]+)(?:\s+([^\s\d]+))?(?:\s+(\d+))?$", normalized, re.I)
    if pick_match:
        top_n = int(pick_match.group(3) or "5")
        return BotReply(build_screen_text(pick_match.group(1), top_n=top_n, market=pick_match.group(2)), "pick")

    zh_pick_match = re.match(r"^(?:选股|推荐)\s+([^\s]+)(?:\s+([^\s\d]+))?(?:\s+(\d+))?$", normalized, re.I)
    if zh_pick_match:
        top_n = int(zh_pick_match.group(3) or "5")
        return BotReply(build_screen_text(zh_pick_match.group(1), top_n=top_n, market=zh_pick_match.group(2)), "pick")

    score_match = re.match(r"^/(?:score)\s+([^\s]+)(?:\s+([^\s]+))?$", normalized, re.I)
    if score_match:
        return BotReply(build_stock_report_text(score_match.group(1).strip(), market=score_match.group(2)), "score")

    zh_score_match = re.match(r"^(?:评分|打分|分析)\s+([^\s]+)(?:\s+([^\s]+))?$", normalized, re.I)
    if zh_score_match:
        return BotReply(build_stock_report_text(zh_score_match.group(1).strip(), market=zh_score_match.group(2)), "score")

    bare_ticker = re.fullmatch(r"[A-Za-z.\-]{1,12}", normalized)
    if bare_ticker:
        return BotReply(build_stock_report_text(normalized), "score")

    if "质量" in normalized:
        return BotReply(build_screen_text("quality"), "pick")
    if "动量" in normalized or "趋势" in normalized:
        return BotReply(build_screen_text("momentum"), "pick")
    if "价值" in normalized:
        return BotReply(build_screen_text("value"), "pick")
    if "低波" in normalized or "防守" in normalized:
        return BotReply(build_screen_text("defensive"), "pick")

    return BotReply(
        "我当前更擅长结构化的量化选股命令。\n\n试试:\n- 策略列表\n- 选股 质量 A股\n- 选股 动量 A股\n- 评分 600519 A股\n- 股票池 A股",
        "help",
    )


def handle_message(message: str) -> BotReply:
    try:
        return dispatch_message(message)
    except Exception as exc:
        hint = ""
        if "Unknown strategy" in str(exc):
            hint = "\n\n可用策略: 质量 / 动量 / 价值 / 低波"
        elif "Ticker" in str(exc):
            hint = "\n\n提示: 先发送 `股票池` 或 `股票池 A股` 看当前股票池里有哪些代码。"
        elif "urlopen error" in str(exc).lower() or "timed out" in str(exc).lower() or "eastmoney" in str(exc).lower() or "sina" in str(exc).lower():
            hint = "\n\n提示: A股全量股票池需要联网拉取免费行情快照。你也可以先试 `选股 质量 样本`。"
        return BotReply(
            f"Request failed: {exc}{hint}\n\n试试 `帮助` 查看支持的命令。",
            "error",
        )


def resolve_wechat_config() -> WeChatCallbackConfig:
    settings = load_local_settings()
    token = os.environ.get("WECHAT_OFFICIAL_TOKEN") or settings.get("wechat_official_token")
    if not isinstance(token, str) or not token.strip():
        raise RuntimeError(
            "Missing WeChat Official Account token. Set WECHAT_OFFICIAL_TOKEN or add "
            '{"wechat_official_token":"..."} to quant_wechat_bot/.cache/local_settings.json.'
        )
    return WeChatCallbackConfig(token=token.strip())


def normalize_wechat_event_key(event_key: str) -> str:
    return event_key.strip().upper()


def resolve_wechat_menu_actions() -> dict[str, str]:
    settings = load_local_settings()
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
    cleaned = text.replace("\r\n", "\n").strip()
    if len(cleaned) <= WECHAT_REPLY_CHARS:
        return cleaned
    return cleaned[: WECHAT_REPLY_CHARS - 40].rstrip() + "\n\n[内容较长，请到网页端查看完整结果]"


def wechat_help_text() -> str:
    return textwrap.dedent(
        """
        Quant WeChat Bot

        可直接发送:
        1. 策略列表
        2. 选股 质量
        3. 选股 动量
        4. 选股 价值
        5. 评分 NVDA

        推荐菜单:
        - 帮助
        - 选股
        - 评分
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
        if normalized_prefix in {"STOCK", "TICKER"} and payload:
            return handle_message(f"评分 {payload}")
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
        return "菜单点击已收到。你也可以直接发送 `选股 质量` 或 `评分 NVDA`。"
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
            "当前先支持文本消息。你可以发送 `策略列表`、`选股 质量` 或 `评分 NVDA`。",
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
<html lang="zh-CN">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Quant WeChat Bot</title>
    <style>
      :root {
        --bg: #f3f6f2;
        --panel: rgba(248, 251, 247, 0.92);
        --panel-strong: #ffffff;
        --ink: #16231d;
        --muted: #5a6a62;
        --accent: #176b4d;
        --accent-2: #c67a2c;
        --line: rgba(22, 35, 29, 0.1);
        --shadow: 0 24px 80px rgba(37, 66, 52, 0.14);
      }

      * {
        box-sizing: border-box;
      }

      body {
        margin: 0;
        min-height: 100vh;
        background:
          radial-gradient(circle at top left, rgba(198, 122, 44, 0.16), transparent 30%),
          radial-gradient(circle at bottom right, rgba(23, 107, 77, 0.14), transparent 28%),
          linear-gradient(135deg, #f4f8f1 0%, #e8efe7 100%);
        color: var(--ink);
        font-family: "Avenir Next", "PingFang SC", "Microsoft YaHei", sans-serif;
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
        line-height: 1.58;
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
        background: rgba(23, 107, 77, 0.1);
        color: var(--accent);
        font: inherit;
        cursor: pointer;
        transition: transform 180ms ease, background 180ms ease;
      }

      .pill:hover {
        transform: translateY(-1px);
        background: rgba(23, 107, 77, 0.16);
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
        background: rgba(198, 122, 44, 0.12);
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
        line-height: 1.55;
      }

      .msg.user {
        align-self: flex-end;
        background: linear-gradient(135deg, #176b4d, #14573f);
        color: #f4fff8;
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
        background: rgba(255, 255, 255, 0.92);
        padding: 16px 18px;
        font: inherit;
        color: var(--ink);
      }

      button.primary {
        align-self: end;
        border: 0;
        border-radius: 18px;
        padding: 14px 18px;
        background: linear-gradient(135deg, #c67a2c, #9f5f1c);
        color: #fff9f1;
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
          <h1>Quant WeChat Bot</h1>
        </div>
        <p class="lede">
          一个面向个人开发者和公开仓库的量化选股机器人 MVP。它把同一套策略命令同时暴露给本地网页、
          JSON API 和微信回调，方便你后面继续接自己的数据源或公众号。
        </p>
        <div>
          <div class="eyebrow">Quick Prompts</div>
          <div class="pill-list">
            <button class="pill" data-prompt="帮助">帮助</button>
            <button class="pill" data-prompt="策略列表">策略列表</button>
            <button class="pill" data-prompt="选股 质量">选股 质量</button>
            <button class="pill" data-prompt="选股 动量">选股 动量</button>
            <button class="pill" data-prompt="选股 价值">选股 价值</button>
            <button class="pill" data-prompt="评分 NVDA">评分 NVDA</button>
          </div>
        </div>
        <p class="foot">
          JSON endpoint: <code>POST /api/chat</code><br>
          Payload: <code>{"message":"选股 质量"}</code>
        </p>
      </section>

      <section class="panel chat">
        <div class="chat-head">
          <div>
            <div class="eyebrow">Interactive Surface</div>
            <strong>先在浏览器里测策略，再把同一套命令接到微信。</strong>
          </div>
          <span class="badge">Quant MVP</span>
        </div>

        <div id="messages"></div>

        <form id="composer" class="composer">
          <textarea id="message" placeholder="试试：选股 质量 / 选股 动量 / 评分 NVDA"></textarea>
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
        "先试 `策略列表`，或者直接发 `选股 质量` / `评分 NVDA`。"
      );
    </script>
  </body>
</html>
"""


class BotHTTPRequestHandler(BaseHTTPRequestHandler):
    server_version = "QuantWeChatBot/0.1"

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
    print(f"Quant WeChat Bot listening on http://{host}:{port}")
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
