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


class TrendStrategyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.universe_path = Path(self.temp_dir.name) / "universe.csv"
        rows = [
            {
                "ticker": "600519",
                "name": "Alpha",
                "sector": "消费",
                "price": "120.0",
                "market_cap_b": "180.0",
                "pe": "20.0",
                "pb": "4.0",
                "roe": "0.0",
                "revenue_growth": "0.0",
                "momentum_20d": "0.0",
                "momentum_60d": "0.0",
                "volatility_20d": "0.0",
                "dividend_yield": "0.0",
            },
            {
                "ticker": "000333",
                "name": "Bravo",
                "sector": "家电",
                "price": "80.0",
                "market_cap_b": "150.0",
                "pe": "18.0",
                "pb": "3.0",
                "roe": "0.0",
                "revenue_growth": "0.0",
                "momentum_20d": "0.0",
                "momentum_60d": "0.0",
                "volatility_20d": "0.0",
                "dividend_yield": "0.0",
            },
            {
                "ticker": "300750",
                "name": "Charlie",
                "sector": "新能源",
                "price": "45.0",
                "market_cap_b": "130.0",
                "pe": "22.0",
                "pb": "5.0",
                "roe": "0.0",
                "revenue_growth": "0.0",
                "momentum_20d": "0.0",
                "momentum_60d": "0.0",
                "volatility_20d": "0.0",
                "dividend_yield": "0.0",
            },
        ]
        with self.universe_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
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
