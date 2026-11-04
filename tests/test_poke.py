"""Poke eligibility, persistent quotas and atomic credits against real SQLite."""

import asyncio
import io
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from test_group_rest import GroupEvent
from test_wake import Context

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_access_gate import economy, ledger
from astrbot_plugin_access_gate.main import AccessGatePlugin
from astrbot_plugin_access_gate.poke import PokeSettings, parse_notice
from astrbot_plugin_access_gate.poke_card import PokeCards

WHEN = datetime(2026, 11, 3, 10, tzinfo=economy.BEIJING)


class PokeEvent(GroupEvent):
    def __init__(self, *, sequence=0, bot="999", **kwargs):
        super().__init__("", **kwargs)
        self.bot = bot
        self.message_obj.raw_message = {
            "post_type": "notice",
            "notice_type": "notify",
            "sub_type": "poke",
            "self_id": int(bot),
            "user_id": int(self.get_sender_id()),
            "target_id": int(bot),
            "group_id": self.get_group_id(),
            "time": WHEN.timestamp() + sequence,
        }
        self.message_obj.message = [SimpleNamespace(type="Poke", id=bot)]

    def get_self_id(self):
        return self.bot

    def chain_result(self, parts):
        return parts


def run(coro):
    return asyncio.run(coro)


def policy(**changes):
    return PokeSettings.from_config(
        {
            "poke_reward_probability": 100,
            "poke_reward_min": 3,
            "poke_reward_max": 3,
            "poke_reward_reply_mode": "text",
            **changes,
        }
    )


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(economy, "now", lambda: WHEN)
    result = economy.Economy(tmp_path / "credits.db")
    yield result
    result.store.close()


def award(service, sequence=0, settings=None, **fields):
    event = PokeEvent(sequence=sequence, **fields)
    return run(service.award_poke(event, parse_notice(event), settings or policy()))


def plugin(service, **config):
    result = AccessGatePlugin(
        Context("", {}),
        {
            "group_whitelist": ["20001"],
            "private_whitelist": ["10001"],
            "poke_reward_probability": 100,
            "poke_reward_min": 3,
            "poke_reward_max": 3,
            "poke_reward_reply_mode": "text",
            **config,
        },
    )
    result.economy = service
    return result


def test_award_is_in_existing_balance_and_filterable_ledger(service):
    assert award(service) == {"status": "awarded", "points": 3, "balance": 3}
    assert run(service.balance("qq", "10001")) == 3
    row = run(service.ledger({"kind": "poke"}))["items"][0]
    assert row["kind"] == "poke" and row["points_delta"] == 3
    assert row["group_id"] == "20001"
    assert row["details"]["bot_id"] == "999"
    assert run(service.overview())["global"]["spent_yuan"] == 0


def test_duplicates_cannot_reroll_even_with_unlimited_quota(service):
    settings = policy(poke_reward_cooldown_hours=0)
    assert award(service, settings=settings)["status"] == "awarded"
    assert award(service, settings=settings)["status"] == "duplicate"
    assert award(service, 1, settings=settings)["status"] == "awarded"
    assert run(service.balance("qq", "10001")) == 6


def test_cooldown_survives_reopen_and_is_shared_across_groups_private_and_bots(
    service, tmp_path, monkeypatch
):
    award(service)
    other = economy.Economy(tmp_path / "credits.db")
    try:
        assert award(other, 1, group_id="20002")["status"] == "cooldown"
        assert award(other, 2, private=True, group_id="")["status"] == "cooldown"
        assert award(other, 3, bot="888")["status"] == "cooldown"
        assert award(other, 4, sender_id="10002")["status"] == "awarded"
        monkeypatch.setattr(economy, "now", lambda: WHEN + timedelta(hours=8))
        assert award(other, 5)["status"] == "awarded"
    finally:
        other.store.close()


def test_rolling_window_with_two_slots_does_not_extend_on_denials(service, monkeypatch):
    settings = policy(poke_reward_max_times=2)
    assert award(service, settings=settings)["status"] == "awarded"
    monkeypatch.setattr(economy, "now", lambda: WHEN + timedelta(hours=1))
    assert award(service, 1, settings=settings)["status"] == "awarded"
    monkeypatch.setattr(economy, "now", lambda: WHEN + timedelta(hours=7))
    assert award(service, 2, settings=settings)["status"] == "cooldown"
    monkeypatch.setattr(economy, "now", lambda: WHEN + timedelta(hours=8))
    assert award(service, 3, settings=settings)["status"] == "awarded"
    assert award(service, 4, settings=settings)["status"] == "cooldown"


@pytest.mark.parametrize(
    "mode,expected", [("success", "awarded"), ("attempt", "cooldown")]
)
def test_miss_counting_mode_and_replay(service, mode, expected):
    assert (
        award(
            service,
            settings=policy(poke_reward_probability=0, poke_reward_count_mode=mode),
        )["status"]
        == "miss"
    )
    assert award(service, settings=policy())["status"] == "duplicate"
    assert (
        award(service, 1, settings=policy(poke_reward_count_mode=mode))["status"]
        == expected
    )


@pytest.mark.parametrize("offset,expected", [(0, 1), (5, 6)])
def test_reward_range_includes_both_endpoints(service, monkeypatch, offset, expected):
    draws = iter([0, offset])
    monkeypatch.setattr(ledger.secrets, "randbelow", lambda bound: next(draws))
    receipt = award(service, settings=policy(poke_reward_min=1, poke_reward_max=6))
    assert receipt["points"] == expected


@pytest.mark.parametrize("draw,expected", [(299999, "awarded"), (300000, "miss")])
def test_probability_boundary(service, monkeypatch, draw, expected):
    draws = iter([draw, 0])
    monkeypatch.setattr(ledger.secrets, "randbelow", lambda bound: next(draws))
    assert (
        award(service, settings=policy(poke_reward_probability=30))["status"]
        == expected
    )


@pytest.mark.parametrize("duplicate", [False, True])
def test_concurrent_connections_grant_only_one_award(service, tmp_path, duplicate):
    stores = [ledger.Ledger(tmp_path / "credits.db") for _ in range(4)]

    def execute(index):
        event = PokeEvent(sequence=0 if duplicate else index)
        return stores[index % 4].award_poke(
            economy.identity(event), parse_notice(event), policy(), WHEN
        )

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(execute, range(24)))
        assert sum(item["status"] == "awarded" for item in outcomes) == 1
        assert run(service.balance("qq", "10001")) == 3
        assert run(service.ledger({"kind": "poke"}))["total"] == 1
    finally:
        for store in stores:
            store.close()


def test_failed_ledger_write_rolls_back_award_and_quota(service, monkeypatch):
    original = service.store._insert

    def fail(*args, **kwargs):
        raise RuntimeError("disk failure")

    monkeypatch.setattr(service.store, "_insert", fail)
    with pytest.raises(RuntimeError, match="disk failure"):
        award(service)
    assert run(service.balance("qq", "10001")) == 0
    monkeypatch.setattr(service.store, "_insert", original)
    assert award(service)["status"] == "awarded"


def test_existing_database_gets_poke_table_without_changing_balance(service, tmp_path):
    run(
        service.adjust(
            {
                "platform": "qq",
                "user_id": "10001",
                "delta": 12,
                "reason": "existing balance",
                "request_id": "existing-balance",
            },
            "admin",
        )
    )
    with service.store.transaction():
        service.store.db.execute("DROP TABLE poke_events")
    other = economy.Economy(tmp_path / "credits.db")
    try:
        assert run(other.balance("qq", "10001")) == 12
        assert award(other)["balance"] == 15
        assert run(other.ledger({"kind": "adjustment"}))["total"] == 1
    finally:
        other.store.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"target_id": 123},
        {"self_id": 123},
        {"user_id": 123},
        {"group_id": "wrong"},
        {"post_type": "message"},
        {"notice_type": "group_increase"},
        {"sub_type": "honor"},
        {"time": None},
        {"time": True},
        {"time": float("nan")},
        {"time": -1},
    ],
)
def test_only_valid_pokes_targeting_this_bot_are_parsed(changes):
    event = PokeEvent()
    event.message_obj.raw_message.update(changes)
    assert parse_notice(event) is None


def test_self_poke_other_platform_and_text_lookalike_are_ignored():
    assert parse_notice(PokeEvent(sender_id="999")) is None
    assert parse_notice(PokeEvent(platform="webchat")) is None
    event = PokeEvent()
    event.message_obj.raw_message = None
    event.message_str = "[CQ:poke,id=999]"
    assert parse_notice(event) is None


@pytest.mark.parametrize(
    "config",
    [
        {"poke_reward_probability": 101},
        {"poke_reward_probability": float("inf")},
        {"poke_reward_min": 0},
        {"poke_reward_max": True},
        {"poke_reward_min": 7, "poke_reward_max": 6},
        {"poke_reward_max": 10001},
        {"poke_reward_cooldown_hours": -1},
        {"poke_reward_max_times": 0},
        {"poke_reward_count_mode": "invalid"},
        {"poke_reward_reply_mode": "invalid"},
        {"poke_reward_reply": "x" * 1001},
    ],
)
def test_invalid_configuration_is_rejected(config):
    with pytest.raises(ValueError):
        PokeSettings.from_config(config)


def test_text_template_always_includes_actual_reward_and_handles_braces():
    receipt = {"points": 3, "balance": 20.125}
    assert (
        policy(poke_reward_reply="谢谢 {nickname}！余额 {balance}").text(
            receipt, "{points}"
        )
        == "谢谢 {points}！余额 20.125\n积分+3"
    )
    assert policy(poke_reward_reply="积分+{points}").text(receipt, "Alice") == "积分+3"


@pytest.mark.parametrize("entry", ["enforce_access", "route_wake"])
def test_handler_sends_reward_once_and_suppresses_llm(service, entry):
    gate, event = plugin(service, wake_enabled=False), PokeEvent()
    run(getattr(gate, entry)(event))
    assert event.sent == ["嘿嘿，送你一点小幸运～\n积分+3"]
    assert not event.stopped and event.call_llm
    assert event.is_at_or_wake_command is False
    run(gate.route_wake(event))
    assert event.sent == ["嘿嘿，送你一点小幸运～\n积分+3"]
    assert run(service.balance("qq", "10001")) == 3
    assert not gate.context.calls


@pytest.mark.parametrize(
    "config,event_fields",
    [
        ({"poke_reward_enabled": False}, {}),
        ({"credits_enabled": False}, {}),
        ({"group_whitelist": []}, {}),
        ({"private_whitelist": []}, {"private": True, "group_id": ""}),
        (
            {
                "group_rest_rules": [
                    {"group_id": "20001", "start": "11:00", "end": "12:00"}
                ]
            },
            {},
        ),
    ],
)
def test_existing_gate_and_disable_switches_prevent_reward(
    service, monkeypatch, config, event_fields
):
    gate, event = plugin(service, **config), PokeEvent(**event_fields)
    monkeypatch.setattr(gate, "_now", lambda: WHEN)
    run(gate.enforce_access(event))
    assert not event.sent
    assert run(service.balance("qq", "10001")) == 0


def test_private_poke_is_supported_and_miss_and_cooldown_stay_silent(service):
    gate = plugin(service, poke_reward_probability=0)
    run(gate.enforce_access(event := PokeEvent(private=True, group_id="")))
    assert not event.sent
    gate.config["poke_reward_probability"] = 100
    run(gate.enforce_access(PokeEvent(sequence=1, private=True, group_id="")))
    run(gate.enforce_access(event := PokeEvent(sequence=2)))
    assert not event.sent and not event.stopped
    assert run(service.balance("qq", "10001")) == 3


@pytest.mark.parametrize(
    "mode,fail", [("image", False), ("both", False), ("image", True)]
)
def test_image_reply_modes_and_text_fallback_do_not_double_award(
    service, monkeypatch, mode, fail
):
    components = ModuleType("astrbot.api.message_components")
    components.Image = SimpleNamespace(fromBytes=lambda data: data)
    monkeypatch.setitem(sys.modules, "astrbot.api.message_components", components)
    gate = plugin(service, poke_reward_reply_mode=mode)

    def render(points):
        assert points == 3
        if fail:
            raise OSError("broken image")
        return b"test-png"

    gate.poke_cards = SimpleNamespace(render=render)
    event = PokeEvent()
    run(gate.enforce_access(event))
    text = "嘿嘿，送你一点小幸运～\n积分+3"
    assert event.sent == (
        [text] if fail else [[b"test-png"]] + ([text] if mode == "both" else [])
    )
    assert not event.stopped
    assert run(service.balance("qq", "10001")) == 3


@pytest.mark.parametrize("variant,points", [("happy", 1), ("wink", 6), ("pout", 10000)])
def test_bundled_reward_cards_render_configurable_amounts(variant, points):
    data = PokeCards().render(points, variant=variant)
    with Image.open(io.BytesIO(data)) as image:
        assert image.size == (768, 768)
        assert image.format == "PNG"


def test_reply_failure_leaves_committed_credit_and_replay_does_not_award_again(service):
    gate, event = plugin(service), PokeEvent()

    async def fail_send(result):
        raise OSError("send failed")

    event.send = fail_send
    run(gate.enforce_access(event))
    assert not event.stopped
    assert run(service.balance("qq", "10001")) == 3
    replay = PokeEvent()
    run(gate.enforce_access(replay))
    assert replay.sent == []
    assert run(service.balance("qq", "10001")) == 3


@pytest.mark.parametrize(
    "outcome",
    [
        "awarded",
        "miss",
        "cooldown",
        "duplicate",
        "not_ready",
        "invalid_config",
        "storage_error",
        "send_error",
    ],
)
def test_reward_allows_original_poke_image_handler_to_run(
    service, monkeypatch, outcome
):
    gate, event = plugin(service), PokeEvent()
    # Other plugins may inspect the core's wake flag when matching a poke.
    event.is_at_or_wake_command = True
    if outcome == "miss":
        gate.config["poke_reward_probability"] = 0
    elif outcome in ("cooldown", "duplicate"):
        award(service, sequence=1 if outcome == "cooldown" else 0)
    elif outcome == "not_ready":
        gate.economy = None
    elif outcome == "invalid_config":
        gate.config["poke_reward_min"] = 7

    before = run(service.balance("qq", "10001"))
    spy = AsyncMock(wraps=service.award_poke)
    if outcome == "storage_error":
        spy.side_effect = OSError("database unavailable")
    monkeypatch.setattr(service, "award_poke", spy)
    original_picture = [b"original-poke-image"]
    if outcome == "send_error":
        send = event.send

        async def fail_reward_reply(result):
            if isinstance(result, str):
                raise OSError("reward reply failed")
            await send(result)

        event.send = fail_reward_reply

    async def original_poke_plugin(message):
        await message.send(original_picture)

    async def dispatch():
        # Run the two gate listeners, then the original lower-priority plugin.
        for handler in (gate.enforce_access, gate.route_wake, original_poke_plugin):
            if event.is_stopped():
                break
            await handler(event)

    run(dispatch())
    assert event.sent[-1:] == [original_picture]
    assert event.is_at_or_wake_command is True
    assert not event.stopped and event.call_llm
    assert not gate.context.calls
    assert spy.await_count == (0 if outcome in ("not_ready", "invalid_config") else 1)
    assert run(service.balance("qq", "10001")) == before + (
        3 if outcome in ("awarded", "send_error") else 0
    )
    expected_replies = (
        ["嘿嘿，送你一点小幸运～\n积分+3", original_picture]
        if outcome == "awarded"
        else [original_picture]
    )
    assert event.sent == expected_replies
