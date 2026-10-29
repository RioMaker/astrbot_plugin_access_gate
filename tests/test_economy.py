"""Financial invariants across RP, admission, pricing and reconciliation."""

import asyncio
import copy
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_access_gate import economy, ledger
from astrbot_plugin_access_gate.pricing import DEFAULTS
from astrbot_plugin_access_gate.runtime import RuntimeGateDenied


class Event:
    def __init__(self, user="u1", group="g1", platform="aiocqhttp"):
        self.user, self.group, self.platform = user, group, platform

    def get_sender_id(self):
        return self.user

    def get_group_id(self):
        return self.group

    def get_platform_name(self):
        return self.platform

    def get_sender_name(self):
        return "测试用户"


def response(input_=98000, output=2000, cached=0):
    return {
        "raw_completion": {
            "usage": {
                "prompt_tokens": input_,
                "completion_tokens": output,
                "prompt_cache_hit_tokens": cached,
                "prompt_cache_miss_tokens": input_ - cached,
            }
        }
    }


PROVIDER = SimpleNamespace(provider_config={"id": "test", "model": "deepseek-flash"})
WHEN = datetime(2026, 10, 29, 20, tzinfo=economy.BEIJING)


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(economy, "now", lambda: WHEN)
    result = economy.Economy(tmp_path / "credits.db")
    yield result
    result.store.close()


def run(coro):
    return asyncio.run(coro)


def award(service, user="u1", group="g1", rp=50):
    return run(
        service.award(Event(user, group), {"date": "2026-10-29", "luck_value": rp})
    )


def settings(service, **changes):
    payload = run(service.settings())
    payload["values"].update(changes)
    return run(service.update_settings(payload, "admin"))


def test_rp_daily_unique_across_groups_and_restart(service, tmp_path):
    assert award(service)
    assert not award(service, group="g2", rp=100)
    other = economy.Economy(tmp_path / "credits.db")
    try:
        assert not award(other)
        assert run(other.balance("qq", "u1")) == 20
        assert run(other.ledger({}))["total"] == 1
    finally:
        other.store.close()


def test_parallel_rp_credit_is_atomic(service):
    person = economy.identity(Event())
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(
            pool.map(
                lambda _: service.store.award(
                    person, "2026-10-29", 50, 20, WHEN.isoformat()
                ),
                range(40),
            )
        )
    assert sum(outcomes) == 1
    assert run(service.balance("qq", "u1")) == 20


def test_rp_receipt_uses_actual_award_and_latest_balance(service):
    record = {"date": "2026-10-29", "luck_value": 73}
    first = run(service.award(Event(), record, with_receipt=True))
    assert first == {
        "status": "awarded",
        "awarded_points": 20.46,
        "daily_points": 20.46,
        "balance": 20.46,
        "date": "2026-10-29",
    }
    ticket = run(service.begin(Event(), "", PROVIDER, {}))
    run(service.settle(ticket, response(), False))
    settings(service, rp_base_points=500, rp_step_points=10)
    repeat = run(service.award(Event(group="other"), record, with_receipt=True))
    assert repeat == {
        "status": "claimed",
        "awarded_points": 0,
        "daily_points": 20.46,
        "balance": 18.34,
        "date": "2026-10-29",
    }
    assert "access_gate_credit" not in record


def test_parallel_receipts_only_one_reports_new_points(service):
    person = economy.identity(Event())
    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(
            pool.map(
                lambda _: service.store.award(
                    person, "2026-10-29", 50, 20, WHEN.isoformat(), True
                ),
                range(16),
            )
        )
    assert sum(item["status"] == "awarded" for item in receipts) == 1
    assert sum(item["awarded_points"] for item in receipts) == 20
    assert all(item["balance"] == 20 for item in receipts)


def test_default_cost_is_212_points_and_snapshot_survives_settings_change(service):
    award(service)
    ticket = run(service.begin(Event(), "", PROVIDER, {}))
    settings(service, points_per_yuan=200)
    run(service.settle(ticket, response(), False))
    item = run(service.ledger({"kind": "consumption"}))["items"][0]
    assert item["points_delta"] == -2.12
    assert item["cost_yuan"] == 0.106
    assert item["total_tokens"] == 100000
    assert item["details"]["price"]["points_per_yuan"] == 20
    assert run(service.balance("qq", "u1")) == 17.88


def test_independent_plugin_does_not_consume_points_or_budget(service):
    settings(service, global_daily_yuan=0, group_daily_yuan=0)
    ticket = run(service.begin(Event(), "astrbot_plugin_liuyao", PROVIDER, {}))
    assert ticket is None
    run(service.settle(ticket, response(), False))
    data = run(service.overview())
    assert data["global"]["spent_yuan"] == 0
    assert data["global"]["reserved_yuan"] == 0
    assert run(service.ledger({}))["total"] == 0
    assert run(service.balance("qq", "u1")) == 0


def test_legacy_plugin_charge_setting_is_ignored(service):
    payload = service.store.settings(copy.deepcopy(DEFAULTS))
    payload["values"]["charged_plugins"] = ["astrbot_plugin_liuyao"]
    service.store.save_settings(
        payload["revision"], payload["values"], "admin", WHEN.isoformat()
    )
    assert run(service.settings())["values"]["charged_plugins"] == []
    ticket = run(service.begin(Event(), "astrbot_plugin_liuyao", PROVIDER, {}))
    assert ticket is None
    run(service.settle(ticket, response(), False))
    assert run(service.balance("qq", "u1")) == 0


def test_global_budget_applies_to_gate_chat(service):
    award(service)
    settings(service, global_daily_yuan=0.212)
    ticket = run(service.begin(Event(), "", PROVIDER, {}))
    with pytest.raises(RuntimeGateDenied, match="全局"):
        run(service.begin(Event("u2", "g2"), "", PROVIDER, {}))
    run(service.settle(ticket, response(input_=300000, output=0), False))
    assert run(service.overview())["global"]["remaining_yuan"] == 0
    with pytest.raises(RuntimeGateDenied, match="全局"):
        run(service.begin(Event("u3", "g3"), "", PROVIDER, {}))


def test_group_override_and_default_are_separate(service):
    award(service)
    award(service, user="u2", group="g2")
    settings(service, group_daily_yuan=0, group_overrides={"g1": 1, "qq:g2": 0.5})
    with pytest.raises(RuntimeGateDenied, match="本群"):
        run(service.begin(Event(group="g3"), "", PROVIDER, {}))
    a = run(service.begin(Event(group="g1"), "", PROVIDER, {}))
    b = run(service.begin(Event(user="u2", group="g2"), "", PROVIDER, {}))
    run(service.settle(a, response(), False))
    run(service.settle(b, response(), False))
    groups = {item["group_id"]: item for item in run(service.overview())["groups"]}
    assert groups["g1"]["limit_yuan"] == 1
    assert groups["g2"]["limit_yuan"] == 0.5


def test_one_positive_balance_allows_finish_then_debt_blocks_next(service):
    run(
        service.adjust(
            {
                "platform": "qq",
                "user_id": "u1",
                "delta": 0.01,
                "reason": "边界验证",
                "request_id": "adjustment_test_01",
            },
            "admin",
        )
    )
    ticket = run(service.begin(Event(), "", PROVIDER, {}))
    run(service.settle(ticket, response(), False))
    assert run(service.balance("qq", "u1")) == -2.11
    with pytest.raises(RuntimeGateDenied, match="积分不足"):
        run(service.begin(Event(), "", PROVIDER, {}))


def test_same_user_parallel_charged_calls_denied(service):
    award(service)
    run(service.begin(Event(), "", PROVIDER, {}))
    with pytest.raises(RuntimeGateDenied, match="正在结算"):
        run(service.begin(Event(group="g2"), "", PROVIDER, {}))


def test_unknown_usage_preserves_reservation_and_can_be_reconciled(service):
    award(service)
    ticket = run(service.begin(Event(), "", PROVIDER, {}))
    run(service.settle(ticket, None, True))
    report = run(service.overview())
    assert report["pending_count"] == 1
    assert report["global"]["reserved_yuan"] == 0.212
    assert report["global"]["spent_yuan"] == 0
    row = run(
        service.resolve(
            {
                "request_id": ticket["request_id"],
                "reason": "已核对供应商账单",
                "usage": {
                    "cache_hit_tokens": 50000,
                    "cache_miss_tokens": 48000,
                    "output_tokens": 2000,
                },
            },
            "admin",
        )
    )
    assert row["cost_yuan"] == 0.057
    assert row["points_delta"] == -1.14
    assert run(service.overview())["global"]["reserved_yuan"] == 0
    with pytest.raises(ValueError):
        run(
            service.resolve(
                {"request_id": ticket["request_id"], "reason": "重复", "cancel": True},
                "admin",
            )
        )


def test_admin_cannot_resolve_active_request(service):
    award(service)
    ticket = run(service.begin(Event(), "", PROVIDER, {}))
    with pytest.raises(ValueError, match="只有用量未知"):
        run(
            service.resolve(
                {"request_id": ticket["request_id"], "reason": "禁止", "cancel": True},
                "admin",
            )
        )


def test_same_process_reload_does_not_expose_inflight_as_unknown(service, tmp_path):
    award(service)
    ticket = run(service.begin(Event(), "", PROVIDER, {}))
    other = economy.Economy(tmp_path / "credits.db")
    try:
        assert other.store.get_request(ticket["request_id"])["status"] == "reserved"
        run(service.settle(ticket, response(), False))
        assert run(other.overview())["global"]["spent_yuan"] == 0.106
    finally:
        other.store.close()


def test_process_crash_preserves_unknown_reservation(service, tmp_path, monkeypatch):
    award(service)
    ticket = run(service.begin(Event(), "", PROVIDER, {}))
    monkeypatch.setattr(ledger, "PROCESS_TOKEN", "new-process")
    other = economy.Economy(tmp_path / "credits.db")
    try:
        assert other.store.get_request(ticket["request_id"])["status"] == "unknown"
        assert run(other.overview())["global"]["reserved_yuan"] == 0.212
    finally:
        other.store.close()


def test_failed_settlement_recovers_as_unknown_on_reload(
    service, tmp_path, monkeypatch
):
    award(service)
    ticket = run(service.begin(Event(), "", PROVIDER, {}))

    def unavailable(*args, **kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(service.store, "settle", unavailable)
    with pytest.raises(OSError):
        run(service.settle(ticket, response(), False))
    assert service.fault
    other = economy.Economy(tmp_path / "credits.db")
    try:
        assert other.store.get_request(ticket["request_id"])["status"] == "unknown"
        row = run(
            other.resolve(
                {
                    "request_id": ticket["request_id"],
                    "reason": "恢复后核对账单",
                    "usage": {
                        "cache_miss_tokens": 98000,
                        "cache_hit_tokens": 0,
                        "output_tokens": 2000,
                    },
                },
                "admin",
            )
        )
        assert row["cost_yuan"] == 0.106
        assert run(other.balance("qq", "u1")) == 17.88
    finally:
        other.store.close()


def test_double_settlement_is_idempotent(service):
    award(service)
    ticket = run(service.begin(Event(), "", PROVIDER, {}))
    run(service.settle(ticket, response(), False))
    run(service.settle(ticket, response(), False))
    assert run(service.balance("qq", "u1")) == 17.88


def test_optimistic_settings_revision(service):
    first = run(service.settings())
    settings(service, group_daily_yuan=3)
    with pytest.raises(ValueError, match="其他页面"):
        run(service.update_settings(first, "admin"))


def test_missing_event_and_unknown_model_fail_before_reserve(service):
    with pytest.raises(RuntimeGateDenied, match="没有有效"):
        run(service.begin(None, "", PROVIDER, {}))
    with pytest.raises(RuntimeGateDenied):
        run(service.begin(Event(), "", PROVIDER, {"model": "unknown-expensive-model"}))
    assert run(service.ledger({}))["total"] == 0


def test_admin_adjust_idempotency_and_conflict(service):
    payload = {
        "platform": "qq",
        "user_id": "u1",
        "delta": 20,
        "reason": "补发",
        "request_id": "idempotent_adjust_1",
    }
    run(service.adjust(payload, "admin"))
    run(service.adjust(payload, "admin"))
    assert run(service.balance("qq", "u1")) == 20
    with pytest.raises(ValueError):
        run(service.adjust({**payload, "delta": 10}, "admin"))


def test_ledger_filters_do_not_interpret_sql(service):
    award(service)
    assert run(service.ledger({"user_id": "u1' OR 1=1 --"}))["total"] == 0


def test_personal_balance_is_exact_even_with_many_substring_matches(service):
    award(service)
    for index in range(40):
        award(service, user=f"prefix-u1-{index}", rp=100)
    assert run(service.balance("qq", "u1")) == 20


def test_quote_current_defaults(service):
    data = run(service.quote())
    assert data["points_per_yuan"] == 20
    assert json.loads(json.dumps(data))["currency"] == "CNY"


def test_beijing_midnight_uses_new_daily_bucket(service, monkeypatch):
    award(service)
    settings(service, global_daily_yuan=0.212)
    ticket = run(service.begin(Event(), "", PROVIDER, {}))
    run(service.settle(ticket, response(input_=212000, output=0), False))
    monkeypatch.setattr(economy, "now", lambda: WHEN.replace(day=30, hour=0))
    new_ticket = run(service.begin(Event(), "", PROVIDER, {}))
    assert new_ticket["request_id"] != ticket["request_id"]
    assert run(service.overview())["global"]["spent_yuan"] == 0


def test_cancel_unknown_with_audit_releases_reserved_money(service):
    award(service)
    ticket = run(service.begin(Event(), "", PROVIDER, {}))
    run(service.settle(ticket, None, True))
    row = run(
        service.resolve(
            {
                "request_id": ticket["request_id"],
                "reason": "上游证实未收费",
                "cancel": True,
            },
            "admin",
        )
    )
    assert row["status"] == "cancelled"
    assert row["details"]["resolved_by"] == "admin"
    assert run(service.overview())["global"]["reserved_yuan"] == 0


@pytest.mark.parametrize("delta", [True, float("nan"), float("inf"), 0, 1000001])
def test_invalid_adjustment_rejected(service, delta):
    with pytest.raises(ValueError):
        run(
            service.adjust(
                {
                    "platform": "qq",
                    "user_id": "u1",
                    "delta": delta,
                    "reason": "test",
                    "request_id": "invalid_adjustment",
                },
                "admin",
            )
        )


def test_financial_settings_reject_nonfinite_values(service):
    values = copy.deepcopy(DEFAULTS)
    values["points_per_yuan"] = float("inf")
    with pytest.raises(ValueError):
        run(service.update_settings({"revision": 0, "values": values}, "admin"))
