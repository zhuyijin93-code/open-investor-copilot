from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    from . import bot_service, quant_engine
except ImportError:  # pragma: no cover - allows direct script execution
    import bot_service  # type: ignore
    import quant_engine  # type: ignore


PROJECT_ROOT = Path(__file__).resolve().parent
WORKSPACE_ROOT = PROJECT_ROOT.parent
SETTINGS_PATH = PROJECT_ROOT / ".cache" / "local_settings.json"
ROOT_SETTINGS_PATH = WORKSPACE_ROOT / ".cache" / "local_settings.json"
STATE_PATH = PROJECT_ROOT / ".cache" / "close_digest_state.json"
EASTMONEY_SEARCH_URL = "https://searchapi.eastmoney.com/api/suggest/get"
EASTMONEY_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EASTMONEY_SEARCH_TOKEN = "D43BF722C8E33BDC906FB84D85E326E8"
YAHOO_CHART_URL = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/137.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": DEFAULT_USER_AGENT,
    "Accept": "application/json,text/plain,*/*",
}
YAHOO_USER_AGENT = "Mozilla/5.0"
EASTMONEY_SECIDS = {
    "000001.SS": "1.000001",
    "399001.SZ": "0.399001",
    "399006.SZ": "0.399006",
    "000300.SS": "1.000300",
    "600519.SS": "1.600519",
    "300750.SZ": "0.300750",
    "601318.SS": "1.601318",
    "600036.SS": "1.600036",
    "000333.SZ": "0.000333",
    "^HSI": "100.HSI",
    "2828.HK": "116.02828",
    "3033.HK": "116.03033",
    "2800.HK": "116.02800",
    "0700.HK": "116.00700",
    "9988.HK": "116.09988",
    "3690.HK": "116.03690",
    "1810.HK": "116.01810",
    "1299.HK": "116.01299",
    "^GSPC": "100.SPX",
    "^IXIC": "100.NDX",
    "^DJI": "100.DJIA",
    "IWM": "107.IWM",
    "NVDA": "105.NVDA",
    "MSFT": "106.MSFT",
    "AMZN": "105.AMZN",
    "TSLA": "105.TSLA",
    "GOOG": "105.GOOG",
    "QQQ": "105.QQQ",
    "SPY": "107.SPY",
}
YAHOO_SYMBOLS = {
    "^GSPC": "^GSPC",
    "^IXIC": "^NDX",
    "^DJI": "^DJI",
}


@dataclasses.dataclass(frozen=True)
class MarketConfig:
    code: str
    label: str
    timezone: str
    close_hour: int
    close_minute: int
    aliases: tuple[str, ...]
    primary_symbols: tuple[tuple[str, str], ...]
    style_symbols: tuple[tuple[str, str], ...]
    watchlist_defaults: tuple[tuple[str, str], ...]


MARKETS: dict[str, MarketConfig] = {
    "CN": MarketConfig(
        code="CN",
        label="A股",
        timezone="Asia/Shanghai",
        close_hour=15,
        close_minute=0,
        aliases=("cn", "a", "a股", "ashare", "沪深", "中国"),
        primary_symbols=(
            ("000001.SS", "上证"),
            ("399001.SZ", "深成"),
            ("399006.SZ", "创业板"),
            ("000300.SS", "沪深300"),
        ),
        style_symbols=(
            ("399006.SZ", "创业板"),
            ("000300.SS", "沪深300"),
            ("000001.SS", "上证"),
        ),
        watchlist_defaults=(
            ("600519.SS", "贵州茅台"),
            ("300750.SZ", "宁德时代"),
            ("601318.SS", "中国平安"),
            ("600036.SS", "招商银行"),
            ("000333.SZ", "美的集团"),
        ),
    ),
    "HK": MarketConfig(
        code="HK",
        label="港股",
        timezone="Asia/Shanghai",
        close_hour=16,
        close_minute=0,
        aliases=("hk", "港股", "hongkong", "hong-kong"),
        primary_symbols=(
            ("^HSI", "恒指"),
            ("2828.HK", "国企ETF"),
            ("3033.HK", "恒科ETF"),
            ("2800.HK", "盈富基金"),
        ),
        style_symbols=(
            ("3033.HK", "恒科ETF"),
            ("^HSI", "恒指"),
            ("2828.HK", "国企ETF"),
        ),
        watchlist_defaults=(
            ("0700.HK", "腾讯"),
            ("9988.HK", "阿里"),
            ("3690.HK", "美团"),
            ("1810.HK", "小米"),
            ("1299.HK", "友邦"),
        ),
    ),
    "US": MarketConfig(
        code="US",
        label="美股",
        timezone="America/New_York",
        close_hour=16,
        close_minute=0,
        aliases=("us", "usa", "美股", "na"),
        primary_symbols=(
            ("^GSPC", "标普500"),
            ("^IXIC", "纳指100"),
            ("^DJI", "道指"),
            ("IWM", "罗素2000"),
        ),
        style_symbols=(
            ("^IXIC", "纳指100"),
            ("^GSPC", "标普500"),
            ("IWM", "罗素2000"),
        ),
        watchlist_defaults=(
            ("NVDA", "英伟达"),
            ("MSFT", "微软"),
            ("AMZN", "亚马逊"),
            ("TSLA", "特斯拉"),
            ("GOOG", "谷歌"),
        ),
    ),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build and send close-of-market digests to personal WeChat.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser("build", help="Generate the latest close digest and print it.")
    send_parser = subparsers.add_parser("send", help="Generate and send the latest close digest to personal WeChat.")

    for subparser in (build_parser, send_parser):
        subparser.add_argument("--market", required=True, help="Market: A股/CN, 港股/HK, 美股/US.")
        subparser.add_argument(
            "--state-path",
            default=str(STATE_PATH),
            help=f"State file for dedupe. Default: {STATE_PATH}.",
        )
        subparser.add_argument(
            "--force",
            action="store_true",
            help="Ignore close-time and holiday freshness checks.",
        )

    build_parser.add_argument("--output", help="Optional file to write the digest to.")

    send_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve the send chain but do not actually deliver to WeChat.",
    )
    send_parser.add_argument(
        "--quiet-skip",
        action="store_true",
        help="Exit quietly when there is no new digest to send.",
    )
    return parser.parse_args()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_settings() -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for path in (ROOT_SETTINGS_PATH, SETTINGS_PATH):
        if not path.exists():
            continue
        try:
            payload = load_json(path)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            merged.update(payload)
    return merged


def normalize_market(value: str) -> MarketConfig:
    normalized = value.strip().lower()
    for config in MARKETS.values():
        if normalized == config.code.lower() or normalized in config.aliases:
            return config
    supported = ", ".join(f"{item.label}/{item.code}" for item in MARKETS.values())
    raise RuntimeError(f"Unsupported market `{value}`. Supported: {supported}.")


def symbol_market(symbol: str) -> str:
    upper = symbol.strip().upper()
    if upper.endswith(".HK"):
        return "HK"
    if upper.endswith(".SS") or upper.endswith(".SZ"):
        return "CN"
    return "US"


def build_query(params: dict[str, Any]) -> str:
    return "&".join(
        f"{urllib.parse.quote(str(key), safe='')}"
        f"={urllib.parse.quote(str(value), safe=',:.')}"
        for key, value in params.items()
        if value is not None
    )


def get_json(url: str, params: dict[str, Any], *, source_name: str, user_agent: str = DEFAULT_USER_AGENT) -> Any:
    query = build_query(params)
    full_url = f"{url}?{query}" if query else url
    headers = {**DEFAULT_HEADERS, "User-Agent": user_agent}
    request = urllib.request.Request(full_url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        try:
            completed = subprocess.run(
                [
                    "curl",
                    "-sS",
                    "-A",
                    user_agent,
                    full_url,
                ],
                capture_output=True,
                check=True,
                text=True,
            )
            return json.loads(completed.stdout)
        except Exception as curl_exc:
            raise RuntimeError(
                f"{source_name} request failed: {exc}; curl fallback failed: {curl_exc}. "
                "If this is running in a network-restricted sandbox, rerun with network approval."
            ) from curl_exc


def eastmoney_get_json(url: str, params: dict[str, Any]) -> Any:
    return get_json(url, params, source_name="Eastmoney quote")


def yahoo_chart_symbol(symbol: str) -> str:
    upper = symbol.strip().upper()
    if upper in YAHOO_SYMBOLS:
        return YAHOO_SYMBOLS[upper]
    if upper.endswith(".HK") and upper[:-3].isdigit():
        return f"{int(upper[:-3]):04d}.HK"
    if upper.endswith(".SS"):
        return upper[:-3] + ".SS"
    if upper.endswith(".SZ"):
        return upper[:-3] + ".SZ"
    if "." not in upper and symbol_market(upper) == "US":
        return upper
    return upper


def yahoo_get_json(symbol: str) -> Any:
    return get_json(
        YAHOO_CHART_URL.format(symbol=urllib.parse.quote(yahoo_chart_symbol(symbol), safe="")),
        {
            "range": "10d",
            "interval": "1d",
            "includePrePost": "false",
        },
        source_name="Yahoo chart",
        user_agent=YAHOO_USER_AGENT,
    )


def parse_yahoo_quote(symbol: str, payload: Any) -> dict[str, Any]:
    chart = payload.get("chart") or {}
    error = chart.get("error")
    if error:
        raise RuntimeError(f"Yahoo returned chart error for `{symbol}`: {error}")
    results = chart.get("result") or []
    if not results:
        raise RuntimeError(f"Yahoo returned no chart data for `{symbol}`.")
    result = results[0]
    timestamps = result.get("timestamp") or []
    closes = (((result.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
    points = [
        (int(timestamp), float(close))
        for timestamp, close in zip(timestamps, closes)
        if timestamp is not None and close is not None
    ]
    if len(points) < 2:
        raise RuntimeError(f"Yahoo returned insufficient chart data for `{symbol}`.")
    previous_timestamp, previous_close = points[-2]
    latest_timestamp, latest_close = points[-1]
    meta = result.get("meta") or {}
    timezone_name = str(meta.get("exchangeTimezoneName") or "America/New_York")
    latest_date = dt.datetime.fromtimestamp(latest_timestamp, tz=dt.timezone.utc).astimezone(
        ZoneInfo(timezone_name)
    ).date()
    change_pct = 0.0 if previous_close == 0 else (latest_close - previous_close) / previous_close * 100
    return {
        "symbol": symbol.strip().upper(),
        "name": str(meta.get("longName") or meta.get("shortName") or symbol).strip(),
        "price": latest_close,
        "prev_close": previous_close,
        "change_pct": change_pct,
        "session_date": latest_date.isoformat(),
        "regularMarketPrice": latest_close,
        "regularMarketPreviousClose": previous_close,
        "regularMarketChangePercent": change_pct,
        "regularMarketTime": latest_timestamp,
    }


def fetch_yahoo_symbol_quote(symbol: str) -> dict[str, Any]:
    return parse_yahoo_quote(symbol, yahoo_get_json(symbol))


def eastmoney_search(symbol: str) -> str:
    payload = eastmoney_get_json(
        EASTMONEY_SEARCH_URL,
        {
            "input": symbol,
            "type": 14,
            "token": EASTMONEY_SEARCH_TOKEN,
            "count": 10,
        },
    )
    items = payload.get("QuotationCodeTable", {}).get("Data", [])
    normalized = symbol.strip().upper().lstrip("^")
    for item in items:
        code = str(item.get("Code") or "").upper()
        unified = str(item.get("UnifiedCode") or "").upper()
        quote_id = str(item.get("QuoteID") or "").strip()
        if quote_id and normalized in {code, unified}:
            return quote_id
    if items:
        first = items[0]
        quote_id = str(first.get("QuoteID") or "").strip()
        if quote_id:
            return quote_id
    raise RuntimeError(f"Unable to resolve Eastmoney quote id for symbol `{symbol}`.")


def resolve_secid(symbol: str) -> str:
    upper = symbol.strip().upper()
    if upper in EASTMONEY_SECIDS:
        return EASTMONEY_SECIDS[upper]
    if upper.endswith(".SS") and upper[:-3].isdigit():
        return f"1.{upper[:-3]}"
    if upper.endswith(".SZ") and upper[:-3].isdigit():
        return f"0.{upper[:-3]}"
    if upper.endswith(".HK") and upper[:-3].isdigit():
        return f"116.{upper[:-3].zfill(5)}"
    return eastmoney_search(upper)


def fetch_eastmoney_symbol_quote(symbol: str) -> dict[str, Any]:
    payload = eastmoney_get_json(
        EASTMONEY_KLINE_URL,
        {
            "secid": resolve_secid(symbol),
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
            "klt": 101,
            "fqt": 0,
            "end": 20500000,
            "lmt": 2,
        },
    )
    data = payload.get("data") or {}
    klines = data.get("klines") or []
    if len(klines) < 2:
        raise RuntimeError(f"Eastmoney returned insufficient kline data for `{symbol}`.")
    previous_raw = str(klines[-2]).split(",")
    latest_raw = str(klines[-1]).split(",")
    if len(previous_raw) < 3 or len(latest_raw) < 3:
        raise RuntimeError(f"Unexpected kline payload for `{symbol}`: {klines[-2:]}")
    previous_close = float(previous_raw[2])
    latest_close = float(latest_raw[2])
    latest_date = dt.date.fromisoformat(latest_raw[0])
    change_pct = 0.0 if previous_close == 0 else (latest_close - previous_close) / previous_close * 100
    return {
        "symbol": symbol.strip().upper(),
        "name": str(data.get("name") or symbol).strip(),
        "price": latest_close,
        "prev_close": previous_close,
        "change_pct": change_pct,
        "session_date": latest_date.isoformat(),
    }


def fetch_symbol_quote(symbol: str) -> dict[str, Any]:
    errors: list[str] = []
    for fetcher in (fetch_eastmoney_symbol_quote, fetch_yahoo_symbol_quote):
        try:
            return fetcher(symbol)
        except Exception as exc:
            fetcher_name = getattr(fetcher, "__name__", fetcher.__class__.__name__)
            errors.append(f"{fetcher_name}: {exc}")
    raise RuntimeError("; ".join(errors))


def fetch_quotes(symbols: list[str]) -> dict[str, dict[str, Any]]:
    resolved: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for symbol in symbols:
        try:
            resolved[symbol.upper()] = fetch_symbol_quote(symbol)
        except Exception as exc:
            errors.append(f"{symbol}: {exc}")
    if not resolved:
        raise RuntimeError("Failed to fetch quote data. " + " | ".join(errors))
    return resolved


def as_float(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def extract_change_pct(item: dict[str, Any]) -> float | None:
    direct_change = as_float(item.get("change_pct"))
    if direct_change is not None:
        return direct_change
    direct = as_float(item.get("regularMarketChangePercent"))
    if direct is not None:
        return direct
    price = as_float(item.get("regularMarketPrice"))
    prev_close = as_float(item.get("regularMarketPreviousClose"))
    if price in (None, 0) or prev_close in (None, 0):
        return None
    return (price - prev_close) / prev_close * 100


def extract_market_time(item: dict[str, Any], timezone_name: str) -> dt.datetime | None:
    raw_session = item.get("session_date")
    if isinstance(raw_session, str) and raw_session:
        parsed = dt.date.fromisoformat(raw_session)
        return dt.datetime.combine(parsed, dt.time(16, 0), tzinfo=ZoneInfo(timezone_name))
    timestamp = item.get("regularMarketTime")
    if not isinstance(timestamp, (int, float)):
        return None
    return dt.datetime.fromtimestamp(int(timestamp), tz=dt.timezone.utc).astimezone(ZoneInfo(timezone_name))


def quote_line(label: str, item: dict[str, Any]) -> str | None:
    change_pct = extract_change_pct(item)
    if change_pct is None:
        return None
    return f"{label} {change_pct:+.2f}%"


def quote_change(item: dict[str, Any]) -> float | None:
    return extract_change_pct(item)


def merge_watchlist(config: MarketConfig, settings: dict[str, Any]) -> list[tuple[str, str]]:
    merged: list[tuple[str, str]] = list(config.watchlist_defaults)
    raw_mapping = settings.get("close_digest_watchlists")
    if isinstance(raw_mapping, dict):
        for raw_key, raw_values in raw_mapping.items():
            if str(raw_key).strip().lower() not in {config.code.lower(), config.label.lower()}:
                continue
            if isinstance(raw_values, list):
                for value in raw_values:
                    if not isinstance(value, str) or not value.strip():
                        continue
                    merged.append((value.strip().upper(), value.strip().upper()))
    raw_watchlist = settings.get("watchlist")
    if isinstance(raw_watchlist, list):
        for value in raw_watchlist:
            if not isinstance(value, str) or not value.strip():
                continue
            symbol = value.strip().upper()
            if symbol_market(symbol) == config.code:
                merged.append((symbol, symbol))

    deduped: list[tuple[str, str]] = []
    seen: set[str] = set()
    for symbol, label in merged:
        key = symbol.upper()
        if key in seen:
            continue
        seen.add(key)
        deduped.append((key, label))
    return deduped


def latest_session_date(quotes: dict[str, dict[str, Any]], config: MarketConfig) -> dt.date:
    dates = [
        market_time.date()
        for item in quotes.values()
        if (market_time := extract_market_time(item, config.timezone)) is not None
    ]
    if not dates:
        raise RuntimeError(f"No quote timestamps returned for {config.label}.")
    return max(dates)


def market_now(config: MarketConfig) -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).astimezone(ZoneInfo(config.timezone))


def is_market_closed(now_value: dt.datetime, config: MarketConfig) -> bool:
    if now_value.weekday() >= 5:
        return False
    cutoff = now_value.replace(hour=config.close_hour, minute=config.close_minute, second=0, microsecond=0)
    return now_value >= cutoff


def describe_close_timing(config: MarketConfig, session_date: dt.date) -> tuple[bool, str]:
    now_value = market_now(config)
    current_market_date = now_value.date()
    if not is_market_closed(now_value, config):
        return False, f"{config.label}尚未收盘，当前市场时间 {now_value.strftime('%Y-%m-%d %H:%M')}。"
    if session_date < current_market_date:
        return False, f"{config.label}当前没有新的收盘数据，最新交易日仍是 {session_date.isoformat()}。"
    return True, ""


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = load_json(path)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def save_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def already_sent(path: Path, market_code: str, session_date: dt.date) -> bool:
    state = load_state(path)
    return session_date.isoformat() in state.get(market_code, {})


def mark_sent(path: Path, market_code: str, session_date: dt.date, digest_text: str) -> None:
    state = load_state(path)
    bucket = state.setdefault(market_code, {})
    bucket[session_date.isoformat()] = {
        "sent_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "preview": digest_text[:200],
    }
    save_state(path, state)


def market_tone(changes: list[float]) -> str:
    if not changes:
        return "整体波动不大"
    avg_change = sum(changes) / len(changes)
    positives = sum(change > 0 for change in changes)
    if avg_change >= 1.2 and positives >= 3:
        return "整体强势收涨"
    if avg_change >= 0.4 and positives >= 2:
        return "整体偏强收盘"
    if avg_change <= -1.2 and positives <= 1:
        return "整体明显走弱"
    if avg_change <= -0.4 and positives <= 2:
        return "整体偏弱收盘"
    return "整体震荡收盘"


def style_line(config: MarketConfig, quotes: dict[str, dict[str, Any]]) -> str | None:
    primary_symbol, primary_label = config.style_symbols[0]
    benchmark_symbol, benchmark_label = config.style_symbols[1]
    primary = quotes.get(primary_symbol.upper())
    benchmark = quotes.get(benchmark_symbol.upper())
    if not primary or not benchmark:
        return None
    primary_change = quote_change(primary)
    benchmark_change = quote_change(benchmark)
    if primary_change is None or benchmark_change is None:
        return None
    delta = primary_change - benchmark_change
    lead = primary_label if delta >= 0 else benchmark_label
    lag = benchmark_label if delta >= 0 else primary_label
    description = f"风格: {lead}相对更强，跑赢{lag} {abs(delta):.2f}pct"

    if len(config.style_symbols) < 3:
        return description
    third_symbol, third_label = config.style_symbols[2]
    third = quotes.get(third_symbol.upper())
    third_change = quote_change(third) if third else None
    if config.code == "US" and third_change is not None:
        if third_change > benchmark_change:
            description += "；小盘同步走强"
        else:
            description += "；小盘相对承压"
    elif config.code == "CN" and third_change is not None:
        if benchmark_change > third_change:
            description += "；权重表现更稳"
        else:
            description += "；大盘权重偏弱"
    elif config.code == "HK" and third_change is not None:
        if third_change > benchmark_change:
            description += "；国企权重更稳"
        else:
            description += "；科技弹性更大"
    return description


def mover_lines(watch_quotes: list[tuple[str, str, dict[str, Any]]]) -> list[str]:
    with_change = []
    for symbol, label, item in watch_quotes:
        change_pct = quote_change(item)
        if change_pct is None:
            continue
        with_change.append((symbol, label, change_pct))

    if not with_change:
        return []

    leaders = sorted([item for item in with_change if item[2] > 0], key=lambda value: value[2], reverse=True)[:2]
    laggards = sorted([item for item in with_change if item[2] < 0], key=lambda value: value[2])[:2]
    lines: list[str] = []
    if leaders:
        lines.append("关注走强: " + "、".join(f"{label} {change:+.2f}%" for _, label, change in leaders))
    if laggards:
        lines.append("关注承压: " + "、".join(f"{label} {change:+.2f}%" for _, label, change in laggards))
    return lines


def quant_signal_line(config: MarketConfig) -> str | None:
    if config.code != "CN":
        return None
    try:
        universe_path = bot_service.resolve_universe_path("A股")
        _, picks, _ = quant_engine.screen_stocks(universe_path, "quality", top_n=3)
    except Exception:
        return None
    if not picks:
        return None
    summary = " / ".join(f"{item['ticker']} {item['name']}" for item in picks[:3])
    return f"量化观察: 质量策略前排 {summary}"


def build_digest(config: MarketConfig, settings: dict[str, Any]) -> tuple[str, dt.date]:
    watchlist = merge_watchlist(config, settings)
    lookup: dict[str, str] = {symbol.upper(): label for symbol, label in [*config.primary_symbols, *watchlist]}
    symbols = list(lookup.keys())
    quotes = fetch_quotes(symbols)
    session_date = latest_session_date(quotes, config)

    primary_lines = []
    primary_changes: list[float] = []
    for symbol, label in config.primary_symbols:
        item = quotes.get(symbol.upper())
        if not item:
            continue
        line = quote_line(label, item)
        if line:
            primary_lines.append(line)
        change_pct = quote_change(item)
        if change_pct is not None:
            primary_changes.append(change_pct)

    if not primary_lines:
        raise RuntimeError(f"{config.label} primary quotes are unavailable.")

    watch_quotes = [
        (symbol.upper(), label, quotes[symbol.upper()])
        for symbol, label in watchlist
        if symbol.upper() in quotes
    ]

    lines = [
        f"【{config.label}收盘总结｜{session_date.isoformat()}】",
        f"主线: {market_tone(primary_changes)}。",
        "指数: " + " | ".join(primary_lines),
    ]
    maybe_style = style_line(config, quotes)
    if maybe_style:
        lines.append(maybe_style)
    lines.extend(mover_lines(watch_quotes))
    maybe_quant = quant_signal_line(config)
    if maybe_quant:
        lines.append(maybe_quant)
    lines.append("仅供复盘参考，不构成投资建议。")
    return "\n".join(lines), session_date


def send_wechat_message(message: str, dry_run: bool) -> None:
    command = [
        "node",
        str(PROJECT_ROOT / "send_personal_wechat.mjs"),
        "--message",
        message,
    ]
    if dry_run:
        command.append("--dry-run")
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        output = "\n".join(part for part in (completed.stderr.strip(), completed.stdout.strip()) if part)
        raise RuntimeError(format_wechat_send_error(output))
    if completed.stdout:
        sys.stdout.write(completed.stdout)
    if completed.stderr:
        sys.stderr.write(completed.stderr)


def format_wechat_send_error(error_text: str) -> str:
    if "ret=-2" in error_text or '"ret":-2' in error_text:
        return (
            f"WeChat send failed: {error_text}. The cached WeChat context_token is likely expired. "
            "Send any message to the personal WeChat bot while it is running, then rerun this command."
        )
    if "ENOTFOUND" in error_text or "getaddrinfo" in error_text:
        return (
            f"WeChat send failed: {error_text}. DNS lookup failed for the WeChat bridge host; "
            "check network access from this runtime."
        )
    return f"WeChat send failed: {error_text or 'Unknown WeChat send failure.'}"


def write_output(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content + "\n", encoding="utf-8")


def run_build(args: argparse.Namespace) -> int:
    config = normalize_market(args.market)
    digest, _ = build_digest(config, load_settings())
    if args.output:
        write_output(Path(args.output).expanduser(), digest)
    sys.stdout.write(digest + "\n")
    return 0


def run_send(args: argparse.Namespace) -> int:
    config = normalize_market(args.market)
    state_path = Path(args.state_path).expanduser()
    digest, session_date = build_digest(config, load_settings())

    if not args.force:
        ready, reason = describe_close_timing(config, session_date)
        if not ready:
            if not args.quiet_skip:
                print(reason)
            return 0
        if already_sent(state_path, config.code, session_date):
            if not args.quiet_skip:
                print(f"{config.label} {session_date.isoformat()} 的收盘总结已经发送过，跳过。")
            return 0

    send_wechat_message(digest, dry_run=args.dry_run)
    if not args.dry_run:
        mark_sent(state_path, config.code, session_date, digest)
    print(f"{config.label} 收盘总结已处理: {session_date.isoformat()}")
    return 0


def main() -> int:
    args = parse_args()
    try:
        if args.command == "build":
            return run_build(args)
        if args.command == "send":
            return run_send(args)
        raise RuntimeError(f"Unsupported command: {args.command}")
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
