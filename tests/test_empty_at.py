import asyncio
import unittest
from datetime import datetime
from unittest.mock import patch

from test_access_gate import main
from test_group_rest import GroupEvent
from test_wake import At, Context, Image, Plain, Reply


class EmptyAtTests(unittest.TestCase):
    def dispatch(self, chain, *, config=None, gate_only=False, parsed=None, hour=20):
        context = Context('{"should_reply":true}', {})
        plugin = main.AccessGatePlugin(
            context,
            {"group_whitelist": ["20001"], **(config or {})},
        )
        event = GroupEvent("")
        event.message_obj.message = chain
        event.is_wake = True
        event.is_at_or_wake_command = True
        if parsed is not None:
            event.extras["handlers_parsed_params"] = parsed

        async def run():
            await plugin.enforce_access(event)
            if not event.is_stopped() and not gate_only:
                await plugin.route_wake(event)

        now = datetime(2026, 10, 13, hour, tzinfo=main.REST_TIMEZONE)
        with patch.object(plugin, "_now", return_value=now):
            asyncio.run(run())
        return event, context

    def assert_no_agent(self, event, context):
        self.assertTrue(event.is_stopped())
        self.assertTrue(event.call_llm)
        self.assertFalse(event.is_at_or_wake_command)
        self.assertEqual(context.calls, [])

    def test_gate_alone_stops_at_without_information(self):
        event, context = self.dispatch([At()], gate_only=True)
        self.assert_no_agent(event, context)
        self.assertEqual(event.sent, ["我在，请继续说。"])

    def test_invisible_and_whitespace_only_text_is_not_information(self):
        for text in ("", " \t\n", "\u200b", "\u200d\ufeff", "\x00\u200b \n"):
            with self.subTest(text=repr(text)):
                event, context = self.dispatch([At(), Plain(text)])
                self.assert_no_agent(event, context)
                self.assertEqual(event.sent, ["我在，请继续说。"])

    def test_at_without_information_is_blocked_when_wake_is_disabled(self):
        for config in (
            {"wake_enabled": False},
            {"wake_on_at": False},
            {"wake_enabled": False, "wake_on_at": False},
        ):
            with self.subTest(config=config):
                event, context = self.dispatch([At()], config=config)
                self.assert_no_agent(event, context)
                self.assertEqual(event.sent, [])

    def test_reply_metadata_without_content_is_not_information(self):
        reply = Reply()
        reply.message_str = ""
        reply.text = ""
        reply.chain = None
        reply.id = "previous-message"
        event, context = self.dispatch([reply, At()])
        self.assert_no_agent(event, context)
        self.assertEqual(event.sent, ["我在，请继续说。"])

    def test_empty_at_is_not_a_command_even_with_parsed_metadata(self):
        event, context = self.dispatch(
            [At()],
            parsed={"plugin.empty_match": {}},
            config={"group_rest_rules": [{"group_id": "20001"}]},
        )
        self.assert_no_agent(event, context)
        self.assertEqual(event.sent, ["休息ing，工作时间：08:00-16:00"])

    def test_real_text_image_and_quoted_content_keep_normal_wake(self):
        for chain in (
            [At(), Plain("你好")],
            [At(), Plain("\u200b你好")],
            [At(), Image()],
            [Reply(), At()],
        ):
            with self.subTest(chain=chain):
                event, context = self.dispatch(chain)
                self.assertFalse(event.is_stopped())
                self.assertTrue(event.is_at_or_wake_command)
                self.assertFalse(event.call_llm)
                self.assertEqual(event.sent, [])
                self.assertEqual(context.calls, [])

    def test_quoted_image_is_information_even_without_quoted_text(self):
        reply = Reply()
        reply.message_str = ""
        reply.chain = [Image()]
        event, context = self.dispatch([reply, At()])
        self.assertFalse(event.is_stopped())
        self.assertFalse(event.call_llm)
        self.assertEqual(event.sent, [])
        self.assertEqual(context.calls, [])

    def test_rest_prompt_takes_precedence_over_empty_at_prompt(self):
        for config in ({}, {"wake_enabled": False}, {"wake_on_at": False}):
            with self.subTest(config=config):
                event, context = self.dispatch(
                    [At(), Plain("\u200b")],
                    config={"group_rest_rules": [{"group_id": "20001"}], **config},
                )
                self.assert_no_agent(event, context)
                self.assertEqual(event.sent, ["休息ing，工作时间：08:00-16:00"])

    def test_open_window_keeps_empty_at_prompt_and_allows_informative_at(self):
        rules = [{"group_id": "20001", "weekdays": ["星期二"]}]
        event, context = self.dispatch(
            [At()], hour=10, config={"group_rest_rules": rules}
        )
        self.assert_no_agent(event, context)
        self.assertEqual(event.sent, ["我在，请继续说。"])

        event, context = self.dispatch(
            [At(), Plain("你好")], hour=10, config={"group_rest_rules": rules}
        )
        self.assertFalse(event.is_stopped())
        self.assertFalse(event.call_llm)
        self.assertTrue(event.is_at_or_wake_command)
        self.assertEqual(event.sent, [])
        self.assertEqual(context.calls, [])

    def test_unselected_weekday_still_blocks_empty_at_inside_time_window(self):
        event, context = self.dispatch(
            [At()],
            hour=10,
            config={
                "group_rest_rules": [{"group_id": "20001", "weekdays": ["星期三"]}]
            },
        )
        self.assert_no_agent(event, context)
        self.assertEqual(event.sent, ["休息ing，工作时间：08:00-16:00"])

    def test_at_command_remains_available_during_rest(self):
        event, context = self.dispatch(
            [At(), Plain("/help")],
            config={"group_rest_rules": [{"group_id": "20001"}]},
        )
        self.assertFalse(event.is_stopped())
        self.assertTrue(event.call_llm)
        self.assertEqual(event.sent, [])
        self.assertEqual(context.calls, [])

    def test_whitelist_denial_stays_silent(self):
        event, context = self.dispatch([At()], config={"group_whitelist": []})
        self.assertTrue(event.is_stopped())
        self.assertEqual(event.sent, [])
        self.assertEqual(context.calls, [])


if __name__ == "__main__":
    unittest.main()
