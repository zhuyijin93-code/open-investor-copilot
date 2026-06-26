from __future__ import annotations

import dataclasses
import datetime as dt
from pathlib import Path

try:
    from . import market_close_digest, trend_strategy
except ImportError:  # pragma: no cover - allows direct script execution
    import market_close_digest  # type: ignore
    import trend_strategy  # type: ignore


PROJECT_ROOT = Path(__file__).resolve().parent


@dataclasses.dataclass(frozen=True)
class MacroAssetSpec:
    symbol: str
    label: str
    theme: str
    market_code: str
    risk_bucket: str
    signal_kind: str = "risk"


@dataclasses.dataclass(frozen=True)
class MacroOpportunity:
    spec: MacroAssetSpec
    session_date: dt.date
    close: float
    day_change: float
    ret5: float
    ret20: float
    ma20: float
    pct_from_ma20: float
    score: float
    setup: str
    note: str


MACRO_ASSET_SPECS: tuple[MacroAssetSpec, ...] = (
    MacroAssetSpec("000300.SS", "沪深300", "A股宽基", "CN", "risk", "risk"),
    MacroAssetSpec("399006.SZ", "创业板", "A股成长", "CN", "risk", "risk"),
    MacroAssetSpec("^HSI", "恒指", "港股宽基", "HK", "risk", "risk"),
    MacroAssetSpec("3033.HK", "恒科ETF", "港股科技", "HK", "risk", "risk"),
    MacroAssetSpec("SPY", "标普500", "美股宽基", "US", "risk", "risk"),
    MacroAssetSpec("QQQ", "纳指100", "美股科技", "US", "risk", "risk"),
    MacroAssetSpec("IWM", "罗素2000", "美股小盘", "US", "risk", "risk"),
    MacroAssetSpec("XLF", "金融ETF", "金融风格", "US", "risk", "risk"),
    MacroAssetSpec("XLE", "能源ETF", "能源风格", "US", "risk", "risk"),
    MacroAssetSpec("GLD", "黄金ETF", "黄金避险", "US", "defensive", "defensive"),
    MacroAssetSpec("TLT", "长债ETF", "长债避险", "US", "defensive", "defensive"),
    MacroAssetSpec("USO", "原油ETF", "原油通胀", "US", "cyclical", "cyclical"),
    MacroAssetSpec("DX-Y.NYB", "美元指数", "美元流动性", "GLOBAL", "macro", "dollar"),
    MacroAssetSpec("^TNX", "10Y美债利率", "全球利率", "GLOBAL", "macro", "yield"),
    MacroAssetSpec("^VIX", "VIX波动率", "风险情绪", "GLOBAL", "macro", "volatility"),
)


def macro_market_label(value: str | None) -> str:
    if value is None or not value.strip():
        return "全市场"
    normalized = value.strip().lower()
    if normalized in {"all", "global", "world", "全市场", "全部", "所有", "全球"}:
        return "全市场"
    return market_close_digest.normalize_market(value).label


def selected_macro_assets(market: str | None = None) -> tuple[MacroAssetSpec, ...]:
    label = macro_market_label(market)
    if label == "全市场":
        return MACRO_ASSET_SPECS
    code = market_close_digest.normalize_market(label).code
    return tuple(item for item in MACRO_ASSET_SPECS if item.market_code in {code, "GLOBAL"})


def macro_signal_note(spec: MacroAssetSpec, *, close: float, ma20: float, ret5: float, ret20: float) -> tuple[str, str]:
    above_ma = close > ma20
    if spec.signal_kind == "risk":
        if above_ma and ret20 >= 8 and ret5 >= 1:
            return "顺势做多", f"{spec.theme}继续领跑，适合优先看同方向最强个股。"
        if above_ma and ret20 >= 2:
            return "趋势观察", f"{spec.theme}维持强势，但更适合等回踩或确认后跟进。"
        if not above_ma and ret20 <= -4:
            return "降权回避", f"{spec.theme}处于弱势区，先别急着逆势抄底。"
        return "中性观察", f"{spec.theme}暂时没有形成清晰趋势，保持观察。"
    if spec.signal_kind == "defensive":
        if above_ma and ret20 >= 3:
            return "对冲关注", f"{spec.theme}抬头，适合作为组合防守或风险对冲观察。"
        if above_ma:
            return "防守观察", f"{spec.theme}略偏强，适合在风险资产走弱时做备选。"
        return "防守降温", f"{spec.theme}自身也不强，说明防守需求暂未明显升温。"
    if spec.signal_kind == "cyclical":
        if above_ma and ret20 >= 5:
            return "景气跟踪", f"{spec.theme}走强，说明通胀/周期交易有升温迹象。"
        if not above_ma and ret20 <= -4:
            return "景气降温", f"{spec.theme}回落，周期交易热度在降温。"
        return "中性观察", f"{spec.theme}仍在震荡，先结合其他市场信号确认。"
    if spec.signal_kind == "dollar":
        if above_ma and ret20 >= 2:
            return "流动性逆风", "美元偏强往往会压制全球风险偏好，仓位宜更保守。"
        if not above_ma and ret20 <= -2:
            return "流动性顺风", "美元回落有利于风险偏好修复，更适合看成长和弹性资产。"
        return "美元震荡", "美元方向感不强，暂时不单独放大其影响。"
    if spec.signal_kind == "yield":
        if above_ma and ret20 >= 2:
            return "利率抬升", "美债利率上行会抬高贴现率，对长久期成长资产偏不友好。"
        if not above_ma and ret20 <= -2:
            return "利率回落", "美债利率回落会缓解估值压力，更有利于成长和科技风格。"
        return "利率震荡", "利率暂时横盘，对风格切换的指向性有限。"
    if spec.signal_kind == "volatility":
        if above_ma and ret20 >= 5:
            return "波动升温", "VIX 抬升意味着避险需求升温，先控制仓位和节奏。"
        if not above_ma and ret20 <= -5:
            return "波动降温", "VIX 走弱说明恐慌回落，风险资产的容错率在提升。"
        return "波动中性", "VIX 暂时没有形成单边信号，情绪面偏中性。"
    if above_ma and ret20 >= 5:
        return "景气跟踪", f"{spec.theme}走强，说明通胀/周期交易有升温迹象。"
    if not above_ma and ret20 <= -4:
        return "景气降温", f"{spec.theme}回落，周期交易热度在降温。"
    return "中性观察", f"{spec.theme}仍在震荡，先结合其他市场信号确认。"


def signal_score_multiplier(spec: MacroAssetSpec) -> float:
    if spec.signal_kind in {"dollar", "yield", "volatility"}:
        return -1.0
    return 1.0


def evaluate_macro_asset(spec: MacroAssetSpec, history: list[tuple[dt.date, float]]) -> MacroOpportunity | None:
    if len(history) < 25:
        return None
    closes = [close for _, close in history]
    session_date = history[-1][0]
    close = closes[-1]
    previous_close = closes[-2]
    ma20 = trend_strategy.moving_average(closes, 20)
    ret5 = trend_strategy.trailing_return(closes, 5)
    ret20 = trend_strategy.trailing_return(closes, 20)
    day_change = 0.0 if previous_close == 0 else (close / previous_close - 1.0) * 100.0
    pct_from_ma20 = 0.0 if ma20 == 0 else (close / ma20 - 1.0) * 100.0
    multiplier = signal_score_multiplier(spec)
    score = (
        (ret20 * 0.7 + ret5 * 0.5 + pct_from_ma20 * 0.8) * multiplier
        + (2.0 if close > ma20 else -2.0) * multiplier
    )
    setup, note = macro_signal_note(spec, close=close, ma20=ma20, ret5=ret5, ret20=ret20)
    return MacroOpportunity(
        spec=spec,
        session_date=session_date,
        close=round(close, 2),
        day_change=round(day_change, 2),
        ret5=round(ret5, 2),
        ret20=round(ret20, 2),
        ma20=round(ma20, 2),
        pct_from_ma20=round(pct_from_ma20, 2),
        score=round(score, 2),
        setup=setup,
        note=note,
    )


def build_macro_opportunities(
    market: str | None = None,
    *,
    history_fetcher: trend_strategy.HistoryFetcher | None = None,
) -> tuple[tuple[MacroOpportunity, ...], list[str]]:
    assets = selected_macro_assets(market)
    histories, failures = trend_strategy.load_histories(
        [item.symbol for item in assets],
        history_fetcher=history_fetcher,
    )
    opportunities: list[MacroOpportunity] = []
    for item in assets:
        history = histories.get(item.symbol)
        if history is None:
            continue
        evaluated = evaluate_macro_asset(item, history)
        if evaluated is not None:
            opportunities.append(evaluated)
    opportunities.sort(key=lambda item: (-item.score, item.spec.label))
    return tuple(opportunities), failures


def risk_posture(opportunities: tuple[MacroOpportunity, ...]) -> tuple[str, int, int]:
    risk_assets = [item for item in opportunities if item.spec.risk_bucket == "risk"]
    risk_total = len(risk_assets)
    risk_on = sum(item.close > item.ma20 for item in risk_assets)
    if risk_total == 0:
        return "未知", 0, 0
    ratio = risk_on / risk_total
    if ratio >= 0.67:
        return "偏进攻", risk_on, risk_total
    if ratio <= 0.33:
        return "偏防守", risk_on, risk_total
    return "中性轮动", risk_on, risk_total


def market_leaders(opportunities: tuple[MacroOpportunity, ...]) -> tuple[MacroOpportunity, ...]:
    leaders: list[MacroOpportunity] = []
    seen: set[str] = set()
    for item in opportunities:
        if item.spec.market_code == "GLOBAL":
            continue
        code = item.spec.market_code
        if code in seen:
            continue
        seen.add(code)
        leaders.append(item)
    return tuple(leaders)


def preferred_opportunities(opportunities: tuple[MacroOpportunity, ...]) -> tuple[MacroOpportunity, ...]:
    candidates = [
        item
        for item in opportunities
        if item.spec.signal_kind not in {"dollar", "yield", "volatility"}
        if item.setup in {"顺势做多", "趋势观察", "对冲关注", "景气跟踪"}
    ]
    return tuple(candidates[:4])


def weak_links(opportunities: tuple[MacroOpportunity, ...]) -> tuple[MacroOpportunity, ...]:
    ordered = sorted(opportunities, key=lambda item: (item.score, item.spec.label))
    selected = [
        item
        for item in ordered
        if item.spec.signal_kind not in {"dollar", "yield", "volatility"}
        if item.setup in {"降权回避", "景气降温", "防守降温"}
    ]
    return tuple(selected[:3])


def macro_variable_signals(opportunities: tuple[MacroOpportunity, ...]) -> tuple[MacroOpportunity, ...]:
    selected = [
        item
        for item in opportunities
        if item.spec.signal_kind in {"dollar", "yield", "volatility"}
    ]
    return tuple(sorted(selected, key=lambda item: item.spec.label))


def macro_wind_summary(opportunities: tuple[MacroOpportunity, ...]) -> str | None:
    variables = macro_variable_signals(opportunities)
    if not variables:
        return None
    labels = []
    for item in variables:
        labels.append(f"{item.spec.label} {item.setup}")
    return " | ".join(labels)


def format_macro_scan(
    market: str | None = None,
    *,
    history_fetcher: trend_strategy.HistoryFetcher | None = None,
) -> str:
    opportunities, failures = build_macro_opportunities(market, history_fetcher=history_fetcher)
    if not opportunities:
        raise RuntimeError("No macro opportunity signals could be evaluated from the current market data.")
    session_date = max(item.session_date for item in opportunities)
    label = macro_market_label(market)
    posture, risk_on, risk_total = risk_posture(opportunities)
    leaders = market_leaders(opportunities)
    preferred = preferred_opportunities(opportunities)
    weak = weak_links(opportunities)
    macro_variables = macro_variable_signals(opportunities)
    lines = [
        f"【宏观机会扫描｜{session_date.isoformat()}】",
        f"观察范围: {label}",
        f"风险偏好: {posture} | 风险资产 {risk_on}/{risk_total} 站上 MA20",
    ]
    wind_summary = macro_wind_summary(opportunities)
    if wind_summary:
        lines.append(f"关键变量: {wind_summary}")
    if leaders:
        lines.extend(["", "跨市场强弱"])
        for item in leaders:
            lines.append(
                f"- {item.spec.market_code}: {item.spec.label} | 日内 {item.day_change:+.2f}% | 5D {item.ret5:+.1f}% | 20D {item.ret20:+.1f}% | 偏离 MA20 {item.pct_from_ma20:+.1f}%"
            )
    if macro_variables:
        lines.extend(["", "关键宏观变量"])
        for item in macro_variables:
            lines.append(
                f"- {item.spec.label} | 日内 {item.day_change:+.2f}% | 5D {item.ret5:+.1f}% | 20D {item.ret20:+.1f}% | 偏离 MA20 {item.pct_from_ma20:+.1f}% | {item.setup}"
            )
            lines.append(f"  {item.note}")
    if preferred:
        lines.extend(["", "可关注机会"])
        for index, item in enumerate(preferred, start=1):
            lines.append(
                f"{index}. {item.spec.label} | {item.spec.theme} | 日内 {item.day_change:+.2f}% | 5D {item.ret5:+.1f}% | 20D {item.ret20:+.1f}%"
            )
            lines.append(
                f"   信号: {item.setup} | 收盘 {item.close:.2f} vs MA20 {item.ma20:.2f} ({item.pct_from_ma20:+.1f}%) | {item.note}"
            )
    if weak:
        lines.extend(["", "降温/回避"])
        for item in weak:
            lines.append(
                f"- {item.spec.label} | {item.spec.theme} | 20D {item.ret20:+.1f}% | 偏离 MA20 {item.pct_from_ma20:+.1f}% | {item.setup}"
            )
    if failures:
        lines.extend(["", f"数据提示: 有 {len(failures)} 个宏观标的未成功抓取，已自动跳过。"])
    lines.extend(
        [
            "",
            "说明:",
            "- 这是一层先选市场/主题的宏观雷达，用来决定先看哪一类交易，而不是直接替代个股执行。",
            "- 更适合先看 `宏观机会`，再下沉到 `推荐日报`、`趋势选股` 或 `交易计划`。",
        ]
    )
    return "\n".join(lines)
