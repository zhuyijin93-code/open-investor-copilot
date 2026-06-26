from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from quant_wechat_bot import recommendation_digest, trend_strategy


class RecommendationDigestTests(unittest.TestCase):
    def test_mark_sent_and_already_sent_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            session_date = dt.date(2026, 6, 26)
            self.assertFalse(recommendation_digest.already_sent(state_path, "全市场", 3, session_date))
            recommendation_digest.mark_sent(state_path, "全市场", 3, session_date, "digest")
            self.assertTrue(recommendation_digest.already_sent(state_path, "全市场", 3, session_date))

    def test_build_recommendation_digest_uses_snapshot_session_date(self) -> None:
        snapshot = trend_strategy.TrendSnapshot(
            market_label="A股",
            as_of=dt.date(2025, 1, 4),
            candidate_count=3,
            evaluated_count=2,
            history_failures=0,
            invested_weight=0.5,
            cash_weight=0.5,
            regimes=(
                trend_strategy.RegimeSnapshot(
                    market_code="CN",
                    market_label="A股",
                    benchmark_symbol="000300.SS",
                    benchmark_name="沪深300",
                    as_of=dt.date(2025, 1, 3),
                    close=100.0,
                    ma20=98.0,
                    ma60=95.0,
                    ma120=90.0,
                    ret60=12.0,
                    risk_on=True,
                    signals_on=3,
                ),
            ),
            constraints=trend_strategy.PortfolioConstraints(
                max_position_weight=0.25,
                max_sector_positions=2,
                max_sector_weight=0.35,
                target_gross_exposure=1.0,
                market_weight_budget={"CN": 1.0},
            ),
            exit_rules=trend_strategy.TrendExitRules(stop_loss_pct=0.12, trailing_stop_pct=0.15, trend_break_window=20),
            execution_rules=trend_strategy.TrendExecutionRules(
                entry_starter_fraction=0.6,
                min_add_on_trigger_pct=0.03,
                max_add_on_trigger_pct=0.08,
            ),
            order_sizing_rules=trend_strategy.OrderSizingRules(
                market_capital={},
                lot_size_by_market={"CN": 100},
                lot_size_by_ticker={},
                currency_by_market={"CN": "CNY"},
            ),
            market_exposures=tuple(),
            constraint_diagnostics=trend_strategy.ConstraintDiagnostics(),
            previous_rebalance_date=None,
            trade_plan=tuple(),
            execution_plan=tuple(),
            picks=tuple(),
        )
        with (
            mock.patch(
                "quant_wechat_bot.recommendation_digest.bot_service.resolve_trend_universe_path",
                return_value=Path("/tmp/universe.csv"),
            ),
            mock.patch(
                "quant_wechat_bot.recommendation_digest.trend_strategy.build_trend_snapshot",
                return_value=snapshot,
            ),
        ):
            digest, session_date, market_label, top_n = recommendation_digest.build_recommendation_digest("A股", 3)
        self.assertIn("推荐日报", digest)
        self.assertEqual(session_date, dt.date(2025, 1, 3))
        self.assertEqual(market_label, "A股")
        self.assertEqual(top_n, 3)


if __name__ == "__main__":
    unittest.main()
