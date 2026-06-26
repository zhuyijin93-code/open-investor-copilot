from __future__ import annotations

import csv
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

EASTMONEY_A_SHARE_URL = "https://push2.eastmoney.com/api/qt/clist/get"
EASTMONEY_FIELDS = "f12,f14,f2,f3,f5,f6,f20,f21,f9,f23,f8,f10,f15,f16,f17,f18,f62,f100,f115,f152"
EASTMONEY_FS_A_SHARE = "m:1+t:2,m:0+t:6,m:0+t:80"
EASTMONEY_FS_HK = "m:128+t:3,m:128+t:4,m:128+t:1,m:128+t:2"
EASTMONEY_FS_US = "m:105,m:106,m:107"
EASTMONEY_PAGE_SIZE = 5000
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
HK_EXCLUDED_NAME_PARTS = ("ETF", "杠杆", "反向", "牛熊", "认购", "认沽", "界内证", "基金")
US_EXCLUDED_NAME_PARTS = (
    "ETF",
    "ETN",
    "FUND",
    "TRUST",
    "WARRANT",
    "RIGHT",
    "UNIT",
    "PREFERRED",
    "DEPOSITARY SHARE",
    "PROSHARES",
    "ISHARES",
    "SPDR",
    "VANGUARD",
    "INVESCO",
    "DIREXION",
    "GLOBAL X",
)


def _to_float(value: Any, default: float = 0.0) -> float:
    if value in (None, "", "-"):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _common_quote_checks(item: dict[str, Any], min_amount: float = 0.0) -> tuple[str, str, float, float] | None:
    code = str(item.get("f12") or "").strip().upper()
    name = str(item.get("f14") or "").strip()
    price = _to_float(item.get("f2"))
    amount = _to_float(item.get("f6"))
    if not code or not name or price <= 0 or amount < min_amount:
        return None
    return code, name, price, amount


def _is_tradeable_a_share(item: dict[str, Any], min_amount_yuan: float = 0.0) -> bool:
    checked = _common_quote_checks(item, min_amount_yuan)
    if checked is None:
        return False
    code, name, _, _ = checked
    if "ST" in name.upper() or "退" in name:
        return False
    return code.startswith(("600", "601", "603", "605", "000", "001", "002", "003", "300", "301", "688", "689"))


def _is_tradeable_hk_share(item: dict[str, Any], min_amount: float = 0.0) -> bool:
    checked = _common_quote_checks(item, min_amount)
    if checked is None:
        return False
    code, name, _, _ = checked
    sector = str(item.get("f100") or "").strip()
    upper_name = name.upper()
    if not code.isdigit() or len(code) != 5:
        return False
    if sector in {"", "-"}:
        return False
    return not any(fragment in upper_name for fragment in HK_EXCLUDED_NAME_PARTS)


def _is_tradeable_us_share(item: dict[str, Any], min_amount: float = 0.0) -> bool:
    checked = _common_quote_checks(item, min_amount)
    if checked is None:
        return False
    code, name, _, _ = checked
    sector = str(item.get("f100") or "").strip()
    upper_name = name.upper()
    if not code.replace(".", "").replace("-", "").isalnum():
        return False
    if len(code) == 5 and code[-1] in {"W", "R", "U", "V", "Z"}:
        return False
    if sector in {"", "-"}:
        return False
    return not any(fragment in upper_name for fragment in US_EXCLUDED_NAME_PARTS)


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


def _fetch_eastmoney_page(page: int, page_size: int, fs: str) -> dict[str, Any]:
    params = {
        "pn": page,
        "pz": page_size,
        "po": 1,
        "np": 1,
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": 2,
        "invt": 2,
        "fid": "f3",
        "fs": fs,
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


def _eastmoney_market_cap_b(raw_value: Any) -> float:
    return _to_float(raw_value) / 1_000_000_000


def _normalize_eastmoney_row(item: dict[str, Any], market: str) -> dict[str, str] | None:
    code = str(item.get("f12") or "").strip().upper()
    name = str(item.get("f14") or "").strip()
    price = _to_float(item.get("f2"))
    pct_change = _to_float(item.get("f3"))
    sector = str(item.get("f100") or "").strip()
    if market == "a":
        ticker = code
        sector = sector if sector not in {"", "-"} else _a_share_sector(code)
    elif market == "hk":
        ticker = f"{code}.HK"
        sector = sector if sector not in {"", "-"} else "港股"
    else:
        ticker = code
        sector = sector if sector not in {"", "-"} else "美股"
    return {
        "ticker": ticker,
        "name": name,
        "sector": sector,
        "price": f"{price:.2f}",
        "market_cap_b": f"{_eastmoney_market_cap_b(item.get('f20')):.2f}",
        "pe": f"{_to_float(item.get('f115'), _to_float(item.get('f9'))):.2f}",
        "pb": f"{_to_float(item.get('f23')):.2f}",
        "roe": "0.00",
        "revenue_growth": "0.00",
        "momentum_20d": f"{pct_change:.2f}",
        "momentum_60d": f"{pct_change:.2f}",
        "volatility_20d": f"{abs(pct_change):.2f}",
        "dividend_yield": "0.00",
    }


def _fetch_market_rows(
    *,
    fs: str,
    market: str,
    limit: int,
    min_amount: float,
    validator: Any,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    total = None
    page = 1
    target_limit = limit if limit and limit > 0 else None
    while target_limit is None or len(rows) < target_limit:
        payload = _fetch_eastmoney_page(page, EASTMONEY_PAGE_SIZE, fs)
        data = payload.get("data") or {}
        total = total or int(data.get("total") or 0)
        items = data.get("diff") or []
        if not items:
            break
        for item in items:
            if not validator(item, min_amount):
                continue
            row = _normalize_eastmoney_row(item, market)
            if row is None:
                continue
            rows.append(row)
            if target_limit is not None and len(rows) >= target_limit:
                break
        if total and page * EASTMONEY_PAGE_SIZE >= total:
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


def _normalize_sina_row(item: dict[str, Any]) -> dict[str, str] | None:
    code = str(item.get("code") or "").strip()
    name = str(item.get("name") or "").strip()
    price = _to_float(item.get("trade"))
    pct_change = _to_float(item.get("changepercent"))
    mock_item = {"f12": code, "f14": name, "f2": price, "f6": item.get("amount")}
    if not _is_tradeable_a_share(mock_item, min_amount_yuan=0):
        return None
    return {
        "ticker": code,
        "name": name,
        "sector": _a_share_sector(code),
        "price": f"{price:.2f}",
        # Sina mktcap is in 10k yuan. Convert to billions of yuan.
        "market_cap_b": f"{_to_float(item.get('mktcap')) / 100_000:.2f}",
        "pe": f"{_to_float(item.get('per')):.2f}",
        "pb": f"{_to_float(item.get('pb')):.2f}",
        "roe": "0.00",
        "revenue_growth": "0.00",
        "momentum_20d": f"{pct_change:.2f}",
        "momentum_60d": f"{pct_change:.2f}",
        "volatility_20d": f"{abs(pct_change):.2f}",
        "dividend_yield": "0.00",
    }


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
            row = _normalize_sina_row(item)
            if row:
                rows.append(row)
            if target_limit is not None and len(rows) >= target_limit:
                break
        page += 1
    return rows


def fetch_a_share_rows(limit: int = 0, min_amount_yuan: float = 0.0) -> list[dict[str, str]]:
    errors: list[str] = []
    fetchers = (
        lambda selected_limit, selected_min_amount: _fetch_market_rows(
            fs=EASTMONEY_FS_A_SHARE,
            market="a",
            limit=selected_limit,
            min_amount=selected_min_amount,
            validator=_is_tradeable_a_share,
        ),
        _fetch_sina_rows,
    )
    for fetcher in fetchers:
        try:
            rows = fetcher(limit, min_amount_yuan)
        except Exception as exc:  # network endpoints can fail independently
            errors.append(f"{getattr(fetcher, '__name__', 'eastmoney_a_share')}: {exc}")
            continue
        if rows:
            return rows
    raise RuntimeError("Free A-share sources returned no usable rows. " + " | ".join(errors))


def fetch_hk_share_rows(limit: int = 0, min_amount_hkd: float = 0.0) -> list[dict[str, str]]:
    rows = _fetch_market_rows(
        fs=EASTMONEY_FS_HK,
        market="hk",
        limit=limit,
        min_amount=min_amount_hkd,
        validator=_is_tradeable_hk_share,
    )
    if rows:
        return rows
    raise RuntimeError("Free HK-share source returned no usable rows.")


def fetch_us_share_rows(limit: int = 0, min_amount_usd: float = 0.0) -> list[dict[str, str]]:
    rows = _fetch_market_rows(
        fs=EASTMONEY_FS_US,
        market="us",
        limit=limit,
        min_amount=min_amount_usd,
        validator=_is_tradeable_us_share,
    )
    if rows:
        return rows
    raise RuntimeError("Free US-share source returned no usable rows.")


def _write_universe_csv(output: Path, rows: list[dict[str, str]]) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(output.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(output)
    return output


def _refresh_universe(
    output_path: str | Path,
    *,
    limit: int,
    max_age_seconds: int,
    fetcher: Any,
    min_amount: dict[str, float],
) -> Path:
    output = Path(output_path)
    if output.exists() and time.time() - output.stat().st_mtime < max_age_seconds:
        return output
    rows = fetcher(limit=limit, **min_amount)
    return _write_universe_csv(output, rows)


def refresh_a_share_universe(
    output_path: str | Path,
    *,
    limit: int = 0,
    min_amount_yuan: float = 0.0,
    max_age_seconds: int = 900,
) -> Path:
    return _refresh_universe(
        output_path,
        limit=limit,
        max_age_seconds=max_age_seconds,
        fetcher=fetch_a_share_rows,
        min_amount={"min_amount_yuan": min_amount_yuan},
    )


def refresh_hk_share_universe(
    output_path: str | Path,
    *,
    limit: int = 0,
    min_amount_hkd: float = 0.0,
    max_age_seconds: int = 1800,
) -> Path:
    return _refresh_universe(
        output_path,
        limit=limit,
        max_age_seconds=max_age_seconds,
        fetcher=fetch_hk_share_rows,
        min_amount={"min_amount_hkd": min_amount_hkd},
    )


def refresh_us_share_universe(
    output_path: str | Path,
    *,
    limit: int = 0,
    min_amount_usd: float = 0.0,
    max_age_seconds: int = 1800,
) -> Path:
    return _refresh_universe(
        output_path,
        limit=limit,
        max_age_seconds=max_age_seconds,
        fetcher=fetch_us_share_rows,
        min_amount={"min_amount_usd": min_amount_usd},
    )


def _load_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def refresh_global_universe(
    output_path: str | Path,
    *,
    a_share_path: str | Path,
    hk_share_path: str | Path,
    us_share_path: str | Path,
    a_share_limit: int = 0,
    hk_share_limit: int = 0,
    us_share_limit: int = 0,
    a_share_min_amount_yuan: float = 0.0,
    hk_share_min_amount_hkd: float = 0.0,
    us_share_min_amount_usd: float = 0.0,
    a_share_cache_seconds: int = 900,
    hk_share_cache_seconds: int = 1800,
    us_share_cache_seconds: int = 1800,
    max_age_seconds: int = 1800,
) -> Path:
    output = Path(output_path)
    if output.exists() and time.time() - output.stat().st_mtime < max_age_seconds:
        return output

    a_path = refresh_a_share_universe(
        a_share_path,
        limit=a_share_limit,
        min_amount_yuan=a_share_min_amount_yuan,
        max_age_seconds=a_share_cache_seconds,
    )
    hk_path = refresh_hk_share_universe(
        hk_share_path,
        limit=hk_share_limit,
        min_amount_hkd=hk_share_min_amount_hkd,
        max_age_seconds=hk_share_cache_seconds,
    )
    us_path = refresh_us_share_universe(
        us_share_path,
        limit=us_share_limit,
        min_amount_usd=us_share_min_amount_usd,
        max_age_seconds=us_share_cache_seconds,
    )

    merged: list[dict[str, str]] = []
    seen: set[str] = set()
    for path in (a_path, hk_path, us_path):
        for row in _load_csv_rows(Path(path)):
            ticker = str(row.get("ticker") or "").strip().upper()
            if not ticker or ticker in seen:
                continue
            seen.add(ticker)
            merged.append(row)
    return _write_universe_csv(output, merged)
