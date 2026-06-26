from __future__ import annotations

import csv
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from quant_wechat_bot import trend_strategy


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


def build_series(start_price: float, slope: float, days: int = 320) -> list[tuple[dt.date, float]]:
    base = dt.date(2025, 1, 1)
    return [(base + dt.timedelta(days=index), start_price + slope * index) for index in range(days)]


def build_custom_series(prices: list[float], start: dt.date | None = None) -> list[tuple[dt.date, float]]:
    base = start or dt.date(2025, 1, 1)
    return [(base + dt.timedelta(days=index), price) for index, price in enumerate(prices)]


def build_row(ticker: str, name: str, sector: str, price: float, market_cap_b: float) -> dict[str, str]:
    return {
        "ticker": ticker,
        "name": name,
        "sector": sector,
        "price": f"{price:.1f}",
        "market_cap_b": f"{market_cap_b:.1f}",
        "pe": "20.0",
        "pb": "4.0",
        "roe": "0.0",
        "revenue_growth": "0.0",
        "momentum_20d": "0.0",
        "momentum_60d": "0.0",
        "volatility_20d": "0.0",
        "dividend_yield": "0.0",
    }


def write_universe_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


class TrendStrategyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.universe_path = Path(self.temp_dir.name) / "universe.csv"
        rows = [
            build_row("600519", "Alpha", "消费", 120.0, 180.0),
            build_row("000333", "Bravo", "家电", 80.0, 150.0),
            build_row("300750", "Charlie", "新能源", 45.0, 130.0),
        ]
        write_universe_csv(self.universe_path, rows)
        self.histories = {
            "000300.SS": build_series(100.0, 0.25),
            "600519.SS": build_series(50.0, 0.55),
            "000333.SZ": build_series(40.0, 0.35),
            "300750.SZ": build_series(80.0, -0.10),
        }

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_history_symbol_for_a_share_tickers(self) -> None:
        self.assertEqual(trend_strategy.history_symbol_for_ticker("600519"), "600519.SS")
        self.assertEqual(trend_strategy.history_symbol_for_ticker("000333"), "000333.SZ")
        self.assertEqual(trend_strategy.history_symbol_for_ticker("00700.HK"), "00700.HK")

    def test_build_trend_snapshot_selects_positive_trend_names(self) -> None:
        snapshot = trend_strategy.build_trend_snapshot(
            self.universe_path,
            "A股",
            top_n=2,
            history_fetcher=self.histories.__getitem__,
        )
        tickers = [item.ticker for item in snapshot.picks]
        self.assertEqual(snapshot.market_label, "A股")
        self.assertTrue(snapshot.regimes[0].risk_on)
        self.assertEqual(tickers, ["600519", "000333"])
        self.assertAlmostEqual(sum(item.weight for item in snapshot.picks), 0.5, places=3)

    def test_build_trend_snapshot_stays_in_cash_when_benchmark_is_weak(self) -> None:
        weak_histories = dict(self.histories)
        weak_histories["000300.SS"] = build_series(200.0, -0.30)
        snapshot = trend_strategy.build_trend_snapshot(
            self.universe_path,
            "A股",
            top_n=2,
            history_fetcher=weak_histories.__getitem__,
        )
        self.assertFalse(snapshot.regimes[0].risk_on)
        self.assertEqual(len(snapshot.picks), 0)
        self.assertAlmostEqual(snapshot.cash_weight, 1.0, places=3)

    def test_backtest_report_is_positive_for_rising_trend_names(self) -> None:
        with mock.patch("quant_wechat_bot.trend_strategy.market_close_digest.load_settings", return_value={}):
            report = trend_strategy.backtest_trend_strategy(
                self.universe_path,
                "A股",
                lookback_months=6,
                top_n=2,
                history_fetcher=self.histories.__getitem__,
            )
        self.assertGreater(report.total_return, 0)
        self.assertGreater(report.gross_total_return, report.total_return)
        self.assertGreater(report.total_cost_drag, 0)
        self.assertGreater(report.win_rate, 0.5)
        self.assertTrue(report.periods)
        self.assertTrue(report.latest_snapshot.picks)
        self.assertGreater(report.average_cost_drag, 0)
        self.assertTrue(report.periods[-1].contributions)
        self.assertTrue(report.current_sector_exposures)
        self.assertTrue(report.daily_curve)
        self.assertIsNotNone(report.best_day)
        self.assertIsNotNone(report.worst_day)
        self.assertGreaterEqual(report.average_invested_weight, 0)
        self.assertIsInstance(report.exit_reason_counts, tuple)

    def test_backtest_cost_model_can_be_overridden_from_settings(self) -> None:
        with mock.patch(
            "quant_wechat_bot.trend_strategy.market_close_digest.load_settings",
            return_value={
                "trend_backtest_costs": {
                    "commission_bps": 1.5,
                    "slippage_bps": 4.0,
                    "sell_tax_bps": {"CN": 12.0},
                }
            },
        ):
            cost_model = trend_strategy.load_backtest_cost_model()
        self.assertEqual(cost_model.commission_bps, 1.5)
        self.assertEqual(cost_model.slippage_bps, 4.0)
        self.assertEqual(cost_model.sell_tax_bps["CN"], 12.0)

    def test_exit_rules_can_be_overridden_from_settings(self) -> None:
        with mock.patch(
            "quant_wechat_bot.trend_strategy.market_close_digest.load_settings",
            return_value={
                "trend_exit_rules": {
                    "stop_loss_pct": 0.08,
                    "trailing_stop_pct": 0.10,
                    "trend_break_window": 10,
                }
            },
        ):
            exit_rules = trend_strategy.load_exit_rules()
        self.assertAlmostEqual(exit_rules.stop_loss_pct, 0.08)
        self.assertAlmostEqual(exit_rules.trailing_stop_pct, 0.10)
        self.assertEqual(exit_rules.trend_break_window, 10)

    def test_build_trend_snapshot_enforces_sector_weight_cap(self) -> None:
        sector_path = Path(self.temp_dir.name) / "sector_cap.csv"
        write_universe_csv(
            sector_path,
            [
                build_row("600519", "Alpha", "科技", 120.0, 180.0),
                build_row("000333", "Bravo", "科技", 80.0, 150.0),
                build_row("300750", "Charlie", "工业", 60.0, 130.0),
            ],
        )
        histories = {
            "000300.SS": build_series(100.0, 0.25),
            "600519.SS": build_series(50.0, 0.65),
            "000333.SZ": build_series(48.0, 0.63),
            "300750.SZ": build_series(45.0, 0.55),
        }
        with mock.patch(
            "quant_wechat_bot.trend_strategy.market_close_digest.load_settings",
            return_value={
                "trend_portfolio_constraints": {
                    "max_position_weight": 0.25,
                    "max_sector_positions": 2,
                    "max_sector_weight": 0.35,
                    "target_gross_exposure": 0.75,
                }
            },
        ):
            snapshot = trend_strategy.build_trend_snapshot(
                sector_path,
                "A股",
                top_n=3,
                history_fetcher=histories.__getitem__,
            )
        tech_weight = sum(item.weight for item in snapshot.picks if item.sector == "科技")
        self.assertAlmostEqual(tech_weight, 0.35, places=3)
        self.assertAlmostEqual(snapshot.invested_weight, 0.60, places=3)
        self.assertEqual(snapshot.constraint_diagnostics.partial_weight_positions, 1)

    def test_build_trend_snapshot_enforces_market_budget_for_global_market(self) -> None:
        global_path = Path(self.temp_dir.name) / "global.csv"
        write_universe_csv(
            global_path,
            [
                build_row("600519", "Kweichow", "消费", 120.0, 180.0),
                build_row("00700.HK", "Tencent", "互联网", 380.0, 350.0),
                build_row("NVDA", "Nvidia", "半导体", 900.0, 1200.0),
                build_row("MSFT", "Microsoft", "软件", 430.0, 1100.0),
                build_row("AAPL", "Apple", "硬件", 210.0, 1000.0),
                build_row("AMZN", "Amazon", "电商", 180.0, 950.0),
            ],
        )
        histories = {
            "000300.SS": build_series(100.0, 0.25),
            "^HSI": build_series(18000.0, 15.0),
            "SPY": build_series(400.0, 0.8),
            "600519.SS": build_series(50.0, 0.65),
            "00700.HK": build_series(200.0, 0.60),
            "NVDA": build_series(300.0, 2.10),
            "MSFT": build_series(250.0, 1.80),
            "AAPL": build_series(180.0, 1.60),
            "AMZN": build_series(160.0, 1.40),
        }
        with mock.patch(
            "quant_wechat_bot.trend_strategy.market_close_digest.load_settings",
            return_value={
                "trend_portfolio_constraints": {
                    "max_position_weight": 0.25,
                    "max_sector_positions": 5,
                    "max_sector_weight": 1.0,
                    "target_gross_exposure": 1.0,
                    "market_weight_budget": {
                        "CN": 0.2,
                        "HK": 0.2,
                        "US": 0.6,
                    },
                }
            },
        ):
            snapshot = trend_strategy.build_trend_snapshot(
                global_path,
                "全市场",
                top_n=5,
                history_fetcher=histories.__getitem__,
            )
        exposure_map = {item.market_code: item for item in snapshot.market_exposures}
        self.assertAlmostEqual(exposure_map["CN"].actual_weight, 0.2, places=3)
        self.assertAlmostEqual(exposure_map["HK"].actual_weight, 0.2, places=3)
        self.assertAlmostEqual(exposure_map["US"].actual_weight, 0.6, places=3)
        self.assertEqual(exposure_map["US"].count, 3)
        self.assertGreater(snapshot.constraint_diagnostics.skipped_market_budget_limit, 0)
        self.assertAlmostEqual(snapshot.invested_weight, 1.0, places=3)

    def test_transaction_cost_drag_uses_market_specific_sell_tax(self) -> None:
        cost_model = trend_strategy.BacktestCostModel(
            commission_bps=2.0,
            slippage_bps=8.0,
            sell_tax_bps={"CN": 10.0, "US": 0.0},
        )
        previous = (
            trend_strategy.TrendPick("600519", "600519.SS", "Alpha", "消费", "A股", 100, 1, 0.20, 0, 0, 0, 0, 0, 10),
            trend_strategy.TrendPick("NVDA", "NVDA", "Nvidia", "半导体", "美股", 100, 1, 0.20, 0, 0, 0, 0, 0, 10),
        )
        buy_turnover, sell_turnover, cost_drag = trend_strategy.transaction_cost_drag(previous, tuple(), cost_model)
        self.assertAlmostEqual(buy_turnover, 0.0, places=4)
        self.assertAlmostEqual(sell_turnover, 0.4, places=4)
        self.assertAlmostEqual(cost_drag, 0.0006, places=6)

    def test_simulate_period_portfolio_moves_to_cash_after_stop_loss(self) -> None:
        base = dt.date(2025, 1, 1)
        history = build_custom_series([100.0, 104.0, 109.0, 88.0, 87.0], start=base)
        holding = trend_strategy.TrendPick(
            "600519",
            "600519.SS",
            "Alpha",
            "消费",
            "A股",
            100.0,
            10.0,
            0.50,
            0,
            0,
            0,
            0,
            0,
            100.0,
        )
        gross_return, contributions, exit_events, daily_path, average_invested_weight = trend_strategy.simulate_period_portfolio(
            (holding,),
            {"600519.SS": history},
            base,
            base + dt.timedelta(days=4),
            [item[0] for item in history],
            trend_strategy.TrendExitRules(stop_loss_pct=0.10, trailing_stop_pct=0.30, trend_break_window=0),
        )
        self.assertAlmostEqual(gross_return, -0.06, places=4)
        self.assertEqual(len(exit_events), 1)
        self.assertEqual(exit_events[0].reason, "止损")
        self.assertAlmostEqual(contributions[0].contribution, -0.06, places=4)
        self.assertAlmostEqual(daily_path[-1].gross_value, 0.94, places=4)
        self.assertLess(average_invested_weight, 0.5)

    def test_build_trade_plan_marks_stop_loss_sell_reason(self) -> None:
        base = dt.date(2025, 1, 1)
        history = build_custom_series([100.0, 103.0, 108.0, 88.0, 87.0], start=base)
        previous = (
            trend_strategy.TrendPick(
                "600519",
                "600519.SS",
                "Alpha",
                "消费",
                "A股",
                100.0,
                10.0,
                0.25,
                0,
                0,
                0,
                0,
                0,
                100.0,
            ),
        )
        current: tuple[trend_strategy.TrendPick, ...] = tuple()
        current_regimes = {
            "CN": trend_strategy.RegimeSnapshot(
                market_code="CN",
                market_label="A股",
                benchmark_symbol="000300.SS",
                benchmark_name="沪深300",
                as_of=base + dt.timedelta(days=4),
                close=100.0,
                ma20=99.0,
                ma60=98.0,
                ma120=97.0,
                ret60=1.0,
                risk_on=True,
                signals_on=3,
            )
        }
        plan = trend_strategy.build_trade_plan(
            previous,
            current,
            current_regimes,
            {"600519.SS": history},
            [item[0] for item in history],
            base,
            base + dt.timedelta(days=4),
            trend_strategy.TrendExitRules(stop_loss_pct=0.10, trailing_stop_pct=0.30, trend_break_window=0),
        )
        self.assertEqual(plan[0].action, "卖出")
        self.assertEqual(plan[0].reason, "止损")

    def test_sector_exposure_summary_groups_weights(self) -> None:
        picks = (
            trend_strategy.TrendPick("A", "A.SS", "A", "科技", "A股", 100, 1, 0.25, 0, 0, 0, 0, 0, 10),
            trend_strategy.TrendPick("B", "B.SS", "B", "科技", "A股", 100, 1, 0.25, 0, 0, 0, 0, 0, 10),
            trend_strategy.TrendPick("C", "C.SS", "C", "消费", "A股", 100, 1, 0.20, 0, 0, 0, 0, 0, 10),
        )
        exposures = trend_strategy.summarize_sector_exposures(picks)
        self.assertEqual(exposures[0].sector, "科技")
        self.assertAlmostEqual(exposures[0].weight, 0.5)
        self.assertEqual(exposures[0].count, 2)


if __name__ == "__main__":
    unittest.main()
