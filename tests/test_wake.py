import asyncio
import json
from types import SimpleNamespace
import unittest
from test_access_gate import main
from mock_astrbot import MockEvent


class Plain:
    def __init__(self, text):
        self.text = text


class Reply:
    message_str = '可可子你好'


class Image:
    pass


class At:
    qq = '999'


class Event(MockEvent):
    def __init__(self, text):
        super().__init__(private=True, message=text)
        self.message_obj.message = [Plain(text)]
        self.call_llm = False
        self.is_at_or_wake_command = False
        self.sent = []
        self.extras = {}

    def get_self_id(self):
        return '999'

    def is_stopped(self):
        return self.stopped

    def should_call_llm(self, suppress):
        self.call_llm = suppress

    def set_extra(self, key, value):
        self.extras[key] = value

    async def send(self, result):
        self.sent.append(result)


class Context:
    def __init__(self, answer, settings):
        self.answer = answer
        self.settings = settings
        self.calls = []

    def get_config(self, umo=None):
        return {'provider_settings': self.settings}

    def get_using_provider(self, umo):
        return self

    async def text_chat(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.answer, Exception):
            raise self.answer
        return SimpleNamespace(completion_text=self.answer)


class WakeTests(unittest.TestCase):
    def run_route(self, text, answer='{"should_reply":true}', config=None, chain=None, settings=None):
        ctx = Context(answer, settings or {})
        cfg = {'private_whitelist': ['10001']}
        cfg.update(config or {})
        plugin = main.AccessGatePlugin(ctx, cfg)
        event = Event(text)
        if chain is not None:
            event.message_obj.message = chain
        asyncio.run(plugin.route_wake(event))
        return event, ctx

    def test_direct_and_attachments(self):
        picture = Image()
        e, c = self.run_route('可可子， 帮我看看', chain=[Plain('可可子， 帮我看看'), picture])
        self.assertEqual(e.message_str, '帮我看看')
        self.assertIn(picture, e.message_obj.message)
        self.assertTrue(e.is_at_or_wake_command)
        self.assertFalse(e.call_llm)
        self.assertEqual(c.calls, [])

    def test_judge_true_uses_original_without_tools_or_history(self):
        e, c = self.run_route('这个问题可可子知道吗')
        self.assertFalse(e.call_llm)
        self.assertEqual(len(c.calls), 1)
        self.assertEqual(json.loads(c.calls[0]['prompt'])['message'], e.message_str)
        self.assertEqual(c.calls[0]['contexts'], [])
        self.assertNotIn('func_tool', c.calls[0])

    def test_judge_false_invalid_and_failure_are_silent(self):
        for answer in ('{"should_reply":false}', '{"should_reply":"true"}', 'invalid', TimeoutError()):
            with self.subTest(answer=answer):
                e, c = self.run_route('昨天可可子说过', answer)
                self.assertTrue(e.is_stopped())
                self.assertTrue(e.call_llm)
                self.assertEqual(e.sent, [])

    def test_whitelist_denial_prevents_paid_request(self):
        e, c = self.run_route('这事可可子知道吗', config={'private_whitelist': []})
        self.assertTrue(e.is_stopped())
        self.assertEqual(c.calls, [])

    def test_commands_and_unrelated_text_do_not_wake(self):
        for text in ('/help 可可子', '普通消息'):
            e, c = self.run_route(text)
            self.assertTrue(e.call_llm)
            self.assertFalse(e.is_stopped())
            self.assertEqual(c.calls, [])

    def test_quote_does_not_trigger(self):
        e, c = self.run_route('你好', chain=[Reply(), Plain('你好')])
        self.assertTrue(e.call_llm)
        self.assertEqual(c.calls, [])

    def test_at_preserves_native_wake(self):
        e, c = self.run_route('你好', chain=[At(), Plain('你好')])
        self.assertFalse(e.call_llm)
        self.assertTrue(e.is_at_or_wake_command)
        self.assertEqual(c.calls, [])

    def test_empty_name_uses_fixed_reply(self):
        e, c = self.run_route('可可子')
        self.assertTrue(e.is_stopped())
        self.assertEqual(e.sent, ['我在，请继续说。'])
        self.assertEqual(c.calls, [])

    def test_global_disable_and_extra_prefix_prevent_request(self):
        for settings in ({'enable': False}, {'wake_prefix': '子'}):
            e, c = self.run_route('这事可可子知道吗', settings=settings)
            self.assertTrue(e.call_llm)
            self.assertEqual(c.calls, [])

    def test_wake_can_be_disabled(self):
        e, c = self.run_route('普通消息', config={'wake_enabled': False})
        self.assertFalse(e.call_llm)
        self.assertEqual(c.calls, [])
