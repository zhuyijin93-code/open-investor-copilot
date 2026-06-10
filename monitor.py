#!/usr/bin/env python3
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import html
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parent
STATE_PATH = ROOT / ".cache" / "watch_state.json"
LOCAL_SETTINGS_PATH = ROOT / ".cache" / "local_settings.json"
SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT", "13F Monitor admin@example.com")
HOUSE_BASE = "https://disclosures-clerk.house.gov/"
BUNDLED_PYTHON = pathlib.Path(
    "/Users/admin/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
)


WATCHLIST = [
    {
        "key": "buffett",
        "label": "巴菲特 / Berkshire Hathaway",
        "kind": "sec_13f",
        "cik": "0001067983",
        "owner": "exclude",
    },
    {
        "key": "buffett_company",
        "label": "巴菲特 / Berkshire 公司层面",
        "kind": "berkshire_company",
        "cik": "0001067983",
        "owner": "exclude",
    },
    {
        "key": "duan",
        "label": "段永平 / H&H International Investment",
        "kind": "sec_13f",
        "cik": "0001759760",
        "owner": "exclude",
    },
    {
        "key": "ackman",
        "label": "阿克曼 / Pershing Square",
        "kind": "sec_13f",
        "cik": "0001336528",
        "owner": "exclude",
    },
    {
        "key": "tepper",
        "label": "泰珀 / Appaloosa",
        "kind": "sec_13f",
        "cik": "0001656456",
        "owner": "exclude",
    },
    {
        "key": "lilu",
        "label": "李录 / Himalaya Capital",
        "kind": "sec_13f",
        "cik": "0001709323",
        "owner": "exclude",
    },
    {
        "key": "pelosi",
        "label": "佩洛西 / Nancy Pelosi",
        "kind": "house_ptr",
        "last_name": "Pelosi",
        "state": "CA",
        "district": "11",
    },
    {
        "key": "huang",
        "label": "黄仁勋 / Jensen Huang",
        "kind": "sec_form4",
        "cik": "0001197649",
        "owner": "only",
    },
]


@dataclasses.dataclass
class Snapshot:
    key: str
    label: str
    id: str
    title: str
    summary_lines: list[str]
    source_url: str
    meta: dict[str, Any]


@dataclasses.dataclass
class Position:
    issuer: str
    title: str
    cusip: str
    shares: int
    value_usd: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor official filing feeds.")
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Print the latest filing summaries without reading or writing state.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the normal change detection flow but print the Lark message instead of sending it.",
    )
    parser.add_argument(
        "--keys",
        help="Comma-separated watch keys to run. Default: all. Example: buffett,buffett_company,duan or pelosi,huang",
    )
    return parser.parse_args()


def build_opener(cookie_jar: Any | None = None) -> urllib.request.OpenerDirector:
    handlers: list[Any] = []
    if cookie_jar is not None:
        handlers.append(urllib.request.HTTPCookieProcessor(cookie_jar))
    return urllib.request.build_opener(*handlers)


def fetch_text(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    opener: urllib.request.OpenerDirector | None = None,
) -> str:
    request = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data else "GET")
    with (opener or urllib.request.build_opener()).open(request, timeout=30) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def fetch_json(url: str, *, headers: dict[str, str] | None = None) -> dict[str, Any]:
    return json.loads(fetch_text(url, headers=headers))


def fetch_bytes(
    url: str,
    *,
    headers: dict[str, str] | None = None,
) -> bytes:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.build_opener().open(request, timeout=30) as response:
        return response.read()


def sec_headers() -> dict[str, str]:
    return {"User-Agent": SEC_USER_AGENT, "Accept-Encoding": "identity"}


def node_text(node: ET.Element | None, path: str) -> str:
    if node is None:
        return ""
    value = node.findtext(path)
    return value.strip() if value else ""


def compact_number(value: float) -> str:
    abs_value = abs(value)
    if abs_value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    if abs_value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if abs_value >= 1_000:
        return f"{value / 1_000:.2f}K"
    return f"{value:,.0f}"


def format_money(value_usd: int) -> str:
    return f"${compact_number(float(value_usd))}"


def format_shares(shares: int) -> str:
    return compact_number(float(shares))


def signed(value: str, positive: bool) -> str:
    return f"+{value}" if positive else f"-{value}"


def format_pct(value: float) -> str:
    return f"{value:.1%}"


def truncate_text(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def render_table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def render_row(row: list[str]) -> str:
        return " | ".join(cell.ljust(widths[index]) for index, cell in enumerate(row))

    parts = [render_row(headers), "-+-".join("-" * width for width in widths)]
    parts.extend(render_row(row) for row in rows)
    return "```text\n" + "\n".join(parts) + "\n```"


def amount_range_sort_key(amount: str) -> float:
    matches = [float(item.replace(",", "")) for item in re.findall(r"\$([\d,]+(?:\.\d+)?)", amount)]
    if not matches:
        return 0.0
    return max(matches)


def load_state() -> dict[str, str]:
    if not STATE_PATH.exists():
        return {}
    return json.loads(STATE_PATH.read_text())


def load_local_settings() -> dict[str, Any]:
    if not LOCAL_SETTINGS_PATH.exists():
        return {}
    return json.loads(LOCAL_SETTINGS_PATH.read_text())


def save_state(state: dict[str, str]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def selected_watchlist(keys_arg: str | None) -> list[dict[str, str]]:
    if not keys_arg:
        return WATCHLIST
    requested = [item.strip() for item in keys_arg.split(",") if item.strip()]
    lookup = {watch["key"]: watch for watch in WATCHLIST}
    invalid = [item for item in requested if item not in lookup]
    if invalid:
        raise RuntimeError(f"Unknown watch key(s): {', '.join(invalid)}")
    return [lookup[item] for item in requested]


def parse_atom_entries(feed_xml: str) -> list[dict[str, str]]:
    root = ET.fromstring(feed_xml)
    entries: list[dict[str, str]] = []
    for entry in root.findall("{*}entry"):
        filing_href = node_text(entry, ".//{*}filing-href")
        accession = node_text(entry, ".//{*}accession-number")
        filing_type = node_text(entry, ".//{*}filing-type")
        filing_date = node_text(entry, ".//{*}filing-date")
        entries.append(
            {
                "accession": accession,
                "filing_type": filing_type,
                "filing_date": filing_date,
                "filing_href": filing_href,
            }
        )
    return entries


def sec_feed(cik: str, filing_type: str, owner: str, count: int = 10) -> list[dict[str, str]]:
    params = {
        "action": "getcompany",
        "CIK": cik,
        "type": filing_type,
        "owner": owner,
        "count": str(count),
        "output": "atom",
    }
    url = "https://www.sec.gov/cgi-bin/browse-edgar?" + urllib.parse.urlencode(params)
    return parse_atom_entries(fetch_text(url, headers=sec_headers()))


def filing_index_url(filing_href: str) -> str:
    return filing_href.rsplit("/", 1)[0] + "/index.json"


def fetch_sec_index(filing_href: str) -> list[dict[str, str]]:
    data = fetch_json(filing_index_url(filing_href), headers=sec_headers())
    return data["directory"]["item"]


def sec_doc_url(filing_href: str, doc_name: str) -> str:
    return filing_href.rsplit("/", 1)[0] + f"/{doc_name}"


def parse_doc_period_from_name(doc_name: str) -> dt.date:
    match = re.search(r"(\d{8})", doc_name)
    if not match:
        raise RuntimeError(f"Unable to infer period from document name: {doc_name}")
    return dt.datetime.strptime(match.group(1), "%Y%m%d").date()


def sort_sec_entries(entries: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(entries, key=lambda entry: (entry["filing_date"], entry["accession"]), reverse=True)


def sec_entries_for_types(cik: str, filing_types: list[str], owner: str, count_each: int = 4) -> list[dict[str, str]]:
    merged: list[dict[str, str]] = []
    seen: set[str] = set()
    for filing_type in filing_types:
        for entry in sec_feed(cik, filing_type, owner, count=count_each):
            if entry["accession"] in seen:
                continue
            merged.append(entry)
            seen.add(entry["accession"])
    return sort_sec_entries(merged)


def choose_primary_html(items: list[dict[str, str]], pattern: str) -> str:
    matcher = re.compile(pattern, re.I)
    for item in items:
        name = item["name"]
        if matcher.search(name):
            return name
    raise RuntimeError(f"No primary HTML document matched pattern: {pattern}")


def choose_doc(items: list[dict[str, str]], *, exact: str | None = None, pattern: str | None = None, exclude: set[str] | None = None) -> str:
    exclude = exclude or set()
    if exact:
        for item in items:
            if item["name"].lower() == exact.lower():
                return item["name"]
    if pattern:
        matcher = re.compile(pattern, re.I)
        for item in items:
            name = item["name"]
            if name in exclude:
                continue
            if matcher.search(name):
                return name
    for item in items:
        name = item["name"]
        if name in exclude:
            continue
        if name.lower().endswith(".xml"):
            return name
    raise RuntimeError("No matching XML document found in SEC filing index.")


def parse_13f_cover(xml_text: str) -> dict[str, Any]:
    root = ET.fromstring(xml_text)
    return {
        "period": node_text(root, ".//{*}periodOfReport") or node_text(root, ".//{*}reportCalendarOrQuarter"),
        "table_entries": int(node_text(root, ".//{*}tableEntryTotal") or "0"),
        "table_value_usd": int(node_text(root, ".//{*}tableValueTotal") or "0"),
        "signature_date": node_text(root, ".//{*}signatureDate"),
    }


def parse_13f_positions(xml_text: str) -> dict[str, Position]:
    root = ET.fromstring(xml_text)
    totals: dict[str, Position] = {}
    shares_by_key: defaultdict[str, int] = defaultdict(int)
    value_by_key: defaultdict[str, int] = defaultdict(int)
    labels_by_key: dict[str, tuple[str, str]] = {}

    for row in root.findall(".//{*}infoTable"):
        issuer = node_text(row, ".//{*}nameOfIssuer")
        title = node_text(row, ".//{*}titleOfClass")
        cusip = node_text(row, ".//{*}cusip")
        shares = int(node_text(row, ".//{*}sshPrnamt") or "0")
        value_usd = int(node_text(row, ".//{*}value") or "0")
        shares_by_key[cusip] += shares
        value_by_key[cusip] += value_usd
        labels_by_key[cusip] = (issuer, title)

    for cusip, shares in shares_by_key.items():
        issuer, title = labels_by_key[cusip]
        totals[cusip] = Position(
            issuer=issuer,
            title=title,
            cusip=cusip,
            shares=shares,
            value_usd=value_by_key[cusip],
        )
    return totals


def parse_display_million_amount(value: str) -> int:
    return int(round(float(value.replace(",", "")) * 1_000_000))


def parse_scaled_amount(value: str, unit: str) -> int:
    multiplier = 1_000_000_000 if unit.lower() == "billion" else 1_000_000
    return int(round(float(value.replace(",", "")) * multiplier))


def strip_html_text(html_text: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", html_text))
    return re.sub(r"\s+", " ", text).strip()


def extract_inline_xbrl_amount(html_text: str, labels: list[str]) -> int | None:
    for label in labels:
        pattern = re.compile(rf"{re.escape(label)}.*?<ix:nonFraction[^>]*>([\d,\.]+)</ix:nonFraction>", re.I | re.S)
        match = pattern.search(html_text)
        if match:
            return parse_display_million_amount(match.group(1))
    return None


def extract_berkshire_cash_equivalent_tbill_amounts(html_text: str) -> tuple[int, int] | None:
    text = strip_html_text(html_text)
    match = re.search(
        r"Includes U\.S\. Treasury Bills with maturities of three months or less when purchased of \$\s*([\d,\.]+)\s*(billion|million) at .+? and \$\s*([\d,\.]+)\s*(billion|million) at .+?\.",
        text,
        re.I,
    )
    if not match:
        return None
    current = parse_scaled_amount(match.group(1), match.group(2))
    previous = parse_scaled_amount(match.group(3), match.group(4))
    return current, previous


def extract_berkshire_unsettled_tbill_amounts(html_text: str) -> tuple[int, int] | None:
    text = strip_html_text(html_text)
    match = re.search(
        r"Includes unsettled purchases of U\.S\. Treasury Bills of \$\s*([\d,\.]+)\s*(billion|million) at .+? and \$\s*([\d,\.]+)\s*(billion|million) at .+?\.",
        text,
        re.I,
    )
    if not match:
        return None
    current = parse_scaled_amount(match.group(1), match.group(2))
    previous = parse_scaled_amount(match.group(3), match.group(4))
    return current, previous


def format_money_delta(current: int, previous: int) -> str:
    delta = current - previous
    if delta == 0:
        return "持平"
    return signed(format_money(abs(delta)), delta > 0)


def extract_berkshire_japan_summary(report_year: int) -> dict[str, Any] | None:
    report_url = f"https://www.berkshirehathaway.com/{report_year}ar/{report_year}ar.pdf"
    try:
        pdf_bytes = fetch_bytes(report_url)
    except Exception:
        return None

    with tempfile.NamedTemporaryFile(prefix=f"berkshire_{report_year}_", suffix=".pdf", delete=False) as handle:
        handle.write(pdf_bytes)
        temp_path = pathlib.Path(handle.name)
    try:
        pdf_text = extract_pdf_text_with_bundled_python(temp_path)
    finally:
        temp_path.unlink(missing_ok=True)

    total_match = re.search(
        r"Mitsubishi Corporation.*?ITOCHU Corporation.*?Mitsui\s*&\s*Co\., Ltd\..*?Marubeni Corporation.*?Sumitomo Corporation.*?Total\s+\$?\s*([\d,]+)\s+\$?\s*([\d,]+)\s+\$?\s*([\d,]+)",
        pdf_text,
        re.I | re.S,
    )
    if not total_match:
        return None

    borrow_match = re.search(
        r"borrowed in Japan .*? average cost of ([\d.]+)%, with a weighted-average life of approximately ([\d.]+) years",
        pdf_text,
        re.I | re.S,
    )

    return {
        "report_year": report_year,
        "cost_basis": parse_scaled_amount(total_match.group(1), "million"),
        "market_value": parse_scaled_amount(total_match.group(2), "million"),
        "dividends": parse_scaled_amount(total_match.group(3), "million"),
        "borrow_cost_pct": borrow_match.group(1) if borrow_match else None,
        "debt_life_years": borrow_match.group(2) if borrow_match else None,
        "report_url": report_url,
    }


def summarize_position_change(latest: Position | None, previous: Position | None) -> tuple[str, float]:
    if latest and not previous:
        return (
            f"新增 {latest.issuer} ({latest.title}) | {format_shares(latest.shares)} 股 | {format_money(latest.value_usd)}",
            float(latest.value_usd),
        )
    if previous and not latest:
        return (
            f"清仓 {previous.issuer} ({previous.title}) | {format_shares(previous.shares)} 股 | {format_money(previous.value_usd)}",
            float(previous.value_usd),
        )
    assert latest and previous
    share_delta = latest.shares - previous.shares
    direction = "加仓" if share_delta > 0 else "减仓"
    reference_price = latest.value_usd / latest.shares if latest.shares else previous.value_usd / max(previous.shares, 1)
    changed_value = abs(share_delta) * reference_price
    return (
        f"{direction} {latest.issuer} ({latest.title}) | {signed(format_shares(abs(share_delta)), share_delta > 0)} 股 | 当前持仓 {format_money(latest.value_usd)}",
        float(changed_value),
    )


def top_changes(latest_positions: dict[str, Position], previous_positions: dict[str, Position]) -> dict[str, list[str]]:
    buckets = {"新增": [], "加仓": [], "减仓": [], "清仓": []}
    scored: dict[str, list[tuple[float, str]]] = {bucket: [] for bucket in buckets}

    all_keys = set(latest_positions) | set(previous_positions)
    for key in all_keys:
        latest = latest_positions.get(key)
        previous = previous_positions.get(key)
        if latest and previous and latest.shares == previous.shares:
            continue
        line, score = summarize_position_change(latest, previous)
        if latest and not previous:
            scored["新增"].append((score, line))
        elif previous and not latest:
            scored["清仓"].append((score, line))
        elif latest and previous and latest.shares > previous.shares:
            scored["加仓"].append((score, line))
        else:
            scored["减仓"].append((score, line))

    for bucket, items in scored.items():
        items.sort(key=lambda item: item[0], reverse=True)
        buckets[bucket] = [line for _, line in items[:5]]
    return buckets


def change_counts(latest_positions: dict[str, Position], previous_positions: dict[str, Position]) -> dict[str, int]:
    counts = {"新增": 0, "加仓": 0, "减仓": 0, "清仓": 0}
    all_keys = set(latest_positions) | set(previous_positions)
    for key in all_keys:
        latest = latest_positions.get(key)
        previous = previous_positions.get(key)
        if latest and not previous:
            counts["新增"] += 1
        elif previous and not latest:
            counts["清仓"] += 1
        elif latest and previous:
            if latest.shares > previous.shares:
                counts["加仓"] += 1
            elif latest.shares < previous.shares:
                counts["减仓"] += 1
    return counts


def position_change_label(latest: Position, previous: Position | None) -> str:
    if previous is None:
        return "新增"
    delta = latest.shares - previous.shares
    if delta == 0:
        return "持平"
    return signed(format_shares(abs(delta)), delta > 0)


def previous_rank_map(positions: dict[str, Position]) -> dict[str, int]:
    ranked = sorted(positions.values(), key=lambda position: position.value_usd, reverse=True)
    return {position.cusip: index for index, position in enumerate(ranked, start=1)}


def exit_rows(latest_positions: dict[str, Position], previous_positions: dict[str, Position], limit: int = 5) -> list[list[str]]:
    exited = [position for cusip, position in previous_positions.items() if cusip not in latest_positions]
    exited.sort(key=lambda position: position.value_usd, reverse=True)
    rows: list[list[str]] = []
    for position in exited[:limit]:
        rows.append(
            [
                truncate_text(position.issuer, 26),
                format_money(position.value_usd),
                format_shares(position.shares),
            ]
        )
    return rows


def ranked_position_rows(
    latest_positions: dict[str, Position],
    previous_positions: dict[str, Position],
    *,
    total_value_usd: int,
    limit: int = 10,
) -> list[list[str]]:
    ranked = sorted(latest_positions.values(), key=lambda position: position.value_usd, reverse=True)
    previous_ranks = previous_rank_map(previous_positions)
    rows: list[list[str]] = []
    for index, position in enumerate(ranked[:limit], start=1):
        previous = previous_positions.get(position.cusip)
        rows.append(
            [
                str(index),
                str(previous_ranks.get(position.cusip, "NEW")),
                truncate_text(position.issuer, 28),
                format_money(position.value_usd),
                format_pct(position.value_usd / total_value_usd if total_value_usd else 0),
                format_shares(position.shares),
                position_change_label(position, previous),
            ]
        )
    return rows


def snapshot_sec_13f(watch: dict[str, str]) -> Snapshot:
    entries = sec_feed(watch["cik"], "13F-HR", watch["owner"], count=6)
    if len(entries) < 2:
        raise RuntimeError(f"{watch['label']} did not return enough 13F filings to compare.")

    latest_entry = entries[0]
    previous_entry = entries[1]

    latest_items = fetch_sec_index(latest_entry["filing_href"])
    previous_items = fetch_sec_index(previous_entry["filing_href"])

    latest_cover_doc = choose_doc(latest_items, exact="primary_doc.xml")
    latest_info_doc = choose_doc(latest_items, exact="infotable.xml", pattern=r"info.*table.*\.xml", exclude={latest_cover_doc})
    previous_cover_doc = choose_doc(previous_items, exact="primary_doc.xml")
    previous_info_doc = choose_doc(previous_items, exact="infotable.xml", pattern=r"info.*table.*\.xml", exclude={previous_cover_doc})

    latest_cover = parse_13f_cover(fetch_text(sec_doc_url(latest_entry["filing_href"], latest_cover_doc), headers=sec_headers()))
    latest_positions = parse_13f_positions(fetch_text(sec_doc_url(latest_entry["filing_href"], latest_info_doc), headers=sec_headers()))
    previous_positions = parse_13f_positions(fetch_text(sec_doc_url(previous_entry["filing_href"], previous_info_doc), headers=sec_headers()))
    ranked_rows = ranked_position_rows(
        latest_positions,
        previous_positions,
        total_value_usd=latest_cover["table_value_usd"],
        limit=10,
    )
    exited_rows = exit_rows(latest_positions, previous_positions, limit=5)
    counts = change_counts(latest_positions, previous_positions)

    lines = [
        f"新 13F: {latest_entry['filing_type']} | filed {latest_entry['filing_date']} | period {latest_cover['period']}",
        f"持仓数 {len(latest_positions)} | 披露市值 {format_money(latest_cover['table_value_usd'])}",
        f"变动概览: 新增 {counts['新增']} / 加仓 {counts['加仓']} / 减仓 {counts['减仓']} / 清仓 {counts['清仓']}",
        "Top holdings:",
        render_table(
            ["#", "Prev #", "Holding", "Value", "Wt", "Shares", "Delta"],
            ranked_rows,
        ),
    ]
    if exited_rows:
        lines.extend(
            [
                "Exited positions:",
                render_table(["Holding", "Last value", "Last shares"], exited_rows),
            ]
        )

    return Snapshot(
        key=watch["key"],
        label=watch["label"],
        id=latest_entry["accession"],
        title=f"{watch['label']} 最新 13F",
        summary_lines=lines,
        source_url=latest_entry["filing_href"],
        meta={
            "filing_date": latest_entry["filing_date"],
            "period": latest_cover["period"],
        },
    )


def snapshot_berkshire_company(watch: dict[str, str]) -> Snapshot:
    entries = sec_entries_for_types(watch["cik"], ["10-Q", "10-K"], watch["owner"], count_each=4)
    if len(entries) < 2:
        raise RuntimeError(f"{watch['label']} did not return enough 10-Q/10-K filings to compare.")

    latest_entry = entries[0]
    previous_entry = entries[1]

    latest_items = fetch_sec_index(latest_entry["filing_href"])
    previous_items = fetch_sec_index(previous_entry["filing_href"])
    latest_doc = choose_primary_html(latest_items, r"brka-\d{8}\.htm$")
    previous_doc = choose_primary_html(previous_items, r"brka-\d{8}\.htm$")

    latest_html = fetch_text(sec_doc_url(latest_entry["filing_href"], latest_doc), headers=sec_headers())
    previous_html = fetch_text(sec_doc_url(previous_entry["filing_href"], previous_doc), headers=sec_headers())
    latest_period = parse_doc_period_from_name(latest_doc)

    latest_cash = extract_inline_xbrl_amount(latest_html, ["Cash and cash equivalents"])
    latest_treasury = extract_inline_xbrl_amount(latest_html, ["Short-term investments in U.S. Treasury Bills"])
    previous_cash = extract_inline_xbrl_amount(previous_html, ["Cash and cash equivalents"])
    previous_treasury = extract_inline_xbrl_amount(previous_html, ["Short-term investments in U.S. Treasury Bills"])
    if latest_cash is None or latest_treasury is None or previous_cash is None or previous_treasury is None:
        raise RuntimeError(f"{watch['label']} missing cash or Treasury Bill values in company filing.")

    latest_payable = extract_inline_xbrl_amount(
        latest_html,
        [
            "Payable for purchase of U.S. Treasury Bills and other liabilities",
            "Payable for purchase of U.S. Treasury Bills",
        ],
    )
    previous_payable = extract_inline_xbrl_amount(
        previous_html,
        [
            "Payable for purchase of U.S. Treasury Bills and other liabilities",
            "Payable for purchase of U.S. Treasury Bills",
        ],
    )
    short_tbill_cash_eq = extract_berkshire_cash_equivalent_tbill_amounts(latest_html)
    unsettled_tbill = extract_berkshire_unsettled_tbill_amounts(latest_html)

    rows = [
        [
            "Cash & equivalents",
            format_money(latest_cash),
            format_money(previous_cash),
            format_money_delta(latest_cash, previous_cash),
        ],
        [
            "U.S. T-Bills",
            format_money(latest_treasury),
            format_money(previous_treasury),
            format_money_delta(latest_treasury, previous_treasury),
        ],
        [
            "Cash + T-Bills",
            format_money(latest_cash + latest_treasury),
            format_money(previous_cash + previous_treasury),
            format_money_delta(latest_cash + latest_treasury, previous_cash + previous_treasury),
        ],
    ]
    if short_tbill_cash_eq:
        rows.append(
            [
                "<=3m T-Bills in cash eq",
                format_money(short_tbill_cash_eq[0]),
                format_money(short_tbill_cash_eq[1]),
                format_money_delta(short_tbill_cash_eq[0], short_tbill_cash_eq[1]),
            ]
        )
    elif latest_payable is not None and previous_payable is not None:
        rows.append(
            [
                "T-Bill purchases payable",
                format_money(latest_payable),
                format_money(previous_payable),
                format_money_delta(latest_payable, previous_payable),
            ]
        )
    if unsettled_tbill:
        rows.append(
            [
                "Unsettled T-Bill buys",
                format_money(unsettled_tbill[0]),
                format_money(unsettled_tbill[1]),
                format_money_delta(unsettled_tbill[0], unsettled_tbill[1]),
            ]
        )

    annual_report_year = latest_period.year if latest_entry["filing_type"] == "10-K" else latest_period.year - 1
    japan_summary = extract_berkshire_japan_summary(annual_report_year)

    lines = [
        f"新公司披露: {latest_entry['filing_type']} | filed {latest_entry['filing_date']} | period {latest_period.isoformat()}",
        "Liquidity snapshot:",
        render_table(["Metric", "Current", "Prev", "Delta"], rows),
    ]
    if japan_summary:
        japan_rows = [
            ["Cost basis", format_money(japan_summary["cost_basis"])],
            ["Market value", format_money(japan_summary["market_value"])],
            ["Dividends", format_money(japan_summary["dividends"])],
        ]
        if japan_summary.get("borrow_cost_pct"):
            japan_rows.append(["JPY debt cost", f"{japan_summary['borrow_cost_pct']}%"])
        if japan_summary.get("debt_life_years"):
            japan_rows.append(["Debt W.A. life", f"{japan_summary['debt_life_years']}y"])
        lines.extend(
            [
                f"Japan 5 trading houses ({japan_summary['report_year']} annual report):",
                render_table(["Metric", "Value"], japan_rows),
                f"Annual report: {japan_summary['report_url']}",
            ]
        )

    return Snapshot(
        key=watch["key"],
        label=watch["label"],
        id=latest_entry["accession"],
        title=f"{watch['label']} 最新公司披露",
        summary_lines=lines,
        source_url=latest_entry["filing_href"],
        meta={
            "filing_date": latest_entry["filing_date"],
            "period": latest_period.isoformat(),
            "filing_type": latest_entry["filing_type"],
        },
    )


def parse_form4_transactions(xml_text: str) -> list[list[str]]:
    root = ET.fromstring(xml_text)
    code_map = {
        "P": "买入",
        "S": "卖出",
        "F": "代扣税处置",
        "G": "赠与/转移",
        "M": "期权行权",
        "A": "授予/归属",
    }
    transactions: list[tuple[float, list[str]]] = []
    for row in root.findall(".//nonDerivativeTransaction"):
        asset = node_text(row, ".//securityTitle/value") or "Common Stock"
        date = node_text(row, ".//transactionDate/value")
        code = node_text(row, ".//transactionCoding/transactionCode")
        shares = int(node_text(row, ".//transactionAmounts/transactionShares/value") or "0")
        price = node_text(row, ".//transactionAmounts/transactionPricePerShare/value")
        direction = node_text(row, ".//transactionAmounts/transactionAcquiredDisposedCode/value")
        owner = node_text(row, ".//ownershipNature/natureOfOwnership/value") or "直接/未注明"
        direction_text = "增持" if direction == "A" else "处置"
        verb = code_map.get(code, code)
        price_text = f"${price}" if price and price != "0" else "-"
        sort_key = shares * float(price) if price and price != "0" else float(shares)
        transactions.append(
            (
                sort_key,
                [
                    asset,
                    date,
                    verb,
                    f"{direction_text} {format_shares(shares)}",
                    price_text,
                    truncate_text(owner, 26),
                ],
            )
        )
    transactions.sort(key=lambda item: item[0], reverse=True)
    rows: list[list[str]] = []
    for index, (_, row) in enumerate(transactions[:6], start=1):
        rows.append([str(index), *row])
    return rows


def snapshot_sec_form4(watch: dict[str, str]) -> Snapshot:
    entries = sec_feed(watch["cik"], "4", watch["owner"], count=5)
    if not entries:
        raise RuntimeError(f"{watch['label']} did not return any Form 4 filings.")
    latest_entry = entries[0]
    items = fetch_sec_index(latest_entry["filing_href"])
    doc_name = choose_doc(items, pattern=r"\.xml$")
    transactions = parse_form4_transactions(fetch_text(sec_doc_url(latest_entry["filing_href"], doc_name), headers=sec_headers()))
    lines = [
        f"新 Form 4 | filed {latest_entry['filing_date']}",
        render_table(["#", "Asset", "Date", "Action", "Shares", "Price", "Owner"], transactions),
    ]
    return Snapshot(
        key=watch["key"],
        label=watch["label"],
        id=latest_entry["accession"],
        title=f"{watch['label']} 最新 Form 4",
        summary_lines=lines,
        source_url=latest_entry["filing_href"],
        meta={"filing_date": latest_entry["filing_date"]},
    )


def extract_pdf_text_with_bundled_python(pdf_path: pathlib.Path) -> str:
    if not BUNDLED_PYTHON.exists():
        raise RuntimeError("Bundled Python with pypdf is not available.")
    extractor = (
        "from pypdf import PdfReader; "
        "import json, sys; "
        "reader = PdfReader(sys.argv[1]); "
        "print(json.dumps([page.extract_text() or '' for page in reader.pages], ensure_ascii=False))"
    )
    result = subprocess.run(
        [str(BUNDLED_PYTHON), "-c", extractor, str(pdf_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    pages = json.loads(result.stdout)
    return "\n".join(pages)


def parse_pelosi_transactions(pdf_text: str) -> list[list[str]]:
    cleaned = pdf_text.replace("\x00", "")
    cleaned = re.sub(r"ID Owner Asset Transaction\s*Type\s*Date Notification\s*Date\s*Amount Cap\.\s*Gains >\s*\$200\?", "", cleaned)
    cleaned = re.sub(r"\* For the complete list.*", "", cleaned, flags=re.S)
    chunks = [chunk.strip() for chunk in re.split(r"(?=SP )", cleaned) if chunk.strip().startswith("SP ")]

    rows: list[tuple[float, list[str]]] = []
    for chunk in chunks:
        chunk = re.sub(r"\s+", " ", chunk)
        match = re.search(
            r"^SP (?P<asset>.+?) (?P<action>[A-Z](?: \(partial\))?) (?P<tx>\d{2}/\d{2}/\d{4})(?P<notify>\d{2}/\d{2}/\d{4})(?P<amount>\$[\d,\.]+(?:\s*-\s*\$[\d,\.]+)?) ",
            chunk,
        )
        if not match:
            continue
        asset = match.group("asset").strip()
        description = ""
        desc_match = re.search(r"D[^:]*:\s*(.+)$", chunk)
        if desc_match:
            description = truncate_text(desc_match.group(1).strip(), 42)
        ticker_match = re.search(r"\(([A-Z\.]+)\)", asset)
        ticker = ticker_match.group(1) if ticker_match else "-"
        rows.append(
            (
                amount_range_sort_key(match.group("amount")),
                [
                    ticker,
                    truncate_text(asset, 28),
                    match.group("action"),
                    match.group("tx"),
                    match.group("amount").replace(" ", ""),
                    description or "-",
                ],
            )
        )

    rows.sort(key=lambda item: item[0], reverse=True)
    ranked_rows: list[list[str]] = []
    for index, (_, row) in enumerate(rows[:10], start=1):
        ranked_rows.append([str(index), *row])
    return ranked_rows


def house_member_rows(last_name: str, year: int, state: str, district: str) -> list[dict[str, str]]:
    import http.cookiejar

    cookie_jar = http.cookiejar.CookieJar()
    opener = build_opener(cookie_jar)
    search_html = fetch_text(
        urllib.parse.urljoin(HOUSE_BASE, "FinancialDisclosure/ViewSearch"),
        headers={"User-Agent": "Mozilla/5.0"},
        opener=opener,
    )
    match = re.search(r'__RequestVerificationToken" type="hidden" value="([^"]+)"', search_html)
    if not match:
        raise RuntimeError("Failed to locate House anti-forgery token.")
    payload = urllib.parse.urlencode(
        {
            "LastName": last_name,
            "FilingYear": str(year),
            "State": state,
            "District": district,
            "__RequestVerificationToken": match.group(1),
        }
    ).encode()
    result_html = fetch_text(
        urllib.parse.urljoin(HOUSE_BASE, "FinancialDisclosure/ViewMemberSearchResult"),
        headers={
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": HOUSE_BASE.rstrip("/"),
            "Referer": urllib.parse.urljoin(HOUSE_BASE, "FinancialDisclosure/ViewSearch"),
        },
        data=payload,
        opener=opener,
    )

    pattern = re.compile(
        r'<tr[^>]*>\s*<td[^>]*class="memberName">\s*<a href="([^"]+)"[^>]*>([^<]+)</a>\s*</td>\s*'
        r'<td[^>]*data-label="Office">([^<]*)</td>\s*'
        r'<td[^>]*data-label="Filing Year">([^<]*)</td>\s*'
        r'<td[^>]*data-label="Filing">([^<]*)</td>',
        re.S,
    )
    rows = []
    for href, name, office, filing_year, filing_type in pattern.findall(result_html):
        rows.append(
            {
                "href": urllib.parse.urljoin(HOUSE_BASE, href),
                "name": html.unescape(name).strip(),
                "office": office.strip(),
                "filing_year": filing_year.strip(),
                "filing_type": filing_type.strip(),
            }
        )
    return rows


def snapshot_house_ptr(watch: dict[str, str]) -> Snapshot:
    current_year = dt.datetime.now(dt.timezone.utc).year
    rows: list[dict[str, str]] = []
    for year in (current_year, current_year - 1):
        rows.extend(house_member_rows(watch["last_name"], year, watch["state"], watch["district"]))
    if not rows:
        raise RuntimeError(f"{watch['label']} did not return any House PTR results.")

    def row_rank(row: dict[str, str]) -> tuple[int, int]:
        match = re.search(r"/(\d{4})/(\d+)\.pdf$", row["href"])
        filing_year = int(match.group(1)) if match else int(row["filing_year"])
        filing_number = int(match.group(2)) if match else 0
        return filing_year, filing_number

    latest = sorted(rows, key=row_rank, reverse=True)[0]
    pdf_bytes = fetch_bytes(latest["href"], headers={"User-Agent": "Mozilla/5.0"})
    with tempfile.NamedTemporaryFile(prefix="pelosi_ptr_", suffix=".pdf", delete=False) as handle:
        handle.write(pdf_bytes)
        temp_path = pathlib.Path(handle.name)
    try:
        pdf_text = extract_pdf_text_with_bundled_python(temp_path)
    finally:
        temp_path.unlink(missing_ok=True)

    transaction_rows = parse_pelosi_transactions(pdf_text)
    lines = [
        f"新 House PTR | filing year {latest['filing_year']} | {latest['filing_type']}",
        render_table(
            ["Name", "Office", "Year", "Type"],
            [[truncate_text(latest["name"], 24), latest["office"], latest["filing_year"], latest["filing_type"]]],
        ),
        "Top disclosed transactions:",
        render_table(["#", "Ticker", "Asset", "Type", "Date", "Amount", "Details"], transaction_rows),
        f"PDF: {latest['href']}",
    ]
    return Snapshot(
        key=watch["key"],
        label=watch["label"],
        id=latest["href"],
        title=f"{watch['label']} 最新 PTR",
        summary_lines=lines,
        source_url=latest["href"],
        meta={"filing_year": latest["filing_year"], "filing_type": latest["filing_type"]},
    )


def collect_snapshots(watches: list[dict[str, str]]) -> list[Snapshot]:
    snapshots: list[Snapshot] = []
    for watch in watches:
        if watch["kind"] == "sec_13f":
            snapshots.append(snapshot_sec_13f(watch))
        elif watch["kind"] == "berkshire_company":
            snapshots.append(snapshot_berkshire_company(watch))
        elif watch["kind"] == "sec_form4":
            snapshots.append(snapshot_sec_form4(watch))
        elif watch["kind"] == "house_ptr":
            snapshots.append(snapshot_house_ptr(watch))
        else:
            raise RuntimeError(f"Unsupported watch kind: {watch['kind']}")
    return snapshots


def format_snapshot(snapshot: Snapshot) -> str:
    lines = [snapshot.title]
    lines.extend(snapshot.summary_lines)
    lines.append(f"Source: {snapshot.source_url}")
    return "\n".join(lines)


def format_lark_message(changed: list[Snapshot]) -> str:
    timestamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    blocks = [f"最新披露提醒\n生成时间: {timestamp}"]
    for snapshot in changed:
        blocks.append(format_snapshot(snapshot))
    return "\n\n".join(blocks)


def detect_lark_user_id() -> str | None:
    for env_name in ("LARK_USER_ID", "LARK_OPEN_ID"):
        env_value = os.environ.get(env_name)
        if env_value:
            return env_value

    try:
        result = subprocess.run(
            ["lark-cli", "auth", "status"],
            check=True,
            capture_output=True,
            text=True,
        )
        data = json.loads(result.stdout)
    except Exception:
        return None

    if data.get("userOpenId"):
        return data["userOpenId"]
    user_identity = data.get("identities", {}).get("user", {})
    return user_identity.get("openId")


def detect_group_webhook() -> str | None:
    env_value = os.environ.get("LARK_GROUP_WEBHOOK")
    if env_value:
        return env_value

    settings = load_local_settings()
    webhook = settings.get("lark_group_webhook")
    return webhook if isinstance(webhook, str) and webhook else None


def send_lark_private_message(message: str) -> None:
    user_id = detect_lark_user_id()
    if not user_id:
        raise RuntimeError("Unable to resolve a target Lark user ID. Set LARK_USER_ID or re-auth lark-cli.")

    subprocess.run(
        [
            "lark-cli",
            "im",
            "+messages-send",
            "--as",
            "bot",
            "--user-id",
            user_id,
            "--markdown",
            message,
        ],
        check=True,
    )


def send_lark_group_webhook(message: str) -> None:
    webhook = detect_group_webhook()
    if not webhook:
        return

    payload = {
        "msg_type": "text",
        "content": {
            "text": message,
        },
    }
    request = urllib.request.Request(
        webhook,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.build_opener().open(request, timeout=30) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        body = response.read().decode(charset, errors="replace")
    if body:
        data = json.loads(body)
        if data.get("code") not in (0, None):
            raise RuntimeError(f"Group webhook returned code {data.get('code')}: {data.get('msg')}")


def send_lark_outputs(message: str) -> None:
    errors: list[str] = []

    try:
        send_lark_private_message(message)
    except Exception as exc:
        errors.append(f"private message failed: {exc}")

    try:
        send_lark_group_webhook(message)
    except Exception as exc:
        errors.append(f"group webhook failed: {exc}")

    if errors:
        raise RuntimeError("; ".join(errors))


def preview(snapshots: list[Snapshot]) -> int:
    for snapshot in snapshots:
        print(format_snapshot(snapshot))
        print()
    return 0


def main() -> int:
    args = parse_args()
    try:
        watches = selected_watchlist(args.keys)
        snapshots = collect_snapshots(watches)
    except Exception as exc:
        print(f"Error while collecting filings: {exc}", file=sys.stderr)
        return 1

    if args.preview:
        return preview(snapshots)

    current_state = {snapshot.key: snapshot.id for snapshot in snapshots}
    saved_state = load_state()

    if not saved_state:
        save_state(current_state)
        print(f"State bootstrapped at {STATE_PATH}")
        return 0

    changed = [snapshot for snapshot in snapshots if saved_state.get(snapshot.key) != snapshot.id]
    if not changed:
        print("No new filings detected.")
        return 0

    message = format_lark_message(changed)
    if args.dry_run:
        print(message)
        return 0

    try:
        send_lark_outputs(message)
    except Exception as exc:
        print(f"Lark send failed: {exc}", file=sys.stderr)
        return 1

    merged_state = dict(saved_state)
    merged_state.update(current_state)
    save_state(merged_state)
    print(f"Sent {len(changed)} filing update(s) to Lark.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
