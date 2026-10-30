"""Notification cooldowns must not mute other people or extend on every ping."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_access_gate import main
from test_group_rest import GroupEvent
from test_wake import At, Context, Plain


class CreditDenialTests(unittest.TestCase):
    def setUp(self):
        self.plugin = main.AccessGatePlugin(Context("", {}), {})
        self.error = SimpleNamespace(reason="insufficient_points")

    @staticmethod
    def event(**kwargs):
        event = GroupEvent(**kwargs)
        event.message_obj.message = [At(), Plain("你好")]
        return event

    def allowed(self, seconds, event=None, error=None):
        with patch.object(main, "monotonic", return_value=seconds):
            return self.plugin._credit_denial_notice_allowed(
                event or self.event(), error or self.error
            )

    def test_fixed_five_minute_window_does_not_slide_when_bot_keeps_mentioning(self):
        self.assertTrue(self.allowed(0))
        self.assertFalse(self.allowed(10))
        self.assertFalse(self.allowed(299))
        self.assertTrue(self.allowed(300))
        self.assertFalse(self.allowed(301))

    def test_different_groups_users_platforms_and_bots_have_independent_windows(self):
        self.assertTrue(self.allowed(0))
        self.assertTrue(self.allowed(1, self.event(group_id="other")))
        self.assertTrue(self.allowed(1, self.event(sender_id="other")))
        self.assertTrue(self.allowed(1, self.event(platform="other")))
        event = self.event()
        event.get_self_id = lambda: "another_bot"
        event.message_obj.message[0] = At()
        event.message_obj.message[0].qq = "another_bot"
        self.assertTrue(self.allowed(1, event))
        self.assertFalse(self.allowed(2))

    def test_plain_name_wake_and_other_denials_do_not_consume_this_cooldown(self):
        event = self.event()
        event.message_obj.message = [Plain("可可子你好")]
        for second in (0, 1):
            self.assertTrue(self.allowed(second, event))
            self.assertTrue(self.allowed(second, error=SimpleNamespace(reason="")))
        self.assertTrue(self.allowed(2))
        self.assertFalse(self.allowed(3))

    def test_configured_interval_and_disabled_cooldown(self):
        self.plugin.config["credits_insufficient_notice_interval"] = 60
        self.assertTrue(self.allowed(0))
        self.assertFalse(self.allowed(59))
        self.assertTrue(self.allowed(60))
        self.plugin.config["credits_insufficient_notice_interval"] = 0
        self.assertTrue(self.allowed(61))
        self.assertTrue(self.allowed(61))

    def test_invalid_interval_uses_safe_default(self):
        for value in (-1, 86401, True, "30", None):
            with self.subTest(value=value):
                self.plugin._credit_denial_notices.clear()
                self.plugin.config["credits_insufficient_notice_interval"] = value
                self.assertTrue(self.allowed(0))
                self.assertFalse(self.allowed(299))
                self.assertTrue(self.allowed(300))
