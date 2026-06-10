#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys
import textwrap
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parent
LOCAL_SETTINGS_PATH = ROOT / ".cache" / "local_settings.json"
FINCHAT_API_URL = "https://api.finchat.io/v1/query"
FISCAL_API_BASE = "https://api.fiscal.ai"
DEFAULT_HTTP_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/137.0.0.0 Safari/537.36"
)

PORTALS: dict[str, dict[str, str]] = {
    "koyfin": {
        "home": "https://app.koyfin.com/",
        "movers": "https://app.koyfin.com/mov",
        "sectors": "https://app.koyfin.com/setf",
        "calendar": "https://app.koyfin.com/ecal",
        "dashboards": "https://app.koyfin.com/myd",
        "help": "https://www.koyfin.com/help/",
        "pricing": "https://www.koyfin.com/pricing/",
    },
    "finchat": {
        "home": "https://finchat.io/",
        "pricing": "https://marketing.finchat.io/pricing/",
        "guide": "https://finchat.io/blog/how-to-use-finchat/",
        "api": "https://docs.finchat.io/copilot-api",
    },
}

DEFAULT_FINCHAT_RULES = [
    "Answer in concise Chinese unless the question clearly requires another language.",
    "Focus on what matters for an investor: the main driver, valuation context, risks, and what to watch next.",
]

FREE_FISCAL_COMPANY_KEYS = {
    "MSFT": "NASDAQ_MSFT",
    "NVDA": "NASDAQ_NVDA",
    "AMZN": "NASDAQ_AMZN",
    "GOOG": "NASDAQ_GOOG",
    "GOOGL": "NASDAQ_GOOG",
    "TSLA": "NASDAQ_TSLA",
    "LLY": "NYSE_LLY",
    "AVGO": "NASDAQ_AVGO",
    "V": "NYSE_V",
    "MA": "NYSE_MA",
    "PG": "NYSE_PG",
    "NFLX": "NASDAQ_NFLX",
    "MCD": "NYSE_MCD",
    "AMGN": "NASDAQ_AMGN",
    "CAT": "NYSE_CAT",
    "UBER": "NYSE_UBER",
    "MDT": "NYSE_MDT",
    "DUK": "NYSE_DUK",
    "EQIX": "NASDAQ_EQIX",
    "BRO": "NYSE_BRO",
    "ZM": "NASDAQ_ZM",
    "MKC": "NYSE_MKC",
    "RYAN": "NYSE_RYAN",
    "MOH": "NYSE_MOH",
    "CFG": "NYSE_CFG",
    "JPM": "NYSE_JPM",
    "ASML": "NASDAQ_ASML",
    "SHEL": "NYSE_SHEL",
    "SONY": "NYSE_SONY",
    "CB": "NYSE_CB",
    "MELI": "NASDAQ_MELI",
    "CSU": "TSX_CSU",
    "ATD": "TSX_ATD",
    "DOL": "TSX_DOL",
    "CLS": "TSX_CLS",
    "TFII": "TSX_TFII",
    "MC": "XPAR_MC",
    "NESN": "XSWX_NESN",
    "RMS": "XPAR_RMS",
    "SIE": "XETR_SIE",
    "AIR": "XPAR_AIR",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Launch Koyfin/FinChat and ask FinChat questions from this repo."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    links_parser = subparsers.add_parser("links", help="Print the available portal shortcuts.")
    links_parser.add_argument(
        "--portal",
        choices=["all", *PORTALS.keys()],
        default="all",
        help="Limit output to a single portal.",
    )

    open_parser = subparsers.add_parser("open", help="Open a Koyfin or FinChat page in your browser.")
    open_parser.add_argument("portal", choices=["koyfin", "finchat", "all"])
    open_parser.add_argument(
        "--page",
        help="Optional page key such as movers, sectors, pricing, api, or guide.",
    )
    open_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the URLs without opening them.",
    )

    ask_parser = subparsers.add_parser("ask-finchat", help="Send a question to the FinChat Copilot API.")
    ask_parser.add_argument("query", help="Question to send to FinChat.")
    ask_parser.add_argument(
        "--language",
        default="zh-CN",
        help="Language hint for FinChat, default: zh-CN.",
    )
    ask_parser.add_argument(
        "--rule",
        action="append",
        default=[],
        help="Additional instruction applied to the prompt. Can be passed multiple times.",
    )
    ask_parser.add_argument(
        "--no-search",
        action="store_true",
        help="Disable FinChat's web search fallback.",
    )
    ask_parser.add_argument(
        "--no-inline-sources",
        action="store_true",
        help="Disable inline sourcing in the returned answer.",
    )
    ask_parser.add_argument(
        "--send-lark",
        action="store_true",
        help="Forward the final answer to the configured Lark targets.",
    )
    ask_parser.add_argument(
        "--title",
        default="FinChat 市场问答",
        help="Title used when --send-lark is enabled.",
    )

    market_brief_parser = subparsers.add_parser(
        "market-brief",
        help="Generate a structured market-hotspot brief with FinChat.",
    )
    market_brief_parser.add_argument(
        "--markets",
        default="US,HK,CN",
        help="Comma-separated markets to cover. Default: US,HK,CN.",
    )
    market_brief_parser.add_argument(
        "--horizon",
        choices=["today", "week"],
        default="today",
        help="Whether to focus on today's setup or the current week.",
    )
    market_brief_parser.add_argument(
        "--send-lark",
        action="store_true",
        help="Forward the final answer to the configured Lark targets.",
    )
    market_brief_parser.add_argument(
        "--title",
        default="市场热点简报",
        help="Title used when --send-lark is enabled.",
    )

    stock_brief_parser = subparsers.add_parser(
        "stock-brief",
        help="Generate a structured stock brief with FinChat.",
    )
    stock_brief_parser.add_argument("ticker", help="Ticker or company name, for example NVDA or 腾讯控股.")
    stock_brief_parser.add_argument(
        "--send-lark",
        action="store_true",
        help="Forward the final answer to the configured Lark targets.",
    )
    stock_brief_parser.add_argument(
        "--title",
        help="Optional title used when --send-lark is enabled.",
    )

    watchlist_brief_parser = subparsers.add_parser(
        "watchlist-brief",
        help="Summarize a configured watchlist with FinChat.",
    )
    watchlist_brief_parser.add_argument(
        "--tickers",
        help="Comma-separated tickers or company names. Defaults to watchlist in local settings.",
    )
    watchlist_brief_parser.add_argument(
        "--send-lark",
        action="store_true",
        help="Forward the final answer to the configured Lark targets.",
    )
    watchlist_brief_parser.add_argument(
        "--title",
        default="自选列表简报",
        help="Title used when --send-lark is enabled.",
    )

    free_news_parser = subparsers.add_parser(
        "top-news-free",
        help="Fetch a free top-news digest from Fiscal.ai.",
    )
    free_news_parser.add_argument(
        "--limit",
        type=int,
        default=8,
        help="Number of headlines to display. Default: 8.",
    )
    free_news_parser.add_argument(
        "--importance-max",
        type=int,
        default=2,
        help="Highest importance score to include, 1 is most material. Default: 2.",
    )

    free_stock_parser = subparsers.add_parser(
        "stock-brief-free",
        help="Fetch a free stock brief from Fiscal.ai for supported companies.",
    )
    free_stock_parser.add_argument(
        "ticker",
        help="Supported ticker or canonical company key, for example NVDA or NASDAQ_NVDA.",
    )

    free_watchlist_parser = subparsers.add_parser(
        "watchlist-free",
        help="Fetch a compact free watchlist snapshot from Fiscal.ai.",
    )
    free_watchlist_parser.add_argument(
        "--tickers",
        help="Comma-separated supported tickers. Defaults to fiscal_watchlist in local settings.",
    )
    free_watchlist_parser.add_argument(
        "--send-lark",
        action="store_true",
        help="Forward the final answer to the configured Lark targets.",
    )
    free_watchlist_parser.add_argument(
        "--title",
        default="Fiscal 免费自选股快照",
        help="Title used when --send-lark is enabled.",
    )

    free_market_parser = subparsers.add_parser(
        "market-brief-free",
        help="Build a compact free market brief from Fiscal.ai top news and your free watchlist.",
    )
    free_market_parser.add_argument(
        "--tickers",
        help="Comma-separated supported tickers. Defaults to fiscal_watchlist in local settings.",
    )
    free_market_parser.add_argument(
        "--top-news-limit",
        type=int,
        default=6,
        help="Number of top news headlines to include. Default: 6.",
    )
    free_market_parser.add_argument(
        "--watch-news-limit",
        type=int,
        default=3,
        help="Number of watchlist news lines to include. Default: 3.",
    )
    free_market_parser.add_argument(
        "--send-lark",
        action="store_true",
        help="Forward the final answer to the configured Lark targets.",
    )
    free_market_parser.add_argument(
        "--title",
        default="免费市场简报",
        help="Title used when --send-lark is enabled.",
    )
    return parser.parse_args()


def load_local_settings() -> dict[str, Any]:
    if not LOCAL_SETTINGS_PATH.exists():
        return {}
    return json.loads(LOCAL_SETTINGS_PATH.read_text(encoding="utf-8"))


def resolve_finchat_api_key() -> str:
    settings = load_local_settings()
    api_key = os.environ.get("FINCHAT_API_KEY") or settings.get("finchat_api_key")
    if api_key:
        return str(api_key)
    raise RuntimeError(
        "Missing FinChat API key. Set FINCHAT_API_KEY or add "
        '{"finchat_api_key":"..."} to .cache/local_settings.json.'
    )


def resolve_fiscal_api_key() -> str:
    settings = load_local_settings()
    api_key = os.environ.get("FISCAL_API_KEY") or settings.get("fiscal_api_key")
    if api_key:
        return str(api_key)
    raise RuntimeError(
        "Missing Fiscal.ai API key. Set FISCAL_API_KEY or add "
        '{"fiscal_api_key":"..."} to .cache/local_settings.json.'
    )


def selected_portals(portal: str) -> dict[str, dict[str, str]]:
    if portal == "all":
        return PORTALS
    return {portal: PORTALS[portal]}


def print_links(portal: str) -> None:
    chunks: list[str] = []
    for portal_name, pages in selected_portals(portal).items():
        lines = [f"{portal_name}:"]
        for page_name, url in pages.items():
            lines.append(f"  {page_name:<10} {url}")
        chunks.append("\n".join(lines))
    print("\n\n".join(chunks))


def urls_to_open(portal: str, page: str | None) -> list[str]:
    if portal == "all":
        if page:
            raise RuntimeError("--page cannot be used with portal=all.")
        return [PORTALS["koyfin"]["home"], PORTALS["finchat"]["home"]]

    pages = PORTALS[portal]
    if page:
        if page not in pages:
            raise RuntimeError(
                f"Unknown page '{page}' for {portal}. Available pages: {', '.join(sorted(pages))}"
            )
        return [pages[page]]
    return [pages["home"]]


def open_urls(urls: list[str], dry_run: bool) -> None:
    if dry_run:
        for url in urls:
            print(url)
        return

    for url in urls:
        opened = webbrowser.open(url)
        status = "opened" if opened else "requested"
        print(f"{status}: {url}")


def fiscal_api_get(api_key: str, path: str, params: dict[str, Any]) -> Any:
    query = urllib.parse.urlencode({key: value for key, value in params.items() if value is not None})
    url = f"{FISCAL_API_BASE}{path}"
    if query:
        url += f"?{query}"

    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": DEFAULT_HTTP_USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://fiscal.ai",
            "Referer": "https://fiscal.ai/",
            "X-Api-Key": api_key,
        },
        method="GET",
    )
    try:
        with urllib.request.build_opener().open(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Fiscal.ai API HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Fiscal.ai API request failed: {exc}. "
            "If this is running in a network-restricted sandbox, rerun with network approval."
        ) from exc


def is_feature_unavailable_error(exc: RuntimeError, feature_name: str) -> bool:
    message = str(exc).lower()
    return feature_name.lower() in message and "requires" in message and "feature" in message


def supported_free_tickers_text() -> str:
    return ", ".join(sorted(FREE_FISCAL_COMPANY_KEYS))


def normalize_fiscal_company_key(symbol: str) -> str:
    cleaned = symbol.strip().upper()
    if not cleaned:
        raise RuntimeError("Ticker is empty.")
    if "_" in cleaned:
        return cleaned
    if cleaned in FREE_FISCAL_COMPANY_KEYS:
        return FREE_FISCAL_COMPANY_KEYS[cleaned]
    raise RuntimeError(
        f"{symbol} is not in the configured free Fiscal.ai list. "
        f"Supported examples: {supported_free_tickers_text()}"
    )


def company_key_label(company_key: str) -> str:
    return company_key.split("_", 1)[-1]


def unwrap_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("data", "results", "items", "records"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def first_text(item: dict[str, Any], keys: list[str]) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def first_number(item: dict[str, Any], keys: list[str]) -> float | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                continue
    return None


def format_pct_change(current: float, previous: float | None) -> str:
    if previous in (None, 0):
        return "n/a"
    delta = (current - previous) / previous
    return f"{delta:+.1%}"


def render_table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def render_row(row: list[str]) -> str:
        return " | ".join(cell.ljust(widths[index]) for index, cell in enumerate(row))

    parts = [render_row(headers), "-+-".join("-" * width for width in widths)]
    parts.extend(render_row(row) for row in rows)
    return "\n".join(parts)


def safe_date_text(raw: str) -> str:
    if not raw:
        return "-"
    return raw.split("T", 1)[0]


def fiscal_company_profile(api_key: str, company_key: str) -> dict[str, Any]:
    payload = fiscal_api_get(api_key, "/v2/company/profile", {"companyKey": company_key})
    if isinstance(payload, dict):
        return payload
    raise RuntimeError(f"Unexpected Fiscal.ai profile response for {company_key}.")


def fiscal_company_prices(api_key: str, company_key: str) -> list[dict[str, Any]]:
    return unwrap_records(
        fiscal_api_get(api_key, "/v1/company/stock-prices", {"companyKey": company_key})
    )


def fiscal_company_news(api_key: str, company_key: str, limit: int = 5) -> list[dict[str, Any]]:
    news = unwrap_records(
        fiscal_api_get(
            api_key,
            "/v1/company/news",
            {"companyKey": company_key, "importance": 1},
        )
    )
    if not news:
        news = unwrap_records(fiscal_api_get(api_key, "/v1/company/news", {"companyKey": company_key}))
    return news[:limit]


def fiscal_company_filings(api_key: str, company_key: str, limit: int = 3) -> list[dict[str, Any]]:
    return unwrap_records(fiscal_api_get(api_key, "/v2/company/filings", {"companyKey": company_key}))[:limit]


def fiscal_top_news(api_key: str, limit: int, importance_max: int) -> list[dict[str, Any]]:
    params = {
        "pageSize": max(1, min(limit, 25)),
        "minImportance": 1,
        "maxImportance": max(1, min(importance_max, 5)),
    }
    try:
        return unwrap_records(fiscal_api_get(api_key, "/v1/top-news", params))[:limit]
    except RuntimeError as exc:
        if is_feature_unavailable_error(exc, "news"):
            return []
        raise


def price_summary_rows(prices: list[dict[str, Any]]) -> list[list[str]]:
    if not prices:
        return [["Latest", "-", "-", "-"]]

    latest = prices[0]
    latest_price = first_number(latest, ["close", "price", "adjustedClose", "value"])
    latest_date = safe_date_text(first_text(latest, ["date", "tradingDate"]))
    if latest_price is None:
        return [["Latest", latest_date, "-", "-"]]

    checkpoints = [("Latest", 0), ("5D", 4), ("20D", 19)]
    rows: list[list[str]] = []
    for label, index in checkpoints:
        if index >= len(prices):
            rows.append([label, "-", "-", "-"])
            continue
        point = prices[index]
        point_price = first_number(point, ["close", "price", "adjustedClose", "value"])
        point_date = safe_date_text(first_text(point, ["date", "tradingDate"]))
        if point_price is None:
            rows.append([label, point_date, "-", "-"])
            continue
        rows.append(
            [
                label,
                point_date,
                f"{point_price:,.2f}",
                format_pct_change(latest_price, point_price if index else latest_price),
            ]
        )
    return rows


def latest_and_previous_price(prices: list[dict[str, Any]], previous_index: int = 4) -> tuple[float | None, float | None]:
    latest_price = first_number(prices[0], ["close", "price", "adjustedClose", "value"]) if prices else None
    previous_price = (
        first_number(prices[previous_index], ["close", "price", "adjustedClose", "value"])
        if len(prices) > previous_index
        else None
    )
    return latest_price, previous_price


def render_news_rows(news: list[dict[str, Any]]) -> list[list[str]]:
    rows: list[list[str]] = []
    for item in news:
        rows.append(
            [
                safe_date_text(first_text(item, ["publishedAt", "date", "collectedAt"])),
                first_text(item, ["eventType", "category"]) or "-",
                str(int(first_number(item, ["importance"]) or 0)) if first_number(item, ["importance"]) is not None else "-",
                first_text(item, ["headline", "title", "summary", "text"])[:90] or "-",
            ]
        )
    return rows or [["-", "-", "-", "-"]]


def render_filing_rows(filings: list[dict[str, Any]]) -> list[list[str]]:
    rows: list[list[str]] = []
    for item in filings:
        rows.append(
            [
                safe_date_text(first_text(item, ["filing_date", "filingDate"])),
                first_text(item, ["document_type", "documentType", "formType"]) or "-",
                safe_date_text(first_text(item, ["report_date", "reportDate"])),
            ]
        )
    return rows or [["-", "-", "-"]]


def fiscal_stock_brief_text(company_key: str, profile: dict[str, Any], prices: list[dict[str, Any]], news: list[dict[str, Any]], filings: list[dict[str, Any]]) -> str:
    name = first_text(profile, ["name", "companyName"]) or company_key_label(company_key)
    ticker = first_text(profile, ["ticker"]) or company_key_label(company_key)
    sector = first_text(profile, ["sector"])
    industry = first_text(profile, ["industry"])
    country = first_text(profile, ["countryName", "country"])
    description = first_text(profile, ["shortDescription", "description"])

    parts = [
        f"{name} ({ticker})",
        f"Company key: {company_key}",
    ]
    meta_bits = [item for item in [sector, industry, country] if item]
    if meta_bits:
        parts.append(" / ".join(meta_bits))
    if description:
        parts.append("")
        parts.append(textwrap.fill(description, width=96))

    parts.extend(
        [
            "",
            "Price snapshot:",
            "```text",
            render_table(["Point", "Date", "Price", "Vs latest"], price_summary_rows(prices)),
            "```",
            "",
            "Recent news:",
            "```text",
            render_table(["Date", "Type", "Imp", "Headline"], render_news_rows(news)),
            "```",
            "",
            "Recent filings:",
            "```text",
            render_table(["Filed", "Type", "Report"], render_filing_rows(filings)),
            "```",
        ]
    )
    return "\n".join(parts).strip() + "\n"


def fiscal_top_news_text(items: list[dict[str, Any]]) -> str:
    rows: list[list[str]] = []
    for item in items:
        rows.append(
            [
                safe_date_text(first_text(item, ["publishedAt", "date", "collectedAt"])),
                first_text(item, ["companyKey", "ticker", "symbol"]) or "-",
                str(int(first_number(item, ["importance"]) or 0)) if first_number(item, ["importance"]) is not None else "-",
                first_text(item, ["eventType", "category"]) or "-",
                first_text(item, ["headline", "title", "summary", "text"])[:80] or "-",
            ]
        )
    if not rows:
        rows = [["-", "-", "-", "-", "No news found"]]
    return "\n".join(
        [
            "Fiscal.ai top news",
            "```text",
            render_table(["Date", "Company", "Imp", "Type", "Headline"], rows),
            "```",
        ]
    ) + "\n"


def fiscal_watchlist_headline_rows(api_key: str, company_keys: list[str], limit: int) -> list[list[str]]:
    rows: list[list[str]] = []
    for company_key in company_keys:
        if len(rows) >= limit:
            break
        news = fiscal_company_news(api_key, company_key, limit=1)
        if not news:
            continue
        item = news[0]
        rows.append(
            [
                safe_date_text(first_text(item, ["publishedAt", "date", "collectedAt"])),
                company_key_label(company_key),
                str(int(first_number(item, ["importance"]) or 0)) if first_number(item, ["importance"]) is not None else "-",
                first_text(item, ["eventType", "category"]) or "-",
                first_text(item, ["headline", "title", "summary", "text"])[:80] or "-",
            ]
        )
    return rows


def fiscal_watchlist_text(api_key: str, company_keys: list[str]) -> str:
    rows = fiscal_watchlist_rows(api_key, company_keys)
    return "\n".join(
        [
            "Fiscal.ai free watchlist snapshot",
            "```text",
            render_table(["Ticker", "Name", "Price", "5D", "Sector"], rows or [["-", "-", "-", "-", "-"]]),
            "```",
        ]
    ) + "\n"


def fiscal_watchlist_rows(api_key: str, company_keys: list[str]) -> list[list[str]]:
    return fiscal_watchlist_rows_from_snapshots(fiscal_watchlist_snapshot(api_key, company_keys))


def fiscal_watchlist_rows_from_snapshots(snapshots: list[dict[str, Any]]) -> list[list[str]]:
    rows: list[list[str]] = []
    for item in snapshots:
        rows.append(
            [
                str(item.get("ticker") or company_key_label(str(item.get("company_key") or ""))),
                str(item.get("name") or company_key_label(str(item.get("company_key") or "")))[:24],
                f"{item['latest_price']:,.2f}" if item.get("latest_price") is not None else "-",
                f"{item['change_5d']:+.1%}" if item.get("change_5d") is not None else "-",
                str(item.get("sector") or "-"),
            ]
        )
    return rows


def fiscal_watchlist_snapshot(api_key: str, company_keys: list[str]) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    for company_key in company_keys:
        profile = fiscal_company_profile(api_key, company_key)
        prices = fiscal_company_prices(api_key, company_key)
        latest_price, previous_price = latest_and_previous_price(prices, previous_index=4)
        ticker = first_text(profile, ["ticker"]) or company_key_label(company_key)
        name = first_text(profile, ["name", "companyName"]) or company_key_label(company_key)
        sector = first_text(profile, ["sector"]) or "-"
        change_5d = None
        if latest_price not in (None, 0) and previous_price not in (None, 0):
            change_5d = (latest_price - previous_price) / previous_price
        snapshots.append(
            {
                "company_key": company_key,
                "ticker": ticker,
                "name": name,
                "sector": sector,
                "latest_price": latest_price,
                "previous_price": previous_price,
                "change_5d": change_5d,
            }
        )
    return snapshots


def fiscal_watchlist_news_lines(api_key: str, company_keys: list[str], limit: int) -> list[str]:
    lines: list[str] = []
    for company_key in company_keys:
        if len(lines) >= limit:
            break
        news = fiscal_company_news(api_key, company_key, limit=1)
        if not news:
            continue
        item = news[0]
        ticker = company_key_label(company_key)
        headline = first_text(item, ["headline", "title", "summary", "text"]) or "No headline"
        published = safe_date_text(first_text(item, ["publishedAt", "date", "collectedAt"]))
        event_type = first_text(item, ["eventType", "category"]) or "news"
        lines.append(f"- {ticker} | {published} | {event_type} | {headline[:110]}")
    return lines


def fiscal_market_brief_text(
    *,
    top_news_items: list[dict[str, Any]],
    top_news_rows: list[list[str]],
    watchlist_rows: list[list[str]],
    snapshots: list[dict[str, Any]],
    watchlist_news_lines: list[str],
) -> str:
    today = dt.date.today().isoformat()
    strongest = max(
        (item for item in snapshots if item.get("change_5d") is not None),
        key=lambda item: item["change_5d"],
        default=None,
    )
    weakest = min(
        (item for item in snapshots if item.get("change_5d") is not None),
        key=lambda item: item["change_5d"],
        default=None,
    )

    summary_lines = [f"Fiscal.ai free market brief | {today}"]
    if strongest:
        summary_lines.append(
            f"Best 5D move on watchlist: {strongest['ticker']} ({strongest['change_5d']:+.1%})"
        )
    if weakest:
        summary_lines.append(
            f"Worst 5D move on watchlist: {weakest['ticker']} ({weakest['change_5d']:+.1%})"
        )

    parts = [
        "\n".join(summary_lines),
        "",
        "Top news:" if top_news_items else "Watchlist headlines:",
        "```text",
        render_table(
            ["Date", "Company", "Imp", "Type", "Headline"],
            top_news_rows or [["-", "-", "-", "-", "No news found"]],
        ),
        "```",
        "",
        "Watchlist snapshot:",
        "```text",
        render_table(["Ticker", "Name", "Price", "5D", "Sector"], watchlist_rows or [["-", "-", "-", "-", "-"]]),
        "```",
    ]

    if watchlist_news_lines:
        parts.extend(["", "Watchlist catalysts:"])
        parts.extend(watchlist_news_lines)

    return "\n".join(parts).strip() + "\n"


def finchat_payload(
    query: str,
    *,
    language: str = "zh-CN",
    extra_rules: list[str] | None = None,
    enable_search: bool = True,
    inline_sourcing: bool = True,
) -> dict[str, Any]:
    return {
        "query": query,
        "history": [],
        "inlineSourcing": inline_sourcing,
        "stream": False,
        "generateChatTitle": True,
        "generateFollowUpQuestions": True,
        "rules": [*DEFAULT_FINCHAT_RULES, *(extra_rules or [])],
        "enableSearch": enable_search,
        "language": language,
    }


def call_finchat_api(api_key: str, payload: dict[str, Any]) -> Any:
    request = urllib.request.Request(
        FINCHAT_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.build_opener().open(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"FinChat API HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"FinChat API request failed: {exc}. "
            "If this is running in a network-restricted sandbox, rerun with network approval."
        ) from exc


def extract_assistant_message(response: Any) -> tuple[str, str | None, list[str]]:
    title: str | None = None
    follow_ups: list[str] = []

    if isinstance(response, dict):
        title = response.get("title") if isinstance(response.get("title"), str) else None
        follow_ups = [item for item in response.get("followUpQuestions", []) if isinstance(item, str)]
        messages = response.get("messages", [])
    else:
        messages = response

    if not isinstance(messages, list):
        raise RuntimeError(f"Unexpected FinChat response shape: {type(response)!r}")

    assistant_parts: list[str] = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        if item.get("role") != "assistant":
            continue
        content = item.get("content")
        if isinstance(content, str) and content.strip():
            assistant_parts.append(content.strip())
        maybe_title = item.get("title")
        if title is None and isinstance(maybe_title, str) and maybe_title.strip():
            title = maybe_title.strip()
        inline_follow_ups = item.get("followUpQuestions")
        if not follow_ups and isinstance(inline_follow_ups, list):
            follow_ups = [entry for entry in inline_follow_ups if isinstance(entry, str)]

    if not assistant_parts:
        raise RuntimeError("FinChat returned no assistant response.")

    return "\n\n".join(assistant_parts), title, follow_ups


def render_finchat_output(answer: str, title: str | None, follow_ups: list[str]) -> str:
    parts = []
    if title:
        parts.append(title)
        parts.append("=" * len(title))
        parts.append("")
    parts.append(answer.strip())
    if follow_ups:
        parts.append("")
        parts.append("Follow-up ideas:")
        parts.extend(f"- {item}" for item in follow_ups[:5])
    return "\n".join(parts).strip() + "\n"


def send_to_lark(title: str, body: str) -> None:
    from monitor import send_lark_outputs

    message = f"{title}\n\n{body}"
    send_lark_outputs(message)


def emit_text_output(body: str, *, send_lark: bool = False, lark_title: str | None = None) -> int:
    sys.stdout.write(body)
    if send_lark:
        send_to_lark(lark_title or "市场简报", body)
        print("\nSent to Lark.")
    return 0


def run_finchat_query(
    query: str,
    *,
    language: str = "zh-CN",
    extra_rules: list[str] | None = None,
    enable_search: bool = True,
    inline_sourcing: bool = True,
    send_lark: bool = False,
    lark_title: str | None = None,
) -> int:
    api_key = resolve_finchat_api_key()
    response = call_finchat_api(
        api_key,
        finchat_payload(
            query,
            language=language,
            extra_rules=extra_rules,
            enable_search=enable_search,
            inline_sourcing=inline_sourcing,
        ),
    )
    answer, title, follow_ups = extract_assistant_message(response)
    output = render_finchat_output(answer, title, follow_ups)
    sys.stdout.write(output)

    if send_lark:
        final_title = lark_title if lark_title else (title or "FinChat 市场问答")
        send_to_lark(final_title, output)
        print("\nSent to Lark.")

    return 0


def normalize_csv_items(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def setting_list(name: str) -> list[str]:
    values = load_local_settings().get(name, [])
    if not isinstance(values, list):
        return []
    return [str(value).strip() for value in values if str(value).strip()]


def market_brief_query(markets: list[str], horizon: str) -> str:
    scope = "今天" if horizon == "today" else "本周到目前为止"
    market_text = "、".join(markets)
    return textwrap.dedent(
        f"""
        请给我一份面向个人投资者的{scope}市场热点简报，覆盖 {market_text} 市场。
        请按下面结构输出：
        1. 先给一句话总结现在市场主线。
        2. 列出 3-5 个最重要的热点或风险点，每个都说明：
           - 发生了什么
           - 为什么市场在意
           - 受益/受损的板块或资产
        3. 给出 3 个最值得继续跟踪的方向，并说明接下来应关注哪些数据、财报、政策或价格信号。
        4. 如果有明显的拥挤交易或高风险叙事，也单独提醒。
        5. 整体尽量具体、可执行，少空话。
        """
    ).strip()


def stock_brief_query(ticker: str) -> str:
    return textwrap.dedent(
        f"""
        请从投资者视角给我一份 {ticker} 的个股速览。
        请按下面结构输出：
        1. 用两三句话说明公司现在最核心的投资逻辑。
        2. 最近 3 个最重要的驱动因素或变化。
        3. 当前市场最关注的估值锚点或关键指标。
        4. 主要风险点。
        5. 接下来 1-3 个季度最值得跟踪的催化剂或验证点。
        6. 最后给一个“适合继续观察 / 适合逢低研究 / 需要谨慎回避”的倾向性结论，并解释原因。
        请写得简洁、具体，不要泛泛而谈。
        """
    ).strip()


def watchlist_brief_query(tickers: list[str]) -> str:
    joined = "、".join(tickers)
    return textwrap.dedent(
        f"""
        请给我一份自选股简报，覆盖：{joined}。
        对每个标的都用统一格式输出：
        1. 当前最重要的看点一句话
        2. 最近新增或强化的正面因素
        3. 最近新增或强化的风险因素
        4. 接下来最值得跟踪的一个事件或指标
        5. 用“偏强 / 中性 / 偏弱”做一个很简短的观察评级
        最后再补一个汇总：这份名单里现在谁最值得优先看，谁需要最谨慎。
        """
    ).strip()


def run_finchat(args: argparse.Namespace) -> int:
    return run_finchat_query(
        args.query,
        language=args.language,
        extra_rules=args.rule,
        enable_search=not args.no_search,
        inline_sourcing=not args.no_inline_sources,
        send_lark=args.send_lark,
        lark_title=args.title,
    )


def run_market_brief(args: argparse.Namespace) -> int:
    markets = normalize_csv_items(args.markets)
    if not markets:
        markets = setting_list("market_brief_markets") or ["US", "HK", "CN"]
    return run_finchat_query(
        market_brief_query(markets, args.horizon),
        extra_rules=[
            "Prioritize what's changed recently and avoid generic macro boilerplate.",
            "If a market has little that matters today, say so briefly instead of filling space.",
        ],
        send_lark=args.send_lark,
        lark_title=args.title,
    )


def run_stock_brief(args: argparse.Namespace) -> int:
    title = args.title or f"{args.ticker} 个股速览"
    return run_finchat_query(
        stock_brief_query(args.ticker),
        extra_rules=[
            "Be explicit about what would make the thesis stronger or weaker from here.",
        ],
        send_lark=args.send_lark,
        lark_title=title,
    )


def run_watchlist_brief(args: argparse.Namespace) -> int:
    tickers = normalize_csv_items(args.tickers) if args.tickers else setting_list("watchlist")
    if not tickers:
        raise RuntimeError(
            "No watchlist configured. Pass --tickers NVDA,TSLA or add "
            '{"watchlist":["NVDA","TSLA"]} to .cache/local_settings.json.'
        )
    return run_finchat_query(
        watchlist_brief_query(tickers),
        extra_rules=[
            "Keep each ticker section concise and comparable.",
        ],
        send_lark=args.send_lark,
        lark_title=args.title,
    )


def run_top_news_free(args: argparse.Namespace) -> int:
    api_key = resolve_fiscal_api_key()
    items = fiscal_top_news(api_key, args.limit, args.importance_max)
    if items:
        return emit_text_output(fiscal_top_news_text(items))
    rows = fiscal_watchlist_headline_rows(
        api_key,
        [normalize_fiscal_company_key(item) for item in setting_list("fiscal_watchlist")],
        args.limit,
    )
    return emit_text_output(
        "\n".join(
            [
                "Fiscal.ai top news",
                "Top-news endpoint is not enabled on this plan, so this is a watchlist-headline fallback.",
                "```text",
                render_table(["Date", "Company", "Imp", "Type", "Headline"], rows or [["-", "-", "-", "-", "No news found"]]),
                "```",
            ]
        )
        + "\n"
    )


def run_stock_brief_free(args: argparse.Namespace) -> int:
    api_key = resolve_fiscal_api_key()
    company_key = normalize_fiscal_company_key(args.ticker)
    output = fiscal_stock_brief_text(
        company_key,
        fiscal_company_profile(api_key, company_key),
        fiscal_company_prices(api_key, company_key),
        fiscal_company_news(api_key, company_key),
        fiscal_company_filings(api_key, company_key),
    )
    return emit_text_output(output)


def run_watchlist_free(args: argparse.Namespace) -> int:
    tickers = normalize_csv_items(args.tickers) if args.tickers else setting_list("fiscal_watchlist")
    if not tickers:
        raise RuntimeError(
            "No Fiscal.ai watchlist configured. Pass --tickers NVDA,MSFT or add "
            '{"fiscal_watchlist":["NVDA","MSFT"]} to .cache/local_settings.json.'
        )
    company_keys = [normalize_fiscal_company_key(item) for item in tickers]
    api_key = resolve_fiscal_api_key()
    return emit_text_output(
        fiscal_watchlist_text(api_key, company_keys),
        send_lark=args.send_lark,
        lark_title=args.title,
    )


def run_market_brief_free(args: argparse.Namespace) -> int:
    tickers = normalize_csv_items(args.tickers) if args.tickers else setting_list("fiscal_watchlist")
    if not tickers:
        raise RuntimeError(
            "No Fiscal.ai watchlist configured. Pass --tickers NVDA,MSFT or add "
            '{"fiscal_watchlist":["NVDA","MSFT"]} to .cache/local_settings.json.'
        )
    company_keys = [normalize_fiscal_company_key(item) for item in tickers]
    api_key = resolve_fiscal_api_key()
    top_news_items = fiscal_top_news(api_key, args.top_news_limit, importance_max=2)
    top_news_rows = (
        [
            [
                safe_date_text(first_text(item, ["publishedAt", "date", "collectedAt"])),
                first_text(item, ["companyKey", "ticker", "symbol"]) or "-",
                str(int(first_number(item, ["importance"]) or 0))
                if first_number(item, ["importance"]) is not None
                else "-",
                first_text(item, ["eventType", "category"]) or "-",
                first_text(item, ["headline", "title", "summary", "text"])[:80] or "-",
            ]
            for item in top_news_items
        ]
        if top_news_items
        else fiscal_watchlist_headline_rows(api_key, company_keys, args.top_news_limit)
    )
    snapshots = fiscal_watchlist_snapshot(api_key, company_keys)
    watchlist_rows = fiscal_watchlist_rows_from_snapshots(snapshots)
    watchlist_news_lines = fiscal_watchlist_news_lines(api_key, company_keys, args.watch_news_limit)
    output = fiscal_market_brief_text(
        top_news_items=top_news_items,
        top_news_rows=top_news_rows,
        watchlist_rows=watchlist_rows,
        snapshots=snapshots,
        watchlist_news_lines=watchlist_news_lines,
    )
    return emit_text_output(output, send_lark=args.send_lark, lark_title=args.title)


def main() -> int:
    args = parse_args()
    if args.command == "links":
        print_links(args.portal)
        return 0

    if args.command == "open":
        open_urls(urls_to_open(args.portal, args.page), args.dry_run)
        return 0

    if args.command == "ask-finchat":
        return run_finchat(args)

    if args.command == "market-brief":
        return run_market_brief(args)

    if args.command == "stock-brief":
        return run_stock_brief(args)

    if args.command == "watchlist-brief":
        return run_watchlist_brief(args)

    if args.command == "top-news-free":
        return run_top_news_free(args)

    if args.command == "stock-brief-free":
        return run_stock_brief_free(args)

    if args.command == "watchlist-free":
        return run_watchlist_free(args)

    if args.command == "market-brief-free":
        return run_market_brief_free(args)

    raise RuntimeError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        wrapped = textwrap.fill(str(exc), width=88)
        print(wrapped, file=sys.stderr)
        raise SystemExit(1)
