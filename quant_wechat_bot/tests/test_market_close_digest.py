from __future__ import annotations

import datetime as dt
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from quant_wechat_bot import market_close_digest


class MarketCloseDigestTests(unittest.TestCase):
    def test_merge_watchlist_uses_defaults_and_matching_user_symbols(self) -> None:
        config = market_close_digest.normalize_market("A股")
        watchlist = market_close_digest.merge_watchlist(
            config,
            {
                "watchlist": ["NVDA", "0700.HK", "600036.SS"],
                "close_digest_watchlists": {"A股": ["300750.SZ"]},
            },
        )
        symbols = [symbol for symbol, _ in watchlist]
        self.assertIn("600036.SS", symbols)
        self.assertIn("300750.SZ", symbols)
        self.assertNotIn("NVDA", symbols)

    def test_build_digest_formats_primary_style_and_watch_lines(self) -> None:
        config = market_close_digest.normalize_market("US")
        fake_quotes = {
            "^GSPC": {"symbol": "^GSPC", "regularMarketChangePercent": 0.6, "regularMarketTime": 1718236800},
            "^IXIC": {"symbol": "^IXIC", "regularMarketChangePercent": 1.2, "regularMarketTime": 1718236800},
            "^DJI": {"symbol": "^DJI", "regularMarketChangePercent": -0.1, "regularMarketTime": 1718236800},
            "IWM": {"symbol": "IWM", "regularMarketChangePercent": 0.9, "regularMarketTime": 1718236800},
            "NVDA": {"symbol": "NVDA", "regularMarketChangePercent": 3.1, "regularMarketTime": 1718236800},
            "MSFT": {"symbol": "MSFT", "regularMarketChangePercent": -1.4, "regularMarketTime": 1718236800},
        }
        with (
            mock.patch("quant_wechat_bot.market_close_digest.fetch_quotes", return_value=fake_quotes),
            mock.patch("quant_wechat_bot.market_close_digest.quant_signal_line", return_value=None),
        ):
            text, session_date = market_close_digest.build_digest(config, {"close_digest_watchlists": {"US": ["NVDA", "MSFT"]}})
        self.assertEqual(session_date.isoformat(), "2024-06-12")
        self.assertIn("【美股收盘总结｜2024-06-12】", text)
        self.assertIn("指数: 标普500 +0.60% | 纳指100 +1.20% | 道指 -0.10% | 罗素2000 +0.90%", text)
        self.assertIn("风格: 纳指100相对更强", text)
        self.assertIn("关注走强: 英伟达 +3.10%", text)
        self.assertIn("关注承压: 微软 -1.40%", text)

    def test_fetch_symbol_quote_falls_back_to_yahoo(self) -> None:
        payload = {
            "chart": {
                "result": [
                    {
                        "meta": {
                            "longName": "NVIDIA Corporation",
                            "exchangeTimezoneName": "America/New_York",
                        },
                        "timestamp": [1718064000, 1718150400],
                        "indicators": {"quote": [{"close": [120.0, 126.0]}]},
                    }
                ],
                "error": None,
            }
        }
        with (
            mock.patch(
                "quant_wechat_bot.market_close_digest.fetch_eastmoney_symbol_quote",
                side_effect=RuntimeError("eastmoney down"),
            ),
            mock.patch("quant_wechat_bot.market_close_digest.yahoo_get_json", return_value=payload),
        ):
            quote = market_close_digest.fetch_symbol_quote("NVDA")
        self.assertEqual(quote["symbol"], "NVDA")
        self.assertEqual(quote["name"], "NVIDIA Corporation")
        self.assertAlmostEqual(quote["change_pct"], 5.0)

    def test_yahoo_symbol_mapping_preserves_digest_index_meaning(self) -> None:
        self.assertEqual(market_close_digest.yahoo_chart_symbol("^IXIC"), "^NDX")
        self.assertEqual(market_close_digest.yahoo_chart_symbol("0700.HK"), "0700.HK")
        self.assertEqual(market_close_digest.yahoo_chart_symbol("NVDA"), "NVDA")

    def test_describe_close_timing_rejects_stale_session_after_close(self) -> None:
        config = market_close_digest.normalize_market("A股")
        fake_now = dt.datetime(2026, 6, 10, 15, 10, tzinfo=dt.timezone.utc).astimezone(ZoneInfo("Asia/Shanghai"))
        with mock.patch("quant_wechat_bot.market_close_digest.market_now", return_value=fake_now):
            ready, reason = market_close_digest.describe_close_timing(config, dt.date(2026, 6, 9))
        self.assertFalse(ready)
        self.assertIn("最新交易日仍是 2026-06-09", reason)

    def test_mark_sent_and_already_sent_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            session_date = dt.date(2026, 6, 10)
            self.assertFalse(market_close_digest.already_sent(state_path, "US", session_date))
            market_close_digest.mark_sent(state_path, "US", session_date, "digest")
            self.assertTrue(market_close_digest.already_sent(state_path, "US", session_date))

    def test_format_wechat_send_error_explains_expired_context_token(self) -> None:
        message = market_close_digest.format_wechat_send_error('Error: sendmessage returned ret=-2: {"ret":-2}')
        self.assertIn("context_token is likely expired", message)
        self.assertIn("Send any message to the personal WeChat bot", message)

    def test_send_wechat_message_wraps_sender_failure(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["node"],
            returncode=1,
            stdout="",
            stderr='Error: sendmessage returned ret=-2: {"ret":-2}\n',
        )
        with mock.patch("quant_wechat_bot.market_close_digest.subprocess.run", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "context_token is likely expired"):
                market_close_digest.send_wechat_message("digest", dry_run=False)


if __name__ == "__main__":
    unittest.main()
