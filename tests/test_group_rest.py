import asyncio
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_access_gate import main
from test_wake import At, Context, Event, Image, Plain, Reply


class GroupEvent(Event):
    def __init__(
        self,
        text="你好",
        *,
        group_id="20001",
        sender_id="10001",
        private=False,
        platform="aiocqhttp",
    ):
        super().__init__(text)
        self._private = private
        self._group_id = group_id
        self._sender_id = sender_id
        self._platform = platform
        self.unified_msg_origin = f"{platform}:GroupMessage:{group_id}"


class GroupRestTests(unittest.TestCase):
    WEEKDAYS = [f"星期{day}" for day in "一二三四五六日"]

    @staticmethod
    def rule(**fields):
        return {"group_id": "20001", **fields}

    def run_event(
        self, event=None, *, hour=20, minute=0, now=None, config=None, route_only=False
    ):
        context = Context('{"should_reply":true}', {})
        settings = {
            "group_whitelist": ["20001", "20002"],
            "private_whitelist": ["10001"],
            "group_rest_rules": [self.rule()],
            **(config or {}),
        }
        plugin = main.AccessGatePlugin(context, settings)
        event = event or GroupEvent()
        now = now or datetime(2026, 10, 10, hour, minute, tzinfo=main.REST_TIMEZONE)

        async def dispatch():
            if not route_only:
                await plugin.enforce_access(event)
            if not event.is_stopped():
                await plugin.route_wake(event)

        with patch.object(plugin, "_now", return_value=now):
            asyncio.run(dispatch())
        return event, context, plugin

    def test_default_open_schedule_boundaries(self):
        for hour, minute, resting in (
            (7, 59, True),
            (8, 0, False),
            (15, 59, False),
            (16, 0, True),
        ):
            with self.subTest(hour=hour, minute=minute):
                event = GroupEvent()
                event.message_obj.message.insert(0, At())
                event, context, _ = self.run_event(event, hour=hour, minute=minute)
                self.assertEqual(event.is_stopped(), resting)
                self.assertEqual(
                    event.sent,
                    ["休息ing，工作时间：08:00-16:00"] if resting else [],
                )
                self.assertEqual(context.calls, [])
                self.assertEqual(event.is_at_or_wake_command, not resting)
                self.assertEqual(event.call_llm, resting)

    def test_cross_midnight_boundaries(self):
        rules = [self.rule(start="22:00", end="06:00")]
        for hour, minute, resting in (
            (21, 59, True),
            (22, 0, False),
            (23, 59, False),
            (0, 0, False),
            (5, 59, False),
            (6, 0, True),
        ):
            with self.subTest(hour=hour, minute=minute):
                event = GroupEvent()
                event.message_obj.message.insert(0, At())
                event, _, _ = self.run_event(
                    event, hour=hour, minute=minute, config={"group_rest_rules": rules}
                )
                self.assertEqual(event.is_stopped(), resting)
                self.assertEqual(
                    event.sent, ["休息ing，工作时间：22:00-06:00"] if resting else []
                )

    def test_weekday_selection_supports_each_single_day_multiple_days_and_all_days(
        self,
    ):
        selections = [[day] for day in self.WEEKDAYS] + [
            ["星期一", "星期三", "星期日"],
            self.WEEKDAYS,
        ]
        for selected in selections:
            for offset, day in enumerate(self.WEEKDAYS):
                with self.subTest(selected=selected, day=day):
                    now = datetime(2026, 10, 5 + offset, 10, tzinfo=main.REST_TIMEZONE)
                    event, context, _ = self.run_event(
                        GroupEvent("可可子，你好"),
                        now=now,
                        config={"group_rest_rules": [self.rule(weekdays=selected)]},
                    )
                    resting = day not in selected
                    self.assertEqual(event.is_stopped(), resting)
                    self.assertEqual(event.call_llm, resting)
                    self.assertEqual(event.is_at_or_wake_command, not resting)
                    self.assertEqual(event.sent, [])
                    self.assertEqual(context.calls, [])

    def test_missing_weekdays_opens_the_time_window_every_day(self):
        for offset in range(7):
            with self.subTest(weekday=offset):
                now = datetime(2026, 10, 5 + offset, 10, tzinfo=main.REST_TIMEZONE)
                event, _, plugin = self.run_event(now=now)
                self.assertFalse(event.is_stopped())
                self.assertEqual(plugin.rest_rules[0].weekdays, frozenset(range(7)))

    def test_weekday_and_time_must_both_match_to_open(self):
        rules = [self.rule(weekdays=["星期五"], start="08:00", end="16:00")]
        for day, hour, resting in (
            (9, 10, False),
            (9, 20, True),
            (10, 10, True),
            (10, 20, True),
        ):
            for route_only in (False, True):
                with self.subTest(day=day, hour=hour, route_only=route_only):
                    event = GroupEvent()
                    event.message_obj.message.insert(0, At())
                    event.is_at_or_wake_command = True
                    event, context, _ = self.run_event(
                        event,
                        now=datetime(2026, 10, day, hour, tzinfo=main.REST_TIMEZONE),
                        config={"group_rest_rules": rules},
                        route_only=route_only,
                    )
                    self.assertEqual(event.is_stopped(), resting)
                    self.assertEqual(event.call_llm, resting)
                    self.assertEqual(event.is_at_or_wake_command, not resting)
                    self.assertEqual(
                        event.sent,
                        ["休息ing，工作时间：08:00-16:00"] if resting else [],
                    )
                    self.assertEqual(context.calls, [])

    def test_cross_midnight_uses_current_weekday_including_sunday_to_monday(self):
        rules = [self.rule(weekdays=["星期五", "星期日"], start="22:00", end="06:00")]
        for day, hour, minute, resting in (
            (9, 5, 59, False),
            (9, 6, 0, True),
            (9, 21, 59, True),
            (9, 22, 0, False),
            (9, 23, 59, False),
            (10, 0, 0, True),
            (10, 5, 59, True),
            (10, 6, 0, True),
            (10, 22, 0, True),
            (11, 5, 59, False),
            (11, 22, 0, False),
            (12, 0, 0, True),
            (12, 5, 59, True),
            (12, 6, 0, True),
        ):
            with self.subTest(day=day, hour=hour, minute=minute):
                event = GroupEvent()
                event.message_obj.message.insert(0, At())
                event, context, _ = self.run_event(
                    event,
                    now=datetime(
                        2026, 10, day, hour, minute, tzinfo=main.REST_TIMEZONE
                    ),
                    config={"group_rest_rules": rules},
                )
                self.assertEqual(event.is_stopped(), resting)
                self.assertEqual(event.call_llm, resting)
                self.assertEqual(event.is_at_or_wake_command, not resting)
                self.assertEqual(len(event.sent), int(resting))
                self.assertEqual(context.calls, [])

    def test_invalid_weekdays_reject_only_the_invalid_rule_and_log_error(self):
        invalid = (
            [],
            None,
            "星期一",
            1,
            ("星期一",),
            [True],
            [0],
            ["周一"],
            ["星期一", "星期八"],
            [["星期一"]],
        )
        for weekdays in invalid:
            with self.subTest(weekdays=weekdays):
                rule = self.rule(weekdays=weekdays)
                with patch.object(main.logger, "error") as log_error:
                    event, context, plugin = self.run_event(
                        GroupEvent("可可子，你好"),
                        config={"group_rest_rules": [rule]},
                    )
                self.assertEqual(plugin.rest_rules, ())
                self.assertFalse(event.is_stopped())
                self.assertTrue(event.is_at_or_wake_command)
                self.assertFalse(event.call_llm)
                self.assertEqual(context.calls, [])
                log_error.assert_called_once()
                with patch.object(main.logger, "error"):
                    event, _, plugin = self.run_event(
                        config={"group_rest_rules": [rule, self.rule()]}
                    )
                self.assertTrue(event.is_stopped())
                self.assertEqual(len(plugin.rest_rules), 1)

    def test_duplicate_weekdays_are_deduplicated(self):
        event, _, plugin = self.run_event(
            hour=10,
            config={
                "group_rest_rules": [self.rule(weekdays=["星期六", "星期二", "星期六"])]
            },
        )
        self.assertFalse(event.is_stopped())
        self.assertIsInstance(plugin.rest_rules[0].weekdays, frozenset)
        self.assertEqual(plugin.rest_rules[0].weekdays, frozenset({1, 5}))

    def test_later_matching_weekday_can_open_the_group(self):
        rules = [
            self.rule(weekdays=["星期五"], reply="首条未开放"),
            self.rule(weekdays=["星期六"], reply="后条开放"),
        ]
        event = GroupEvent()
        event.message_obj.message.insert(0, At())
        event, context, _ = self.run_event(
            event, hour=10, config={"group_rest_rules": rules}
        )
        self.assertFalse(event.is_stopped())
        self.assertFalse(event.call_llm)
        self.assertTrue(event.is_at_or_wake_command)
        self.assertEqual(event.sent, [])
        self.assertEqual(context.calls, [])

    def test_work_placeholders_describe_configured_days_and_open_time(self):
        for weekdays, start, end, expected in (
            (
                self.WEEKDAYS[:5],
                "08:00",
                "16:00",
                "星期一、星期二、星期三、星期四、星期五 08:00-16:00",
            ),
            (["星期五"], "08:00", "16:00", "星期五 08:00-16:00"),
            (["星期五"], "22:00", "06:00", "星期五 22:00-06:00"),
            (
                ["星期日", "星期五", "星期五"],
                "22:00",
                "06:00",
                "星期五、星期日 22:00-06:00",
            ),
            (self.WEEKDAYS, "22:00", "06:00", "每天 22:00-06:00"),
        ):
            with self.subTest(weekdays=weekdays, start=start, end=end):
                event = GroupEvent()
                event.message_obj.message.insert(0, At())
                event, _, _ = self.run_event(
                    event,
                    config={
                        "group_rest_rules": [
                            self.rule(
                                weekdays=weekdays,
                                start=start,
                                end=end,
                                reply="{work_days} {work_start}-{work_end}",
                            )
                        ]
                    },
                )
                self.assertEqual(event.sent, [expected])

    def test_weekday_match_preserves_commands_wake_and_empty_at_policy(self):
        rules = [self.rule(weekdays=self.WEEKDAYS[:5])]
        for day, hour, resting in ((9, 10, False), (9, 20, True), (10, 10, True)):
            for kind in ("command", "core_command", "name", "mention", "empty_at"):
                with self.subTest(day=day, hour=hour, kind=kind):
                    text = {
                        "command": "/unknown_command",
                        "core_command": "help",
                        "name": "可可子，你好",
                        "mention": "这个问题可可子知道吗",
                        "empty_at": "",
                    }[kind]
                    event = GroupEvent(text)
                    if kind in {"command", "core_command", "empty_at"}:
                        event.message_obj.message.insert(0, At())
                        event.is_at_or_wake_command = True
                    if kind == "core_command":
                        event.extras["handlers_parsed_params"] = {"plugin.help": {}}
                    event, context, _ = self.run_event(
                        event,
                        now=datetime(2026, 10, day, hour, tzinfo=main.REST_TIMEZONE),
                        config={"group_rest_rules": rules},
                    )
                    if kind in {"command", "core_command"}:
                        self.assertFalse(event.is_stopped())
                        self.assertTrue(event.call_llm)
                        self.assertEqual(event.sent, [])
                    elif kind == "empty_at":
                        self.assertTrue(event.is_stopped())
                        self.assertTrue(event.call_llm)
                        self.assertFalse(event.is_at_or_wake_command)
                        self.assertEqual(
                            event.sent,
                            ["休息ing，工作时间：08:00-16:00"]
                            if resting
                            else ["我在，请继续说。"],
                        )
                    else:
                        self.assertEqual(event.is_stopped(), resting)
                        self.assertEqual(event.call_llm, resting)
                        self.assertEqual(event.is_at_or_wake_command, not resting)
                        self.assertEqual(event.sent, [])
                    self.assertEqual(
                        len(context.calls), int(kind == "mention" and not resting)
                    )

    def test_weekday_switches_at_beijing_midnight_on_a_utc_host(self):
        for minute, resting in ((59, True), (0, False)):
            instant = datetime(
                2026, 10, 11, 15 if minute else 16, minute, tzinfo=timezone.utc
            )
            with self.subTest(instant=instant):
                context = Context('{"should_reply":true}', {})
                plugin = main.AccessGatePlugin(
                    context,
                    {
                        "group_whitelist": ["20001"],
                        "group_rest_rules": [
                            self.rule(weekdays=["星期一"], start="00:00", end="01:00")
                        ],
                    },
                )
                event = GroupEvent()
                event.message_obj.message.insert(0, At())

                async def dispatch():
                    await plugin.enforce_access(event)
                    if not event.is_stopped():
                        await plugin.route_wake(event)

                with patch.object(main, "datetime", wraps=datetime) as clock:
                    clock.now.side_effect = lambda tz: instant.astimezone(tz)
                    asyncio.run(dispatch())
                self.assertTrue(clock.now.called)
                for call in clock.now.call_args_list:
                    self.assertEqual(call.args, (main.REST_TIMEZONE,))
                self.assertEqual(event.is_stopped(), resting)
                self.assertEqual(len(event.sent), int(resting))
                self.assertEqual(event.call_llm, resting)
                self.assertEqual(context.calls, [])

    def test_silent_messages_do_not_call_classifier(self):
        for text in (
            "普通消息",
            "可可子，帮我看看",
            "这个问题可可子知道吗",
        ):
            with self.subTest(text=text):
                event, context, _ = self.run_event(GroupEvent(text))
                self.assertTrue(event.is_stopped())
                self.assertTrue(event.call_llm)
                self.assertEqual(event.sent, [])
                self.assertEqual(context.calls, [])

    def test_commands_are_allowed_with_or_without_at(self):
        for text in ("/rtlink score", "/help", "!help", "！help"):
            for at in (False, True):
                with self.subTest(text=text, at=at):
                    event = GroupEvent(text)
                    if at:
                        event.message_obj.message.insert(0, At())
                    event.is_at_or_wake_command = True
                    event, context, _ = self.run_event(event)
                    self.assertFalse(event.is_stopped())
                    self.assertTrue(event.call_llm)
                    self.assertEqual(event.sent, [])
                    self.assertEqual(context.calls, [])

    def test_command_prefix_stripped_by_core_is_still_allowed(self):
        event = GroupEvent("/rtlink score")
        event.message_str = "rtlink score"
        event, _, _ = self.run_event(event)
        self.assertFalse(event.is_stopped())
        self.assertEqual(event.sent, [])

    def test_core_matched_command_without_slash_is_allowed(self):
        event = GroupEvent("help")
        event.message_obj.message.insert(0, At())
        event.is_at_or_wake_command = True
        event.extras["handlers_parsed_params"] = {"plugin.help": {}}
        event, context, _ = self.run_event(event)
        self.assertFalse(event.is_stopped())
        self.assertTrue(event.call_llm)
        self.assertEqual(event.sent, [])
        self.assertEqual(context.calls, [])

    def test_command_exception_does_not_bypass_whitelist(self):
        event, context, _ = self.run_event(
            GroupEvent("/rtlink score"), config={"group_whitelist": []}
        )
        self.assertTrue(event.is_stopped())
        self.assertEqual(event.sent, [])
        self.assertEqual(context.calls, [])

    def test_unknown_commands_do_not_fall_back_to_chat_when_wake_is_disabled(self):
        event = GroupEvent("/unknown_command")
        event.message_obj.message.insert(0, At())
        event.is_at_or_wake_command = True
        event, context, _ = self.run_event(event, config={"wake_enabled": False})
        self.assertFalse(event.is_stopped())
        self.assertTrue(event.call_llm)
        self.assertEqual(event.sent, [])
        self.assertEqual(context.calls, [])

    def test_at_only_and_at_media_never_enter_default_agent(self):
        for chain in ([At()], [At(), Plain(" \t\n")], [At(), Image()]):
            for route_only in (False, True):
                with self.subTest(chain=chain, route_only=route_only):
                    event = GroupEvent("")
                    event.message_obj.message = chain
                    event.is_at_or_wake_command = True
                    event.is_wake = True
                    event, context, _ = self.run_event(event, route_only=route_only)
                    self.assertTrue(event.is_stopped())
                    self.assertTrue(event.call_llm)
                    self.assertFalse(event.is_at_or_wake_command)
                    self.assertEqual(event.sent, ["休息ing，工作时间：08:00-16:00"])
                    self.assertEqual(context.calls, [])

    def test_core_quote_wake_is_cleared_without_reply(self):
        event = GroupEvent("")
        event.message_obj.message = [Reply()]
        event.is_at_or_wake_command = True
        event, context, _ = self.run_event(event)
        self.assertTrue(event.is_stopped())
        self.assertTrue(event.call_llm)
        self.assertFalse(event.is_at_or_wake_command)
        self.assertEqual(event.sent, [])
        self.assertEqual(context.calls, [])

    def test_at_other_all_and_quoted_at_are_silent(self):
        other = At()
        other.qq = "123"
        all_members = At()
        all_members.qq = "all"
        quoted = Reply()
        quoted.message = [At()]
        forward = SimpleNamespace(message=[At()])
        for part in (other, all_members, quoted, forward, Plain("@999")):
            with self.subTest(part=part):
                event = GroupEvent()
                event.message_obj.message.insert(0, part)
                event, context, _ = self.run_event(event)
                self.assertTrue(event.is_stopped())
                self.assertEqual(event.sent, [])
                self.assertEqual(context.calls, [])

    def test_private_webchat_and_unconfigured_group_keep_wake_behavior(self):
        for fields in (
            {"private": True, "group_id": ""},
            {"platform": "webchat"},
            {"group_id": "20002"},
        ):
            with self.subTest(fields=fields):
                event, context, _ = self.run_event(GroupEvent("可可子，你好", **fields))
                self.assertFalse(event.is_stopped())
                self.assertEqual(event.sent, [])
                self.assertEqual(context.calls, [])
                if fields.get("platform") != "webchat":
                    self.assertTrue(event.is_at_or_wake_command)

    def test_whitelist_rejection_remains_silent(self):
        event = GroupEvent()
        event.message_obj.message.insert(0, At())
        event, context, _ = self.run_event(event, config={"group_whitelist": []})
        self.assertTrue(event.is_stopped())
        self.assertEqual(event.sent, [])
        self.assertEqual(context.calls, [])

    def test_admin_whitelist_bypass_does_not_bypass_rest(self):
        event = GroupEvent()
        event._admin = True
        event.message_obj.message.insert(0, At())
        event, _, _ = self.run_event(event, config={"group_whitelist": []})
        self.assertTrue(event.is_stopped())
        self.assertEqual(event.sent, ["休息ing，工作时间：08:00-16:00"])

    def test_management_command_is_available_during_rest(self):
        event = GroupEvent("/accessgate status")
        event._admin = True
        context = Context('{"should_reply":true}', {})
        plugin = main.AccessGatePlugin(context, {"group_rest_rules": [self.rule()]})

        async def dispatch():
            results = [
                result async for result in plugin.accessgate_command(event, "status")
            ]
            if not event.is_stopped():
                await plugin.enforce_access(event)
            return results

        now = datetime(2026, 10, 10, 20, tzinfo=main.REST_TIMEZONE)
        with patch.object(plugin, "_now", return_value=now):
            results = asyncio.run(dispatch())
        self.assertIn("群聊开放时段：开启，1 条有效规则（北京时间）", results[0])
        self.assertTrue(event.is_stopped())
        self.assertEqual(context.calls, [])

    def test_disabled_rest_restores_normal_wake(self):
        event, _, _ = self.run_event(
            GroupEvent("可可子，你好"), config={"group_rest_enabled": False}
        )
        self.assertFalse(event.is_stopped())
        self.assertTrue(event.is_at_or_wake_command)
        self.assertFalse(event.call_llm)

    def test_rest_is_independent_of_wake_switches_and_handler_entry(self):
        for config in (
            {"wake_enabled": False},
            {"wake_on_at": False},
            {"wake_enabled": False, "wake_on_at": False},
        ):
            for route_only in (False, True):
                with self.subTest(config=config, route_only=route_only):
                    event = GroupEvent()
                    event.message_obj.message.insert(0, At())
                    event, context, _ = self.run_event(
                        event, config=config, route_only=route_only
                    )
                    self.assertTrue(event.is_stopped())
                    self.assertEqual(event.sent, ["休息ing，工作时间：08:00-16:00"])
                    self.assertEqual(context.calls, [])

    def test_group_umo_rule(self):
        rules = [self.rule(group_id="aiocqhttp:GroupMessage:20001")]
        for platform, hour, resting in (
            ("aiocqhttp", 10, False),
            ("aiocqhttp", 20, True),
            ("other-platform", 20, False),
        ):
            with self.subTest(platform=platform, hour=hour):
                event = GroupEvent(platform=platform)
                event.message_obj.message.insert(0, At())
                event, _, _ = self.run_event(
                    event, hour=hour, config={"group_rest_rules": rules}
                )
                self.assertEqual(event.is_stopped(), resting)
                self.assertEqual(len(event.sent), int(resting))

    def test_different_groups_can_have_different_times_and_content(self):
        rules = [
            self.rule(reply="第一群休息"),
            self.rule(
                group_id="20002",
                start="09:30",
                end="17:30",
                reply="第二群工作时间 09:30-17:30",
            ),
        ]
        for group_id, reply in (
            ("20001", "第一群休息"),
            ("20002", "第二群工作时间 09:30-17:30"),
        ):
            event = GroupEvent(group_id=group_id)
            event.message_obj.message.insert(0, At())
            event, _, _ = self.run_event(event, config={"group_rest_rules": rules})
            self.assertEqual(event.sent, [reply])
        for group_id, resting in (("20001", True), ("20002", False)):
            with self.subTest(group_id=group_id):
                event = GroupEvent(group_id=group_id)
                event.message_obj.message.insert(0, At())
                event, _, _ = self.run_event(
                    event, hour=16, minute=30, config={"group_rest_rules": rules}
                )
                self.assertEqual(event.is_stopped(), resting)

    def test_any_matching_window_opens_and_closed_window_uses_first_group_reply(self):
        rules = [
            self.rule(group_id="20002", reply="不相关的群"),
            self.rule(start="08:00", end="12:00", reply="首条群规则"),
            self.rule(start="11:00", end="16:00", reply="第二条群规则"),
        ]
        for hour, resting in (
            (7, True),
            (8, False),
            (11, False),
            (12, False),
            (15, False),
            (16, True),
        ):
            with self.subTest(hour=hour):
                event = GroupEvent()
                event.message_obj.message.insert(0, At())
                event, context, _ = self.run_event(
                    event, hour=hour, config={"group_rest_rules": rules}
                )
                self.assertEqual(event.is_stopped(), resting)
                self.assertEqual(event.call_llm, resting)
                self.assertEqual(event.sent, ["首条群规则"] if resting else [])
                self.assertEqual(context.calls, [])

    def test_separate_windows_must_not_combine_their_weekdays_and_times(self):
        rules = [
            self.rule(
                weekdays=["星期五"], start="08:00", end="12:00", reply="首条群规则"
            ),
            self.rule(
                weekdays=["星期六"], start="14:00", end="18:00", reply="后条群规则"
            ),
        ]
        for day, hour, resting in (
            (9, 10, False),
            (9, 15, True),
            (10, 10, True),
            (10, 15, False),
        ):
            with self.subTest(day=day, hour=hour):
                event = GroupEvent()
                event.message_obj.message.insert(0, At())
                event, _, _ = self.run_event(
                    event,
                    now=datetime(2026, 10, day, hour, tzinfo=main.REST_TIMEZONE),
                    config={"group_rest_rules": rules},
                )
                self.assertEqual(event.is_stopped(), resting)
                self.assertEqual(event.sent, ["首条群规则"] if resting else [])

    def test_other_group_open_window_cannot_open_the_restricted_group(self):
        rules = [
            self.rule(group_id="20002", start="18:00", end="23:00"),
            self.rule(reply="当前群未开放"),
        ]
        event = GroupEvent()
        event.message_obj.message.insert(0, At())
        event, _, _ = self.run_event(event, config={"group_rest_rules": rules})
        self.assertTrue(event.is_stopped())
        self.assertEqual(event.sent, ["当前群未开放"])

    def test_custom_reply_placeholders_and_literal_braces(self):
        rules = [
            self.rule(
                reply="休息 {rest_start}~{rest_end}；工作 {work_start}~{work_end}；{说明}"
            )
        ]
        event = GroupEvent()
        event.message_obj.message.insert(0, At())
        event, _, _ = self.run_event(event, config={"group_rest_rules": rules})
        self.assertEqual(event.sent, ["休息 16:00~08:00；工作 08:00~16:00；{说明}"])

    def test_invalid_rules_do_not_block_and_do_not_hide_valid_rule(self):
        invalid = [
            None,
            self.rule(start="8:00"),
            self.rule(start="24:00"),
            self.rule(end="16:60"),
            self.rule(start="08:00", end="08:00"),
            self.rule(reply=" "),
            self.rule(reply=None),
        ]
        for item in invalid:
            with self.subTest(item=item):
                event, _, plugin = self.run_event(
                    GroupEvent("可可子，你好"), config={"group_rest_rules": [item]}
                )
                self.assertFalse(event.is_stopped())
                self.assertTrue(event.is_at_or_wake_command)
                self.assertEqual(plugin.rest_rules, ())
        event, _, plugin = self.run_event(
            config={"group_rest_rules": invalid + [self.rule()]}
        )
        self.assertTrue(event.is_stopped())
        self.assertEqual(len(plugin.rest_rules), 1)

    def test_empty_missing_and_non_list_rules_do_not_affect_existing_groups(self):
        for rules in ([], [{"group_id": ""}], None, "20001"):
            with self.subTest(rules=rules):
                event, _, plugin = self.run_event(
                    GroupEvent("可可子，你好"), config={"group_rest_rules": rules}
                )
                self.assertFalse(event.is_stopped())
                self.assertEqual(plugin.rest_rules, ())
        plugin = main.AccessGatePlugin(Context("", {}), {})
        self.assertEqual(plugin.rest_rules, ())

    def test_schema_default_does_not_select_a_group(self):
        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
                encoding="utf-8"
            )
        )
        defaults = {key: value.get("default") for key, value in schema.items()}
        plugin = main.AccessGatePlugin(Context("", {}), defaults)
        self.assertEqual(plugin.rest_rules, ())
        default = defaults["group_rest_rules"][0]
        self.assertEqual((default["start"], default["end"]), ("08:00", "16:00"))
        template = schema["group_rest_rules"]["templates"][default["__template_key"]][
            "items"
        ]
        self.assertEqual(default["weekdays"], self.WEEKDAYS)
        self.assertEqual(template["weekdays"]["type"], "list")
        self.assertEqual(template["weekdays"]["options"], self.WEEKDAYS)
        self.assertEqual(
            default,
            {
                "__template_key": "group_rest",
                **{key: field["default"] for key, field in template.items()},
            },
        )

    def test_timezone_is_beijing_even_on_a_utc_host(self):
        self.assertEqual(
            main.AccessGatePlugin._now().utcoffset().total_seconds(), 8 * 3600
        )
        instant = datetime(2026, 10, 10, 0, tzinfo=timezone.utc)
        with patch.object(main, "datetime") as clock:
            clock.now.side_effect = lambda tz: instant.astimezone(tz)
            now = main.AccessGatePlugin._now()
        self.assertEqual((now.hour, now.minute), (8, 0))
        clock.now.assert_called_once_with(main.REST_TIMEZONE)

    def test_bot_own_messages_do_not_reply(self):
        event = GroupEvent(sender_id="999")
        event.message_obj.message.insert(0, At())
        event, context, _ = self.run_event(event)
        self.assertTrue(event.is_stopped())
        self.assertEqual(event.sent, [])
        self.assertEqual(context.calls, [])

    def test_duplicate_handler_invocation_does_not_send_twice(self):
        event = GroupEvent()
        event.message_obj.message.insert(0, At())
        event, _, plugin = self.run_event(event)
        asyncio.run(plugin.route_wake(event))
        self.assertEqual(len(event.sent), 1)


if __name__ == "__main__":
    unittest.main()
