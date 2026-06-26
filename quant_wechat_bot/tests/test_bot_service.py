from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from quant_wechat_bot import bot_service, quant_engine


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_CSV = PROJECT_ROOT / "sample_universe.csv"


class QuantEngineTests(unittest.TestCase):
    def test_resolve_strategy_supports_chinese_alias(self) -> None:
        strategy = quant_engine.resolve_strategy("质量")
        self.assertEqual(strategy.key, "quality")

    def test_screen_output_contains_ranked_stock(self) -> None:
        output = quant_engine.format_screen_output(SAMPLE_CSV, "momentum", top_n=3)
        self.assertIn("Top Picks", output)
        self.assertIn("NVDA", output)

    def test_stock_report_shows_multi_strategy_scores(self) -> None:
        output = quant_engine.format_stock_report(SAMPLE_CSV, "MSFT")
        self.assertIn("多策略评分", output)
        self.assertIn("质量动量", output)

    def test_stock_report_accepts_hk_ticker_alias(self) -> None:
        hk_row = {
            "ticker": "00700.HK",
            "name": "腾讯控股",
            "sector": "软件服务",
            "price": 380.0,
            "market_cap_b": 3500.0,
            "pe": 18.0,
            "pb": 3.5,
            "roe": 0.0,
            "revenue_growth": 0.0,
            "momentum_20d": 8.0,
            "momentum_60d": 12.0,
            "volatility_20d": 9.0,
            "dividend_yield": 0.0,
        }
        with mock.patch("quant_wechat_bot.quant_engine.load_universe", return_value=[hk_row]):
            output = quant_engine.format_stock_report("ignored.csv", "700.HK", "港股")
        self.assertIn("00700.HK", output)
        self.assertIn("腾讯控股", output)


class BotServiceTests(unittest.TestCase):
    def test_bind_defaults_to_localhost_without_cloud_port(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(bot_service.resolve_bind_host(), "127.0.0.1")
            self.assertEqual(bot_service.resolve_bind_port(), 8790)

    def test_bind_defaults_to_public_host_when_port_is_provided(self) -> None:
        with mock.patch.dict("os.environ", {"PORT": "10080"}, clear=True):
            self.assertEqual(bot_service.resolve_bind_host(), "0.0.0.0")
            self.assertEqual(bot_service.resolve_bind_port(), 10080)

    def test_help_shortcut_returns_help(self) -> None:
        reply = bot_service.handle_message("帮助")
        self.assertEqual(reply.command, "help")
        self.assertIn("Quant WeChat Bot", reply.text)

    def test_pick_quality_routes_to_screen(self) -> None:
        with mock.patch("quant_wechat_bot.bot_service.build_screen_text", return_value="screen output") as build_screen:
            reply = bot_service.handle_message("选股 质量")
        build_screen.assert_called_once_with("质量", top_n=5, market=None)
        self.assertEqual(reply.command, "pick")
        self.assertEqual(reply.text, "screen output")

    def test_score_routes_to_stock_report(self) -> None:
        with mock.patch("quant_wechat_bot.bot_service.build_stock_report_text", return_value="score output") as build_score:
            reply = bot_service.handle_message("评分 NVDA")
        build_score.assert_called_once_with("NVDA", market=None)
        self.assertEqual(reply.command, "score")
        self.assertEqual(reply.text, "score output")

    def test_pick_quality_a_share_routes_to_screen(self) -> None:
        with mock.patch("quant_wechat_bot.bot_service.build_screen_text", return_value="a output") as build_screen:
            reply = bot_service.handle_message("选股 质量 A股")
        build_screen.assert_called_once_with("质量", top_n=5, market="A股")
        self.assertEqual(reply.command, "pick")
        self.assertEqual(reply.text, "a output")

    def test_close_digest_routes_to_market_summary(self) -> None:
        with mock.patch(
            "quant_wechat_bot.bot_service.build_close_digest_text",
            return_value="digest output",
        ) as build_digest:
            reply = bot_service.handle_message("收盘总结 美股")
        build_digest.assert_called_once_with("美股")
        self.assertEqual(reply.command, "close")
        self.assertEqual(reply.text, "digest output")

    def test_default_market_is_used_when_configured(self) -> None:
        with (
            mock.patch("quant_wechat_bot.bot_service.load_local_settings", return_value={"default_market": "A股"}),
            mock.patch("quant_wechat_bot.bot_service.quant_engine.format_screen_output", return_value="ok") as render,
            mock.patch("quant_wechat_bot.bot_service.data_sources.refresh_a_share_universe", return_value=PROJECT_ROOT / ".cache" / "a.csv"),
        ):
            reply = bot_service.build_screen_text("quality")
        self.assertEqual(reply, "ok")
        render.assert_called_once()

    def test_default_market_prefers_environment_variable(self) -> None:
        with mock.patch.dict("os.environ", {"QUANT_WECHAT_DEFAULT_MARKET": "A股"}, clear=True):
            self.assertEqual(bot_service.resolve_default_market(), "A股")

    def test_render_deployment_defaults_to_global_market(self) -> None:
        with (
            mock.patch.dict("os.environ", {"RENDER_SERVICE_ID": "srv-test"}, clear=True),
            mock.patch("quant_wechat_bot.bot_service.load_local_settings", return_value={}),
        ):
            self.assertEqual(bot_service.resolve_default_market(), "全市场")

    def test_render_deployment_prefers_bundled_global_snapshot(self) -> None:
        bundled = PROJECT_ROOT / "universe_snapshots" / "global_snapshot.csv"
        with (
            mock.patch.dict("os.environ", {"RENDER_SERVICE_ID": "srv-test"}, clear=True),
            mock.patch("quant_wechat_bot.bot_service.load_local_settings", return_value={}),
            mock.patch("quant_wechat_bot.bot_service.resolve_bundled_universe_path", return_value=bundled),
            mock.patch("quant_wechat_bot.bot_service.data_sources.refresh_global_universe") as refresh,
        ):
            path = bot_service.resolve_universe_path("全市场")
        self.assertEqual(path, bundled)
        refresh.assert_not_called()

    def test_resolve_universe_path_supports_hk_market(self) -> None:
        with (
            mock.patch("quant_wechat_bot.bot_service.prefer_bundled_universes", return_value=False),
            mock.patch("quant_wechat_bot.bot_service.load_local_settings", return_value={}),
            mock.patch("quant_wechat_bot.bot_service.data_sources.refresh_hk_share_universe", return_value=PROJECT_ROOT / ".cache" / "hk.csv") as refresh,
        ):
            path = bot_service.resolve_universe_path("港股")
        self.assertEqual(path, PROJECT_ROOT / ".cache" / "hk.csv")
        refresh.assert_called_once()

    def test_resolve_universe_path_supports_global_market(self) -> None:
        with (
            mock.patch("quant_wechat_bot.bot_service.prefer_bundled_universes", return_value=False),
            mock.patch("quant_wechat_bot.bot_service.load_local_settings", return_value={}),
            mock.patch("quant_wechat_bot.bot_service.data_sources.refresh_global_universe", return_value=PROJECT_ROOT / ".cache" / "global.csv") as refresh,
        ):
            path = bot_service.resolve_universe_path("全市场")
        self.assertEqual(path, PROJECT_ROOT / ".cache" / "global.csv")
        refresh.assert_called_once()

    def test_verify_wechat_signature(self) -> None:
        signature = bot_service.wechat_signature("token123", "1718000000", "nonce456")
        self.assertTrue(
            bot_service.verify_wechat_signature("token123", signature, "1718000000", "nonce456")
        )
        self.assertFalse(
            bot_service.verify_wechat_signature("token123", "bad-signature", "1718000000", "nonce456")
        )

    def test_build_wechat_reply_for_text_message(self) -> None:
        with mock.patch(
            "quant_wechat_bot.bot_service.handle_message",
            return_value=bot_service.BotReply("选股结果", "pick"),
        ):
            reply_xml = bot_service.build_wechat_reply(
                """
                <xml>
                  <ToUserName><![CDATA[gh_test]]></ToUserName>
                  <FromUserName><![CDATA[user_openid]]></FromUserName>
                  <CreateTime>1718000000</CreateTime>
                  <MsgType><![CDATA[text]]></MsgType>
                  <Content><![CDATA[选股 质量]]></Content>
                  <MsgId>1234567890</MsgId>
                </xml>
                """
            )
        assert reply_xml is not None
        self.assertIn("<Content><![CDATA[选股结果]]></Content>", reply_xml)

    def test_build_wechat_reply_for_subscribe_event(self) -> None:
        reply_xml = bot_service.build_wechat_reply(
            """
            <xml>
              <ToUserName><![CDATA[gh_test]]></ToUserName>
              <FromUserName><![CDATA[user_openid]]></FromUserName>
              <CreateTime>1718000000</CreateTime>
              <MsgType><![CDATA[event]]></MsgType>
              <Event><![CDATA[subscribe]]></Event>
            </xml>
            """
        )
        assert reply_xml is not None
        self.assertIn("Quant WeChat Bot", reply_xml)

    def test_wechat_click_event_uses_default_menu_mapping(self) -> None:
        with (
            mock.patch("quant_wechat_bot.bot_service.load_local_settings", return_value={}),
            mock.patch(
                "quant_wechat_bot.bot_service.handle_message",
                return_value=bot_service.BotReply("menu result", "pick"),
            ) as handle,
        ):
            reply_xml = bot_service.build_wechat_reply(
                """
                <xml>
                  <ToUserName><![CDATA[gh_test]]></ToUserName>
                  <FromUserName><![CDATA[user_openid]]></FromUserName>
                  <CreateTime>1718000000</CreateTime>
                  <MsgType><![CDATA[event]]></MsgType>
                  <Event><![CDATA[CLICK]]></Event>
                  <EventKey><![CDATA[MENU_PICK_QUALITY]]></EventKey>
                </xml>
                """
            )
        handle.assert_called_once_with("选股 质量")
        assert reply_xml is not None
        self.assertIn("menu result", reply_xml)

    def test_wechat_click_event_supports_custom_mapping(self) -> None:
        with (
            mock.patch(
                "quant_wechat_bot.bot_service.load_local_settings",
                return_value={"wechat_menu_actions": {"menu_focus": "评分 MSFT"}},
            ),
            mock.patch(
                "quant_wechat_bot.bot_service.handle_message",
                return_value=bot_service.BotReply("msft result", "score"),
            ) as handle,
        ):
            reply_xml = bot_service.build_wechat_reply(
                """
                <xml>
                  <ToUserName><![CDATA[gh_test]]></ToUserName>
                  <FromUserName><![CDATA[user_openid]]></FromUserName>
                  <CreateTime>1718000000</CreateTime>
                  <MsgType><![CDATA[event]]></MsgType>
                  <Event><![CDATA[CLICK]]></Event>
                  <EventKey><![CDATA[menu_focus]]></EventKey>
                </xml>
                """
            )
        handle.assert_called_once_with("评分 MSFT")
        assert reply_xml is not None
        self.assertIn("msft result", reply_xml)

    def test_wechat_click_event_supports_stock_shortcut(self) -> None:
        with mock.patch(
            "quant_wechat_bot.bot_service.handle_message",
            return_value=bot_service.BotReply("score result", "score"),
        ) as handle:
            bot_service.dispatch_wechat_menu_event("STOCK:NVDA")
        handle.assert_called_once_with("评分 NVDA")


if __name__ == "__main__":
    unittest.main()
