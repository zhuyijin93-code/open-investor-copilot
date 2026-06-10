from __future__ import annotations

import unittest
from unittest import mock

import bot_service


class BotServiceTests(unittest.TestCase):
    def test_help_shortcut_returns_help(self) -> None:
        reply = bot_service.handle_message("帮助")
        self.assertEqual(reply.command, "help")
        self.assertIn("Open Investor Copilot", reply.text)

    def test_watch_keyword_routes_to_filing_preview(self) -> None:
        with mock.patch("bot_service.build_filing_preview", return_value="filing output") as build_preview:
            reply = bot_service.handle_message("巴菲特最新披露")
        build_preview.assert_called_once_with("buffett")
        self.assertEqual(reply.command, "filings")
        self.assertEqual(reply.text, "filing output")

    def test_market_shortcut_routes_to_free_market_brief(self) -> None:
        with mock.patch("bot_service.build_free_market_text", return_value="market output") as build_market:
            reply = bot_service.handle_message("市场简报")
        build_market.assert_called_once_with()
        self.assertEqual(reply.command, "market")
        self.assertEqual(reply.text, "market output")

    def test_unknown_message_falls_back_to_finchat(self) -> None:
        with mock.patch("bot_service.build_finchat_answer", return_value="answer") as build_answer:
            reply = bot_service.handle_message("英伟达最近的风险是什么")
        build_answer.assert_called_once_with("英伟达最近的风险是什么")
        self.assertEqual(reply.command, "ask")
        self.assertIn("I treated this as a research question.", reply.text)

    def test_verify_wechat_signature(self) -> None:
        signature = bot_service.wechat_signature("token123", "1718000000", "nonce456")
        self.assertTrue(
            bot_service.verify_wechat_signature("token123", signature, "1718000000", "nonce456")
        )
        self.assertFalse(
            bot_service.verify_wechat_signature("token123", "bad-signature", "1718000000", "nonce456")
        )

    def test_build_wechat_reply_for_text_message(self) -> None:
        with mock.patch("bot_service.handle_message", return_value=bot_service.BotReply("市场简报内容", "market")):
            reply_xml = bot_service.build_wechat_reply(
                """
                <xml>
                  <ToUserName><![CDATA[gh_test]]></ToUserName>
                  <FromUserName><![CDATA[user_openid]]></FromUserName>
                  <CreateTime>1718000000</CreateTime>
                  <MsgType><![CDATA[text]]></MsgType>
                  <Content><![CDATA[市场简报]]></Content>
                  <MsgId>1234567890</MsgId>
                </xml>
                """
            )
        assert reply_xml is not None
        self.assertIn("<ToUserName><![CDATA[user_openid]]></ToUserName>", reply_xml)
        self.assertIn("<FromUserName><![CDATA[gh_test]]></FromUserName>", reply_xml)
        self.assertIn("<Content><![CDATA[市场简报内容]]></Content>", reply_xml)

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
        self.assertIn("Open Investor Copilot", reply_xml)

    def test_wechat_click_event_uses_default_menu_mapping(self) -> None:
        with mock.patch("bot_service.handle_message", return_value=bot_service.BotReply("menu result", "market")) as handle:
            reply_xml = bot_service.build_wechat_reply(
                """
                <xml>
                  <ToUserName><![CDATA[gh_test]]></ToUserName>
                  <FromUserName><![CDATA[user_openid]]></FromUserName>
                  <CreateTime>1718000000</CreateTime>
                  <MsgType><![CDATA[event]]></MsgType>
                  <Event><![CDATA[CLICK]]></Event>
                  <EventKey><![CDATA[MENU_MARKET]]></EventKey>
                </xml>
                """
            )
        handle.assert_called_once_with("市场简报")
        assert reply_xml is not None
        self.assertIn("menu result", reply_xml)

    def test_wechat_click_event_supports_custom_mapping(self) -> None:
        with (
            mock.patch("bot_service.market_hub.load_local_settings", return_value={"wechat_menu_actions": {"menu_focus": "个股 TSLA"}}),
            mock.patch("bot_service.handle_message", return_value=bot_service.BotReply("tsla result", "stock")) as handle,
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
        handle.assert_called_once_with("个股 TSLA")
        assert reply_xml is not None
        self.assertIn("tsla result", reply_xml)

    def test_wechat_click_event_supports_prefixed_ask_shortcut(self) -> None:
        with mock.patch("bot_service.handle_message", return_value=bot_service.BotReply("ask result", "ask")) as handle:
            bot_service.dispatch_wechat_menu_event("ASK:英伟达最近的风险")
        handle.assert_called_once_with("问一下：英伟达最近的风险")


if __name__ == "__main__":
    unittest.main()
