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
            result = recommendation_digest.build_recommendation_digest("A股", 3)
        self.assertIn("推荐日报", result.text)
        self.assertEqual(result.session_date, dt.date(2025, 1, 3))
        self.assertEqual(result.market_label, "A股")
        self.assertEqual(result.top_n, 3)

    def test_load_recommendation_config_supports_schedule_templates(self) -> None:
        with mock.patch(
            "quant_wechat_bot.recommendation_digest.market_close_digest.load_settings",
            return_value={
                "recommendation_digest": {
                    "schedule_timezone": "Asia/Shanghai",
                    "daily_recommendation": {
                        "enabled": True,
                        "market": "A股",
                        "top_n": 2,
                        "weekdays": ["mon", "fri"],
                    },
                    "weekly_review": {
                        "enabled": True,
                        "market": "全市场",
                        "top_n": 5,
                        "lookback_months": 9,
                        "weekdays": ["sat"],
                    },
                }
            },
        ):
            config = recommendation_digest.load_recommendation_config()
        self.assertEqual(config.daily_recommendation.market, "A股")
        self.assertEqual(config.daily_recommendation.top_n, 2)
        self.assertEqual(config.daily_recommendation.weekdays, (0, 4))
        self.assertEqual(config.weekly_review.lookback_months, 9)
        self.assertEqual(config.weekly_review.weekdays, (5,))

    def test_scheduled_templates_for_weekday_selects_daily_and_weekly(self) -> None:
        config = recommendation_digest.RecommendationDigestConfig(
            schedule_timezone="Asia/Shanghai",
            daily_recommendation=recommendation_digest.TemplateScheduleConfig(
                enabled=True,
                market="A股",
                top_n=3,
                lookback_months=None,
                weekdays=(0, 1, 2, 3, 4),
            ),
            weekly_review=recommendation_digest.TemplateScheduleConfig(
                enabled=True,
                market="全市场",
                top_n=5,
                lookback_months=6,
                weekdays=(5, 6),
            ),
        )
        self.assertEqual(
            recommendation_digest.scheduled_templates_for_weekday(config, 4),
            (("daily", config.daily_recommendation),),
        )
        self.assertEqual(
            recommendation_digest.scheduled_templates_for_weekday(config, 5),
            (("weekly", config.weekly_review),),
        )

    def test_build_weekly_digest_uses_backtest_report_end_date(self) -> None:
        snapshot = trend_strategy.TrendSnapshot(
            market_label="A股",
            as_of=dt.date(2025, 1, 4),
            candidate_count=3,
            evaluated_count=2,
            history_failures=0,
            invested_weight=0.5,
            cash_weight=0.5,
            regimes=tuple(),
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
        report = trend_strategy.BacktestReport(
            market_label="A股",
            start_date=dt.date(2024, 7, 1),
            end_date=dt.date(2025, 1, 3),
            candidate_count=30,
            evaluated_count=20,
            history_failures=0,
            gross_total_return=0.10,
            total_cost_drag=0.01,
            total_return=0.09,
            annualized_return=0.18,
            max_drawdown=-0.06,
            win_rate=0.6,
            average_invested_weight=0.7,
            average_cost_drag=0.003,
            average_period_return=0.02,
            average_turnover=0.15,
            cost_model=trend_strategy.BacktestCostModel(commission_bps=2.0, slippage_bps=8.0, sell_tax_bps={"CN": 10.0}),
            current_sector_exposures=tuple(),
            daily_curve=tuple(),
            best_day=None,
            worst_day=None,
            exit_reason_counts=tuple(),
            periods=tuple(),
            latest_snapshot=snapshot,
        )
        with (
            mock.patch(
                "quant_wechat_bot.recommendation_digest.bot_service.resolve_trend_universe_path",
                return_value=Path("/tmp/universe.csv"),
            ),
            mock.patch(
                "quant_wechat_bot.recommendation_digest.trend_strategy.backtest_trend_strategy",
                return_value=report,
            ),
        ):
            result = recommendation_digest.build_recommendation_digest("A股", 5, template="weekly", lookback_months=6)
        self.assertIn("周复盘", result.text)
        self.assertEqual(result.session_date, dt.date(2025, 1, 3))
        self.assertEqual(result.lookback_months, 6)


if __name__ == "__main__":
    unittest.main()
