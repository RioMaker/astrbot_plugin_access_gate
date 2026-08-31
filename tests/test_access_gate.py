import asyncio
from pathlib import Path
import sys
import unittest


TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(PLUGIN_DIR))

from mock_astrbot import Context, MockEvent, install


LOGGER = install()
import main


class AccessGatePluginTests(unittest.TestCase):
    def make_plugin(self, **config: object) -> main.AccessGatePlugin:
        return main.AccessGatePlugin(Context(config))

    @staticmethod
    def run_gate(plugin: main.AccessGatePlugin, event: MockEvent) -> None:
        asyncio.run(plugin.enforce_access(event))

    def test_listener_runs_at_high_priority(self) -> None:
        self.assertEqual(main.AccessGatePlugin.enforce_access.__priority__, 10_000)
        self.assertEqual(main.AccessGatePlugin.accessgate_command.__priority__, 10_001)

    def test_private_rtlink_bypasses_empty_whitelist(self) -> None:
        plugin = self.make_plugin()
        for message in ("/rtlink", "/rtlink score", "rtlink bind abc"):
            with self.subTest(message=message):
                event = MockEvent(private=True, message=message)
                self.run_gate(plugin, event)
                self.assertFalse(event.stopped)

    def test_original_private_rtlink_text_is_recognized(self) -> None:
        plugin = self.make_plugin()
        event = MockEvent(
            private=True,
            message="rtlink score",
            original_message="/rtlink score",
        )
        self.run_gate(plugin, event)
        self.assertFalse(event.stopped)

    def test_similar_command_does_not_bypass(self) -> None:
        plugin = self.make_plugin()
        event = MockEvent(private=True, message="/rtlinkevil")
        self.run_gate(plugin, event)
        self.assertTrue(event.stopped)

    def test_unlisted_private_message_is_denied(self) -> None:
        plugin = self.make_plugin()
        event = MockEvent(private=True, message="/help")
        self.run_gate(plugin, event)
        self.assertTrue(event.stopped)

    def test_private_whitelist_accepts_sender_id_or_umo(self) -> None:
        for entry in ("10001", "aiocqhttp:FriendMessage:10001"):
            with self.subTest(entry=entry):
                plugin = self.make_plugin(private_whitelist=[entry])
                event = MockEvent(private=True)
                self.run_gate(plugin, event)
                self.assertFalse(event.stopped)

    def test_group_whitelist_is_independent(self) -> None:
        plugin = self.make_plugin(
            group_whitelist=["20001"],
            private_whitelist=["10001"],
        )
        allowed_group = MockEvent(private=False, group_id="20001")
        denied_group = MockEvent(private=False, group_id="20002")
        allowed_private = MockEvent(private=True, sender_id="10001")
        self.run_gate(plugin, allowed_group)
        self.run_gate(plugin, denied_group)
        self.run_gate(plugin, allowed_private)
        self.assertFalse(allowed_group.stopped)
        self.assertTrue(denied_group.stopped)
        self.assertFalse(allowed_private.stopped)

    def test_each_filter_can_be_disabled_independently(self) -> None:
        plugin = self.make_plugin(
            group_whitelist_enabled=False,
            private_whitelist_enabled=True,
        )
        group_event = MockEvent(private=False, group_id="20001")
        private_event = MockEvent(private=True, message="/help")
        self.run_gate(plugin, group_event)
        self.run_gate(plugin, private_event)
        self.assertFalse(group_event.stopped)
        self.assertTrue(private_event.stopped)

    def test_admin_bypass_is_independent(self) -> None:
        plugin = self.make_plugin(
            group_admin_bypass=False,
            private_admin_bypass=True,
        )
        group_admin = MockEvent(private=False, group_id="20001", admin=True)
        private_admin = MockEvent(private=True, message="/help", admin=True)
        self.run_gate(plugin, group_admin)
        self.run_gate(plugin, private_admin)
        self.assertTrue(group_admin.stopped)
        self.assertFalse(private_admin.stopped)

    def test_webchat_bypass(self) -> None:
        plugin = self.make_plugin()
        event = MockEvent(private=True, platform="webchat", message="/help")
        self.run_gate(plugin, event)
        self.assertFalse(event.stopped)

    def test_non_chat_event_without_group_id_is_not_blocked(self) -> None:
        plugin = self.make_plugin()
        event = MockEvent(private=False, group_id="")
        self.run_gate(plugin, event)
        self.assertFalse(event.stopped)

    @staticmethod
    def run_import(
        plugin: main.AccessGatePlugin,
        event: MockEvent,
        content: str,
    ) -> list[str]:
        async def collect() -> list[str]:
            return [result async for result in plugin.accessgate_command(event, content)]

        return asyncio.run(collect())

    def test_batch_import_accepts_json_fragment_and_deduplicates(self) -> None:
        plugin = self.make_plugin(group_whitelist=["20001"])
        event = MockEvent(private=True, admin=True)
        content = '''import group "id_whitelist": [
          "20001", "20002", "20002"
        ],'''
        results = self.run_import(plugin, event, content)
        self.assertTrue(event.stopped)
        self.assertEqual(
            plugin.config["group_whitelist"],
            ["20001", "20002"],
        )
        self.assertTrue(plugin.config.saved)
        self.assertIn("新增 1 条", results[0])

    def test_batch_import_accepts_plain_lines_for_private_whitelist(self) -> None:
        plugin = self.make_plugin()
        event = MockEvent(private=True, admin=True)
        results = self.run_import(
            plugin,
            event,
            "导入 私聊 10001\n10002\n10001",
        )
        self.assertEqual(plugin.config["private_whitelist"], ["10001", "10002"])
        self.assertIn("识别 2 条", results[0])

    def test_non_admin_cannot_batch_import(self) -> None:
        plugin = self.make_plugin()
        event = MockEvent(private=True, admin=False)
        results = self.run_import(plugin, event, "import group 12345")
        self.assertTrue(event.stopped)
        self.assertEqual(results, [])
        self.assertNotIn("group_whitelist", plugin.config)


if __name__ == "__main__":
    unittest.main()
