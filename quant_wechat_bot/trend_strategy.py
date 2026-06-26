from __future__ import annotations

import concurrent.futures
import csv
import dataclasses
import datetime as dt
import math
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

try:
    from . import market_close_digest, quant_engine
except ImportError:  # pragma: no cover - allows direct script execution
    import market_close_digest  # type: ignore
    import quant_engine  # type: ignore


PROJECT_ROOT = Path(__file__).resolve().parent
HISTORY_CACHE_ROOT = PROJECT_ROOT / ".cache" / "trend_history"
TRADING_DAYS_PER_MONTH = 21
HISTORY_WINDOW = 420
MIN_HISTORY_BARS = 140
REBALANCE_DAYS = 20
DEFAULT_TOP_N = 5
MAX_POSITION_WEIGHT = 0.25
MAX_SECTOR_POSITIONS = 2
MAX_SECTOR_WEIGHT = 0.35
TARGET_GROSS_EXPOSURE = 1.0
MIN_POSITION_WEIGHT = 0.02
DEFAULT_STOP_LOSS_PCT = 0.12
DEFAULT_TRAILING_STOP_PCT = 0.15
DEFAULT_TREND_BREAK_WINDOW = 20
DEFAULT_ENTRY_STARTER_FRACTION = 0.6
DEFAULT_MIN_ADD_ON_TRIGGER_PCT = 0.03
DEFAULT_MAX_ADD_ON_TRIGGER_PCT = 0.08
MAX_WORKERS = 6
DEFAULT_COMMISSION_BPS = 2.0
DEFAULT_SLIPPAGE_BPS = 8.0
DEFAULT_SELL_TAX_BPS = {
    "CN": 10.0,
    "HK": 0.0,
    "US": 0.0,
}


@dataclasses.dataclass(frozen=True)
class UniverseConfig:
    code: str
    label: str
    benchmark_symbol: str
    benchmark_name: str
    min_market_cap_b: float
    min_price: float
    max_candidates: int


@dataclasses.dataclass(frozen=True)
class RegimeSnapshot:
    market_code: str
    market_label: str
    benchmark_symbol: str
    benchmark_name: str
    as_of: dt.date
    close: float
    ma20: float
    ma60: float
    ma120: float
    ret60: float
    risk_on: bool
    signals_on: int


@dataclasses.dataclass(frozen=True)
class TrendPick:
    ticker: str
    history_symbol: str
    name: str
    sector: str
    market_label: str
    market_cap_b: float
    score: float
    weight: float
    ret20: float
    ret60: float
    ret120: float
    relative_strength_60d: float
    volatility_20d: float
    close: float


@dataclasses.dataclass(frozen=True)
class TrendSnapshot:
    market_label: str
    as_of: dt.date
    candidate_count: int
    evaluated_count: int
    history_failures: int
    invested_weight: float
    cash_weight: float
    regimes: tuple[RegimeSnapshot, ...]
    constraints: PortfolioConstraints
    exit_rules: TrendExitRules
    execution_rules: TrendExecutionRules
    order_sizing_rules: OrderSizingRules
    market_exposures: tuple[MarketExposure, ...]
    constraint_diagnostics: ConstraintDiagnostics
    previous_rebalance_date: dt.date | None
    trade_plan: tuple[TradeInstruction, ...]
    execution_plan: tuple[ExecutionInstruction, ...]
    picks: tuple[TrendPick, ...]


@dataclasses.dataclass(frozen=True)
class SectorExposure:
    sector: str
    weight: float
    count: int


@dataclasses.dataclass(frozen=True)
class MarketExposure:
    market_code: str
    market_label: str
    target_weight: float
    actual_weight: float
    count: int


@dataclasses.dataclass(frozen=True)
class TradeInstruction:
    action: str
    ticker: str
    name: str
    from_weight: float
    to_weight: float
    reason: str


@dataclasses.dataclass(frozen=True)
class BacktestCostModel:
    commission_bps: float
    slippage_bps: float
    sell_tax_bps: dict[str, float]


@dataclasses.dataclass(frozen=True)
class PortfolioConstraints:
    max_position_weight: float
    max_sector_positions: int
    max_sector_weight: float
    target_gross_exposure: float
    market_weight_budget: dict[str, float]


@dataclasses.dataclass(frozen=True)
class TrendExitRules:
    stop_loss_pct: float
    trailing_stop_pct: float
    trend_break_window: int


@dataclasses.dataclass(frozen=True)
class TrendExecutionRules:
    entry_starter_fraction: float
    min_add_on_trigger_pct: float
    max_add_on_trigger_pct: float


@dataclasses.dataclass(frozen=True)
class OrderSizingRules:
    market_capital: dict[str, float]
    lot_size_by_market: dict[str, int]
    lot_size_by_ticker: dict[str, int]
    currency_by_market: dict[str, str]


@dataclasses.dataclass(frozen=True)
class ConstraintDiagnostics:
    skipped_sector_position_limit: int = 0
    skipped_sector_weight_limit: int = 0
    skipped_market_budget_limit: int = 0
    skipped_small_remainder: int = 0
    partial_weight_positions: int = 0


@dataclasses.dataclass(frozen=True)
class ExitEvent:
    ticker: str
    name: str
    session_date: dt.date
    reason: str
    return_pct: float


@dataclasses.dataclass(frozen=True)
class ExecutionInstruction:
    action: str
    ticker: str
    name: str
    from_weight: float
    to_weight: float
    trigger_price: float | None
    stop_price: float | None
    risk_budget_pct: float
    budget_value: float | None
    currency: str | None
    estimated_quantity: int | None
    estimated_lots: int | None
    note: str


@dataclasses.dataclass(frozen=True)
class PeriodContribution:
    ticker: str
    name: str
    weight: float
    asset_return: float
    contribution: float


@dataclasses.dataclass(frozen=True)
class DailyEquityPoint:
    session_date: dt.date
    gross_value: float
    net_value: float
    drawdown: float


@dataclasses.dataclass(frozen=True)
class DailyReturnSummary:
    session_date: dt.date
    return_pct: float


@dataclasses.dataclass(frozen=True)
class PeriodValuePoint:
    session_date: dt.date
    gross_value: float


@dataclasses.dataclass(frozen=True)
class BacktestPeriod:
    start_date: dt.date
    end_date: dt.date
    gross_return: float
    cost_drag: float
    portfolio_return: float
    invested_weight: float
    turnover: float
    buy_turnover: float
    sell_turnover: float
    holdings: tuple[TrendPick, ...]
    risk_on_markets: tuple[str, ...]
    contributions: tuple[PeriodContribution, ...]
    exit_events: tuple[ExitEvent, ...]
    daily_path: tuple[PeriodValuePoint, ...]


@dataclasses.dataclass(frozen=True)
class ExitReasonCount:
    reason: str
    count: int


@dataclasses.dataclass(frozen=True)
class BacktestReport:
    market_label: str
    start_date: dt.date
    end_date: dt.date
    candidate_count: int
    evaluated_count: int
    history_failures: int
    gross_total_return: float
    total_cost_drag: float
    total_return: float
    annualized_return: float
    max_drawdown: float
    win_rate: float
    average_invested_weight: float
    average_cost_drag: float
    average_period_return: float
    average_turnover: float
    cost_model: BacktestCostModel
    current_sector_exposures: tuple[SectorExposure, ...]
    daily_curve: tuple[DailyEquityPoint, ...]
    best_day: DailyReturnSummary | None
    worst_day: DailyReturnSummary | None
    exit_reason_counts: tuple[ExitReasonCount, ...]
    periods: tuple[BacktestPeriod, ...]
    latest_snapshot: TrendSnapshot


UNIVERSE_CONFIGS: dict[str, UniverseConfig] = {
    "CN": UniverseConfig(
        code="CN",
        label="A股",
        benchmark_symbol="000300.SS",
        benchmark_name="沪深300",
        min_market_cap_b=80.0,
        min_price=3.0,
        max_candidates=36,
    ),
    "HK": UniverseConfig(
        code="HK",
        label="港股",
        benchmark_symbol="^HSI",
        benchmark_name="恒生指数",
        min_market_cap_b=30.0,
        min_price=1.0,
        max_candidates=30,
    ),
    "US": UniverseConfig(
        code="US",
        label="美股",
        benchmark_symbol="SPY",
        benchmark_name="SPY",
        min_market_cap_b=10.0,
        min_price=5.0,
        max_candidates=30,
    ),
}


HistoryFetcher = Callable[[str], list[tuple[dt.date, float]]]


def infer_market_code(ticker: str) -> str:
    upper = ticker.strip().upper()
    if upper.endswith(".HK"):
        return "HK"
    if upper.endswith(".SS") or upper.endswith(".SZ") or upper.isdigit():
        return "CN"
    return "US"


def market_label(value: str | None) -> str:
    if value is None or not value.strip():
        return "全市场"
    normalized = value.strip().lower()
    if normalized in {"all", "global", "world", "全市场", "全部", "所有", "全球", "a+h+us", "ahus"}:
        return "全市场"
    config = market_close_digest.normalize_market(value)
    return config.label


def resolve_market_codes(value: str | None) -> tuple[str, ...]:
    label = market_label(value)
    if label == "全市场":
        return ("CN", "HK", "US")
    config = market_close_digest.normalize_market(label)
    return (config.code,)


def history_symbol_for_ticker(ticker: str) -> str:
    upper = ticker.strip().upper()
    if upper.endswith((".HK", ".SS", ".SZ")):
        return upper
    if upper.isdigit():
        if upper.startswith(("600", "601", "603", "605", "688", "689")):
            return f"{upper}.SS"
        return f"{upper}.SZ"
    return upper


def load_backtest_cost_model() -> BacktestCostModel:
    settings = market_close_digest.load_settings()
    commission_bps = DEFAULT_COMMISSION_BPS
    slippage_bps = DEFAULT_SLIPPAGE_BPS
    sell_tax_bps = dict(DEFAULT_SELL_TAX_BPS)
    payload = settings.get("trend_backtest_costs")
    if isinstance(payload, Mapping):
        raw_commission = payload.get("commission_bps")
        raw_slippage = payload.get("slippage_bps")
        if isinstance(raw_commission, (int, float)):
            commission_bps = float(raw_commission)
        if isinstance(raw_slippage, (int, float)):
            slippage_bps = float(raw_slippage)
        raw_sell_tax = payload.get("sell_tax_bps")
        if isinstance(raw_sell_tax, Mapping):
            for key, value in raw_sell_tax.items():
                if isinstance(key, str) and isinstance(value, (int, float)):
                    sell_tax_bps[key.strip().upper()] = float(value)
    return BacktestCostModel(
        commission_bps=commission_bps,
        slippage_bps=slippage_bps,
        sell_tax_bps=sell_tax_bps,
    )


def clamp_weight(value: object, default: float) -> float:
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return min(max(float(value), 0.0), 1.0)
    return default


def resolve_market_weight_budget(
    target_codes: tuple[str, ...],
    configured: Mapping[str, object] | None,
) -> dict[str, float]:
    if not target_codes:
        return {}
    if len(target_codes) == 1:
        return {target_codes[0]: 1.0}

    explicit: dict[str, float] = {}
    if configured is not None:
        for key, value in configured.items():
            if not isinstance(key, str):
                continue
            code = key.strip().upper()
            if code not in target_codes or not isinstance(value, (int, float)):
                continue
            explicit[code] = min(max(float(value), 0.0), 1.0)

    explicit_sum = sum(explicit.values())
    if explicit_sum >= 1.0:
        if explicit_sum <= 0:
            return {code: 0.0 for code in target_codes}
        return {code: explicit.get(code, 0.0) / explicit_sum for code in target_codes}

    remaining_codes = [code for code in target_codes if code not in explicit]
    remaining_budget = max(0.0, 1.0 - explicit_sum)
    fill_weight = remaining_budget / len(remaining_codes) if remaining_codes else 0.0
    return {code: explicit.get(code, fill_weight) for code in target_codes}


def load_portfolio_constraints(target_codes: tuple[str, ...]) -> PortfolioConstraints:
    settings = market_close_digest.load_settings()
    payload = settings.get("trend_portfolio_constraints")
    max_position_weight = MAX_POSITION_WEIGHT
    max_sector_positions = MAX_SECTOR_POSITIONS
    max_sector_weight = MAX_SECTOR_WEIGHT
    target_gross_exposure = TARGET_GROSS_EXPOSURE
    market_weight_budget = resolve_market_weight_budget(target_codes, None)

    if isinstance(payload, Mapping):
        max_position_weight = clamp_weight(payload.get("max_position_weight"), max_position_weight)
        raw_sector_positions = payload.get("max_sector_positions")
        if isinstance(raw_sector_positions, int) and raw_sector_positions > 0:
            max_sector_positions = raw_sector_positions
        max_sector_weight = clamp_weight(payload.get("max_sector_weight"), max_sector_weight)
        target_gross_exposure = clamp_weight(payload.get("target_gross_exposure"), target_gross_exposure)
        raw_market_budget = payload.get("market_weight_budget")
        market_weight_budget = resolve_market_weight_budget(
            target_codes,
            raw_market_budget if isinstance(raw_market_budget, Mapping) else None,
        )

    max_position_weight = max(max_position_weight, MIN_POSITION_WEIGHT)
    max_sector_weight = max(max_sector_weight, max_position_weight)
    target_gross_exposure = max(target_gross_exposure, MIN_POSITION_WEIGHT)

    return PortfolioConstraints(
        max_position_weight=max_position_weight,
        max_sector_positions=max_sector_positions,
        max_sector_weight=max_sector_weight,
        target_gross_exposure=target_gross_exposure,
        market_weight_budget=market_weight_budget,
    )


def load_exit_rules() -> TrendExitRules:
    settings = market_close_digest.load_settings()
    payload = settings.get("trend_exit_rules")
    stop_loss_pct = DEFAULT_STOP_LOSS_PCT
    trailing_stop_pct = DEFAULT_TRAILING_STOP_PCT
    trend_break_window = DEFAULT_TREND_BREAK_WINDOW
    if isinstance(payload, Mapping):
        stop_loss_pct = clamp_weight(payload.get("stop_loss_pct"), stop_loss_pct)
        trailing_stop_pct = clamp_weight(payload.get("trailing_stop_pct"), trailing_stop_pct)
        raw_window = payload.get("trend_break_window")
        if isinstance(raw_window, int) and raw_window >= 0:
            trend_break_window = raw_window
    return TrendExitRules(
        stop_loss_pct=stop_loss_pct,
        trailing_stop_pct=trailing_stop_pct,
        trend_break_window=trend_break_window,
    )


def load_execution_rules() -> TrendExecutionRules:
    settings = market_close_digest.load_settings()
    payload = settings.get("trend_execution")
    entry_starter_fraction = DEFAULT_ENTRY_STARTER_FRACTION
    min_add_on_trigger_pct = DEFAULT_MIN_ADD_ON_TRIGGER_PCT
    max_add_on_trigger_pct = DEFAULT_MAX_ADD_ON_TRIGGER_PCT
    if isinstance(payload, Mapping):
        entry_starter_fraction = clamp_weight(payload.get("entry_starter_fraction"), entry_starter_fraction)
        min_add_on_trigger_pct = clamp_weight(payload.get("min_add_on_trigger_pct"), min_add_on_trigger_pct)
        max_add_on_trigger_pct = clamp_weight(payload.get("max_add_on_trigger_pct"), max_add_on_trigger_pct)
    entry_starter_fraction = min(max(entry_starter_fraction, 0.2), 1.0)
    min_add_on_trigger_pct = min(max(min_add_on_trigger_pct, 0.0), 0.2)
    max_add_on_trigger_pct = min(max(max_add_on_trigger_pct, min_add_on_trigger_pct), 0.2)
    return TrendExecutionRules(
        entry_starter_fraction=entry_starter_fraction,
        min_add_on_trigger_pct=min_add_on_trigger_pct,
        max_add_on_trigger_pct=max_add_on_trigger_pct,
    )


def load_order_sizing_rules() -> OrderSizingRules:
    settings = market_close_digest.load_settings()
    payload = settings.get("trend_order_sizing")
    market_capital: dict[str, float] = {}
    lot_size_by_market = {"CN": 100, "HK": 100, "US": 1}
    lot_size_by_ticker: dict[str, int] = {}
    currency_by_market = {"CN": "CNY", "HK": "HKD", "US": "USD"}
    if isinstance(payload, Mapping):
        raw_market_capital = payload.get("market_capital")
        if isinstance(raw_market_capital, Mapping):
            for key, value in raw_market_capital.items():
                if isinstance(key, str) and isinstance(value, (int, float)) and float(value) > 0:
                    market_capital[key.strip().upper()] = float(value)
        raw_lot_size_by_market = payload.get("lot_size_by_market")
        if isinstance(raw_lot_size_by_market, Mapping):
            for key, value in raw_lot_size_by_market.items():
                if isinstance(key, str) and isinstance(value, int) and value > 0:
                    lot_size_by_market[key.strip().upper()] = value
        raw_lot_size_by_ticker = payload.get("lot_size_by_ticker")
        if isinstance(raw_lot_size_by_ticker, Mapping):
            for key, value in raw_lot_size_by_ticker.items():
                if isinstance(key, str) and isinstance(value, int) and value > 0:
                    lot_size_by_ticker[key.strip().upper()] = value
        raw_currency_by_market = payload.get("currency_by_market")
        if isinstance(raw_currency_by_market, Mapping):
            for key, value in raw_currency_by_market.items():
                if isinstance(key, str) and isinstance(value, str) and value.strip():
                    currency_by_market[key.strip().upper()] = value.strip().upper()
    return OrderSizingRules(
        market_capital=market_capital,
        lot_size_by_market=lot_size_by_market,
        lot_size_by_ticker=lot_size_by_ticker,
        currency_by_market=currency_by_market,
    )


def summarize_sector_exposures(picks: tuple[TrendPick, ...] | list[TrendPick]) -> tuple[SectorExposure, ...]:
    exposures: dict[str, SectorExposure] = {}
    for item in picks:
        current = exposures.get(item.sector)
        if current is None:
            exposures[item.sector] = SectorExposure(sector=item.sector, weight=item.weight, count=1)
            continue
        exposures[item.sector] = SectorExposure(
            sector=item.sector,
            weight=current.weight + item.weight,
            count=current.count + 1,
        )
    ordered = sorted(exposures.values(), key=lambda item: (-item.weight, item.sector))
    return tuple(
        SectorExposure(sector=item.sector, weight=round(item.weight, 4), count=item.count)
        for item in ordered
    )


def summarize_market_exposures(
    picks: tuple[TrendPick, ...] | list[TrendPick],
    constraints: PortfolioConstraints,
) -> tuple[MarketExposure, ...]:
    exposures: dict[str, MarketExposure] = {}
    for code, target_weight in constraints.market_weight_budget.items():
        exposures[code] = MarketExposure(
            market_code=code,
            market_label=UNIVERSE_CONFIGS[code].label,
            target_weight=target_weight,
            actual_weight=0.0,
            count=0,
        )
    for item in picks:
        code = infer_market_code(item.ticker)
        current = exposures.get(code)
        if current is None:
            exposures[code] = MarketExposure(
                market_code=code,
                market_label=UNIVERSE_CONFIGS[code].label,
                target_weight=0.0,
                actual_weight=item.weight,
                count=1,
            )
            continue
        exposures[code] = MarketExposure(
            market_code=code,
            market_label=current.market_label,
            target_weight=current.target_weight,
            actual_weight=current.actual_weight + item.weight,
            count=current.count + 1,
        )
    ordered = []
    for code in constraints.market_weight_budget:
        exposure = exposures.get(code)
        if exposure is not None:
            ordered.append(exposure)
    for code, exposure in exposures.items():
        if code not in constraints.market_weight_budget:
            ordered.append(exposure)
    return tuple(
        MarketExposure(
            market_code=item.market_code,
            market_label=item.market_label,
            target_weight=round(item.target_weight, 4),
            actual_weight=round(item.actual_weight, 4),
            count=item.count,
        )
        for item in ordered
    )


def exit_rule_summary(exit_rules: TrendExitRules) -> str:
    parts = [
        f"止损 {exit_rules.stop_loss_pct * 100:.0f}%",
        f"移动止盈 {exit_rules.trailing_stop_pct * 100:.0f}%",
    ]
    if exit_rules.trend_break_window > 0:
        parts.append(f"跌破 MA{exit_rules.trend_break_window}")
    return " | ".join(parts)


def trade_action_rank(action: str) -> int:
    order = {
        "卖出": 0,
        "减仓": 1,
        "加仓": 2,
        "买入": 3,
    }
    return order.get(action, 9)


def format_trade_plan_item(item: TradeInstruction) -> str:
    return (
        f"- {item.action} {item.ticker} {item.name} | "
        f"{item.from_weight * 100:.0f}% -> {item.to_weight * 100:.0f}% | {item.reason}"
    )


def execution_rule_summary(execution_rules: TrendExecutionRules) -> str:
    starter = execution_rules.entry_starter_fraction * 100
    return (
        f"首仓 {starter:.0f}% | "
        f"二次加仓触发 {execution_rules.min_add_on_trigger_pct * 100:.0f}%~"
        f"{execution_rules.max_add_on_trigger_pct * 100:.0f}%"
    )


def execution_action_rank(action: str) -> int:
    order = {
        "立即卖出": 0,
        "立即减仓": 1,
        "首仓买入": 2,
        "首仓加仓": 3,
        "突破加仓": 4,
    }
    return order.get(action, 9)


def format_execution_plan_item(item: ExecutionInstruction) -> str:
    price_label = "触发价" if item.action == "突破加仓" else "参考价"
    price_text = f"{price_label} {item.trigger_price:.2f}" if item.trigger_price is not None else "价格以盘中成交为准"
    stop_text = f"止损 {item.stop_price:.2f}" if item.stop_price is not None else "无固定止损价"
    sizing_text_value = execution_order_sizing_text(item)
    sizing_text = f" | {sizing_text_value}" if sizing_text_value else ""
    return (
        f"- {item.action} {item.ticker} {item.name} | "
        f"{item.from_weight * 100:.0f}% -> {item.to_weight * 100:.0f}% | "
        f"{price_text} | {stop_text}{sizing_text} | 风险预算 {item.risk_budget_pct * 100:.1f}% | {item.note}"
    )


TRADE_PLAN_EXPORT_FIELDS = (
    "generated_at",
    "market",
    "as_of",
    "action",
    "ticker",
    "name",
    "from_weight_pct",
    "to_weight_pct",
    "reference_price",
    "stop_price",
    "budget_value",
    "currency",
    "estimated_quantity",
    "estimated_lots",
    "risk_budget_pct",
    "note",
)


def execution_plan_rows(snapshot: TrendSnapshot) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    generated_at = dt.datetime.now().isoformat(timespec="seconds")
    for item in snapshot.execution_plan:
        rows.append(
            {
                "generated_at": generated_at,
                "market": snapshot.market_label,
                "as_of": snapshot.as_of.isoformat(),
                "action": item.action,
                "ticker": item.ticker,
                "name": item.name,
                "from_weight_pct": f"{item.from_weight * 100:.2f}",
                "to_weight_pct": f"{item.to_weight * 100:.2f}",
                "reference_price": "" if item.trigger_price is None else f"{item.trigger_price:.2f}",
                "stop_price": "" if item.stop_price is None else f"{item.stop_price:.2f}",
                "budget_value": "" if item.budget_value is None else f"{item.budget_value:.2f}",
                "currency": item.currency or "",
                "estimated_quantity": "" if item.estimated_quantity is None else str(item.estimated_quantity),
                "estimated_lots": "" if item.estimated_lots is None else str(item.estimated_lots),
                "risk_budget_pct": f"{item.risk_budget_pct * 100:.2f}",
                "note": item.note,
            }
        )
    return rows


def order_sizing_summary(order_sizing_rules: OrderSizingRules) -> str | None:
    if not order_sizing_rules.market_capital:
        return None
    parts = []
    for code in ("CN", "HK", "US"):
        capital = order_sizing_rules.market_capital.get(code)
        if capital is None:
            continue
        currency = order_sizing_rules.currency_by_market.get(code, code)
        parts.append(f"{UNIVERSE_CONFIGS[code].label} {currency} {capital:,.0f}")
    return " | ".join(parts) if parts else None


def execution_order_sizing_text(item: ExecutionInstruction) -> str | None:
    bits: list[str] = []
    if item.budget_value is not None and item.currency is not None:
        bits.append(f"预算 {item.currency} {item.budget_value:,.0f}")
    if item.estimated_quantity is not None:
        if item.estimated_quantity <= 0:
            bits.append("不足 1 手" if item.estimated_lots == 0 else "不足 1 股")
        else:
            qty_text = f"约 {item.estimated_quantity} 股"
            if item.estimated_lots is not None and item.estimated_lots > 0:
                qty_text += f" ({item.estimated_lots} 手)"
            bits.append(qty_text)
    return " | ".join(bits) if bits else None


def snapshot_session_date(snapshot: TrendSnapshot) -> dt.date:
    if snapshot.regimes:
        return max(item.as_of for item in snapshot.regimes)
    return snapshot.as_of


def cache_path_for_symbol(symbol: str) -> Path:
    safe_symbol = re.sub(r"[^A-Z0-9._-]+", "_", symbol.strip().upper())
    return HISTORY_CACHE_ROOT / f"{safe_symbol}.csv"


def load_cached_history(symbol: str, *, max_age_hours: int = 12) -> list[tuple[dt.date, float]] | None:
    path = cache_path_for_symbol(symbol)
    if not path.exists():
        return None
    age_seconds = (dt.datetime.now() - dt.datetime.fromtimestamp(path.stat().st_mtime)).total_seconds()
    if age_seconds > max_age_hours * 3600:
        return None
    rows: list[tuple[dt.date, float]] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            raw_date = str(raw.get("date") or "").strip()
            raw_close = str(raw.get("close") or "").strip()
            if not raw_date or not raw_close:
                continue
            rows.append((dt.date.fromisoformat(raw_date), float(raw_close)))
    if len(rows) < HISTORY_WINDOW:
        return None
    return rows or None


def write_cached_history(symbol: str, history: list[tuple[dt.date, float]]) -> None:
    path = cache_path_for_symbol(symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("date", "close"))
        writer.writeheader()
        for session_date, close in history:
            writer.writerow({"date": session_date.isoformat(), "close": f"{close:.6f}"})


def fetch_eastmoney_history(symbol: str, *, limit: int = HISTORY_WINDOW) -> list[tuple[dt.date, float]]:
    payload = market_close_digest.eastmoney_get_json(
        market_close_digest.EASTMONEY_KLINE_URL,
        {
            "secid": market_close_digest.resolve_secid(symbol),
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
            "klt": 101,
            "fqt": 1,
            "end": 20500000,
            "lmt": limit,
        },
    )
    klines = (payload.get("data") or {}).get("klines") or []
    history: list[tuple[dt.date, float]] = []
    for item in klines:
        parts = str(item).split(",")
        if len(parts) < 3:
            continue
        session_date = dt.date.fromisoformat(parts[0])
        close = float(parts[2])
        history.append((session_date, close))
    if len(history) < MIN_HISTORY_BARS:
        raise RuntimeError(f"Eastmoney returned only {len(history)} bars for `{symbol}`.")
    return history


def fetch_yahoo_history(symbol: str) -> list[tuple[dt.date, float]]:
    payload = market_close_digest.get_json(
        market_close_digest.YAHOO_CHART_URL.format(
            symbol=market_close_digest.urllib.parse.quote(market_close_digest.yahoo_chart_symbol(symbol), safe="")
        ),
        {
            "range": "3y",
            "interval": "1d",
            "includePrePost": "false",
        },
        source_name="Yahoo history",
        user_agent=market_close_digest.YAHOO_USER_AGENT,
    )
    chart = payload.get("chart") or {}
    error = chart.get("error")
    if error:
        raise RuntimeError(f"Yahoo returned chart error for `{symbol}`: {error}")
    results = chart.get("result") or []
    if not results:
        raise RuntimeError(f"Yahoo returned no chart data for `{symbol}`.")
    result = results[0]
    timestamps = result.get("timestamp") or []
    quote = (((result.get("indicators") or {}).get("quote") or [{}])[0])
    closes = quote.get("close") or []
    meta = result.get("meta") or {}
    timezone_name = str(meta.get("exchangeTimezoneName") or "America/New_York")
    history: list[tuple[dt.date, float]] = []
    for timestamp, close in zip(timestamps, closes):
        if timestamp is None or close is None:
            continue
        session_date = dt.datetime.fromtimestamp(int(timestamp), tz=dt.timezone.utc).astimezone(
            market_close_digest.ZoneInfo(timezone_name)
        ).date()
        history.append((session_date, float(close)))
    if len(history) < MIN_HISTORY_BARS:
        raise RuntimeError(f"Yahoo returned only {len(history)} bars for `{symbol}`.")
    return history


def fetch_price_history(symbol: str) -> list[tuple[dt.date, float]]:
    cached = load_cached_history(symbol)
    if cached is not None:
        return cached
    errors: list[str] = []
    for fetcher in (fetch_eastmoney_history, fetch_yahoo_history):
        try:
            history = fetcher(symbol)
            write_cached_history(symbol, history)
            return history
        except Exception as exc:
            errors.append(f"{getattr(fetcher, '__name__', 'history_fetcher')}: {exc}")
    raise RuntimeError("; ".join(errors))


def latest_points(history: list[tuple[dt.date, float]], as_of: dt.date) -> list[tuple[dt.date, float]]:
    return [item for item in history if item[0] <= as_of]


def price_on_or_before(history: list[tuple[dt.date, float]], as_of: dt.date) -> float | None:
    points = latest_points(history, as_of)
    if not points:
        return None
    return points[-1][1]


def moving_average(closes: list[float], window: int) -> float:
    if len(closes) < window:
        raise RuntimeError(f"Need at least {window} closes, got {len(closes)}.")
    values = closes[-window:]
    return sum(values) / len(values)


def trailing_return(closes: list[float], lookback: int) -> float:
    if len(closes) <= lookback:
        raise RuntimeError(f"Need more than {lookback} closes, got {len(closes)}.")
    base = closes[-(lookback + 1)]
    if base == 0:
        return 0.0
    return (closes[-1] / base - 1.0) * 100.0


def annualized_volatility(closes: list[float], window: int = 20) -> float:
    if len(closes) <= window:
        raise RuntimeError(f"Need more than {window} closes, got {len(closes)}.")
    values = closes[-(window + 1) :]
    returns = [(current / previous - 1.0) for previous, current in zip(values[:-1], values[1:]) if previous > 0]
    if len(returns) < 2:
        return 0.0
    avg = sum(returns) / len(returns)
    variance = sum((item - avg) ** 2 for item in returns) / (len(returns) - 1)
    return math.sqrt(variance) * math.sqrt(252.0) * 100.0


def evaluate_regime(
    market_code: str,
    history: list[tuple[dt.date, float]],
    as_of: dt.date,
) -> RegimeSnapshot | None:
    points = latest_points(history, as_of)
    if len(points) < MIN_HISTORY_BARS:
        return None
    closes = [close for _, close in points]
    close = closes[-1]
    ma20 = moving_average(closes, 20)
    ma60 = moving_average(closes, 60)
    ma120 = moving_average(closes, 120)
    ret60 = trailing_return(closes, 60)
    signals_on = sum([close > ma120, ma20 > ma60, ret60 > 0])
    config = UNIVERSE_CONFIGS[market_code]
    return RegimeSnapshot(
        market_code=market_code,
        market_label=config.label,
        benchmark_symbol=config.benchmark_symbol,
        benchmark_name=config.benchmark_name,
        as_of=points[-1][0],
        close=close,
        ma20=ma20,
        ma60=ma60,
        ma120=ma120,
        ret60=ret60,
        risk_on=signals_on >= 2,
        signals_on=signals_on,
    )


def evaluate_signal(
    row: dict[str, object],
    history: list[tuple[dt.date, float]],
    benchmark_history: list[tuple[dt.date, float]],
    as_of: dt.date,
) -> dict[str, float] | None:
    points = latest_points(history, as_of)
    benchmark_points = latest_points(benchmark_history, as_of)
    if len(points) < MIN_HISTORY_BARS or len(benchmark_points) < MIN_HISTORY_BARS:
        return None
    closes = [close for _, close in points]
    benchmark_closes = [close for _, close in benchmark_points]
    close = closes[-1]
    ma20 = moving_average(closes, 20)
    ma60 = moving_average(closes, 60)
    ma120 = moving_average(closes, 120)
    ret20 = trailing_return(closes, 20)
    ret60 = trailing_return(closes, 60)
    ret120 = trailing_return(closes, 120)
    benchmark_ret60 = trailing_return(benchmark_closes, 60)
    relative_strength_60d = ret60 - benchmark_ret60
    vol20 = annualized_volatility(closes, 20)
    passes = close > ma60 and ma20 > ma60 and ma60 > ma120 and ret20 > 0 and ret60 > 0
    score = ret20 * 0.30 + ret60 * 0.40 + ret120 * 0.15 + relative_strength_60d * 0.20 - vol20 * 0.15
    return {
        "close": close,
        "ret20": ret20,
        "ret60": ret60,
        "ret120": ret120,
        "relative_strength_60d": relative_strength_60d,
        "volatility_20d": vol20,
        "score": score,
        "passes": 1.0 if passes else 0.0,
    }


def load_candidate_rows(universe_path: str | Path, market: str | None) -> list[dict[str, object]]:
    rows = quant_engine.load_universe(universe_path)
    target_codes = set(resolve_market_codes(market))
    selected: list[dict[str, object]] = []
    per_market: dict[str, list[dict[str, object]]] = {code: [] for code in target_codes}
    for row in rows:
        ticker = str(row["ticker"])
        code = infer_market_code(ticker)
        if code not in target_codes:
            continue
        config = UNIVERSE_CONFIGS[code]
        if float(row["market_cap_b"]) < config.min_market_cap_b or float(row["price"]) < config.min_price:
            continue
        per_market[code].append(row)
    for code, market_rows in per_market.items():
        selected.extend(
            sorted(
                market_rows,
                key=lambda item: (-float(item["market_cap_b"]), str(item["ticker"])),
            )[: UNIVERSE_CONFIGS[code].max_candidates]
        )
    return selected


def load_histories(
    symbols: list[str],
    history_fetcher: HistoryFetcher | None = None,
) -> tuple[dict[str, list[tuple[dt.date, float]]], list[str]]:
    fetcher = history_fetcher or fetch_price_history
    histories: dict[str, list[tuple[dt.date, float]]] = {}
    failures: list[str] = []

    def fetch_one(symbol: str) -> tuple[str, list[tuple[dt.date, float]]]:
        return symbol, fetcher(symbol)

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(len(symbols), 1))) as executor:
        future_map = {executor.submit(fetch_one, symbol): symbol for symbol in symbols}
        for future in concurrent.futures.as_completed(future_map):
            symbol = future_map[future]
            try:
                resolved_symbol, history = future.result()
                histories[resolved_symbol] = history
            except Exception as exc:
                failures.append(f"{symbol}: {exc}")
    return histories, failures


def build_regime_map(
    target_codes: tuple[str, ...],
    as_of: dt.date,
    history_fetcher: HistoryFetcher | None = None,
) -> tuple[dict[str, RegimeSnapshot], list[str]]:
    benchmark_symbols = [UNIVERSE_CONFIGS[code].benchmark_symbol for code in target_codes]
    histories, failures = load_histories(benchmark_symbols, history_fetcher=history_fetcher)
    regimes: dict[str, RegimeSnapshot] = {}
    for code in target_codes:
        history = histories.get(UNIVERSE_CONFIGS[code].benchmark_symbol)
        if not history:
            continue
        snapshot = evaluate_regime(code, history, as_of)
        if snapshot is not None:
            regimes[code] = snapshot
    return regimes, failures


def select_picks(
    rows: list[dict[str, object]],
    histories: dict[str, list[tuple[dt.date, float]]],
    regimes: dict[str, RegimeSnapshot],
    as_of: dt.date,
    *,
    top_n: int,
    constraints: PortfolioConstraints | None = None,
) -> tuple[list[TrendPick], int, ConstraintDiagnostics]:
    active_constraints = constraints or load_portfolio_constraints(tuple(regimes))
    ranked: list[TrendPick] = []
    evaluated = 0
    for row in rows:
        ticker = str(row["ticker"])
        code = infer_market_code(ticker)
        regime = regimes.get(code)
        if regime is None or not regime.risk_on:
            continue
        history_symbol = history_symbol_for_ticker(ticker)
        history = histories.get(history_symbol)
        benchmark_history = histories.get(regime.benchmark_symbol)
        if history is None or benchmark_history is None:
            continue
        metrics = evaluate_signal(row, history, benchmark_history, as_of)
        if metrics is None:
            continue
        evaluated += 1
        if not bool(metrics["passes"]):
            continue
        ranked.append(
            TrendPick(
                ticker=ticker,
                history_symbol=history_symbol,
                name=str(row["name"]),
                sector=str(row["sector"]),
                market_label=UNIVERSE_CONFIGS[code].label,
                market_cap_b=float(row["market_cap_b"]),
                score=round(metrics["score"], 2),
                weight=0.0,
                ret20=round(metrics["ret20"], 2),
                ret60=round(metrics["ret60"], 2),
                ret120=round(metrics["ret120"], 2),
                relative_strength_60d=round(metrics["relative_strength_60d"], 2),
                volatility_20d=round(metrics["volatility_20d"], 2),
                close=round(metrics["close"], 2),
            )
        )
    ranked.sort(key=lambda item: (-item.score, -item.relative_strength_60d, item.ticker))
    selected: list[TrendPick] = []
    sector_counts: dict[str, int] = {}
    sector_weights: dict[str, float] = {}
    market_weights = {code: 0.0 for code in active_constraints.market_weight_budget}
    diagnostics = ConstraintDiagnostics()
    remaining_weight = active_constraints.target_gross_exposure
    desired_weight = min(
        active_constraints.max_position_weight,
        active_constraints.target_gross_exposure / max(top_n, 1),
    )
    minimum_weight = min(desired_weight, max(MIN_POSITION_WEIGHT, desired_weight * 0.25))
    for item in ranked:
        if len(selected) >= top_n or remaining_weight <= 0:
            break
        if sector_counts.get(item.sector, 0) >= active_constraints.max_sector_positions:
            diagnostics = dataclasses.replace(
                diagnostics,
                skipped_sector_position_limit=diagnostics.skipped_sector_position_limit + 1,
            )
            continue
        code = infer_market_code(item.ticker)
        sector_room = active_constraints.max_sector_weight - sector_weights.get(item.sector, 0.0)
        market_room = active_constraints.market_weight_budget.get(code, 1.0) - market_weights.get(code, 0.0)
        if sector_room <= 0:
            diagnostics = dataclasses.replace(
                diagnostics,
                skipped_sector_weight_limit=diagnostics.skipped_sector_weight_limit + 1,
            )
            continue
        if market_room <= 0:
            diagnostics = dataclasses.replace(
                diagnostics,
                skipped_market_budget_limit=diagnostics.skipped_market_budget_limit + 1,
            )
            continue
        weight = min(desired_weight, remaining_weight, sector_room, market_room)
        if weight < minimum_weight:
            if sector_room < minimum_weight:
                diagnostics = dataclasses.replace(
                    diagnostics,
                    skipped_sector_weight_limit=diagnostics.skipped_sector_weight_limit + 1,
                )
            elif market_room < minimum_weight:
                diagnostics = dataclasses.replace(
                    diagnostics,
                    skipped_market_budget_limit=diagnostics.skipped_market_budget_limit + 1,
                )
            else:
                diagnostics = dataclasses.replace(
                    diagnostics,
                    skipped_small_remainder=diagnostics.skipped_small_remainder + 1,
                )
            continue
        rounded_weight = round(weight, 4)
        if rounded_weight < desired_weight:
            diagnostics = dataclasses.replace(
                diagnostics,
                partial_weight_positions=diagnostics.partial_weight_positions + 1,
            )
        selected.append(dataclasses.replace(item, weight=rounded_weight))
        sector_counts[item.sector] = sector_counts.get(item.sector, 0) + 1
        sector_weights[item.sector] = sector_weights.get(item.sector, 0.0) + rounded_weight
        market_weights[code] = market_weights.get(code, 0.0) + rounded_weight
        remaining_weight = max(0.0, remaining_weight - rounded_weight)
    if not selected:
        return [], evaluated, diagnostics
    return selected, evaluated, diagnostics


def build_trend_snapshot(
    universe_path: str | Path,
    market: str | None,
    *,
    top_n: int = DEFAULT_TOP_N,
    as_of: dt.date | None = None,
    history_fetcher: HistoryFetcher | None = None,
    constraints: PortfolioConstraints | None = None,
) -> TrendSnapshot:
    rows = load_candidate_rows(universe_path, market)
    if not rows:
        raise RuntimeError("No liquid large-cap candidates passed the initial market filters.")
    target_codes = resolve_market_codes(market)
    active_constraints = constraints or load_portfolio_constraints(target_codes)
    exit_rules = load_exit_rules()
    execution_rules = load_execution_rules()
    order_sizing_rules = load_order_sizing_rules()
    effective_as_of = as_of or dt.date.today()
    benchmark_histories, benchmark_failures = load_histories(
        [UNIVERSE_CONFIGS[code].benchmark_symbol for code in target_codes],
        history_fetcher=history_fetcher,
    )
    regimes: dict[str, RegimeSnapshot] = {}
    for code in target_codes:
        history = benchmark_histories.get(UNIVERSE_CONFIGS[code].benchmark_symbol)
        if history is None:
            continue
        snapshot = evaluate_regime(code, history, effective_as_of)
        if snapshot is not None:
            regimes[code] = snapshot
    history_symbols = [history_symbol_for_ticker(str(row["ticker"])) for row in rows]
    stock_histories, stock_failures = load_histories(history_symbols, history_fetcher=history_fetcher)
    merged_histories = {**benchmark_histories, **stock_histories}
    reference_dates = resolve_reference_dates(benchmark_histories, target_codes, effective_as_of)
    picks, evaluated_count, constraint_diagnostics = select_picks(
        rows,
        merged_histories,
        regimes,
        effective_as_of,
        top_n=top_n,
        constraints=active_constraints,
    )
    previous_date = resolve_previous_rebalance_date(reference_dates, effective_as_of)
    previous_picks: tuple[TrendPick, ...] = tuple()
    if previous_date is not None:
        previous_regimes: dict[str, RegimeSnapshot] = {}
        for code in target_codes:
            history = benchmark_histories.get(UNIVERSE_CONFIGS[code].benchmark_symbol)
            if history is None:
                continue
            snapshot = evaluate_regime(code, history, previous_date)
            if snapshot is not None:
                previous_regimes[code] = snapshot
        previous_selection, _, _ = select_picks(
            rows,
            merged_histories,
            previous_regimes,
            previous_date,
            top_n=top_n,
            constraints=active_constraints,
        )
        previous_picks = tuple(previous_selection)
    trade_plan = build_trade_plan(
        previous_picks,
        tuple(picks),
        regimes,
        merged_histories,
        reference_dates,
        previous_date,
        effective_as_of,
        exit_rules,
    )
    execution_plan = build_execution_plan(
        previous_picks,
        tuple(picks),
        trade_plan,
        merged_histories,
        effective_as_of,
        exit_rules,
        execution_rules,
        active_constraints,
        order_sizing_rules,
    )
    invested_weight = round(sum(item.weight for item in picks), 4)
    return TrendSnapshot(
        market_label=market_label(market),
        as_of=effective_as_of,
        candidate_count=len(rows),
        evaluated_count=evaluated_count,
        history_failures=len(benchmark_failures) + len(stock_failures),
        invested_weight=invested_weight,
        cash_weight=round(max(0.0, 1.0 - invested_weight), 4),
        regimes=tuple(regimes[code] for code in target_codes if code in regimes),
        constraints=active_constraints,
        exit_rules=exit_rules,
        execution_rules=execution_rules,
        order_sizing_rules=order_sizing_rules,
        market_exposures=summarize_market_exposures(picks, active_constraints),
        constraint_diagnostics=constraint_diagnostics,
        previous_rebalance_date=previous_date,
        trade_plan=trade_plan,
        execution_plan=execution_plan,
        picks=tuple(picks),
    )


def resolve_reference_dates(
    benchmark_histories: Mapping[str, list[tuple[dt.date, float]]],
    target_codes: tuple[str, ...],
    as_of: dt.date | None = None,
) -> list[dt.date]:
    dates = sorted(
        {
            session_date
            for code in target_codes
            for session_date, _ in benchmark_histories.get(UNIVERSE_CONFIGS[code].benchmark_symbol, [])
            if as_of is None or session_date <= as_of
        }
    )
    return dates


def resolve_previous_rebalance_date(reference_dates: list[dt.date], as_of: dt.date) -> dt.date | None:
    eligible = [item for item in reference_dates if item <= as_of]
    if len(eligible) <= REBALANCE_DAYS:
        return None
    return eligible[-(REBALANCE_DAYS + 1)]


def reference_price_for_ticker(
    ticker: str,
    histories: Mapping[str, list[tuple[dt.date, float]]],
    as_of: dt.date,
) -> float | None:
    history = histories.get(history_symbol_for_ticker(ticker))
    if history is None:
        return None
    return price_on_or_before(history, as_of)


def stop_price_from_entry(reference_price: float | None, exit_rules: TrendExitRules) -> float | None:
    if reference_price in (None, 0) or exit_rules.stop_loss_pct <= 0:
        return None
    return round(reference_price * (1.0 - exit_rules.stop_loss_pct), 2)


def dynamic_add_on_trigger_pct(pick: TrendPick, execution_rules: TrendExecutionRules) -> float:
    daily_vol = max(pick.volatility_20d, 0.0) / 100.0 / math.sqrt(252.0)
    volatility_buffer = daily_vol * 1.5
    return min(
        max(execution_rules.min_add_on_trigger_pct, volatility_buffer),
        execution_rules.max_add_on_trigger_pct,
    )


def tranche_weights(delta_weight: float, execution_rules: TrendExecutionRules) -> tuple[float, float]:
    starter_weight = round(delta_weight * execution_rules.entry_starter_fraction, 4)
    add_on_weight = round(delta_weight - starter_weight, 4)
    if add_on_weight < MIN_POSITION_WEIGHT:
        starter_weight = round(delta_weight, 4)
        add_on_weight = 0.0
    return starter_weight, add_on_weight


def market_budget_reference_weight(code: str, constraints: PortfolioConstraints) -> float:
    value = constraints.market_weight_budget.get(code)
    if value is not None and value > 0:
        return value
    if len(constraints.market_weight_budget) <= 1:
        return 1.0
    return 0.0


def order_budget_metrics(
    ticker: str,
    delta_weight: float,
    reference_price: float | None,
    constraints: PortfolioConstraints,
    order_sizing_rules: OrderSizingRules,
) -> tuple[float | None, str | None, int | None, int | None]:
    market_code = infer_market_code(ticker)
    capital = order_sizing_rules.market_capital.get(market_code)
    if capital is None or capital <= 0:
        return None, None, None, None
    reference_weight = market_budget_reference_weight(market_code, constraints)
    if reference_weight <= 0:
        return None, None, None, None
    budget_value = capital * delta_weight / reference_weight
    currency = order_sizing_rules.currency_by_market.get(market_code)
    if reference_price in (None, 0):
        return round(budget_value, 2), currency, None, None
    lot_size = order_sizing_rules.lot_size_by_ticker.get(
        ticker.strip().upper(),
        order_sizing_rules.lot_size_by_market.get(market_code, 1),
    )
    raw_quantity = int(budget_value / reference_price)
    if raw_quantity <= 0:
        return round(budget_value, 2), currency, 0, 0 if lot_size > 1 else 0
    rounded_quantity = (raw_quantity // lot_size) * lot_size if lot_size > 1 else raw_quantity
    estimated_lots = 0 if lot_size > 1 and rounded_quantity <= 0 else None
    if lot_size > 1 and rounded_quantity > 0:
        estimated_lots = rounded_quantity // lot_size
    return round(budget_value, 2), currency, rounded_quantity, estimated_lots


def detect_exit_reason(
    history: list[tuple[dt.date, float]],
    start_price: float,
    peak_price: float,
    session_date: dt.date,
    exit_rules: TrendExitRules,
) -> str | None:
    price = price_on_or_before(history, session_date)
    if price in (None, 0):
        return None
    if exit_rules.stop_loss_pct > 0 and price <= start_price * (1.0 - exit_rules.stop_loss_pct):
        return "止损"
    if (
        exit_rules.trailing_stop_pct > 0
        and peak_price > start_price
        and price <= peak_price * (1.0 - exit_rules.trailing_stop_pct)
    ):
        return "移动止盈"
    if exit_rules.trend_break_window <= 0:
        return None
    points = latest_points(history, session_date)
    if len(points) <= exit_rules.trend_break_window:
        return None
    closes = [close for _, close in points]
    trend_ma = moving_average(closes, exit_rules.trend_break_window)
    short_lookback = min(20, max(5, exit_rules.trend_break_window))
    short_return = trailing_return(closes, short_lookback) if len(closes) > short_lookback else 0.0
    if price < trend_ma and short_return < 0:
        return f"跌破MA{exit_rules.trend_break_window}"
    return None


def simulate_holding_period(
    holding: TrendPick,
    history: list[tuple[dt.date, float]],
    start_date: dt.date,
    end_date: dt.date,
    reference_dates: list[dt.date],
    exit_rules: TrendExitRules,
) -> tuple[PeriodContribution | None, ExitEvent | None, tuple[PeriodValuePoint, ...], tuple[PeriodValuePoint, ...]]:
    start_price = price_on_or_before(history, start_date)
    if start_price in (None, 0):
        return None, None, tuple(), tuple()
    relevant_dates = [item for item in reference_dates if start_date < item <= end_date]
    if not relevant_dates:
        relevant_dates = [end_date]
    peak_price = float(start_price)
    locked_multiplier = 1.0
    exit_event: ExitEvent | None = None
    active = True
    daily_values: list[PeriodValuePoint] = []
    invested_values: list[PeriodValuePoint] = []
    for session_date in relevant_dates:
        if active:
            price = price_on_or_before(history, session_date)
            if price is None:
                multiplier = locked_multiplier
            else:
                peak_price = max(peak_price, price)
                multiplier = price / start_price
            reason = detect_exit_reason(history, float(start_price), peak_price, session_date, exit_rules)
            if reason is not None and exit_event is None:
                exit_event = ExitEvent(
                    ticker=holding.ticker,
                    name=holding.name,
                    session_date=session_date,
                    reason=reason,
                    return_pct=multiplier - 1.0,
                )
                active = False
                locked_multiplier = multiplier
            else:
                locked_multiplier = multiplier
            invested_values.append(
                PeriodValuePoint(session_date=session_date, gross_value=round(holding.weight * multiplier, 6))
            )
        else:
            multiplier = locked_multiplier
            invested_values.append(PeriodValuePoint(session_date=session_date, gross_value=0.0))
        daily_values.append(
            PeriodValuePoint(session_date=session_date, gross_value=round(holding.weight * multiplier, 6))
        )
    final_multiplier = locked_multiplier
    contribution = PeriodContribution(
        ticker=holding.ticker,
        name=holding.name,
        weight=holding.weight,
        asset_return=final_multiplier - 1.0,
        contribution=holding.weight * (final_multiplier - 1.0),
    )
    return contribution, exit_event, tuple(daily_values), tuple(invested_values)


def simulate_period_portfolio(
    holdings: tuple[TrendPick, ...],
    histories: dict[str, list[tuple[dt.date, float]]],
    start_date: dt.date,
    end_date: dt.date,
    reference_dates: list[dt.date],
    exit_rules: TrendExitRules,
) -> tuple[float, tuple[PeriodContribution, ...], tuple[ExitEvent, ...], tuple[PeriodValuePoint, ...], float]:
    relevant_dates = [item for item in reference_dates if start_date < item <= end_date]
    if not relevant_dates:
        relevant_dates = [end_date]
    cash_weight = max(0.0, 1.0 - sum(item.weight for item in holdings))
    daily_totals = {item: cash_weight for item in relevant_dates}
    daily_invested = {item: 0.0 for item in relevant_dates}
    contributions: list[PeriodContribution] = []
    exit_events: list[ExitEvent] = []
    for holding in holdings:
        history = histories.get(holding.history_symbol)
        if history is None:
            continue
        contribution, exit_event, daily_values, invested_values = simulate_holding_period(
            holding,
            history,
            start_date,
            end_date,
            reference_dates,
            exit_rules,
        )
        if contribution is None:
            continue
        contributions.append(contribution)
        if exit_event is not None:
            exit_events.append(exit_event)
        for point in daily_values:
            daily_totals[point.session_date] = daily_totals.get(point.session_date, cash_weight) + point.gross_value
        for point in invested_values:
            daily_invested[point.session_date] = daily_invested.get(point.session_date, 0.0) + point.gross_value
    daily_path = tuple(
        PeriodValuePoint(session_date=item, gross_value=round(daily_totals.get(item, 1.0), 6))
        for item in relevant_dates
    )
    ending_value = daily_path[-1].gross_value if daily_path else 1.0
    invested_ratios = [
        (daily_invested[item] / daily_totals[item]) if daily_totals[item] > 0 else 0.0
        for item in relevant_dates
    ]
    average_invested_weight = sum(invested_ratios) / len(invested_ratios) if invested_ratios else 0.0
    return (
        ending_value - 1.0,
        tuple(sorted(contributions, key=lambda item: item.contribution, reverse=True)),
        tuple(sorted(exit_events, key=lambda item: (item.session_date, item.ticker))),
        daily_path,
        average_invested_weight,
    )


def summarize_exit_reasons(periods: list[BacktestPeriod]) -> tuple[ExitReasonCount, ...]:
    counts: dict[str, int] = {}
    for period in periods:
        for event in period.exit_events:
            counts[event.reason] = counts.get(event.reason, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return tuple(ExitReasonCount(reason=reason, count=count) for reason, count in ordered)


def build_trade_plan(
    previous: tuple[TrendPick, ...],
    current: tuple[TrendPick, ...],
    current_regimes: Mapping[str, RegimeSnapshot],
    histories: dict[str, list[tuple[dt.date, float]]],
    reference_dates: list[dt.date],
    previous_rebalance_date: dt.date | None,
    as_of: dt.date,
    exit_rules: TrendExitRules,
) -> tuple[TradeInstruction, ...]:
    if previous_rebalance_date is None:
        return tuple()
    previous_map = {item.ticker: item for item in previous}
    current_map = {item.ticker: item for item in current}
    exit_reason_by_ticker: dict[str, str] = {}
    for item in previous:
        history = histories.get(item.history_symbol)
        if history is None:
            continue
        _, exit_event, _, _ = simulate_holding_period(
            item,
            history,
            previous_rebalance_date,
            as_of,
            reference_dates,
            exit_rules,
        )
        if exit_event is not None:
            exit_reason_by_ticker[item.ticker] = exit_event.reason
    plan: list[TradeInstruction] = []
    for ticker, previous_item in previous_map.items():
        current_item = current_map.get(ticker)
        if current_item is None:
            regime = current_regimes.get(infer_market_code(ticker))
            reason = "市场 Risk OFF" if regime is not None and not regime.risk_on else exit_reason_by_ticker.get(ticker, "强度调出")
            plan.append(
                TradeInstruction(
                    action="卖出",
                    ticker=previous_item.ticker,
                    name=previous_item.name,
                    from_weight=previous_item.weight,
                    to_weight=0.0,
                    reason=reason,
                )
            )
            continue
        if abs(current_item.weight - previous_item.weight) < 0.0005:
            continue
        plan.append(
            TradeInstruction(
                action="加仓" if current_item.weight > previous_item.weight else "减仓",
                ticker=current_item.ticker,
                name=current_item.name,
                from_weight=previous_item.weight,
                to_weight=current_item.weight,
                reason="仓位再平衡",
            )
        )
    for ticker, current_item in current_map.items():
        if ticker in previous_map:
            continue
        plan.append(
            TradeInstruction(
                action="买入",
                ticker=current_item.ticker,
                name=current_item.name,
                from_weight=0.0,
                to_weight=current_item.weight,
                reason="新开仓",
            )
        )
    ordered = sorted(
        plan,
        key=lambda item: (trade_action_rank(item.action), -(abs(item.to_weight - item.from_weight)), item.ticker),
    )
    return tuple(ordered)


def build_execution_plan(
    previous: tuple[TrendPick, ...],
    current: tuple[TrendPick, ...],
    trade_plan: tuple[TradeInstruction, ...],
    histories: Mapping[str, list[tuple[dt.date, float]]],
    as_of: dt.date,
    exit_rules: TrendExitRules,
    execution_rules: TrendExecutionRules,
    constraints: PortfolioConstraints,
    order_sizing_rules: OrderSizingRules,
) -> tuple[ExecutionInstruction, ...]:
    previous_map = {item.ticker: item for item in previous}
    current_map = {item.ticker: item for item in current}
    instructions: list[ExecutionInstruction] = []
    for item in trade_plan:
        current_item = current_map.get(item.ticker)
        previous_item = previous_map.get(item.ticker)
        reference_price = reference_price_for_ticker(item.ticker, histories, as_of)
        if reference_price in (None, 0):
            reference_price = (current_item or previous_item).close if (current_item or previous_item) is not None else None
        stop_price = stop_price_from_entry(reference_price, exit_rules)
        delta_weight = abs(item.to_weight - item.from_weight)
        if delta_weight <= 0:
            continue
        budget_value, currency, estimated_quantity, estimated_lots = order_budget_metrics(
            item.ticker,
            delta_weight,
            reference_price,
            constraints,
            order_sizing_rules,
        )
        if item.action in {"卖出", "减仓"}:
            instructions.append(
                ExecutionInstruction(
                    action="立即卖出" if item.action == "卖出" else "立即减仓",
                    ticker=item.ticker,
                    name=item.name,
                    from_weight=item.from_weight,
                    to_weight=item.to_weight,
                    trigger_price=reference_price,
                    stop_price=None,
                    risk_budget_pct=0.0,
                    budget_value=budget_value,
                    currency=currency,
                    estimated_quantity=estimated_quantity,
                    estimated_lots=estimated_lots,
                    note="风控指令优先，次日开盘处理" if "止损" in item.reason or "Risk OFF" in item.reason else item.reason,
                )
            )
            continue
        if current_item is None:
            continue
        starter_weight, add_on_weight = tranche_weights(delta_weight, execution_rules)
        starter_to_weight = round(item.from_weight + starter_weight, 4)
        starter_risk = starter_weight * exit_rules.stop_loss_pct if stop_price is not None else 0.0
        starter_budget_value, starter_currency, starter_quantity, starter_lots = order_budget_metrics(
            item.ticker,
            starter_weight,
            reference_price,
            constraints,
            order_sizing_rules,
        )
        instructions.append(
            ExecutionInstruction(
                action="首仓买入" if item.action == "买入" else "首仓加仓",
                ticker=item.ticker,
                name=item.name,
                from_weight=item.from_weight,
                to_weight=starter_to_weight,
                trigger_price=reference_price,
                stop_price=stop_price,
                risk_budget_pct=starter_risk,
                budget_value=starter_budget_value,
                currency=starter_currency,
                estimated_quantity=starter_quantity,
                estimated_lots=starter_lots,
                note="先打底仓，确认趋势延续后再补齐" if add_on_weight > 0 else "目标仓位一次到位",
            )
        )
        if add_on_weight <= 0:
            continue
        trigger_pct = dynamic_add_on_trigger_pct(current_item, execution_rules)
        add_on_trigger = round(current_item.close * (1.0 + trigger_pct), 2)
        add_on_risk = add_on_weight * exit_rules.stop_loss_pct if stop_price is not None else 0.0
        add_on_budget_value, add_on_currency, add_on_quantity, add_on_lots = order_budget_metrics(
            item.ticker,
            add_on_weight,
            add_on_trigger,
            constraints,
            order_sizing_rules,
        )
        instructions.append(
            ExecutionInstruction(
                action="突破加仓",
                ticker=item.ticker,
                name=item.name,
                from_weight=starter_to_weight,
                to_weight=item.to_weight,
                trigger_price=add_on_trigger,
                stop_price=stop_price,
                risk_budget_pct=add_on_risk,
                budget_value=add_on_budget_value,
                currency=add_on_currency,
                estimated_quantity=add_on_quantity,
                estimated_lots=add_on_lots,
                note=f"若放量站上触发价，再补足剩余 {add_on_weight * 100:.0f}% 仓位",
            )
        )
    ordered = sorted(
        instructions,
        key=lambda item: (
            execution_action_rank(item.action),
            -(abs(item.to_weight - item.from_weight)),
            item.ticker,
        ),
    )
    return tuple(ordered)


def period_return(
    picks: tuple[TrendPick, ...],
    histories: dict[str, list[tuple[dt.date, float]]],
    start_date: dt.date,
    end_date: dt.date,
) -> float:
    return sum(
        item.contribution
        for item in period_contributions(
            picks,
            histories,
            start_date,
            end_date,
        )
    )


def period_contributions(
    picks: tuple[TrendPick, ...],
    histories: dict[str, list[tuple[dt.date, float]]],
    start_date: dt.date,
    end_date: dt.date,
) -> tuple[PeriodContribution, ...]:
    contributions: list[PeriodContribution] = []
    for item in picks:
        history = histories.get(item.history_symbol)
        if history is None:
            continue
        start_price = price_on_or_before(history, start_date)
        end_price = price_on_or_before(history, end_date)
        if start_price in (None, 0) or end_price is None:
            continue
        asset_return = end_price / start_price - 1.0
        contributions.append(
            PeriodContribution(
                ticker=item.ticker,
                name=item.name,
                weight=item.weight,
                asset_return=asset_return,
                contribution=item.weight * asset_return,
            )
        )
    return tuple(sorted(contributions, key=lambda item: item.contribution, reverse=True))


def turnover_ratio(previous: tuple[TrendPick, ...], current: tuple[TrendPick, ...]) -> float:
    previous_weights = {item.ticker: item.weight for item in previous}
    current_weights = {item.ticker: item.weight for item in current}
    tickers = set(previous_weights) | set(current_weights)
    return sum(abs(current_weights.get(ticker, 0.0) - previous_weights.get(ticker, 0.0)) for ticker in tickers) / 2.0


def turnover_breakdown(previous: tuple[TrendPick, ...], current: tuple[TrendPick, ...]) -> tuple[float, float]:
    previous_weights = {item.ticker: item.weight for item in previous}
    current_weights = {item.ticker: item.weight for item in current}
    tickers = set(previous_weights) | set(current_weights)
    buy_turnover = sum(max(current_weights.get(ticker, 0.0) - previous_weights.get(ticker, 0.0), 0.0) for ticker in tickers)
    sell_turnover = sum(max(previous_weights.get(ticker, 0.0) - current_weights.get(ticker, 0.0), 0.0) for ticker in tickers)
    return buy_turnover, sell_turnover


def transaction_cost_drag(
    previous: tuple[TrendPick, ...],
    current: tuple[TrendPick, ...],
    cost_model: BacktestCostModel,
) -> tuple[float, float, float]:
    previous_weights = {item.ticker: item.weight for item in previous}
    current_weights = {item.ticker: item.weight for item in current}
    tickers = set(previous_weights) | set(current_weights)
    buy_turnover = 0.0
    sell_turnover = 0.0
    buy_cost_bps = cost_model.commission_bps + cost_model.slippage_bps
    cost_drag = 0.0
    for ticker in tickers:
        delta = current_weights.get(ticker, 0.0) - previous_weights.get(ticker, 0.0)
        if delta > 0:
            buy_turnover += delta
            cost_drag += delta * buy_cost_bps / 10000.0
            continue
        if delta >= 0:
            continue
        sell_weight = -delta
        sell_turnover += sell_weight
        sell_tax_bps = cost_model.sell_tax_bps.get(infer_market_code(ticker), 0.0)
        cost_drag += sell_weight * (buy_cost_bps + sell_tax_bps) / 10000.0
    return buy_turnover, sell_turnover, cost_drag


def build_daily_equity_curve(
    periods: list[BacktestPeriod],
) -> tuple[DailyEquityPoint, ...]:
    curve: list[DailyEquityPoint] = []
    gross_nav = 1.0
    net_nav = 1.0
    peak = 1.0
    for period in periods:
        gross_start = gross_nav
        net_start = net_nav * (1.0 - period.cost_drag)
        if not period.daily_path:
            period_path = (PeriodValuePoint(session_date=period.end_date, gross_value=1.0 + period.gross_return),)
        else:
            period_path = period.daily_path
        for point in period_path:
            gross_value = gross_start * point.gross_value
            net_value = net_start * point.gross_value
            peak = max(peak, net_value)
            curve.append(
                DailyEquityPoint(
                    session_date=point.session_date,
                    gross_value=round(gross_value, 6),
                    net_value=round(net_value, 6),
                    drawdown=round(net_value / peak - 1.0, 6),
                )
            )
        gross_nav = gross_start * (1.0 + period.gross_return)
        net_nav = net_start * (1.0 + period.gross_return)
    return tuple(curve)


def summarize_daily_returns(curve: tuple[DailyEquityPoint, ...]) -> tuple[DailyReturnSummary | None, DailyReturnSummary | None]:
    if len(curve) < 2:
        return None, None
    daily_returns: list[DailyReturnSummary] = []
    previous = curve[0]
    for point in curve[1:]:
        if previous.net_value <= 0:
            previous = point
            continue
        daily_returns.append(
            DailyReturnSummary(
                session_date=point.session_date,
                return_pct=point.net_value / previous.net_value - 1.0,
            )
        )
        previous = point
    if not daily_returns:
        return None, None
    best = max(daily_returns, key=lambda item: item.return_pct)
    worst = min(daily_returns, key=lambda item: item.return_pct)
    return best, worst


def max_drawdown(equity_curve: list[float]) -> float:
    peak = 1.0
    worst = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        if peak <= 0:
            continue
        drawdown = value / peak - 1.0
        worst = min(worst, drawdown)
    return worst


def backtest_trend_strategy(
    universe_path: str | Path,
    market: str | None,
    *,
    lookback_months: int = 12,
    top_n: int = DEFAULT_TOP_N,
    history_fetcher: HistoryFetcher | None = None,
) -> BacktestReport:
    rows = load_candidate_rows(universe_path, market)
    if not rows:
        raise RuntimeError("No liquid large-cap candidates passed the initial market filters.")
    target_codes = resolve_market_codes(market)
    constraints = load_portfolio_constraints(target_codes)
    exit_rules = load_exit_rules()
    benchmark_symbols = [UNIVERSE_CONFIGS[code].benchmark_symbol for code in target_codes]
    stock_symbols = [history_symbol_for_ticker(str(row["ticker"])) for row in rows]
    histories, failures = load_histories(benchmark_symbols + stock_symbols, history_fetcher=history_fetcher)
    for symbol in benchmark_symbols:
        if symbol not in histories:
            raise RuntimeError(f"Missing benchmark history for `{symbol}`.")
    benchmark_histories = {symbol: histories[symbol] for symbol in benchmark_symbols}
    reference_dates = resolve_reference_dates(benchmark_histories, target_codes)
    required_periods = max(4, lookback_months)
    required_points = required_periods * TRADING_DAYS_PER_MONTH + MIN_HISTORY_BARS
    if len(reference_dates) < required_points:
        raise RuntimeError("Not enough benchmark history to run the requested backtest window.")
    test_dates = reference_dates[-required_points:]
    rebalance_dates = test_dates[MIN_HISTORY_BARS - 1 :: REBALANCE_DAYS]
    if len(rebalance_dates) < 2:
        raise RuntimeError("Not enough rebalance dates after the warm-up window.")
    periods: list[BacktestPeriod] = []
    previous_holdings: tuple[TrendPick, ...] = tuple()
    gross_equity_curve = [1.0]
    net_equity_curve = [1.0]
    cost_model = load_backtest_cost_model()
    for start_date, end_date in zip(rebalance_dates[:-1], rebalance_dates[1:]):
        regimes: dict[str, RegimeSnapshot] = {}
        for code in target_codes:
            regime = evaluate_regime(code, histories[UNIVERSE_CONFIGS[code].benchmark_symbol], start_date)
            if regime is not None:
                regimes[code] = regime
        holdings, evaluated_count, _ = select_picks(
            rows,
            histories,
            regimes,
            start_date,
            top_n=top_n,
            constraints=constraints,
        )
        current_holdings = tuple(holdings)
        gross_return, contributions, exit_events, daily_path, average_invested_weight = simulate_period_portfolio(
            current_holdings,
            histories,
            start_date,
            end_date,
            reference_dates,
            exit_rules,
        )
        buy_turnover, sell_turnover, cost_drag = transaction_cost_drag(previous_holdings, current_holdings, cost_model)
        realized_return = gross_return - cost_drag
        gross_equity_curve.append(gross_equity_curve[-1] * (1.0 + gross_return))
        net_equity_curve.append(net_equity_curve[-1] * (1.0 + realized_return))
        periods.append(
            BacktestPeriod(
                start_date=start_date,
                end_date=end_date,
                gross_return=gross_return,
                cost_drag=cost_drag,
                portfolio_return=realized_return,
                invested_weight=round(average_invested_weight, 4),
                turnover=round(turnover_ratio(previous_holdings, current_holdings), 4),
                buy_turnover=round(buy_turnover, 4),
                sell_turnover=round(sell_turnover, 4),
                holdings=current_holdings,
                risk_on_markets=tuple(
                    regimes[code].market_label for code in target_codes if code in regimes and regimes[code].risk_on
                ),
                contributions=contributions,
                exit_events=exit_events,
                daily_path=daily_path,
            )
        )
        previous_holdings = current_holdings
    latest_snapshot = build_trend_snapshot(
        universe_path,
        market,
        top_n=top_n,
        as_of=rebalance_dates[-1],
        history_fetcher=history_fetcher,
        constraints=constraints,
    )
    gross_total_return = gross_equity_curve[-1] - 1.0
    total_return = net_equity_curve[-1] - 1.0
    calendar_days = max((periods[-1].end_date - periods[0].start_date).days, 1)
    annualized_return = (net_equity_curve[-1] ** (365.0 / calendar_days) - 1.0) if net_equity_curve[-1] > 0 else -1.0
    positive_periods = sum(1 for item in periods if item.portfolio_return > 0)
    average_invested_weight = sum(item.invested_weight for item in periods) / len(periods)
    average_cost_drag = sum(item.cost_drag for item in periods) / len(periods)
    average_period_return = sum(item.portfolio_return for item in periods) / len(periods)
    average_turnover = sum(item.turnover for item in periods) / len(periods)
    daily_curve = build_daily_equity_curve(periods)
    best_day, worst_day = summarize_daily_returns(daily_curve)
    return BacktestReport(
        market_label=market_label(market),
        start_date=periods[0].start_date,
        end_date=periods[-1].end_date,
        candidate_count=len(rows),
        evaluated_count=latest_snapshot.evaluated_count,
        history_failures=len(failures),
        gross_total_return=gross_total_return,
        total_cost_drag=gross_total_return - total_return,
        total_return=total_return,
        annualized_return=annualized_return,
        max_drawdown=max_drawdown(net_equity_curve),
        win_rate=positive_periods / len(periods),
        average_invested_weight=average_invested_weight,
        average_cost_drag=average_cost_drag,
        average_period_return=average_period_return,
        average_turnover=average_turnover,
        cost_model=cost_model,
        current_sector_exposures=summarize_sector_exposures(latest_snapshot.picks),
        daily_curve=daily_curve,
        best_day=best_day,
        worst_day=worst_day,
        exit_reason_counts=summarize_exit_reasons(periods),
        periods=tuple(periods),
        latest_snapshot=latest_snapshot,
    )


def format_regime_line(snapshot: RegimeSnapshot) -> str:
    status = "Risk ON" if snapshot.risk_on else "Risk OFF"
    return (
        f"- {snapshot.market_label}: {status} | {snapshot.benchmark_name} {snapshot.close:.2f} "
        f"| MA20 {snapshot.ma20:.2f} | MA60 {snapshot.ma60:.2f} | MA120 {snapshot.ma120:.2f} "
        f"| 60D {snapshot.ret60:+.1f}%"
    )


def format_trend_snapshot(
    universe_path: str | Path,
    market: str | None,
    *,
    top_n: int = DEFAULT_TOP_N,
    history_fetcher: HistoryFetcher | None = None,
) -> str:
    snapshot = build_trend_snapshot(universe_path, market, top_n=top_n, history_fetcher=history_fetcher)
    lines = [
        "Quant WeChat Bot",
        "",
        "策略: 趋势选股 + 市场过滤 + 仓位管理",
        f"市场: {snapshot.market_label}",
        f"日期: {snapshot.as_of.isoformat()}",
        f"候选池: {snapshot.candidate_count} 只大市值股票，已评估 {snapshot.evaluated_count} 只。",
        "",
        "市场风控",
    ]
    if snapshot.regimes:
        for regime in snapshot.regimes:
            lines.append(format_regime_line(regime))
    else:
        lines.append("- 没有拿到足够的基准历史数据，无法判断市场开关。")
    lines.extend(
        [
            "",
            "仓位建议",
            f"- 目标持仓: {snapshot.constraints.target_gross_exposure * 100:.0f}%",
            f"- 实际持仓: {snapshot.invested_weight * 100:.0f}%",
            f"- 现金缓冲: {snapshot.cash_weight * 100:.0f}%",
            f"- 单票上限: {snapshot.constraints.max_position_weight * 100:.0f}%",
            f"- 单行业上限: {snapshot.constraints.max_sector_weight * 100:.0f}% / 最多 {snapshot.constraints.max_sector_positions} 只",
            f"- 退出规则: {exit_rule_summary(snapshot.exit_rules)}",
            f"- 执行规则: {execution_rule_summary(snapshot.execution_rules)}",
        ]
    )
    sizing_summary = order_sizing_summary(snapshot.order_sizing_rules)
    if sizing_summary:
        lines.append(f"- 资金换算: {sizing_summary}")
    if snapshot.history_failures:
        lines.append(f"- 历史数据抓取失败 {snapshot.history_failures} 个标的，已自动跳过")
    if snapshot.market_exposures:
        lines.extend(["", "市场预算"])
        for item in snapshot.market_exposures:
            lines.append(
                f"- {item.market_label}: 目标 {item.target_weight * 100:.0f}% | 实际 {item.actual_weight * 100:.0f}% ({item.count} 只)"
            )
    diagnostics = snapshot.constraint_diagnostics
    if any(
        (
            diagnostics.skipped_sector_position_limit,
            diagnostics.skipped_sector_weight_limit,
            diagnostics.skipped_market_budget_limit,
            diagnostics.skipped_small_remainder,
            diagnostics.partial_weight_positions,
        )
    ):
        lines.extend(
            [
                "",
                "约束执行",
                f"- 行业名额跳过 {diagnostics.skipped_sector_position_limit} 只 | 行业权重跳过 {diagnostics.skipped_sector_weight_limit} 只 | 市场预算跳过 {diagnostics.skipped_market_budget_limit} 只",
                f"- 小仓位舍弃 {diagnostics.skipped_small_remainder} 只 | 部分仓位入选 {diagnostics.partial_weight_positions} 只",
            ]
        )
    if not snapshot.picks:
        lines.extend(
            [
                "",
                "当前没有入选标的。",
                "通常表示市场过滤是 Risk OFF，或者候选池里没有足够强的趋势结构，此时保持现金更合理。",
                "",
                "说明: 当前回测和选股仍存在生存者偏差，因为候选池来自现有股票快照。",
            ]
        )
        return "\n".join(lines)
    lines.extend(["", "当前入选"])
    for index, item in enumerate(snapshot.picks, start=1):
        lines.append(
            f"{index}. {item.ticker} | {item.name} | 权重 {item.weight * 100:.0f}% | 分数 {item.score:.1f}"
        )
        lines.append(
            f"   {item.market_label} | {item.sector} | 价格 {item.close:.2f} | 市值 {item.market_cap_b:.0f}B"
        )
        stop_price = stop_price_from_entry(item.close, snapshot.exit_rules)
        risk_budget_pct = item.weight * snapshot.exit_rules.stop_loss_pct
        lines.append(
            f"   20D {item.ret20:+.1f}% | 60D {item.ret60:+.1f}% | 120D {item.ret120:+.1f}% | 相对基准 {item.relative_strength_60d:+.1f}%"
        )
        if stop_price is not None:
            lines.append(f"   止损线 {stop_price:.2f} | 单票最大亏损拖累约 {risk_budget_pct * 100:.1f}%")
    exposures = summarize_sector_exposures(snapshot.picks)
    if exposures:
        lines.extend(["", "当前行业暴露"])
        for item in exposures:
            lines.append(f"- {item.sector}: {item.weight * 100:.0f}% ({item.count} 只)")
    if snapshot.previous_rebalance_date is not None:
        lines.extend(["", f"模型调仓清单（对比 {snapshot.previous_rebalance_date.isoformat()}）"])
        if snapshot.trade_plan:
            for item in snapshot.trade_plan[:6]:
                lines.append(format_trade_plan_item(item))
        else:
            lines.append("- 与上一调仓日相比无变化")
    if snapshot.execution_plan:
        lines.extend(["", "次日执行清单"])
        for item in snapshot.execution_plan[:8]:
            lines.append(format_execution_plan_item(item))
    lines.extend(
        [
            "",
            "说明:",
            "- 市场过滤: 基准站上 MA120，且 MA20 > MA60 或 60D 趋势为正时才开仓。",
            "- 选股规则: 价格站上 MA60，MA20 > MA60 > MA120，且 20D/60D 动量均为正。",
            "- 退出规则在回测中按日生效，当前调仓清单按上一调仓日模型仓位推导。",
            "- 执行清单会把买入/加仓拆成底仓和突破加仓两步，方便更接近实盘执行。",
        ]
    )
    return "\n".join(lines)


def format_trading_plan_from_snapshot(snapshot: TrendSnapshot) -> str:
    immediate_actions = [item for item in snapshot.execution_plan if item.action.startswith("立即")]
    staged_entries = [item for item in snapshot.execution_plan if not item.action.startswith("立即")]
    lines = [
        "Quant WeChat Bot",
        "",
        "策略: 次日交易计划",
        f"市场: {snapshot.market_label}",
        f"日期: {snapshot.as_of.isoformat()}",
        f"市场风控: {' / '.join(f'{item.market_label} ' + ('Risk ON' if item.risk_on else 'Risk OFF') for item in snapshot.regimes) if snapshot.regimes else '未知'}",
        f"目标持仓: {snapshot.constraints.target_gross_exposure * 100:.0f}% | 实际持仓: {snapshot.invested_weight * 100:.0f}% | 现金 {snapshot.cash_weight * 100:.0f}%",
        f"退出规则: {exit_rule_summary(snapshot.exit_rules)}",
        f"执行规则: {execution_rule_summary(snapshot.execution_rules)}",
    ]
    sizing_summary = order_sizing_summary(snapshot.order_sizing_rules)
    if sizing_summary:
        lines.append(f"资金换算: {sizing_summary}")
    if snapshot.previous_rebalance_date is not None:
        lines.append(f"模型基准: 对比 {snapshot.previous_rebalance_date.isoformat()} 调仓日")
    if immediate_actions:
        lines.extend(["", "开盘先做"])
        for item in immediate_actions:
            lines.append(format_execution_plan_item(item))
    if staged_entries:
        lines.extend(["", "盘中条件单"])
        for item in staged_entries:
            lines.append(format_execution_plan_item(item))
    if snapshot.picks:
        lines.extend(["", "模型目标仓"])
        for item in snapshot.picks:
            stop_price = stop_price_from_entry(item.close, snapshot.exit_rules)
            stop_text = f" | 止损 {stop_price:.2f}" if stop_price is not None else ""
            lines.append(
                f"- {item.ticker} {item.name} | 目标权重 {item.weight * 100:.0f}% | 现价 {item.close:.2f}{stop_text}"
            )
    if not snapshot.order_sizing_rules.market_capital:
        lines.extend(
            [
                "",
                "资金换算提示:",
                "- 还没配置 `trend_order_sizing.market_capital`，所以暂不显示预算金额和估算股数。",
            ]
        )
    if not snapshot.execution_plan:
        lines.extend(
            [
                "",
                "当前无新增执行动作。",
                "如果你已经按模型持仓运行，今天更像是持仓观察日。",
            ]
        )
    lines.extend(
        [
            "",
            "执行提醒:",
            "- 先处理风控卖单，再处理新开仓和加仓。",
            "- 突破加仓只在价格有效站上触发价时执行，没触发就保留底仓。",
            "- 单票风险预算按目标权重和止损线估算，不含隔夜跳空。",
        ]
    )
    return "\n".join(lines)


def format_recommendation_digest_from_snapshot(snapshot: TrendSnapshot) -> str:
    session_date = snapshot_session_date(snapshot)
    risk_text = (
        " / ".join(
            f"{item.market_label} " + ("Risk ON" if item.risk_on else "Risk OFF")
            for item in snapshot.regimes
        )
        if snapshot.regimes
        else "未知"
    )
    entry_actions = [
        item
        for item in snapshot.execution_plan
        if item.action in {"首仓买入", "首仓加仓"}
    ]
    breakout_actions = {
        item.ticker: item
        for item in snapshot.execution_plan
        if item.action == "突破加仓"
    }
    exit_actions = [item for item in snapshot.execution_plan if item.action.startswith("立即")]
    picks_by_ticker = {item.ticker: item for item in snapshot.picks}
    lines = [
        f"【{snapshot.market_label}推荐日报｜{session_date.isoformat()}】",
        f"市场风控: {risk_text}",
        f"建议仓位: 持仓 {snapshot.invested_weight * 100:.0f}% | 现金 {snapshot.cash_weight * 100:.0f}%",
    ]
    if len(snapshot.market_exposures) > 1:
        allocations = " | ".join(
            f"{item.market_label} {item.actual_weight * 100:.0f}%"
            for item in snapshot.market_exposures
            if item.actual_weight > 0
        )
        if allocations:
            lines.append(f"市场分配: {allocations}")
    if entry_actions:
        lines.extend(["", "今日优先关注"])
        for index, item in enumerate(entry_actions[:3], start=1):
            pick = picks_by_ticker.get(item.ticker)
            breakout = breakout_actions.get(item.ticker)
            target_weight = pick.weight if pick is not None else (breakout.to_weight if breakout is not None else item.to_weight)
            starter_weight = max(0.0, item.to_weight - item.from_weight)
            header = f"{index}. {item.ticker} {item.name} | 目标 {target_weight * 100:.0f}%"
            if pick is not None:
                header += f" | 分数 {pick.score:.1f}"
            lines.append(header)
            market_bits = []
            if pick is not None:
                market_bits.append(f"{pick.market_label}/{pick.sector}")
                market_bits.append(f"现价 {pick.close:.2f}")
                market_bits.append(f"20D {pick.ret20:+.1f}%")
                market_bits.append(f"60D {pick.ret60:+.1f}%")
            elif item.trigger_price is not None:
                market_bits.append(f"参考价 {item.trigger_price:.2f}")
            if item.stop_price is not None:
                market_bits.append(f"止损 {item.stop_price:.2f}")
            if market_bits:
                lines.append("   " + " | ".join(market_bits))
            action_bits = [f"首仓 {starter_weight * 100:.0f}%"]
            if breakout is not None and breakout.trigger_price is not None:
                add_on_weight = max(0.0, breakout.to_weight - breakout.from_weight)
                action_bits.append(f"突破 {breakout.trigger_price:.2f} 再加 {add_on_weight * 100:.0f}%")
            sizing_text = execution_order_sizing_text(item)
            if sizing_text:
                action_bits.append(sizing_text)
            lines.append("   " + " | ".join(action_bits))
    elif snapshot.picks:
        lines.extend(["", "当前核心持仓"])
        for index, item in enumerate(snapshot.picks[:3], start=1):
            lines.append(
                f"{index}. {item.ticker} {item.name} | 目标 {item.weight * 100:.0f}% | 分数 {item.score:.1f}"
            )
            lines.append(
                f"   {item.market_label}/{item.sector} | 现价 {item.close:.2f} | 20D {item.ret20:+.1f}% | 60D {item.ret60:+.1f}%"
            )
    else:
        lines.extend(
            [
                "",
                "当前没有新的趋势开仓推荐。",
                "模型更偏防守，继续等市场 Risk ON 或更强的趋势结构出现。",
            ]
        )
    if exit_actions:
        lines.extend(["", "先处理风控"])
        for item in exit_actions[:3]:
            lines.append(f"- {item.action} {item.ticker} {item.name} | {item.note}")
    if not snapshot.order_sizing_rules.market_capital:
        lines.extend(["", "提示: 还没配置资金桶，当前只输出权重，不输出预算金额和估算股数。"])
    lines.append("")
    lines.append("仅供模型跟踪参考，不构成投资建议。")
    return "\n".join(lines)


def recent_curve_return(curve: tuple[DailyEquityPoint, ...], sessions: int = 5) -> float | None:
    if len(curve) < 2:
        return None
    start_index = max(0, len(curve) - sessions - 1)
    start_value = curve[start_index].net_value
    end_value = curve[-1].net_value
    if start_value <= 0:
        return None
    return end_value / start_value - 1.0


def format_weekly_review_from_report(report: BacktestReport) -> str:
    snapshot = report.latest_snapshot
    risk_text = (
        " / ".join(
            f"{item.market_label} " + ("Risk ON" if item.risk_on else "Risk OFF")
            for item in snapshot.regimes
        )
        if snapshot.regimes
        else "未知"
    )
    weekly_return = recent_curve_return(report.daily_curve, sessions=5)
    last_period = report.periods[-1] if report.periods else None
    entry_actions = [
        item
        for item in snapshot.execution_plan
        if item.action in {"首仓买入", "首仓加仓"}
    ]
    breakout_actions = {
        item.ticker: item
        for item in snapshot.execution_plan
        if item.action == "突破加仓"
    }
    exit_actions = [item for item in snapshot.execution_plan if item.action.startswith("立即")]
    picks_by_ticker = {item.ticker: item for item in snapshot.picks}

    lines = [
        f"【{report.market_label}周复盘 + 下周候选池｜{report.end_date.isoformat()}】",
        f"市场风控: {risk_text}",
        f"当前仓位: 持仓 {snapshot.invested_weight * 100:.0f}% | 现金 {snapshot.cash_weight * 100:.0f}%",
    ]
    recap_bits = [f"近阶段累计 {report.total_return * 100:+.1f}%", f"最大回撤 {report.max_drawdown * 100:.1f}%"]
    if weekly_return is not None:
        recap_bits.insert(0, f"最近5个交易日 {weekly_return * 100:+.1f}%")
    lines.append("周度复盘: " + " | ".join(recap_bits))
    if report.best_day is not None and report.worst_day is not None:
        lines.append(
            f"单日波动: 最好 {report.best_day.return_pct * 100:+.2f}% ({report.best_day.session_date.isoformat()})"
            f" | 最差 {report.worst_day.return_pct * 100:+.2f}% ({report.worst_day.session_date.isoformat()})"
        )
    if last_period is not None:
        regime_text = " / ".join(last_period.risk_on_markets) if last_period.risk_on_markets else "全部 Risk OFF"
        lines.extend(
            [
                "",
                "最近一期调仓",
                f"- 区间 {last_period.start_date.isoformat()} -> {last_period.end_date.isoformat()} | 净收益 {last_period.portfolio_return * 100:+.2f}% | 换手 {last_period.turnover * 100:.0f}% | 风险开关 {regime_text}",
            ]
        )
        if last_period.contributions:
            best = last_period.contributions[0]
            worst = last_period.contributions[-1]
            lines.append(
                f"- 归因: 最强 {best.ticker} {best.contribution * 100:+.2f}% | 最弱 {worst.ticker} {worst.contribution * 100:+.2f}%"
            )
        if last_period.exit_events:
            exit_summary = " / ".join(f"{item.ticker} {item.reason}" for item in last_period.exit_events[:3])
            lines.append(f"- 退出: {exit_summary}")
    if entry_actions:
        lines.extend(["", "下周候选池"])
        for index, item in enumerate(entry_actions[:5], start=1):
            pick = picks_by_ticker.get(item.ticker)
            breakout = breakout_actions.get(item.ticker)
            target_weight = pick.weight if pick is not None else (breakout.to_weight if breakout is not None else item.to_weight)
            starter_weight = max(0.0, item.to_weight - item.from_weight)
            header = f"{index}. {item.ticker} {item.name} | 目标 {target_weight * 100:.0f}%"
            if pick is not None:
                header += f" | 分数 {pick.score:.1f}"
            lines.append(header)
            detail_bits: list[str] = []
            if pick is not None:
                detail_bits.extend(
                    [
                        f"{pick.market_label}/{pick.sector}",
                        f"现价 {pick.close:.2f}",
                        f"20D {pick.ret20:+.1f}%",
                        f"60D {pick.ret60:+.1f}%",
                    ]
                )
            if item.stop_price is not None:
                detail_bits.append(f"止损 {item.stop_price:.2f}")
            if detail_bits:
                lines.append("   " + " | ".join(detail_bits))
            action_bits = [f"首仓 {starter_weight * 100:.0f}%"]
            if breakout is not None and breakout.trigger_price is not None:
                add_on_weight = max(0.0, breakout.to_weight - breakout.from_weight)
                action_bits.append(f"突破 {breakout.trigger_price:.2f} 再加 {add_on_weight * 100:.0f}%")
            sizing_text = execution_order_sizing_text(item)
            if sizing_text:
                action_bits.append(sizing_text)
            lines.append("   " + " | ".join(action_bits))
    elif snapshot.picks:
        lines.extend(["", "下周核心跟踪"])
        for index, item in enumerate(snapshot.picks[:5], start=1):
            lines.append(
                f"{index}. {item.ticker} {item.name} | 目标 {item.weight * 100:.0f}% | 分数 {item.score:.1f}"
            )
            lines.append(
                f"   {item.market_label}/{item.sector} | 现价 {item.close:.2f} | 20D {item.ret20:+.1f}% | 60D {item.ret60:+.1f}%"
            )
    else:
        lines.extend(["", "下周候选池仍为空，继续等市场风险偏好回暖。"])
    if exit_actions:
        lines.extend(["", "下周先处理"])
        for item in exit_actions[:5]:
            lines.append(f"- {item.action} {item.ticker} {item.name} | {item.note}")
    sizing_summary = order_sizing_summary(snapshot.order_sizing_rules)
    if sizing_summary:
        lines.extend(["", f"资金模板: {sizing_summary}"])
    else:
        lines.extend(["", "提示: 还没配置资金桶，当前周报只输出权重，不输出预算金额和估算股数。"])
    lines.append("")
    lines.append("仅供模型跟踪参考，不构成投资建议。")
    return "\n".join(lines)


def format_trading_plan(
    universe_path: str | Path,
    market: str | None,
    *,
    top_n: int = DEFAULT_TOP_N,
    history_fetcher: HistoryFetcher | None = None,
) -> str:
    snapshot = build_trend_snapshot(universe_path, market, top_n=top_n, history_fetcher=history_fetcher)
    return format_trading_plan_from_snapshot(snapshot)


def format_recommendation_digest(
    universe_path: str | Path,
    market: str | None,
    *,
    top_n: int = DEFAULT_TOP_N,
    history_fetcher: HistoryFetcher | None = None,
) -> str:
    snapshot = build_trend_snapshot(universe_path, market, top_n=top_n, history_fetcher=history_fetcher)
    return format_recommendation_digest_from_snapshot(snapshot)


def format_weekly_review(
    universe_path: str | Path,
    market: str | None,
    *,
    lookback_months: int = 6,
    top_n: int = DEFAULT_TOP_N,
    history_fetcher: HistoryFetcher | None = None,
) -> str:
    report = backtest_trend_strategy(
        universe_path,
        market,
        lookback_months=lookback_months,
        top_n=top_n,
        history_fetcher=history_fetcher,
    )
    return format_weekly_review_from_report(report)


def export_trading_plan_csv(
    universe_path: str | Path,
    market: str | None,
    output_path: str | Path,
    *,
    top_n: int = DEFAULT_TOP_N,
    history_fetcher: HistoryFetcher | None = None,
) -> Path:
    snapshot = build_trend_snapshot(universe_path, market, top_n=top_n, history_fetcher=history_fetcher)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=TRADE_PLAN_EXPORT_FIELDS)
        writer.writeheader()
        writer.writerows(execution_plan_rows(snapshot))
    return output


def format_backtest_report(
    universe_path: str | Path,
    market: str | None,
    *,
    lookback_months: int = 12,
    top_n: int = DEFAULT_TOP_N,
    history_fetcher: HistoryFetcher | None = None,
) -> str:
    report = backtest_trend_strategy(
        universe_path,
        market,
        lookback_months=lookback_months,
        top_n=top_n,
        history_fetcher=history_fetcher,
    )
    lines = [
        "Quant WeChat Bot",
        "",
        "策略: 趋势选股 + 市场过滤 + 仓位管理",
        f"市场: {report.market_label}",
        f"区间: {report.start_date.isoformat()} -> {report.end_date.isoformat()}",
        f"候选池: {report.candidate_count} 只当前大市值股票。",
        f"调仓节奏: 每 {REBALANCE_DAYS} 个交易日。",
        "",
        "回测结果",
        f"- 毛收益: {report.gross_total_return * 100:+.1f}%",
        f"- 成本拖累: {-report.total_cost_drag * 100:+.1f}%",
        f"- 累计收益: {report.total_return * 100:+.1f}%",
        f"- 年化收益: {report.annualized_return * 100:+.1f}%",
        f"- 最大回撤: {report.max_drawdown * 100:.1f}%",
        f"- 胜率: {report.win_rate * 100:.0f}%",
        f"- 平均实盘仓位: {report.average_invested_weight * 100:.0f}%",
        f"- 平均单期成本: {report.average_cost_drag * 100:.2f}%",
        f"- 平均单期收益: {report.average_period_return * 100:+.2f}%",
        f"- 平均换手: {report.average_turnover * 100:.0f}%",
        f"- 成本假设: 佣金 {report.cost_model.commission_bps:.1f}bp | 滑点 {report.cost_model.slippage_bps:.1f}bp | A股卖出税 {report.cost_model.sell_tax_bps.get('CN', 0.0):.1f}bp",
        f"- 当前组合约束: 单票 {report.latest_snapshot.constraints.max_position_weight * 100:.0f}% | 单行业 {report.latest_snapshot.constraints.max_sector_weight * 100:.0f}% | 目标总仓位 {report.latest_snapshot.constraints.target_gross_exposure * 100:.0f}%",
        f"- 退出规则: {exit_rule_summary(report.latest_snapshot.exit_rules)}",
        f"- 执行规则: {execution_rule_summary(report.latest_snapshot.execution_rules)}",
    ]
    sizing_summary = order_sizing_summary(report.latest_snapshot.order_sizing_rules)
    if sizing_summary:
        lines.append(f"- 资金换算: {sizing_summary}")
    if report.history_failures:
        lines.append(f"- 历史数据抓取失败: {report.history_failures} 个标的已跳过")
    recent_periods = list(report.periods[-3:])
    if recent_periods:
        lines.extend(["", "最近三次调仓"])
        for period in recent_periods:
            regime_text = " / ".join(period.risk_on_markets) if period.risk_on_markets else "全部 Risk OFF"
            lines.append(
                f"- {period.start_date.isoformat()} -> {period.end_date.isoformat()} | 毛收益 {period.gross_return * 100:+.2f}% | 成本 {-period.cost_drag * 100:+.2f}% | 净收益 {period.portfolio_return * 100:+.2f}% | 换手 {period.turnover * 100:.0f}% | 风险开关 {regime_text}"
            )
            if period.contributions:
                best = period.contributions[0]
                worst = period.contributions[-1]
                lines.append(
                    f"  归因: 最强 {best.ticker} {best.contribution * 100:+.2f}% | 最弱 {worst.ticker} {worst.contribution * 100:+.2f}%"
                )
            if period.exit_events:
                exit_summary = " / ".join(
                    f"{item.ticker} {item.reason}" for item in period.exit_events[:2]
                )
                lines.append(f"  退出: {exit_summary}")
    if report.exit_reason_counts:
        lines.extend(["", "退出统计"])
        for item in report.exit_reason_counts:
            lines.append(f"- {item.reason}: {item.count} 次")
    if report.current_sector_exposures:
        lines.extend(["", "当前行业暴露"])
        for item in report.current_sector_exposures:
            lines.append(f"- {item.sector}: {item.weight * 100:.0f}% ({item.count} 只)")
    if report.latest_snapshot.market_exposures:
        lines.extend(["", "当前市场分配"])
        for item in report.latest_snapshot.market_exposures:
            lines.append(
                f"- {item.market_label}: 目标 {item.target_weight * 100:.0f}% | 实际 {item.actual_weight * 100:.0f}% ({item.count} 只)"
            )
    diagnostics = report.latest_snapshot.constraint_diagnostics
    if any(
        (
            diagnostics.skipped_sector_position_limit,
            diagnostics.skipped_sector_weight_limit,
            diagnostics.skipped_market_budget_limit,
            diagnostics.skipped_small_remainder,
            diagnostics.partial_weight_positions,
        )
    ):
        lines.extend(
            [
                "",
                "当前约束命中",
                f"- 行业名额跳过 {diagnostics.skipped_sector_position_limit} 只 | 行业权重跳过 {diagnostics.skipped_sector_weight_limit} 只 | 市场预算跳过 {diagnostics.skipped_market_budget_limit} 只",
                f"- 小仓位舍弃 {diagnostics.skipped_small_remainder} 只 | 部分仓位入选 {diagnostics.partial_weight_positions} 只",
            ]
        )
    if report.latest_snapshot.previous_rebalance_date is not None:
        lines.extend(["", f"最近一期调仓清单（对比 {report.latest_snapshot.previous_rebalance_date.isoformat()}）"])
        if report.latest_snapshot.trade_plan:
            for item in report.latest_snapshot.trade_plan[:6]:
                lines.append(format_trade_plan_item(item))
        else:
            lines.append("- 最近一期模型仓位无变化")
    if report.latest_snapshot.execution_plan:
        lines.extend(["", "最新执行清单"])
        for item in report.latest_snapshot.execution_plan[:8]:
            lines.append(format_execution_plan_item(item))
    if report.daily_curve:
        lines.extend(["", "最近净值轨迹"])
        for point in report.daily_curve[-5:]:
            lines.append(
                f"- {point.session_date.isoformat()} | 净值 {point.net_value:.3f} | 回撤 {point.drawdown * 100:.1f}%"
            )
    if report.best_day is not None and report.worst_day is not None:
        lines.extend(
            [
                "",
                "单日表现",
                f"- 最好一天: {report.best_day.session_date.isoformat()} {report.best_day.return_pct * 100:+.2f}%",
                f"- 最差一天: {report.worst_day.session_date.isoformat()} {report.worst_day.return_pct * 100:+.2f}%",
            ]
        )
    latest = report.latest_snapshot
    lines.extend(["", "当前信号"])
    if latest.picks:
        for item in latest.picks:
            lines.append(
                f"- {item.ticker} {item.name} | 权重 {item.weight * 100:.0f}% | 60D {item.ret60:+.1f}% | 相对基准 {item.relative_strength_60d:+.1f}%"
            )
    else:
        lines.append("- 当前为空仓信号。")
    lines.extend(
        [
            "",
            "注意:",
            "- 这是一版可执行 MVP，不是机构级回测引擎。",
            "- 当前回看仍有生存者偏差，因为候选池来自今天仍在库里的股票。",
            "- 下一步最值得补的是真实财务因子、分批建仓和实盘指令落地。",
        ]
    )
    return "\n".join(lines)
