from __future__ import annotations

import datetime as dt
import unittest
from unittest import mock

from quant_wechat_bot import macro_opportunity


def build_series(start_price: float, slope: float, days: int = 40) -> list[tuple[dt.date, float]]:
    base = dt.date(2025, 1, 1)
    return [(base + dt.timedelta(days=index), start_price + slope * index) for index in range(days)]


class MacroOpportunityTests(unittest.TestCase):
    def test_format_macro_scan_highlights_leaders_and_weakness(self) -> None:
        specs = (
            macro_opportunity.MacroAssetSpec("CN1", "创业板", "A股成长", "CN", "risk"),
            macro_opportunity.MacroAssetSpec("HK1", "恒指", "港股宽基", "HK", "risk"),
            macro_opportunity.MacroAssetSpec("GLD", "黄金ETF", "黄金避险", "US", "defensive"),
        )
        histories = {
            "CN1": build_series(100.0, 2.0),
            "HK1": build_series(200.0, -1.5),
            "GLD": build_series(150.0, 0.8),
        }
        with mock.patch("quant_wechat_bot.macro_opportunity.MACRO_ASSET_SPECS", specs):
            text = macro_opportunity.format_macro_scan("全市场", history_fetcher=histories.__getitem__)
        self.assertIn("宏观机会扫描", text)
        self.assertIn("风险偏好", text)
        self.assertIn("可关注机会", text)
        self.assertIn("创业板", text)
        self.assertIn("黄金ETF", text)
        self.assertIn("降温/回避", text)
        self.assertIn("恒指", text)

    def test_selected_macro_assets_supports_market_filter(self) -> None:
        specs = (
            macro_opportunity.MacroAssetSpec("CN1", "创业板", "A股成长", "CN", "risk"),
            macro_opportunity.MacroAssetSpec("US1", "纳指100", "美股科技", "US", "risk"),
        )
        with mock.patch("quant_wechat_bot.macro_opportunity.MACRO_ASSET_SPECS", specs):
            assets = macro_opportunity.selected_macro_assets("A股")
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0].market_code, "CN")

    def test_build_macro_opportunities_skips_failed_symbols(self) -> None:
        specs = (
            macro_opportunity.MacroAssetSpec("CN1", "创业板", "A股成长", "CN", "risk"),
            macro_opportunity.MacroAssetSpec("MISS", "缺失资产", "测试", "US", "risk"),
        )
        histories = {
            "CN1": build_series(100.0, 1.0),
        }

        def fetcher(symbol: str) -> list[tuple[dt.date, float]]:
            if symbol not in histories:
                raise RuntimeError("missing")
            return histories[symbol]

        with mock.patch("quant_wechat_bot.macro_opportunity.MACRO_ASSET_SPECS", specs):
            opportunities, failures = macro_opportunity.build_macro_opportunities("全市场", history_fetcher=fetcher)
        self.assertEqual(len(opportunities), 1)
        self.assertEqual(opportunities[0].spec.label, "创业板")
        self.assertEqual(len(failures), 1)


if __name__ == "__main__":
    unittest.main()
