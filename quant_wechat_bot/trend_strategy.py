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
    market_exposures: tuple[MarketExposure, ...]
    constraint_diagnostics: ConstraintDiagnostics
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
class ConstraintDiagnostics:
    skipped_sector_position_limit: int = 0
    skipped_sector_weight_limit: int = 0
    skipped_market_budget_limit: int = 0
    skipped_small_remainder: int = 0
    partial_weight_positions: int = 0


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
    picks, evaluated_count, constraint_diagnostics = select_picks(
        rows,
        merged_histories,
        regimes,
        effective_as_of,
        top_n=top_n,
        constraints=active_constraints,
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
        market_exposures=summarize_market_exposures(picks, active_constraints),
        constraint_diagnostics=constraint_diagnostics,
        picks=tuple(picks),
    )


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
    histories: dict[str, list[tuple[dt.date, float]]],
    reference_dates: list[dt.date],
) -> tuple[DailyEquityPoint, ...]:
    curve: list[DailyEquityPoint] = []
    gross_nav = 1.0
    net_nav = 1.0
    peak = 1.0
    for period in periods:
        gross_start = gross_nav
        net_start = net_nav * (1.0 - period.cost_drag)
        period_dates = [item for item in reference_dates if period.start_date < item <= period.end_date]
        if not period_dates:
            period_dates = [period.end_date]
        for session_date in period_dates:
            gross_return_to_date = period_return(period.holdings, histories, period.start_date, session_date)
            gross_value = gross_start * (1.0 + gross_return_to_date)
            net_value = net_start * (1.0 + gross_return_to_date)
            peak = max(peak, net_value)
            curve.append(
                DailyEquityPoint(
                    session_date=session_date,
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
    benchmark_symbols = [UNIVERSE_CONFIGS[code].benchmark_symbol for code in target_codes]
    stock_symbols = [history_symbol_for_ticker(str(row["ticker"])) for row in rows]
    histories, failures = load_histories(benchmark_symbols + stock_symbols, history_fetcher=history_fetcher)
    for symbol in benchmark_symbols:
        if symbol not in histories:
            raise RuntimeError(f"Missing benchmark history for `{symbol}`.")
    reference_dates = sorted(
        {
            session_date
            for symbol in benchmark_symbols
            for session_date, _ in histories.get(symbol, [])
        }
    )
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
        contributions = period_contributions(current_holdings, histories, start_date, end_date)
        gross_return = sum(item.contribution for item in contributions)
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
                invested_weight=round(sum(item.weight for item in current_holdings), 4),
                turnover=round(turnover_ratio(previous_holdings, current_holdings), 4),
                buy_turnover=round(buy_turnover, 4),
                sell_turnover=round(sell_turnover, 4),
                holdings=current_holdings,
                risk_on_markets=tuple(
                    regimes[code].market_label for code in target_codes if code in regimes and regimes[code].risk_on
                ),
                contributions=contributions,
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
    daily_curve = build_daily_equity_curve(periods, histories, test_dates)
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
        ]
    )
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
        lines.append(
            f"   20D {item.ret20:+.1f}% | 60D {item.ret60:+.1f}% | 120D {item.ret120:+.1f}% | 相对基准 {item.relative_strength_60d:+.1f}%"
        )
    exposures = summarize_sector_exposures(snapshot.picks)
    if exposures:
        lines.extend(["", "当前行业暴露"])
        for item in exposures:
            lines.append(f"- {item.sector}: {item.weight * 100:.0f}% ({item.count} 只)")
    lines.extend(
        [
            "",
            "说明:",
            "- 市场过滤: 基准站上 MA120，且 MA20 > MA60 或 60D 趋势为正时才开仓。",
            "- 选股规则: 价格站上 MA60，MA20 > MA60 > MA120，且 20D/60D 动量均为正。",
            "- 当前结果不含真实交易成本、涨跌停、停牌和点差处理。",
        ]
    )
    return "\n".join(lines)


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
    ]
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
            "- 下一步最值得补的是真实财务因子、止损规则和交易清单导出。",
        ]
    )
    return "\n".join(lines)
