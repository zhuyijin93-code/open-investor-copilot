from __future__ import annotations

import csv
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

EASTMONEY_A_SHARE_URL = "https://push2.eastmoney.com/api/qt/clist/get"
EASTMONEY_FIELDS = "f12,f14,f2,f3,f5,f6,f20,f21,f9,f23,f8,f10,f15,f16,f17,f18,f62,f115,f152"
EASTMONEY_FS_A_SHARE = "m:1+t:2,m:0+t:6,m:0+t:80"
SINA_A_SHARE_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
CSV_FIELDS = [
    "ticker",
    "name",
    "sector",
    "price",
    "market_cap_b",
    "pe",
    "pb",
    "roe",
    "revenue_growth",
    "momentum_20d",
    "momentum_60d",
    "volatility_20d",
    "dividend_yield",
]


def _to_float(value: Any, default: float = 0.0) -> float:
    if value in (None, "", "-"):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _is_tradeable_a_share(item: dict[str, Any], min_amount_yuan: float = 0.0) -> bool:
    code = str(item.get("f12") or "").strip()
    name = str(item.get("f14") or "").strip()
    price = _to_float(item.get("f2"))
    amount = _to_float(item.get("f6"))
    if not code or not name or price <= 0 or amount < min_amount_yuan:
        return False
    if "ST" in name.upper() or "退" in name:
        return False
    return code.startswith(("600", "601", "603", "605", "000", "001", "002", "003", "300", "301", "688", "689"))


def _a_share_sector(code: str) -> str:
    if code.startswith(("688", "689")):
        return "科创板"
    if code.startswith(("300", "301")):
        return "创业板"
    if code.startswith(("000", "001", "002", "003")):
        return "深市A股"
    if code.startswith(("600", "601", "603", "605")):
        return "沪市A股"
    return "A股"


def _fetch_eastmoney_page(page: int, page_size: int) -> dict[str, Any]:
    params = {
        "pn": page,
        "pz": page_size,
        "po": 1,
        "np": 1,
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": 2,
        "invt": 2,
        "fid": "f3",
        "fs": EASTMONEY_FS_A_SHARE,
        "fields": EASTMONEY_FIELDS,
    }
    url = EASTMONEY_A_SHARE_URL + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 QuantWeChatBot/1.0",
            "Referer": "https://quote.eastmoney.com/",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _normalize_quote_row(
    *,
    code: str,
    name: str,
    price: float,
    amount_yuan: float,
    market_cap_yi: float,
    pe: float,
    pb: float,
    pct_change: float,
) -> dict[str, str] | None:
    item = {"f12": code, "f14": name, "f2": price, "f6": amount_yuan}
    if not _is_tradeable_a_share(item, min_amount_yuan=0):
        return None
    return {
        "ticker": code,
        "name": name,
        "sector": _a_share_sector(code),
        "price": f"{price:.2f}",
        "market_cap_b": f"{market_cap_yi:.2f}",
        "pe": f"{pe:.2f}",
        "pb": f"{pb:.2f}",
        "roe": "0.00",
        "revenue_growth": "0.00",
        "momentum_20d": f"{pct_change:.2f}",
        "momentum_60d": f"{pct_change:.2f}",
        "volatility_20d": f"{abs(pct_change):.2f}",
        "dividend_yield": "0.00",
    }


def _fetch_eastmoney_rows(limit: int, min_amount_yuan: float) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    page_size = 100
    total = None
    page = 1
    target_limit = limit if limit and limit > 0 else None
    while target_limit is None or len(rows) < target_limit:
        payload = _fetch_eastmoney_page(page, page_size)
        data = payload.get("data") or {}
        total = total or int(data.get("total") or 0)
        items = data.get("diff") or []
        if not items:
            break
        for item in items:
            if not _is_tradeable_a_share(item, min_amount_yuan):
                continue
            row = _normalize_quote_row(
                code=str(item.get("f12") or "").strip(),
                name=str(item.get("f14") or "").strip(),
                price=_to_float(item.get("f2")),
                amount_yuan=_to_float(item.get("f6")),
                market_cap_yi=_to_float(item.get("f20")) / 100_000_000,
                pe=_to_float(item.get("f115"), _to_float(item.get("f9"))),
                pb=_to_float(item.get("f23")),
                pct_change=_to_float(item.get("f3")),
            )
            if row:
                rows.append(row)
            if target_limit is not None and len(rows) >= target_limit:
                break
        if total and page * page_size >= total:
            break
        page += 1
    return rows


def _fetch_sina_page(page: int, page_size: int) -> list[dict[str, Any]]:
    params = {
        "page": page,
        "num": page_size,
        "sort": "changepercent",
        "asc": 0,
        "node": "hs_a",
        "symbol": "",
        "_s_r_a": "init",
    }
    url = SINA_A_SHARE_URL + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 QuantWeChatBot/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("gbk"))


def _fetch_sina_rows(limit: int, min_amount_yuan: float) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    page_size = 80
    page = 1
    target_limit = limit if limit and limit > 0 else None
    while target_limit is None or len(rows) < target_limit:
        items = _fetch_sina_page(page, page_size)
        if not items:
            break
        for item in items:
            amount_yuan = _to_float(item.get("amount"))
            if amount_yuan < min_amount_yuan:
                continue
            code = str(item.get("code") or "").strip()
            row = _normalize_quote_row(
                code=code,
                name=str(item.get("name") or "").strip(),
                price=_to_float(item.get("trade")),
                amount_yuan=amount_yuan,
                # Sina mktcap/nmc are in 10k yuan. Convert to yi yuan.
                market_cap_yi=_to_float(item.get("mktcap")) / 10_000,
                pe=_to_float(item.get("per")),
                pb=_to_float(item.get("pb")),
                pct_change=_to_float(item.get("changepercent")),
            )
            if row:
                rows.append(row)
            if target_limit is not None and len(rows) >= target_limit:
                break
        page += 1
    return rows


def fetch_a_share_rows(limit: int = 0, min_amount_yuan: float = 0.0) -> list[dict[str, str]]:
    """Fetch a free A-share snapshot and normalize it to the bot CSV schema.

    Eastmoney is tried first and Sina is used as a fallback. These free quote
    endpoints do not include full fundamental history, so ROE/revenue growth/
    dividend yield stay 0 until a richer provider is added. Momentum currently
    uses latest pct change as a rough trend proxy.
    """
    errors: list[str] = []
    for fetcher in (_fetch_eastmoney_rows, _fetch_sina_rows):
        try:
            rows = fetcher(limit, min_amount_yuan)
        except Exception as exc:  # network endpoints can fail independently
            errors.append(f"{fetcher.__name__}: {exc}")
            continue
        if rows:
            return rows
    raise RuntimeError("Free A-share sources returned no usable rows. " + " | ".join(errors))


def refresh_a_share_universe(
    output_path: str | Path,
    *,
    limit: int = 0,
    min_amount_yuan: float = 0.0,
    max_age_seconds: int = 900,
) -> Path:
    output = Path(output_path)
    if output.exists() and time.time() - output.stat().st_mtime < max_age_seconds:
        return output
    output.parent.mkdir(parents=True, exist_ok=True)
    rows = fetch_a_share_rows(limit=limit, min_amount_yuan=min_amount_yuan)
    tmp = output.with_suffix(output.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(output)
    return output
