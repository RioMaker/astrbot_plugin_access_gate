"""Exercise RP integration across reloads, failure, and distinct user flows."""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rp_bridge import RPBridge

DAY = "2026-10-29"


class RP:
    def __init__(self):
        self.record = {"id": 5, "date": DAY, "luck_value": 75}
        self.calls = 0

    async def _get_today_rp_record(self, event):
        self.calls += 1
        return self.record

    async def rp(self, event):
        return await self._get_today_rp_record(event)

    async def get_today_rp(self, event):
        return await self._get_today_rp_record(event)

    async def rp_test(self, event):
        return {"luck_value": 100}


def make_bridge(rp=None, callback=None):
    rp = rp or RP()
    awards = []
    errors = []

    async def award(event, record):
        awards.append((event.get_sender_id(), record))

    stars = [
        SimpleNamespace(name="astrbot_plugin_taiko_rp", star_cls=rp, activated=True)
    ]
    context = SimpleNamespace(get_all_stars=lambda: stars)
    bridge = RPBridge(context, callback or award, errors.append)
    bridge._today = lambda: DAY
    event = SimpleNamespace(get_sender_id=lambda: "123")
    return bridge, rp, event, stars, awards, errors


def test_command_and_tool_share_actual_record_and_preview_never_awards():
    bridge, rp, event, _, awards, _ = make_bridge()
    assert bridge.install()["connected"]
    command = asyncio.run(rp.rp(event))
    tool = asyncio.run(rp.get_today_rp(event))
    asyncio.run(rp.rp_test(event))
    assert command is rp.record and tool is rp.record
    assert awards == [("123", rp.record), ("123", rp.record)]
    assert awards[0][1] is not rp.record
    # The adapter intentionally retries awards; the ledger's unique transaction
    # controls daily duplication and recovers a previous failed award.


def test_refresh_is_idempotent_and_uninstall_restores_class_method():
    bridge, rp, event, _, awards, _ = make_bridge()
    original = rp._get_today_rp_record
    bridge.install()
    wrapper = rp._get_today_rp_record
    bridge.refresh()
    bridge.install()
    assert rp._get_today_rp_record is wrapper
    asyncio.run(rp.rp(event))
    assert len(awards) == 1
    bridge.uninstall()
    assert rp._get_today_rp_record == original
    assert "_get_today_rp_record" not in vars(rp)
    asyncio.run(rp.rp(event))
    assert len(awards) == 1
    assert not bridge.status()["connected"]


def test_rp_hot_reload_restores_old_instance_and_attaches_new():
    bridge, rp, event, stars, awards, _ = make_bridge()
    original = rp._get_today_rp_record
    bridge.install()
    new_rp = RP()
    new_rp.record = {"id": 9, "date": DAY, "luck_value": 20}
    stars[:] = [SimpleNamespace(name="taiko_rp", star_cls=new_rp)]
    bridge.refresh()
    assert rp._get_today_rp_record == original
    asyncio.run(new_rp.rp(event))
    assert awards[0][1]["id"] == 9
    stars.clear()
    bridge.refresh()
    assert not bridge.status()["connected"]
    assert "_get_today_rp_record" not in vars(new_rp)


def test_existing_instance_override_restored_and_other_extensions_not_overwritten():
    bridge, rp, _event, _, awards, errors = make_bridge()

    async def override(event):
        return rp.record

    rp._get_today_rp_record = override
    bridge.install()
    bridge.uninstall()
    assert rp._get_today_rp_record is override
    bridge.install()

    async def replacement(event):
        return rp.record

    rp._get_today_rp_record = replacement
    bridge.refresh()
    assert errors and not bridge.status()["connected"]
    bridge.uninstall()
    assert rp._get_today_rp_record is replacement
    assert not awards


def test_failed_rp_does_not_award_and_retains_original_exception():
    bridge, rp, event, _, awards, _ = make_bridge()

    async def failure(event):
        raise RuntimeError("database unavailable")

    rp._get_today_rp_record = failure
    bridge.install()
    with pytest.raises(RuntimeError, match="database unavailable"):
        asyncio.run(rp.rp(event))
    assert not awards


def test_failed_award_preserves_rp_result_and_records_retryable_error():
    async def failed(event, record):
        raise RuntimeError("ledger unavailable")

    bridge, rp, event, _, _, errors = make_bridge(callback=failed)
    bridge.install()
    assert asyncio.run(rp.rp(event)) is rp.record
    assert "ledger unavailable" in errors[0]
    assert bridge.status()["last_error"] == errors[0]


@pytest.mark.parametrize(
    "record",
    [
        {"id": 5, "date": "2026-10-28", "luck_value": 100},
        {"id": 5, "date": "2026-10-30", "luck_value": 100},
        {"id": 5, "date": DAY, "luck_value": True},
        {"id": 5, "date": DAY, "luck_value": 101},
        {"date": DAY, "luck_value": 50},
        {"id": 0, "date": DAY, "luck_value": 50},
        None,
    ],
)
def test_historical_preview_or_invalid_records_do_not_award(record):
    bridge, rp, event, _, awards, errors = make_bridge()
    rp.record = record
    bridge.install()
    assert asyncio.run(rp.rp(event)) is record
    assert not awards and errors


def test_cross_midnight_record_is_attributed_to_its_own_day():
    bridge, rp, event, _, awards, _ = make_bridge()
    days = iter(["2026-10-29", "2026-10-30"])
    bridge._today = lambda: next(days)
    bridge.install()
    asyncio.run(rp.rp(event))
    assert awards[0][1]["date"] == "2026-10-29"


def test_inflight_uninstall_prevents_late_award():
    async def scenario():
        bridge, rp, event, _, awards, _ = make_bridge()
        entered, proceed = asyncio.Event(), asyncio.Event()

        async def delayed(event):
            entered.set()
            await proceed.wait()
            return rp.record

        rp._get_today_rp_record = delayed
        bridge.install()
        task = asyncio.create_task(rp.rp(event))
        await entered.wait()
        bridge.uninstall()
        proceed.set()
        assert await task is rp.record
        assert not awards

    asyncio.run(scenario())


def test_disabled_wrong_plugin_or_class_objects_never_patched():
    bridge, rp, _, stars, _, _ = make_bridge()
    original = rp._get_today_rp_record
    for metadata in (
        SimpleNamespace(name="other", star_cls=rp),
        SimpleNamespace(name="taiko_rp", star_cls=rp, activated=False),
        SimpleNamespace(name="taiko_rp", star_cls=RP),
    ):
        stars[:] = [metadata]
        assert not bridge.install()["connected"]
        assert rp._get_today_rp_record == original


def test_duplicate_bridge_cannot_stack_or_remove_existing_wrapper():
    first, rp, event, _, awards, _ = make_bridge()
    second, _, _, _, _, errors = make_bridge(rp)
    first.install()
    original_wrapper = rp._get_today_rp_record
    assert not second.install()["connected"]
    assert errors
    second.uninstall()
    assert rp._get_today_rp_record is original_wrapper
    asyncio.run(rp.rp(event))
    assert len(awards) == 1
