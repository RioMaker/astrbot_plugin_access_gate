"""Only Access Gate's own wake decisions opt a message into billing."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_access_gate import main
from test_wake import At, Context, Event, Plain


class CreditScopeTests(unittest.IsolatedAsyncioTestCase):
    def make(self, text, *, complete=True, **config):
        context = Context('{"should_reply":true}', {})
        plugin = main.AccessGatePlugin(
            context, {"private_whitelist": ["10001"], **config}
        )
        plugin.billing_runtime = SimpleNamespace(
            status=lambda: {"complete": complete},
            mark_chat=lambda event: event.set_extra("billing_mark", True),
        )
        return plugin, Event(text), context

    async def test_name_and_at_wake_mark_the_session(self):
        for text, chain in (
            ("可可子，帮我看看", None),
            ("你好", [At(), Plain("你好")]),
        ):
            with self.subTest(text=text):
                plugin, event, _ = self.make(text)
                if chain:
                    event.message_obj.message = chain
                await plugin.route_wake(event)
                self.assertTrue(event.get_extra("billing_mark"))
                self.assertFalse(event.call_llm)

    async def test_mention_is_marked_before_the_classifier_calls_a_model(self):
        for answer in ('{"should_reply":true}', '{"should_reply":false}'):
            with self.subTest(answer=answer):
                plugin, event, context = self.make("这事可可子知道吗")

                async def classify(*, active_event=event, result=answer, **kwargs):
                    self.assertTrue(active_event.get_extra("billing_mark"))
                    return SimpleNamespace(completion_text=result)

                context.text_chat = AsyncMock(side_effect=classify)
                await plugin.route_wake(event)
                context.text_chat.assert_awaited_once()

    async def test_commands_pass_even_when_billing_is_incompatible(self):
        for text in ("/六爻 可可子的问题", "/起卦 问题", "/rp"):
            with self.subTest(text=text):
                plugin, event, context = self.make(text, complete=False)
                await plugin.enforce_access(event)
                await plugin.route_wake(event)
                self.assertFalse(event.is_stopped())
                self.assertIsNone(event.get_extra("billing_mark"))
                self.assertEqual(event.sent, [])
                self.assertEqual(context.calls, [])

    async def test_incompatible_billing_only_stops_gate_wake(self):
        for text in ("可可子你好", "这事可可子知道吗"):
            with self.subTest(text=text):
                plugin, event, context = self.make(text, complete=False)
                await plugin.route_wake(event)
                self.assertTrue(event.is_stopped())
                self.assertTrue(event.call_llm)
                self.assertEqual(len(event.sent), 1)
                self.assertIn("门禁会话已暂停", event.sent[0])
                self.assertEqual(context.calls, [])

    async def test_unrelated_disabled_and_webchat_paths_are_not_marked(self):
        for text, config, platform in (
            ("普通消息", {}, "aiocqhttp"),
            ("可可子你好", {"wake_enabled": False}, "aiocqhttp"),
            ("可可子你好", {}, "webchat"),
            ("可可子你好", {"credits_enabled": False}, "aiocqhttp"),
        ):
            with self.subTest(text=text, config=config, platform=platform):
                plugin, event, context = self.make(text, **config)
                event._platform = platform
                await plugin.route_wake(event)
                self.assertIsNone(event.get_extra("billing_mark"))
                self.assertEqual(context.calls, [])

    async def test_denied_and_fixed_reply_events_are_not_marked(self):
        for text, config in (
            ("可可子你好", {"private_whitelist": []}),
            ("可可子", {}),
        ):
            with self.subTest(text=text):
                plugin, event, context = self.make(text, **config)
                await plugin.route_wake(event)
                self.assertTrue(event.is_stopped())
                self.assertIsNone(event.get_extra("billing_mark"))
                self.assertEqual(context.calls, [])

    async def test_plugin_inventory_always_reports_passthrough(self):
        plugin, _, _ = self.make("普通消息")
        plugin.context.get_all_stars = lambda: [
            SimpleNamespace(name="astrbot_plugin_liuyao", activated=True),
            SimpleNamespace(name=main.PLUGIN_NAME, activated=True),
            SimpleNamespace(name="core", reserved=True),
        ]
        plugins = plugin.credit_plugins()
        self.assertEqual(len(plugins), 1)
        self.assertFalse(plugins[0]["charged"])
        self.assertFalse(plugins[0]["controlled"])
