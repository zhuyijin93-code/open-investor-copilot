from __future__ import annotations

import concurrent.futures
import csv
import dataclasses
import datetime as dt
import math
import re
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
MAX_WORKERS = 6


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
    picks: tuple[TrendPick, ...]


@dataclasses.dataclass(frozen=True)
class BacktestPeriod:
    start_date: dt.date
    end_date: dt.date
    portfolio_return: float
    invested_weight: float
    turnover: float
    holdings: tuple[TrendPick, ...]
    risk_on_markets: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class BacktestReport:
    market_label: str
    start_date: dt.date
    end_date: dt.date
    candidate_count: int
    evaluated_count: int
    history_failures: int
    total_return: float
    annualized_return: float
    max_drawdown: float
    win_rate: float
    average_period_return: float
    average_turnover: float
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
    max_sector_positions: int = MAX_SECTOR_POSITIONS,
    max_position_weight: float = MAX_POSITION_WEIGHT,
) -> tuple[list[TrendPick], int]:
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
    for item in ranked:
        if sector_counts.get(item.sector, 0) >= max_sector_positions:
            continue
        selected.append(item)
        sector_counts[item.sector] = sector_counts.get(item.sector, 0) + 1
        if len(selected) >= top_n:
            break
    if not selected:
        return [], evaluated
    target_weight = min(1.0 / len(selected), max_position_weight)
    weighted = [dataclasses.replace(item, weight=round(target_weight, 4)) for item in selected]
    return weighted, evaluated


def build_trend_snapshot(
    universe_path: str | Path,
    market: str | None,
    *,
    top_n: int = DEFAULT_TOP_N,
    as_of: dt.date | None = None,
    history_fetcher: HistoryFetcher | None = None,
) -> TrendSnapshot:
    rows = load_candidate_rows(universe_path, market)
    if not rows:
        raise RuntimeError("No liquid large-cap candidates passed the initial market filters.")
    target_codes = resolve_market_codes(market)
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
    picks, evaluated_count = select_picks(
        rows,
        merged_histories,
        regimes,
        effective_as_of,
        top_n=top_n,
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
        picks=tuple(picks),
    )


def period_return(
    picks: tuple[TrendPick, ...],
    histories: dict[str, list[tuple[dt.date, float]]],
    start_date: dt.date,
    end_date: dt.date,
) -> float:
    total = 0.0
    for item in picks:
        history = histories.get(item.history_symbol)
        if history is None:
            continue
        start_price = price_on_or_before(history, start_date)
        end_price = price_on_or_before(history, end_date)
        if start_price in (None, 0) or end_price is None:
            continue
        total += item.weight * (end_price / start_price - 1.0)
    return total


def turnover_ratio(previous: tuple[TrendPick, ...], current: tuple[TrendPick, ...]) -> float:
    previous_weights = {item.ticker: item.weight for item in previous}
    current_weights = {item.ticker: item.weight for item in current}
    tickers = set(previous_weights) | set(current_weights)
    return sum(abs(current_weights.get(ticker, 0.0) - previous_weights.get(ticker, 0.0)) for ticker in tickers) / 2.0


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
    equity_curve = [1.0]
    for start_date, end_date in zip(rebalance_dates[:-1], rebalance_dates[1:]):
        regimes: dict[str, RegimeSnapshot] = {}
        for code in target_codes:
            regime = evaluate_regime(code, histories[UNIVERSE_CONFIGS[code].benchmark_symbol], start_date)
            if regime is not None:
                regimes[code] = regime
        holdings, evaluated_count = select_picks(
            rows,
            histories,
            regimes,
            start_date,
            top_n=top_n,
        )
        current_holdings = tuple(holdings)
        realized_return = period_return(current_holdings, histories, start_date, end_date)
        equity_curve.append(equity_curve[-1] * (1.0 + realized_return))
        periods.append(
            BacktestPeriod(
                start_date=start_date,
                end_date=end_date,
                portfolio_return=realized_return,
                invested_weight=round(sum(item.weight for item in current_holdings), 4),
                turnover=round(turnover_ratio(previous_holdings, current_holdings), 4),
                holdings=current_holdings,
                risk_on_markets=tuple(
                    regimes[code].market_label for code in target_codes if code in regimes and regimes[code].risk_on
                ),
            )
        )
        previous_holdings = current_holdings
    latest_snapshot = build_trend_snapshot(
        universe_path,
        market,
        top_n=top_n,
        as_of=rebalance_dates[-1],
        history_fetcher=history_fetcher,
    )
    total_return = equity_curve[-1] - 1.0
    calendar_days = max((periods[-1].end_date - periods[0].start_date).days, 1)
    annualized_return = (equity_curve[-1] ** (365.0 / calendar_days) - 1.0) if equity_curve[-1] > 0 else -1.0
    positive_periods = sum(1 for item in periods if item.portfolio_return > 0)
    average_period_return = sum(item.portfolio_return for item in periods) / len(periods)
    average_turnover = sum(item.turnover for item in periods) / len(periods)
    return BacktestReport(
        market_label=market_label(market),
        start_date=periods[0].start_date,
        end_date=periods[-1].end_date,
        candidate_count=len(rows),
        evaluated_count=latest_snapshot.evaluated_count,
        history_failures=len(failures),
        total_return=total_return,
        annualized_return=annualized_return,
        max_drawdown=max_drawdown(equity_curve),
        win_rate=positive_periods / len(periods),
        average_period_return=average_period_return,
        average_turnover=average_turnover,
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
            f"- 目标持仓: {snapshot.invested_weight * 100:.0f}%",
            f"- 现金缓冲: {snapshot.cash_weight * 100:.0f}%",
            f"- 单票上限: {MAX_POSITION_WEIGHT * 100:.0f}%",
            f"- 同行业最多 {MAX_SECTOR_POSITIONS} 只",
        ]
    )
    if snapshot.history_failures:
        lines.append(f"- 历史数据抓取失败 {snapshot.history_failures} 个标的，已自动跳过")
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
        f"- 累计收益: {report.total_return * 100:+.1f}%",
        f"- 年化收益: {report.annualized_return * 100:+.1f}%",
        f"- 最大回撤: {report.max_drawdown * 100:.1f}%",
        f"- 胜率: {report.win_rate * 100:.0f}%",
        f"- 平均单期收益: {report.average_period_return * 100:+.2f}%",
        f"- 平均换手: {report.average_turnover * 100:.0f}%",
    ]
    if report.history_failures:
        lines.append(f"- 历史数据抓取失败: {report.history_failures} 个标的已跳过")
    recent_periods = list(report.periods[-3:])
    if recent_periods:
        lines.extend(["", "最近三次调仓"])
        for period in recent_periods:
            regime_text = " / ".join(period.risk_on_markets) if period.risk_on_markets else "全部 Risk OFF"
            lines.append(
                f"- {period.start_date.isoformat()} -> {period.end_date.isoformat()} | 收益 {period.portfolio_return * 100:+.2f}% | 换手 {period.turnover * 100:.0f}% | 风险开关 {regime_text}"
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
            "- 下一步最值得补的是真实财务因子、交易成本模型和逐日持仓归因。",
        ]
    )
    return "\n".join(lines)
