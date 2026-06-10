from __future__ import annotations

import csv
import dataclasses
from collections import Counter
from pathlib import Path


@dataclasses.dataclass(frozen=True)
class Strategy:
    key: str
    title: str
    description: str
    weights: dict[str, float]
    filters: tuple[tuple[str, str, float], ...]
    aliases: tuple[str, ...]


METRIC_SPECS = {
    "market_cap_b": {"label": "市值", "higher_better": True, "kind": "number"},
    "pe": {"label": "PE", "higher_better": False, "kind": "number"},
    "pb": {"label": "PB", "higher_better": False, "kind": "number"},
    "roe": {"label": "ROE", "higher_better": True, "kind": "percent"},
    "revenue_growth": {"label": "收入增长", "higher_better": True, "kind": "percent"},
    "momentum_20d": {"label": "20日动量", "higher_better": True, "kind": "percent"},
    "momentum_60d": {"label": "60日动量", "higher_better": True, "kind": "percent"},
    "volatility_20d": {"label": "20日波动率", "higher_better": False, "kind": "percent"},
    "dividend_yield": {"label": "股息率", "higher_better": True, "kind": "percent"},
}

NUMERIC_FIELDS = tuple(METRIC_SPECS.keys()) + ("price",)

STRATEGIES = {
    "quality": Strategy(
        key="quality",
        title="质量动量",
        description="偏向高 ROE、高增长、同时趋势没有走坏的核心龙头。",
        weights={
            "roe": 0.30,
            "revenue_growth": 0.24,
            "momentum_60d": 0.20,
            "volatility_20d": 0.14,
            "pb": 0.12,
        },
        filters=(("market_cap_b", ">=", 300.0), ("momentum_60d", ">=", 0.0)),
        aliases=("质量", "quality", "quality_momentum"),
    ),
    "momentum": Strategy(
        key="momentum",
        title="趋势增强",
        description="偏向中短期价格趋势最强、并且基本面没有掉队的标的。",
        weights={
            "momentum_20d": 0.34,
            "momentum_60d": 0.34,
            "revenue_growth": 0.12,
            "market_cap_b": 0.10,
            "volatility_20d": 0.10,
        },
        filters=(("momentum_20d", ">=", 0.0), ("momentum_60d", ">=", 0.0)),
        aliases=("动量", "momentum", "trend"),
    ),
    "value": Strategy(
        key="value",
        title="低估价值",
        description="偏向估值不高、股息不差、盈利能力还在线的大盘股。",
        weights={
            "pe": 0.30,
            "pb": 0.25,
            "dividend_yield": 0.20,
            "roe": 0.15,
            "revenue_growth": 0.10,
        },
        filters=(("market_cap_b", ">=", 350.0),),
        aliases=("价值", "value", "deep_value"),
    ),
    "defensive": Strategy(
        key="defensive",
        title="低波防守",
        description="偏向波动率更稳、现金流更扎实、适合回撤敏感型选股。",
        weights={
            "volatility_20d": 0.38,
            "dividend_yield": 0.22,
            "market_cap_b": 0.16,
            "momentum_60d": 0.14,
            "roe": 0.10,
        },
        filters=(("market_cap_b", ">=", 350.0),),
        aliases=("低波", "防守", "defensive", "low_vol"),
    ),
}


def coerce_float(value: str) -> float:
    if value is None:
        return 0.0
    stripped = str(value).strip()
    if not stripped:
        return 0.0
    return float(stripped)


def load_universe(path: str | Path) -> list[dict[str, object]]:
    csv_path = Path(path)
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = []
        for raw in reader:
            row: dict[str, object] = {
                "ticker": (raw.get("ticker") or "").strip().upper(),
                "name": (raw.get("name") or "").strip(),
                "sector": (raw.get("sector") or "").strip(),
            }
            for field in NUMERIC_FIELDS:
                row[field] = coerce_float(raw.get(field, "0"))
            rows.append(row)
    if not rows:
        raise RuntimeError(f"No rows found in universe csv: {csv_path}")
    return rows


def resolve_strategy(value: str | None) -> Strategy:
    if value is None or not value.strip():
        return STRATEGIES["quality"]
    normalized = value.strip().lower()
    for strategy in STRATEGIES.values():
        if normalized == strategy.key or normalized in {alias.lower() for alias in strategy.aliases}:
            return strategy
    available = ", ".join(f"{item.title}({item.key})" for item in STRATEGIES.values())
    raise RuntimeError(f"Unknown strategy `{value}`. Available: {available}.")


def passes_filters(row: dict[str, object], strategy: Strategy) -> bool:
    for field, operator_symbol, threshold in strategy.filters:
        value = float(row[field])
        if operator_symbol == ">=" and value < threshold:
            return False
        if operator_symbol == ">" and value <= threshold:
            return False
        if operator_symbol == "<=" and value > threshold:
            return False
        if operator_symbol == "<" and value >= threshold:
            return False
    return True


def percentile_scores(rows: list[dict[str, object]], field: str, higher_better: bool) -> dict[str, float]:
    if len(rows) == 1:
        return {str(rows[0]["ticker"]): 100.0}
    ordered = sorted(rows, key=lambda row: (float(row[field]), str(row["ticker"])))
    scores: dict[str, float] = {}
    denominator = len(ordered) - 1
    for index, row in enumerate(ordered):
        percentile = 100.0 * index / denominator
        score = percentile if higher_better else 100.0 - percentile
        scores[str(row["ticker"])] = round(score, 2)
    return scores


def score_rows(rows: list[dict[str, object]], strategy: Strategy) -> list[dict[str, object]]:
    percentiles = {
        field: percentile_scores(rows, field, bool(spec["higher_better"]))
        for field, spec in METRIC_SPECS.items()
    }
    scored: list[dict[str, object]] = []
    for row in rows:
        factor_scores: dict[str, float] = {}
        contributions: dict[str, float] = {}
        total_score = 0.0
        ticker = str(row["ticker"])
        for field, weight in strategy.weights.items():
            subscore = percentiles[field][ticker]
            factor_scores[field] = subscore
            contribution = subscore * weight
            contributions[field] = contribution
            total_score += contribution
        record = dict(row)
        record["strategy"] = strategy
        record["passes_filter"] = passes_filters(row, strategy)
        record["factor_scores"] = factor_scores
        record["contributions"] = contributions
        record["score"] = round(total_score, 1)
        scored.append(record)
    return sorted(scored, key=lambda item: (-float(item["score"]), str(item["ticker"])))


def format_metric_value(field: str, value: float) -> str:
    spec = METRIC_SPECS[field]
    if spec["kind"] == "percent":
        return f"{value:.1f}%"
    return f"{value:.1f}"


def top_reason_lines(row: dict[str, object], limit: int = 3) -> list[str]:
    contributions = dict(row["contributions"])
    ordered = sorted(contributions.items(), key=lambda item: item[1], reverse=True)[:limit]
    reasons = []
    for field, _ in ordered:
        metric_value = format_metric_value(field, float(row[field]))
        reasons.append(f"{METRIC_SPECS[field]['label']} {metric_value}")
    return reasons


def screen_stocks(path: str | Path, strategy_value: str | None, top_n: int = 5) -> tuple[Strategy, list[dict[str, object]], int]:
    universe = load_universe(path)
    strategy = resolve_strategy(strategy_value)
    scored = score_rows(universe, strategy)
    selected = [row for row in scored if bool(row["passes_filter"])]
    return strategy, selected[:top_n], len(universe)


def market_label(market: str | None) -> str:
    if market and market.strip().lower() in {"a", "a股", "ashare", "a-share", "cn", "china", "沪深", "中国"}:
        return "A股"
    return "样本池"


def format_screen_output(path: str | Path, strategy_value: str | None, top_n: int = 5, market: str | None = None) -> str:
    strategy, picks, universe_size = screen_stocks(path, strategy_value, top_n=top_n)
    if not picks:
        return f"{strategy.title}\n\n当前没有股票通过过滤条件。"
    lines = [
        "Quant WeChat Bot",
        "",
        f"策略: {strategy.title}",
        f"市场: {market_label(market)}",
        strategy.description,
        f"股票池: {universe_size} 只，展示前 {len(picks)} 只。",
        "",
        "Top Picks",
    ]
    for index, row in enumerate(picks, start=1):
        lines.append(
            f"{index}. {row['ticker']} | {row['name']} | 总分 {row['score']:.1f}"
        )
        market_cap_unit = "亿" if market_label(market) == "A股" else "B"
        lines.append(
            f"   行业 {row['sector']} | 价格 {float(row['price']):.2f} | 市值 {float(row['market_cap_b']):.0f}{market_cap_unit}"
        )
        lines.append(f"   亮点: {'; '.join(top_reason_lines(row))}")
    lines.append("")
    example = "评分 600519 A股" if market_label(market) == "A股" else "评分 NVDA"
    if market_label(market) == "A股":
        lines.append("注: 免费 A股源当前使用东方财富快照，基本面因子待接入更完整财报源。")
    lines.append(f"发送 `{example}` 查看单票多策略评分。")
    return "\n".join(lines)


def format_strategy_catalog() -> str:
    lines = ["Quant WeChat Bot", "", "可用策略"]
    for strategy in STRATEGIES.values():
        alias_text = ", ".join(strategy.aliases[:2])
        lines.append(f"- {strategy.title} | 命令: 选股 {alias_text}")
        lines.append(f"  {strategy.description}")
    return "\n".join(lines)


def find_ticker(path: str | Path, ticker: str) -> dict[str, object]:
    normalized = ticker.strip().upper()
    universe = load_universe(path)
    for row in universe:
        if row["ticker"] == normalized:
            return row
    raise RuntimeError(f"Ticker `{ticker}` not found in the current universe csv.")


def format_stock_report(path: str | Path, ticker: str, market: str | None = None) -> str:
    universe = load_universe(path)
    normalized = ticker.strip().upper()
    matched = next((row for row in universe if row["ticker"] == normalized), None)
    if matched is None:
        raise RuntimeError(f"Ticker `{ticker}` not found in the current universe csv.")

    strategy_rows = {}
    for strategy in STRATEGIES.values():
        scored = score_rows(universe, strategy)
        strategy_rows[strategy.key] = next(row for row in scored if row["ticker"] == normalized)

    lines = [
        "Quant WeChat Bot",
        "",
        f"标的: {matched['ticker']} | {matched['name']}",
        f"市场: {market_label(market)}",
        f"行业: {matched['sector']}",
        f"价格: {float(matched['price']):.2f}",
        "",
        "核心因子",
        f"- ROE {float(matched['roe']):.1f}%",
        f"- 收入增长 {float(matched['revenue_growth']):.1f}%",
        f"- 20日动量 {float(matched['momentum_20d']):.1f}%",
        f"- 60日动量 {float(matched['momentum_60d']):.1f}%",
        f"- 20日波动率 {float(matched['volatility_20d']):.1f}%",
        "",
        "多策略评分",
    ]
    for strategy in STRATEGIES.values():
        row = strategy_rows[strategy.key]
        status = "入选" if bool(row["passes_filter"]) else "观察"
        lines.append(f"- {strategy.title}: {float(row['score']):.1f} ({status})")
    best = max(strategy_rows.values(), key=lambda row: float(row["score"]))
    lines.append("")
    lines.append(f"最强匹配: {best['strategy'].title}")
    lines.append(f"原因: {'; '.join(top_reason_lines(best))}")
    return "\n".join(lines)


def format_universe_overview(path: str | Path, market: str | None = None) -> str:
    universe = load_universe(path)
    sectors = Counter(str(row["sector"]) for row in universe)
    lines = [
        "Quant WeChat Bot",
        "",
        f"市场: {market_label(market)}",
        f"当前股票池共 {len(universe)} 只股票。",
        "行业分布",
    ]
    for sector, count in sectors.most_common():
        lines.append(f"- {sector}: {count}")
    lines.append("")
    if market_label(market) == "A股":
        lines.append("数据源: 东方财富免费行情快照；已过滤 ST/退市/低成交额股票。")
    else:
        lines.append("你可以把 `sample_universe.csv` 替换成自己的日频因子导出文件。")
    return "\n".join(lines)
