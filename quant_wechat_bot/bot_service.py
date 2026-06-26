#!/usr/bin/env python3
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import threading
import textwrap
import urllib.parse
import xml.etree.ElementTree as ET
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    from . import data_sources, quant_engine, trend_strategy
except ImportError:  # pragma: no cover - allows `python3 quant_wechat_bot/bot_service.py serve`
    import data_sources  # type: ignore
    import quant_engine  # type: ignore
    import trend_strategy  # type: ignore


DEFAULT_LOCAL_HOST = "127.0.0.1"
DEFAULT_PUBLIC_HOST = "0.0.0.0"
DEFAULT_PORT = 8790
MAX_REPLY_CHARS = 3600
WECHAT_REPLY_CHARS = 1200
WECHAT_CALLBACK_PATH = "/wechat/callback"
PROJECT_ROOT = Path(__file__).resolve().parent
SNAPSHOT_ROOT = PROJECT_ROOT / "universe_snapshots"
DEFAULT_SETTINGS_PATH = PROJECT_ROOT / ".cache" / "local_settings.json"
TREND_CACHE_ROOT = PROJECT_ROOT / ".cache" / "trend_precomputed"
TREND_CACHE_SCHEMA_VERSION = 2
DEFAULT_TREND_PRECOMPUTE_MARKETS = ("全市场", "A股", "美股", "港股")
DEFAULT_TREND_PRECOMPUTE_TOP_N = (2, 5)
DEFAULT_TREND_PRECOMPUTE_MONTHS = (3, 6, 12)
DEFAULT_TREND_PRECOMPUTE_MAX_AGE_MINUTES = 24 * 60
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


@dataclasses.dataclass(frozen=True)
class TrendPrecomputeConfig:
    enabled: bool
    warm_on_startup: bool
    markets: tuple[str, ...]
    top_n_values: tuple[int, ...]
    backtest_months: tuple[int, ...]
    max_age_minutes: int


@dataclasses.dataclass(frozen=True)
class CachedTrendReply:
    text: str
    generated_at: dt.datetime
    stale: bool


TREND_PRECOMPUTE_STATE_LOCK = threading.Lock()
TREND_PRECOMPUTE_RUNNING = False
TREND_PRECOMPUTE_LAST_ERROR: str | None = None


def resolve_bind_host() -> str:
    configured = os.environ.get("QUANT_WECHAT_HOST") or os.environ.get("HOST")
    if configured and configured.strip():
        return configured.strip()
    if os.environ.get("PORT"):
        return DEFAULT_PUBLIC_HOST
    return DEFAULT_LOCAL_HOST


def resolve_bind_port() -> int:
    configured = os.environ.get("QUANT_WECHAT_PORT") or os.environ.get("PORT")
    if configured and configured.strip():
        return int(configured.strip())
    return DEFAULT_PORT


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Interactive quant stock-picking bot with web chat and WeChat callback support."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="Run the local web chat and JSON API.")
    default_host = resolve_bind_host()
    default_port = resolve_bind_port()
    serve_parser.add_argument("--host", default=default_host, help=f"Bind host. Default: {default_host}.")
    serve_parser.add_argument("--port", type=int, default=default_port, help=f"Bind port. Default: {default_port}.")

    chat_parser = subparsers.add_parser("chat", help="Run one chat turn from the terminal.")
    chat_parser.add_argument("message", help="User message to process.")

    precompute_parser = subparsers.add_parser("precompute-trend", help="Build cached trend and backtest replies.")
    precompute_parser.add_argument(
        "--markets",
        nargs="*",
        help="Markets to precompute. Defaults to the configured trend_precompute.markets list.",
    )
    precompute_parser.add_argument(
        "--top-n",
        nargs="*",
        type=int,
        dest="top_n_values",
        help="Trend top-N variants to precompute. Defaults to the configured trend_precompute.top_n_values list.",
    )
    precompute_parser.add_argument(
        "--months",
        nargs="*",
        type=int,
        dest="backtest_months",
        help="Backtest month variants to precompute. Defaults to the configured trend_precompute.backtest_months list.",
    )

    export_parser = subparsers.add_parser("export-trade-plan", help="Export the next-day trade plan as CSV.")
    export_parser.add_argument("--market", default=None, help="Market label, for example A股 / 港股 / 美股 / 全市场.")
    export_parser.add_argument("--top-n", type=int, default=5, help="Target number of model holdings. Default: 5.")
    export_parser.add_argument("--output", required=True, help="CSV output path.")

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
        8. 选股 质量 港股
        9. 选股 质量 美股
        10. 选股 质量 全市场
        11. 评分 NVDA 美股
        12. 评分 00700.HK 港股
        13. 评分 600519 A股
        14. 股票池
        15. 股票池 全市场
        16. 收盘总结 A股
        17. 收盘总结 港股
        18. 收盘总结 美股
        19. 趋势选股 A股
        20. 趋势回测 A股 12
        21. 交易计划 A股

        Slash commands:
        - /help
        - /strategies
        - /pick <quality|momentum|value|defensive> [market] [top_n]
        - /score <ticker> [market]
        - /universe [market]
        - /close [market]
        - /trend [market] [top_n]
        - /backtest [market] [months]
        - /plan [market] [top_n]

        提醒:
        - `样本池` 是仓库自带的小样本
        - `A股 / 港股 / 美股 / 全市场` 都支持免费行情股票池
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


def read_env_setting(name: str) -> str | None:
    value = os.environ.get(name)
    if value and value.strip():
        return value.strip()
    return None


def is_render_deployment() -> bool:
    return any(
        read_env_setting(name)
        for name in ("RENDER", "RENDER_SERVICE_ID", "RENDER_EXTERNAL_URL", "RENDER_SERVICE_NAME")
    )


def resolve_configured_path(settings: dict[str, Any], key: str, fallback_name: str) -> Path:
    configured = settings.get(key) if isinstance(settings, dict) else None
    if isinstance(configured, str) and configured.strip():
        candidate = Path(configured.strip()).expanduser()
        if not candidate.is_absolute():
            candidate = PROJECT_ROOT / configured.strip()
        return candidate
    return PROJECT_ROOT / ".cache" / fallback_name


def resolve_snapshot_path(name: str) -> Path:
    return SNAPSHOT_ROOT / name


def prefer_bundled_universes() -> bool:
    override = read_env_setting("QUANT_WECHAT_PREFER_BUNDLED_UNIVERSES")
    if override is not None:
        return override.strip().lower() not in {"0", "false", "no", "off"}
    return is_render_deployment()


def resolve_bundled_universe_path(market: str) -> Path | None:
    snapshot_map = {
        "a": resolve_snapshot_path("a_share_snapshot.csv"),
        "hk": resolve_snapshot_path("hk_share_snapshot.csv"),
        "us": resolve_snapshot_path("us_share_snapshot.csv"),
        "all": resolve_snapshot_path("global_snapshot.csv"),
        "global": resolve_snapshot_path("global_snapshot.csv"),
    }
    candidate = snapshot_map.get(market)
    if candidate and candidate.exists():
        return candidate
    return None


def resolve_universe_path(market: str | None = None) -> Path:
    settings = load_local_settings()
    requested_market = market if market is not None else resolve_default_market()
    normalized_market = normalize_market(requested_market)
    if prefer_bundled_universes():
        bundled = resolve_bundled_universe_path(normalized_market)
        if bundled is not None:
            return bundled
    if normalized_market == "a":
        candidate = resolve_configured_path(settings, "a_share_universe_csv", "a_share_universe.csv")
        limit = int(settings.get("a_share_limit", 0)) if isinstance(settings, dict) else 0
        min_amount_yuan = float(settings.get("a_share_min_amount_yuan", 0)) if isinstance(settings, dict) else 0.0
        max_age_seconds = int(settings.get("a_share_cache_seconds", 900)) if isinstance(settings, dict) else 900
        return data_sources.refresh_a_share_universe(
            candidate,
            limit=limit,
            min_amount_yuan=min_amount_yuan,
            max_age_seconds=max_age_seconds,
        )
    if normalized_market == "hk":
        candidate = resolve_configured_path(settings, "hk_share_universe_csv", "hk_share_universe.v3.csv")
        limit = int(settings.get("hk_share_limit", 0)) if isinstance(settings, dict) else 0
        min_amount_hkd = float(settings.get("hk_share_min_amount_hkd", 0)) if isinstance(settings, dict) else 0.0
        max_age_seconds = int(settings.get("hk_share_cache_seconds", 1800)) if isinstance(settings, dict) else 1800
        return data_sources.refresh_hk_share_universe(
            candidate,
            limit=limit,
            min_amount_hkd=min_amount_hkd,
            max_age_seconds=max_age_seconds,
        )
    if normalized_market == "us":
        candidate = resolve_configured_path(settings, "us_share_universe_csv", "us_share_universe.v3.csv")
        limit = int(settings.get("us_share_limit", 0)) if isinstance(settings, dict) else 0
        min_amount_usd = float(settings.get("us_share_min_amount_usd", 0)) if isinstance(settings, dict) else 0.0
        max_age_seconds = int(settings.get("us_share_cache_seconds", 1800)) if isinstance(settings, dict) else 1800
        return data_sources.refresh_us_share_universe(
            candidate,
            limit=limit,
            min_amount_usd=min_amount_usd,
            max_age_seconds=max_age_seconds,
        )
    if normalized_market in {"all", "global"}:
        candidate = resolve_configured_path(settings, "global_universe_csv", "global_universe.v4.csv")
        a_share_path = resolve_configured_path(settings, "a_share_universe_csv", "a_share_universe.csv")
        hk_share_path = resolve_configured_path(settings, "hk_share_universe_csv", "hk_share_universe.v3.csv")
        us_share_path = resolve_configured_path(settings, "us_share_universe_csv", "us_share_universe.v3.csv")
        return data_sources.refresh_global_universe(
            candidate,
            a_share_path=a_share_path,
            hk_share_path=hk_share_path,
            us_share_path=us_share_path,
            a_share_limit=int(settings.get("a_share_limit", 0)) if isinstance(settings, dict) else 0,
            hk_share_limit=int(settings.get("hk_share_limit", 0)) if isinstance(settings, dict) else 0,
            us_share_limit=int(settings.get("us_share_limit", 0)) if isinstance(settings, dict) else 0,
            a_share_min_amount_yuan=float(settings.get("a_share_min_amount_yuan", 0)) if isinstance(settings, dict) else 0.0,
            hk_share_min_amount_hkd=float(settings.get("hk_share_min_amount_hkd", 0)) if isinstance(settings, dict) else 0.0,
            us_share_min_amount_usd=float(settings.get("us_share_min_amount_usd", 0)) if isinstance(settings, dict) else 0.0,
            a_share_cache_seconds=int(settings.get("a_share_cache_seconds", 900)) if isinstance(settings, dict) else 900,
            hk_share_cache_seconds=int(settings.get("hk_share_cache_seconds", 1800)) if isinstance(settings, dict) else 1800,
            us_share_cache_seconds=int(settings.get("us_share_cache_seconds", 1800)) if isinstance(settings, dict) else 1800,
            max_age_seconds=int(settings.get("global_cache_seconds", 1800)) if isinstance(settings, dict) else 1800,
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
    if normalized in {"hk", "港股", "hongkong", "hong-kong"}:
        return "hk"
    if normalized in {"us", "usa", "美股", "美"}:
        return "us"
    if normalized in {"all", "global", "world", "全市场", "全部", "所有", "全球", "a+h+us", "ahus"}:
        return "all"
    if normalized in {"sample", "样本", "样本池"}:
        return "sample"
    return normalized


def resolve_default_strategy() -> str:
    env_value = read_env_setting("QUANT_WECHAT_DEFAULT_STRATEGY")
    if env_value is not None:
        return env_value
    settings = load_local_settings()
    value = settings.get("default_strategy") if isinstance(settings, dict) else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "quality"


def resolve_default_market() -> str:
    env_value = read_env_setting("QUANT_WECHAT_DEFAULT_MARKET")
    if env_value is not None:
        return env_value
    settings = load_local_settings()
    value = settings.get("default_market") if isinstance(settings, dict) else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    if is_render_deployment():
        return "全市场"
    return "sample"


def display_market_label(value: str | None) -> str:
    normalized = normalize_market(value)
    mapping = {
        "a": "A股",
        "hk": "港股",
        "us": "美股",
        "all": "全市场",
        "global": "全市场",
    }
    if normalized in mapping:
        return mapping[normalized]
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "全市场"


def parse_boolish(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip():
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return default


def positive_int_tuple(values: object, default: tuple[int, ...]) -> tuple[int, ...]:
    if not isinstance(values, list):
        return default
    parsed: list[int] = []
    for item in values:
        if isinstance(item, int) and item > 0 and item not in parsed:
            parsed.append(item)
    return tuple(parsed) or default


def market_tuple(values: object, default: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(values, list):
        return default
    parsed: list[str] = []
    for item in values:
        if not isinstance(item, str) or not item.strip():
            continue
        label = display_market_label(item)
        if label not in parsed and normalize_market(label) in {"a", "hk", "us", "all"}:
            parsed.append(label)
    return tuple(parsed) or default


def load_trend_precompute_config() -> TrendPrecomputeConfig:
    settings = load_local_settings()
    payload = settings.get("trend_precompute") if isinstance(settings, dict) else None
    enabled = is_render_deployment()
    warm_on_startup = enabled
    markets = DEFAULT_TREND_PRECOMPUTE_MARKETS
    top_n_values = DEFAULT_TREND_PRECOMPUTE_TOP_N
    backtest_months = DEFAULT_TREND_PRECOMPUTE_MONTHS
    max_age_minutes = DEFAULT_TREND_PRECOMPUTE_MAX_AGE_MINUTES
    if isinstance(payload, dict):
        enabled = parse_boolish(payload.get("enabled"), enabled)
        warm_on_startup = parse_boolish(payload.get("warm_on_startup"), warm_on_startup)
        markets = market_tuple(payload.get("markets"), markets)
        top_n_values = positive_int_tuple(payload.get("top_n_values"), top_n_values)
        backtest_months = positive_int_tuple(payload.get("backtest_months"), backtest_months)
        raw_max_age = payload.get("max_age_minutes")
        if isinstance(raw_max_age, int) and raw_max_age > 0:
            max_age_minutes = raw_max_age
    env_enabled = read_env_setting("QUANT_WECHAT_TREND_PRECOMPUTE")
    if env_enabled is not None:
        enabled = parse_boolish(env_enabled, enabled)
    env_warm = read_env_setting("QUANT_WECHAT_TREND_WARM_ON_STARTUP")
    if env_warm is not None:
        warm_on_startup = parse_boolish(env_warm, warm_on_startup)
    return TrendPrecomputeConfig(
        enabled=enabled,
        warm_on_startup=warm_on_startup,
        markets=markets,
        top_n_values=top_n_values,
        backtest_months=backtest_months,
        max_age_minutes=max_age_minutes,
    )


def trend_cache_path(kind: str, market: str, *, top_n: int, months: int | None = None) -> Path:
    market_key = normalize_market(market)
    if kind in {"trend", "plan"}:
        return TREND_CACHE_ROOT / f"{kind}_{market_key}_top{top_n}.json"
    return TREND_CACHE_ROOT / f"backtest_{market_key}_m{months or 0}_top{top_n}.json"


def trend_cache_signature(kind: str) -> str:
    settings = load_local_settings()
    if not isinstance(settings, dict):
        settings = {}
    keys = ["trend_portfolio_constraints", "trend_exit_rules", "trend_execution"]
    if kind == "backtest":
        keys.append("trend_backtest_costs")
    if kind == "plan":
        keys.append("trend_order_sizing")
    payload = {key: settings.get(key) for key in keys}
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()


def write_json_atomic(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def save_precomputed_reply(kind: str, market: str, text: str, *, top_n: int, months: int | None = None) -> Path:
    path = trend_cache_path(kind, market, top_n=top_n, months=months)
    payload = {
        "schema_version": TREND_CACHE_SCHEMA_VERSION,
        "config_signature": trend_cache_signature(kind),
        "kind": kind,
        "market": display_market_label(market),
        "market_key": normalize_market(market),
        "top_n": top_n,
        "months": months,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "reply": text,
    }
    return write_json_atomic(path, payload)


def load_precomputed_reply(
    kind: str,
    market: str,
    *,
    top_n: int,
    months: int | None = None,
    allow_stale: bool = False,
) -> CachedTrendReply | None:
    config = load_trend_precompute_config()
    path = trend_cache_path(kind, market, top_n=top_n, months=months)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if int(payload.get("schema_version") or 0) != TREND_CACHE_SCHEMA_VERSION:
        return None
    if str(payload.get("config_signature") or "") != trend_cache_signature(kind):
        return None
    text = payload.get("reply")
    raw_generated_at = payload.get("generated_at")
    if not isinstance(text, str) or not text.strip() or not isinstance(raw_generated_at, str):
        return None
    try:
        generated_at = dt.datetime.fromisoformat(raw_generated_at)
    except ValueError:
        return None
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=dt.timezone.utc)
    age_seconds = max(
        0.0,
        (dt.datetime.now(dt.timezone.utc) - generated_at.astimezone(dt.timezone.utc)).total_seconds(),
    )
    stale = age_seconds > config.max_age_minutes * 60
    if stale and not allow_stale:
        return None
    return CachedTrendReply(text=text, generated_at=generated_at, stale=stale)


def format_stale_cache_notice(reply: CachedTrendReply) -> str:
    generated_at = reply.generated_at.astimezone().strftime("%Y-%m-%d %H:%M")
    return (
        f"{reply.text}\n\n缓存说明:\n- 当前返回的是 {generated_at} 生成的缓存结果。\n"
        "- 后台正在继续刷新最新趋势数据。"
    )


def render_trend_snapshot_text(market: str | None = None, top_n: int = 5) -> str:
    effective_market = market if market is not None else resolve_default_market()
    return truncate_reply(
        trend_strategy.format_trend_snapshot(
            resolve_trend_universe_path(effective_market),
            effective_market,
            top_n=top_n,
        )
    )


def render_trading_plan_text(market: str | None = None, top_n: int = 5) -> str:
    effective_market = market if market is not None else resolve_default_market()
    return truncate_reply(
        trend_strategy.format_trading_plan(
            resolve_trend_universe_path(effective_market),
            effective_market,
            top_n=top_n,
        )
    )


def render_backtest_report_text(market: str | None = None, months: int = 12, top_n: int = 5) -> str:
    effective_market = market if market is not None else resolve_default_market()
    return truncate_reply(
        trend_strategy.format_backtest_report(
            resolve_trend_universe_path(effective_market),
            effective_market,
            lookback_months=months,
            top_n=top_n,
        )
    )


def should_precompute_request(kind: str, market: str, *, top_n: int, months: int | None = None) -> bool:
    config = load_trend_precompute_config()
    if not config.enabled:
        return False
    if display_market_label(market) not in config.markets:
        return False
    if top_n not in config.top_n_values:
        return False
    if kind == "backtest" and months not in config.backtest_months:
        return False
    return True


def should_defer_to_precompute(kind: str, market: str, *, top_n: int, months: int | None = None) -> bool:
    if not should_precompute_request(kind, market, top_n=top_n, months=months):
        return False
    if kind == "backtest":
        return True
    return normalize_market(market) == "all"


def precompute_waiting_text(kind: str, market: str, *, top_n: int, months: int | None = None) -> str:
    if kind == "trend":
        return (
            f"{display_market_label(market)} 趋势缓存正在预热，预计几十秒内完成。\n\n"
            f"稍后重试 `趋势选股 {display_market_label(market)} {top_n}`，"
            "或先看 `趋势选股 A股 2` / `趋势选股 美股 2`。"
        )
    if kind == "plan":
        return (
            f"{display_market_label(market)} 交易计划缓存正在预热，预计几十秒内完成。\n\n"
            f"稍后重试 `交易计划 {display_market_label(market)} {top_n}`，"
            "或先看 `趋势选股 A股 2`。"
        )
    return (
        f"{display_market_label(market)} 趋势回测缓存正在预热，预计 1-2 分钟内完成。\n\n"
        f"稍后重试 `趋势回测 {display_market_label(market)} {months or 12}`，"
        "或先看 `趋势选股 全市场 5`。"
    )


def precompute_trend_outputs(
    *,
    markets: tuple[str, ...] | None = None,
    top_n_values: tuple[int, ...] | None = None,
    backtest_months: tuple[int, ...] | None = None,
) -> tuple[list[Path], list[str]]:
    config = load_trend_precompute_config()
    selected_markets = markets or config.markets
    selected_top_n = top_n_values or config.top_n_values
    selected_backtest_months = backtest_months or config.backtest_months
    generated: list[Path] = []
    errors: list[str] = []
    for market in selected_markets:
        for top_n in selected_top_n:
            try:
                reply = render_trend_snapshot_text(market, top_n=top_n)
                generated.append(save_precomputed_reply("trend", market, reply, top_n=top_n))
            except Exception as exc:
                errors.append(f"趋势选股 {market} {top_n}: {exc}")
            try:
                reply = render_trading_plan_text(market, top_n=top_n)
                generated.append(save_precomputed_reply("plan", market, reply, top_n=top_n))
            except Exception as exc:
                errors.append(f"交易计划 {market} {top_n}: {exc}")
        for months in selected_backtest_months:
            try:
                reply = render_backtest_report_text(market, months=months)
                generated.append(save_precomputed_reply("backtest", market, reply, top_n=5, months=months))
            except Exception as exc:
                errors.append(f"趋势回测 {market} {months}: {exc}")
    return generated, errors


def background_precompute_worker() -> None:
    global TREND_PRECOMPUTE_LAST_ERROR, TREND_PRECOMPUTE_RUNNING
    try:
        _, errors = precompute_trend_outputs()
        if errors:
            TREND_PRECOMPUTE_LAST_ERROR = "; ".join(errors[:4])
        else:
            TREND_PRECOMPUTE_LAST_ERROR = None
    finally:
        with TREND_PRECOMPUTE_STATE_LOCK:
            TREND_PRECOMPUTE_RUNNING = False


def start_background_trend_precompute() -> None:
    global TREND_PRECOMPUTE_RUNNING
    config = load_trend_precompute_config()
    if not config.enabled or not config.warm_on_startup:
        return
    with TREND_PRECOMPUTE_STATE_LOCK:
        if TREND_PRECOMPUTE_RUNNING:
            return
        TREND_PRECOMPUTE_RUNNING = True
    thread = threading.Thread(target=background_precompute_worker, name="trend-precompute", daemon=True)
    thread.start()


def build_strategy_list_text() -> str:
    return truncate_reply(
        quant_engine.format_strategy_catalog()
        + "\n\n市场用法:\n- 选股 质量 A股\n- 选股 质量 港股\n- 选股 质量 美股\n- 选股 质量 全市场\n- 评分 600519 A股\n- 评分 00700.HK 港股\n- 股票池 全市场"
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


def build_close_digest_text(market: str | None = None) -> str:
    try:
        from . import market_close_digest
    except ImportError:  # pragma: no cover - allows direct script execution
        import market_close_digest  # type: ignore

    effective_market = market if market is not None else "A股"
    config = market_close_digest.normalize_market(effective_market)
    digest, _ = market_close_digest.build_digest(config, market_close_digest.load_settings())
    return truncate_reply(digest)


def resolve_trend_universe_path(market: str | None = None) -> Path:
    effective_market = market if market is not None else resolve_default_market()
    bundled = resolve_bundled_universe_path(normalize_market(effective_market))
    if bundled is not None:
        return bundled
    return resolve_universe_path(effective_market)


def default_trade_plan_export_path(market: str | None = None, top_n: int = 5) -> Path:
    effective_market = display_market_label(market if market is not None else resolve_default_market())
    market_key = normalize_market(effective_market)
    return PROJECT_ROOT / ".cache" / f"trade_plan_{market_key}_top{top_n}.csv"


def build_trend_snapshot_text(market: str | None = None, top_n: int = 5) -> str:
    effective_market = market if market is not None else resolve_default_market()
    cached = load_precomputed_reply("trend", effective_market, top_n=top_n)
    if cached is not None:
        return cached.text
    stale = load_precomputed_reply("trend", effective_market, top_n=top_n, allow_stale=True)
    if stale is not None and should_defer_to_precompute("trend", effective_market, top_n=top_n):
        start_background_trend_precompute()
        return format_stale_cache_notice(stale)
    if should_defer_to_precompute("trend", effective_market, top_n=top_n):
        start_background_trend_precompute()
        return precompute_waiting_text("trend", effective_market, top_n=top_n)
    reply = render_trend_snapshot_text(effective_market, top_n=top_n)
    if should_precompute_request("trend", effective_market, top_n=top_n):
        save_precomputed_reply("trend", effective_market, reply, top_n=top_n)
    return reply


def build_trading_plan_text(market: str | None = None, top_n: int = 5) -> str:
    effective_market = market if market is not None else resolve_default_market()
    cached = load_precomputed_reply("plan", effective_market, top_n=top_n)
    if cached is not None:
        return cached.text
    stale = load_precomputed_reply("plan", effective_market, top_n=top_n, allow_stale=True)
    if stale is not None and should_defer_to_precompute("plan", effective_market, top_n=top_n):
        start_background_trend_precompute()
        return format_stale_cache_notice(stale)
    if should_defer_to_precompute("plan", effective_market, top_n=top_n):
        start_background_trend_precompute()
        return precompute_waiting_text("plan", effective_market, top_n=top_n)
    reply = render_trading_plan_text(effective_market, top_n=top_n)
    if should_precompute_request("plan", effective_market, top_n=top_n):
        save_precomputed_reply("plan", effective_market, reply, top_n=top_n)
    return reply


def build_backtest_report_text(market: str | None = None, months: int = 12, top_n: int = 5) -> str:
    effective_market = market if market is not None else resolve_default_market()
    cached = load_precomputed_reply("backtest", effective_market, top_n=top_n, months=months)
    if cached is not None:
        return cached.text
    stale = load_precomputed_reply("backtest", effective_market, top_n=top_n, months=months, allow_stale=True)
    if stale is not None and should_defer_to_precompute("backtest", effective_market, top_n=top_n, months=months):
        start_background_trend_precompute()
        return format_stale_cache_notice(stale)
    if should_defer_to_precompute("backtest", effective_market, top_n=top_n, months=months):
        start_background_trend_precompute()
        return precompute_waiting_text("backtest", effective_market, top_n=top_n, months=months)
    reply = render_backtest_report_text(effective_market, months=months, top_n=top_n)
    if should_precompute_request("backtest", effective_market, top_n=top_n, months=months):
        save_precomputed_reply("backtest", effective_market, reply, top_n=top_n, months=months)
    return reply


def export_trading_plan_csv_file(market: str | None = None, top_n: int = 5, output_path: str | None = None) -> Path:
    effective_market = market if market is not None else resolve_default_market()
    output = Path(output_path).expanduser() if output_path else default_trade_plan_export_path(effective_market, top_n=top_n)
    if not output.is_absolute():
        output = PROJECT_ROOT / output
    return trend_strategy.export_trading_plan_csv(
        resolve_trend_universe_path(effective_market),
        effective_market,
        output,
        top_n=top_n,
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

    close_match = re.match(r"^(?:/close|收盘总结|盘后总结|盘后|收盘)(?:\s+([^\s]+))?$", normalized, re.I)
    if close_match:
        return BotReply(build_close_digest_text(close_match.group(1)), "close")

    trend_match = re.match(r"^(?:/trend|趋势选股|趋势)(?:\s+([^\s\d]+))?(?:\s+(\d+))?$", normalized, re.I)
    if trend_match:
        top_n = int(trend_match.group(2) or "5")
        return BotReply(build_trend_snapshot_text(trend_match.group(1), top_n=top_n), "trend")

    backtest_match = re.match(r"^(?:/backtest|趋势回测|回测)(?:\s+([^\s\d]+))?(?:\s+(\d+))?$", normalized, re.I)
    if backtest_match:
        months = int(backtest_match.group(2) or "12")
        return BotReply(build_backtest_report_text(backtest_match.group(1), months=months), "backtest")

    plan_match = re.match(r"^(?:/plan|交易计划|执行计划|调仓计划)(?:\s+([^\s\d]+))?(?:\s+(\d+))?$", normalized, re.I)
    if plan_match:
        top_n = int(plan_match.group(2) or "5")
        return BotReply(build_trading_plan_text(plan_match.group(1), top_n=top_n), "plan")

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
        "我当前更擅长结构化的量化选股命令。\n\n试试:\n- 策略列表\n- 选股 质量 全市场\n- 趋势选股 A股\n- 趋势回测 美股 12\n- 交易计划 A股\n- 评分 00700.HK 港股\n- 评分 NVDA 美股\n- 股票池 全市场",
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
            hint = "\n\n提示: 先发送 `股票池`、`股票池 港股`、`股票池 美股` 或 `股票池 全市场` 看当前股票池里有哪些代码。"
        elif "urlopen error" in str(exc).lower() or "timed out" in str(exc).lower() or "eastmoney" in str(exc).lower() or "sina" in str(exc).lower():
            hint = "\n\n提示: 全量股票池和趋势回测都需要联网拉取免费行情数据。你也可以先试 `选股 质量 样本`。"
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
        6. 收盘总结 A股

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


def handle_health(handler: BaseHTTPRequestHandler) -> None:
    write_json(
        handler,
        HTTPStatus.OK,
        {
            "ok": True,
            "service": "quant-wechat-bot",
        },
    )


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
        --bg: #efe6d4;
        --bg-deep: #10211f;
        --panel: rgba(12, 29, 27, 0.9);
        --panel-strong: rgba(17, 39, 36, 0.96);
        --panel-soft: rgba(236, 226, 205, 0.86);
        --ink: #f4ecde;
        --ink-strong: #fff8ee;
        --muted: rgba(244, 236, 222, 0.68);
        --muted-dark: rgba(16, 33, 31, 0.65);
        --accent: #55d6a0;
        --accent-2: #ffb24d;
        --accent-3: #8be0ff;
        --line: rgba(255, 248, 238, 0.1);
        --line-strong: rgba(255, 248, 238, 0.2);
        --shadow: 0 36px 90px rgba(7, 15, 14, 0.28);
        --radius-xl: 34px;
        --radius-lg: 24px;
        --radius-md: 18px;
      }

      * {
        box-sizing: border-box;
      }

      body {
        margin: 0;
        min-height: 100vh;
        background:
          radial-gradient(circle at top left, rgba(255, 178, 77, 0.26), transparent 26%),
          radial-gradient(circle at 82% 12%, rgba(139, 224, 255, 0.18), transparent 24%),
          radial-gradient(circle at bottom right, rgba(85, 214, 160, 0.18), transparent 34%),
          linear-gradient(135deg, #eedfbe 0%, #d6c3a1 42%, #f6eee0 100%);
        color: var(--ink);
        font-family: "Avenir Next", "PingFang SC", "Helvetica Neue", sans-serif;
        overflow-x: hidden;
      }

      body::before,
      body::after {
        content: "";
        position: fixed;
        inset: 0;
        pointer-events: none;
      }

      body::before {
        background:
          linear-gradient(rgba(255, 255, 255, 0.05) 1px, transparent 1px),
          linear-gradient(90deg, rgba(255, 255, 255, 0.04) 1px, transparent 1px);
        background-size: 34px 34px;
        mask-image: radial-gradient(circle at center, black 42%, transparent 100%);
        opacity: 0.22;
      }

      body::after {
        inset: 24px;
        border: 1px solid rgba(16, 33, 31, 0.08);
        border-radius: 40px;
      }

      .shell {
        position: relative;
        width: min(1280px, calc(100vw - 32px));
        margin: 32px auto;
        display: grid;
        grid-template-columns: minmax(310px, 420px) minmax(0, 1fr);
        gap: 22px;
      }

      .panel {
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: var(--radius-xl);
        box-shadow: var(--shadow);
        backdrop-filter: blur(18px);
        position: relative;
        overflow: hidden;
      }

      .panel::before {
        content: "";
        position: absolute;
        inset: 0;
        background: linear-gradient(145deg, rgba(255, 255, 255, 0.05), transparent 28%);
        pointer-events: none;
      }

      .hero {
        padding: 28px;
        display: flex;
        flex-direction: column;
        gap: 22px;
      }

      .hero-top,
      .workspace-top {
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        gap: 16px;
      }

      .eyebrow {
        font-size: 11px;
        letter-spacing: 0.22em;
        text-transform: uppercase;
        color: var(--accent-2);
        margin-bottom: 10px;
      }

      .status-chip,
      .market-chip {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        padding: 10px 14px;
        border-radius: 999px;
        background: rgba(255, 248, 238, 0.08);
        border: 1px solid rgba(255, 248, 238, 0.12);
        color: var(--ink);
        font-size: 13px;
      }

      h1 {
        margin: 0;
        font-family: "Baskerville", "Times New Roman", serif;
        font-size: clamp(40px, 5vw, 74px);
        line-height: 0.9;
        letter-spacing: -0.04em;
      }

      .hero-copy {
        position: relative;
        z-index: 1;
      }

      .lede {
        margin: 0;
        color: var(--muted);
        line-height: 1.7;
        font-size: 16px;
      }

      .signal-grid,
      .prompt-grid,
      .meta-grid {
        display: grid;
        gap: 12px;
      }

      .signal-grid {
        grid-template-columns: repeat(3, minmax(0, 1fr));
      }

      .signal-card,
      .meta-card {
        padding: 16px 18px;
        border-radius: var(--radius-md);
        background: rgba(255, 248, 238, 0.06);
        border: 1px solid rgba(255, 248, 238, 0.1);
      }

      .signal-label,
      .meta-label {
        display: block;
        color: var(--muted);
        font-size: 12px;
        margin-bottom: 8px;
      }

      .signal-value,
      .meta-value {
        display: block;
        color: var(--ink-strong);
        font-size: 17px;
        font-weight: 600;
      }

      .prompt-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .prompt-card {
        position: relative;
        text-align: left;
        border: 1px solid rgba(255, 248, 238, 0.12);
        background:
          linear-gradient(180deg, rgba(255, 248, 238, 0.08), rgba(255, 248, 238, 0.03)),
          rgba(255, 248, 238, 0.02);
        color: var(--ink);
        border-radius: var(--radius-lg);
        padding: 18px;
        cursor: pointer;
        transition: transform 180ms ease, border-color 180ms ease, background 180ms ease;
      }

      .prompt-card:hover,
      .dock-pill:hover,
      .composer-button:hover {
        transform: translateY(-2px);
      }

      .prompt-card:hover {
        border-color: rgba(85, 214, 160, 0.4);
        background:
          linear-gradient(180deg, rgba(85, 214, 160, 0.16), rgba(255, 248, 238, 0.04)),
          rgba(255, 248, 238, 0.03);
      }

      .prompt-kicker {
        display: block;
        font-size: 11px;
        letter-spacing: 0.14em;
        text-transform: uppercase;
        color: var(--accent-3);
        margin-bottom: 8px;
      }

      .prompt-title {
        display: block;
        font-size: 20px;
        font-weight: 700;
        margin-bottom: 8px;
      }

      .prompt-copy {
        display: block;
        font-size: 14px;
        line-height: 1.6;
        color: var(--muted);
      }

      .command-dock {
        display: flex;
        flex-wrap: wrap;
        gap: 10px;
      }

      .dock-pill {
        border: 1px solid rgba(255, 248, 238, 0.14);
        border-radius: 999px;
        padding: 11px 15px;
        background: rgba(255, 248, 238, 0.08);
        color: var(--ink);
        font: inherit;
        cursor: pointer;
        transition: transform 180ms ease, border-color 180ms ease, background 180ms ease;
      }

      .dock-pill:hover {
        border-color: rgba(255, 178, 77, 0.42);
        background: rgba(255, 178, 77, 0.12);
      }

      .api-note {
        padding: 16px 18px;
        border-radius: var(--radius-md);
        background: var(--panel-soft);
        color: #182926;
        box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.55);
      }

      .api-note strong {
        display: block;
        margin-bottom: 8px;
      }

      .api-note code {
        font-family: "SFMono-Regular", "Menlo", monospace;
      }

      .workspace {
        padding: 24px;
        display: flex;
        flex-direction: column;
        min-height: calc(100vh - 64px);
        gap: 18px;
      }

      .workspace-top {
        align-items: center;
      }

      .workspace-title h2 {
        margin: 0;
        font-family: "Baskerville", "Times New Roman", serif;
        font-size: clamp(28px, 4vw, 42px);
        letter-spacing: -0.03em;
      }

      .workspace-title p {
        margin: 10px 0 0;
        color: var(--muted);
        line-height: 1.6;
      }

      .meta-grid {
        grid-template-columns: repeat(3, minmax(0, 1fr));
      }

      .meta-card {
        background: rgba(255, 248, 238, 0.06);
      }

      .tape {
        display: flex;
        align-items: center;
        gap: 12px;
        padding: 14px 16px;
        border-radius: var(--radius-md);
        background: rgba(255, 248, 238, 0.05);
        border: 1px solid rgba(255, 248, 238, 0.08);
        color: var(--muted);
        overflow: hidden;
      }

      .tape-label {
        flex: 0 0 auto;
        color: var(--accent-2);
        font-size: 11px;
        letter-spacing: 0.22em;
        text-transform: uppercase;
      }

      .tape-marquee {
        min-width: 0;
        white-space: nowrap;
        animation: drift 20s linear infinite;
      }

      .desk {
        position: relative;
        flex: 1;
        display: grid;
        grid-template-rows: auto 1fr auto;
        gap: 16px;
        padding: 18px;
        border-radius: 30px;
        background:
          linear-gradient(180deg, rgba(9, 23, 21, 0.86), rgba(14, 34, 31, 0.96)),
          rgba(9, 23, 21, 0.9);
        border: 1px solid rgba(255, 248, 238, 0.08);
      }

      .desk::before {
        content: "";
        position: absolute;
        inset: 0;
        background:
          radial-gradient(circle at top right, rgba(85, 214, 160, 0.15), transparent 24%),
          linear-gradient(transparent 95%, rgba(255, 255, 255, 0.04) 95%);
        background-size: auto, 100% 42px;
        pointer-events: none;
        opacity: 0.8;
      }

      .desk-head {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 16px;
        position: relative;
        z-index: 1;
      }

      .desk-status {
        border-radius: 999px;
        padding: 10px 13px;
        background: rgba(255, 178, 77, 0.12);
        border: 1px solid rgba(255, 178, 77, 0.18);
        color: var(--accent-2);
        font-size: 12px;
        letter-spacing: 0.14em;
        text-transform: uppercase;
      }

      #messages {
        flex: 1;
        overflow: auto;
        display: flex;
        flex-direction: column;
        gap: 16px;
        padding-right: 6px;
        position: relative;
        z-index: 1;
      }

      .msg {
        width: min(760px, 100%);
        display: grid;
        gap: 8px;
        animation: rise 260ms ease;
      }

      .msg.user {
        align-self: flex-end;
      }

      .msg.bot {
        align-self: flex-start;
      }

      .msg-meta {
        font-size: 11px;
        letter-spacing: 0.18em;
        text-transform: uppercase;
        color: var(--muted);
      }

      .bubble {
        padding: 16px 18px;
        border-radius: 22px;
        white-space: pre-wrap;
        line-height: 1.55;
        border: 1px solid rgba(255, 248, 238, 0.08);
      }

      .msg.user .bubble {
        background: linear-gradient(135deg, rgba(85, 214, 160, 0.28), rgba(28, 93, 73, 0.9));
        color: var(--ink-strong);
        border-bottom-right-radius: 8px;
      }

      .msg.bot .bubble {
        background: linear-gradient(135deg, rgba(255, 248, 238, 0.08), rgba(255, 248, 238, 0.03));
        color: var(--ink);
        border-bottom-left-radius: 8px;
      }

      .msg.pending .bubble {
        color: var(--muted);
      }

      .composer {
        position: relative;
        z-index: 1;
      }

      .composer-shell {
        display: grid;
        grid-template-columns: minmax(0, 1fr) auto;
        gap: 14px;
        align-items: end;
        padding: 14px;
        border-radius: 26px;
        background: rgba(255, 248, 238, 0.06);
        border: 1px solid rgba(255, 248, 238, 0.1);
      }

      textarea {
        resize: vertical;
        min-height: 110px;
        max-height: 220px;
        border-radius: 18px;
        border: 1px solid rgba(255, 248, 238, 0.12);
        background: rgba(6, 17, 16, 0.42);
        padding: 16px 18px;
        font: inherit;
        color: var(--ink-strong);
      }

      textarea::placeholder {
        color: rgba(244, 236, 222, 0.46);
      }

      .composer-side {
        display: grid;
        gap: 10px;
        min-width: 172px;
      }

      .composer-hint {
        font-size: 12px;
        line-height: 1.5;
        color: var(--muted);
      }

      .composer-button {
        border: 0;
        border-radius: 18px;
        padding: 15px 18px;
        background: linear-gradient(135deg, #ffb24d, #de7d2d);
        color: #fff9f1;
        font: inherit;
        font-weight: 700;
        cursor: pointer;
        transition: transform 180ms ease, filter 180ms ease;
      }

      .composer-button:hover {
        filter: brightness(1.05);
      }

      .composer-button:disabled {
        cursor: wait;
        opacity: 0.7;
        transform: none;
      }

      @keyframes rise {
        from {
          opacity: 0;
          transform: translateY(10px);
        }
        to {
          opacity: 1;
          transform: translateY(0);
        }
      }

      @keyframes drift {
        from {
          transform: translateX(0);
        }
        to {
          transform: translateX(-18%);
        }
      }

      @media (max-width: 1080px) {
        .shell {
          grid-template-columns: 1fr;
        }

        .workspace {
          min-height: 72vh;
        }
      }

      @media (max-width: 760px) {
        body::after {
          inset: 12px;
          border-radius: 28px;
        }

        .shell {
          width: min(100vw - 20px, 1280px);
          margin: 18px auto;
        }

        .hero,
        .workspace {
          padding: 20px;
        }

        .signal-grid,
        .meta-grid,
        .prompt-grid,
        .composer-shell {
          grid-template-columns: 1fr;
        }

        .hero-top,
        .workspace-top,
        .desk-head {
          flex-direction: column;
          align-items: flex-start;
        }

        .composer-side,
        .composer-button {
          width: 100%;
        }
      }
    </style>
  </head>
  <body>
    <main class="shell">
      <section class="panel hero">
        <div class="hero-top">
          <div class="hero-copy">
            <div class="eyebrow">Signal Desk</div>
            <h1>Quant WeChat Bot</h1>
          </div>
          <span class="status-chip">Chat + API + WeChat</span>
        </div>
        <p class="lede">
          它不该只是一个能回复命令的 demo，而应该像一张随时可用的策略工作台。
          你可以在这里快速切换市场、挑策略、看单票评分，再把同一套命令接去微信或回调接口。
        </p>

        <div class="signal-grid">
          <div class="signal-card">
            <span class="signal-label">Markets</span>
            <span class="signal-value">A股 / 港股 / 美股 / 全市场</span>
          </div>
          <div class="signal-card">
            <span class="signal-label">Command Core</span>
            <span class="signal-value">选股 / 评分 / 收盘总结</span>
          </div>
          <div class="signal-card">
            <span class="signal-label">Delivery</span>
            <span class="signal-value">网页 / JSON / 微信回调</span>
          </div>
        </div>

        <div>
          <div class="eyebrow">Playbooks</div>
          <div class="prompt-grid">
            <button class="prompt-card" data-prompt="选股 质量 全市场">
              <span class="prompt-kicker">Quality Bias</span>
              <span class="prompt-title">质量动量</span>
              <span class="prompt-copy">先把 A/H/US 合在一起看质量和趋势，适合先扫全球强票。</span>
            </button>
            <button class="prompt-card" data-prompt="选股 动量 A股">
              <span class="prompt-kicker">Trend Focus</span>
              <span class="prompt-title">趋势增强</span>
              <span class="prompt-copy">把 20D / 60D 动量放在前面，适合先抓最强方向。</span>
            </button>
            <button class="prompt-card" data-prompt="股票池 全市场">
              <span class="prompt-kicker">Market Breadth</span>
              <span class="prompt-title">全市场股票池</span>
              <span class="prompt-copy">直接查看 A 股、港股、美股合并后的大池子，先确认覆盖面。</span>
            </button>
            <button class="prompt-card" data-prompt="评分 00700.HK 港股">
              <span class="prompt-kicker">Single Name</span>
              <span class="prompt-title">单票评分</span>
              <span class="prompt-copy">直接拉单票做多策略打分，A 股、港股、美股都能直接查。</span>
            </button>
          </div>
        </div>

        <div>
          <div class="eyebrow">Quick Commands</div>
          <div class="command-dock">
            <button class="dock-pill" data-prompt="帮助">帮助</button>
            <button class="dock-pill" data-prompt="策略列表">策略列表</button>
            <button class="dock-pill" data-prompt="股票池 全市场">股票池 全市场</button>
            <button class="dock-pill" data-prompt="评分 00700.HK 港股">评分 00700.HK</button>
            <button class="dock-pill" data-prompt="收盘总结 A股">收盘总结 A股</button>
            <button class="dock-pill" data-prompt="收盘总结 美股">收盘总结 美股</button>
          </div>
        </div>

        <div class="api-note">
          <strong>JSON endpoint</strong>
          <code>POST /api/chat</code><br>
          <code>{"message":"选股 质量 全市场"}</code>
        </div>
      </section>

      <section class="panel workspace">
        <div class="workspace-top">
          <div class="workspace-title">
            <div class="eyebrow">Interactive Desk</div>
            <h2>Command Surface</h2>
            <p>把想法直接打成一句命令，不用翻菜单，不用猜功能埋在哪。</p>
          </div>
          <span class="market-chip">Live Prompt Console</span>
        </div>

        <div class="meta-grid">
          <div class="meta-card">
            <span class="meta-label">Best for</span>
            <span class="meta-value">策略试跑</span>
          </div>
          <div class="meta-card">
            <span class="meta-label">Fastest prompt</span>
            <span class="meta-value">选股 质量 全市场</span>
          </div>
          <div class="meta-card">
            <span class="meta-label">Delivery path</span>
            <span class="meta-value">Web first, WeChat next</span>
          </div>
        </div>

        <div class="tape">
          <span class="tape-label">Prompt tape</span>
          <div class="tape-marquee">帮助 · 策略列表 · 选股 质量 全市场 · 选股 动量 A股 · 评分 00700.HK 港股 · 股票池 全市场 · 收盘总结 美股</div>
        </div>

        <section class="desk">
          <div class="desk-head">
            <div>
              <div class="eyebrow">Session</div>
              <strong>先在这里测策略，再把同一套命令接去微信。</strong>
            </div>
            <span class="desk-status">Quant MVP</span>
          </div>

          <div id="messages"></div>

          <form id="composer" class="composer">
            <div class="composer-shell">
              <textarea id="message" placeholder="试试：选股 质量 全市场 / 股票池 全市场 / 评分 00700.HK 港股 / 评分 NVDA 美股"></textarea>
              <div class="composer-side">
                <div class="composer-hint">`Enter` 发送，`Shift + Enter` 换行。</div>
                <button id="send-button" class="composer-button" type="submit">Run Command</button>
              </div>
            </div>
          </form>
        </section>
      </section>
    </main>

    <script>
      const messages = document.getElementById("messages");
      const composer = document.getElementById("composer");
      const input = document.getElementById("message");
      const sendButton = document.getElementById("send-button");

      function addMessage(role, text, meta = null) {
        const el = document.createElement("article");
        el.className = `msg ${role}`;

        const metaEl = document.createElement("div");
        metaEl.className = "msg-meta";
        metaEl.textContent = meta || (role === "user" ? "You" : "Strategy Desk");

        const bubble = document.createElement("div");
        bubble.className = "bubble";
        bubble.textContent = text;

        el.appendChild(metaEl);
        el.appendChild(bubble);
        messages.appendChild(el);
        messages.scrollTop = messages.scrollHeight;
        return el;
      }

      async function sendMessage(text) {
        addMessage("user", text);
        input.value = "";
        input.focus();
        sendButton.disabled = true;

        const pending = addMessage("bot pending", "正在整理信号...", "Strategy Desk");

        try {
          const response = await fetch("/api/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ message: text })
          });
          const payload = await response.json();
          pending.remove();
          addMessage("bot", payload.reply || "No reply returned.");
        } catch (error) {
          pending.remove();
          addMessage("bot", `Request failed: ${error.message}`);
        } finally {
          sendButton.disabled = false;
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

      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.shiftKey) {
          event.preventDefault();
          composer.requestSubmit();
        }
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
        "今天想先看强趋势、稳健质量，还是直接评一只票？你可以发 `策略列表`，也可以一句话直接下命令。",
        "Strategy Desk"
      );
    </script>
  </body>
</html>
"""


class BotHTTPRequestHandler(BaseHTTPRequestHandler):
    server_version = "QuantWeChatBot/0.1"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in {"/healthz", "/api/health"}:
            handle_health(self)
            return
        if parsed.path == WECHAT_CALLBACK_PATH:
            handle_wechat_get(self, urllib.parse.parse_qs(parsed.query))
            return
        if parsed.path not in {"/", "/index.html"}:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        body = html_page().encode("utf-8")
        write_response(self, HTTPStatus.OK, body, "text/html; charset=utf-8")

    def do_HEAD(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in {"/", "/index.html", "/healthz", "/api/health"}:
            self.send_response(HTTPStatus.OK)
            self.end_headers()
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

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
    start_background_trend_precompute()
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
    if args.command == "precompute-trend":
        markets = tuple(args.markets) if args.markets else None
        top_n_values = tuple(args.top_n_values) if args.top_n_values else None
        backtest_months = tuple(args.backtest_months) if args.backtest_months else None
        generated, errors = precompute_trend_outputs(
            markets=markets,
            top_n_values=top_n_values,
            backtest_months=backtest_months,
        )
        for path in generated:
            print(path)
        if errors:
            for error in errors:
                print(f"ERROR: {error}")
            return 1
        return 0
    if args.command == "export-trade-plan":
        path = export_trading_plan_csv_file(args.market, top_n=args.top_n, output_path=args.output)
        print(path)
        return 0
    if args.command == "serve":
        return run_server(args.host, args.port)
    raise RuntimeError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
