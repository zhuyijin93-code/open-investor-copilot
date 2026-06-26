from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path
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
            macro_opportunity.MacroAssetSpec("DXY", "美元指数", "美元流动性", "GLOBAL", "macro", "dollar"),
        )
        with mock.patch("quant_wechat_bot.macro_opportunity.MACRO_ASSET_SPECS", specs):
            assets = macro_opportunity.selected_macro_assets("A股")
        self.assertEqual(len(assets), 2)
        self.assertEqual(assets[0].market_code, "CN")
        self.assertEqual(assets[1].market_code, "GLOBAL")

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

    def test_ranked_macro_stock_candidates_links_theme_to_universe_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "universe.csv"
            path.write_text(
                "\n".join(
                    [
                        "ticker,name,sector,price,market_cap_b,pe,pb,roe,revenue_growth,momentum_20d,momentum_60d,volatility_20d,dividend_yield",
                        "MSFT,Microsoft,Software,500,3000,30,8,0.2,0.1,8,15,20,0.8",
                        "NVDA,Nvidia,Semiconductor,900,3500,40,15,0.3,0.2,12,25,30,0.1",
                        "XOM,Exxon,Energy,100,450,15,2,0.1,0.05,3,6,18,3.5",
                    ]
                ),
                encoding="utf-8",
            )
            opportunity = macro_opportunity.MacroOpportunity(
                spec=macro_opportunity.MacroAssetSpec("QQQ", "纳指100", "美股科技", "US", "risk", "risk"),
                session_date=dt.date(2025, 2, 1),
                close=100.0,
                day_change=1.0,
                ret5=2.0,
                ret20=8.0,
                ma20=95.0,
                pct_from_ma20=5.0,
                score=10.0,
                setup="趋势观察",
                note="科技维持强势",
            )
            candidates = macro_opportunity.ranked_macro_stock_candidates(path, opportunity, top_n=2)
        self.assertTrue(candidates)
        self.assertEqual(candidates[0].ticker, "NVDA")
        self.assertIn(candidates[1].ticker, {"MSFT", "NVDA"})

    def test_format_macro_scan_includes_dollar_yield_and_vix_signals(self) -> None:
        specs = (
            macro_opportunity.MacroAssetSpec("SPY", "标普500", "美股宽基", "US", "risk", "risk"),
            macro_opportunity.MacroAssetSpec("DXY", "美元指数", "美元流动性", "GLOBAL", "macro", "dollar"),
            macro_opportunity.MacroAssetSpec("TNX", "10Y美债利率", "全球利率", "GLOBAL", "macro", "yield"),
            macro_opportunity.MacroAssetSpec("VIX", "VIX波动率", "风险情绪", "GLOBAL", "macro", "volatility"),
        )
        histories = {
            "SPY": build_series(500.0, 1.0),
            "DXY": build_series(100.0, -0.5),
            "TNX": build_series(4.8, -0.03),
            "VIX": build_series(20.0, -0.2),
        }
        with mock.patch("quant_wechat_bot.macro_opportunity.MACRO_ASSET_SPECS", specs):
            text = macro_opportunity.format_macro_scan("全市场", history_fetcher=histories.__getitem__)
        self.assertIn("关键宏观变量", text)
        self.assertIn("美元指数", text)
        self.assertIn("10Y美债利率", text)
        self.assertIn("VIX波动率", text)
        self.assertIn("流动性顺风", text)
        self.assertIn("利率回落", text)
        self.assertIn("波动降温", text)

    def test_format_macro_scan_includes_linked_stock_baskets(self) -> None:
        specs = (
            macro_opportunity.MacroAssetSpec("QQQ", "纳指100", "美股科技", "US", "risk", "risk"),
            macro_opportunity.MacroAssetSpec("DXY", "美元指数", "美元流动性", "GLOBAL", "macro", "dollar"),
        )
        histories = {
            "QQQ": build_series(400.0, 2.0),
            "DXY": build_series(100.0, -0.5),
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "universe.csv"
            path.write_text(
                "\n".join(
                    [
                        "ticker,name,sector,price,market_cap_b,pe,pb,roe,revenue_growth,momentum_20d,momentum_60d,volatility_20d,dividend_yield",
                        "NVDA,Nvidia,Semiconductor,900,3500,40,15,0.3,0.2,12,25,30,0.1",
                        "MSFT,Microsoft,Software,500,3000,30,8,0.2,0.1,8,15,20,0.8",
                    ]
                ),
                encoding="utf-8",
            )
            with mock.patch("quant_wechat_bot.macro_opportunity.MACRO_ASSET_SPECS", specs):
                text = macro_opportunity.format_macro_scan("全市场", history_fetcher=histories.__getitem__, universe_path=path)
        self.assertIn("主线联动个股", text)
        self.assertIn("NVDA", text)
        self.assertIn("MSFT", text)


if __name__ == "__main__":
    unittest.main()
