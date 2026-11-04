"""Global budgets must control only gate chats and preserve cost attribution."""

import asyncio
from datetime import datetime, timedelta

import pytest
from test_economy import (
    PROVIDER,
    WHEN,
    Event,
    RuntimeGateDenied,
    award,
    economy,
    response,
    run,
    settings,
)


@pytest.fixture(name="service")
def global_service(tmp_path, monkeypatch):
    monkeypatch.setattr(economy, "now", lambda: WHEN)
    result = economy.Economy(tmp_path / "credits.db")
    yield result
    result.store.close()


def test_global_chat_ignores_balance_and_group_limits_but_records_cost(service):
    settings(
        service, global_mode=True, group_daily_yuan=0, group_overrides={"qq:g1": 0}
    )
    ticket = run(service.begin(Event(), "", PROVIDER, {}))
    assert run(service.settle(ticket, response(), False)) == 0
    row = run(service.ledger({}))["items"][0]
    assert row["cost_yuan"] == 0.106
    assert row["total_tokens"] == 100000
    assert row["points_delta"] == row["charged"] == 0
    assert row["details"]["global_mode"] is True
    assert run(service.balance("qq", "u1")) == 0
    report = run(service.overview())
    assert report["global_mode"] is True
    assert report["global"]["spent_yuan"] == report["monthly"]["spent_yuan"] == 0.106
    assert report["totals"]["spent_yuan"] == report["groups"][0]["spent_yuan"] == 0.106
    assert report["groups"][0]["enforced"] is False
    assert run(service.users())["items"][0]["spent_yuan"] == 0.106


def test_global_rp_returns_original_result_without_claiming_or_awarding(service):
    settings(service, global_mode=True)
    record = {"date": WHEN.date().isoformat(), "luck_value": 50}
    assert run(service.award(Event(), record, with_receipt=True)) is None
    assert not run(service.award(Event(), record))
    assert run(service.ledger({}))["total"] == 0
    settings(service, global_mode=False)
    assert award(service)
    settings(service, global_mode=True)
    assert run(service.award(Event(), record, with_receipt=True)) is None
    assert run(service.balance("qq", "u1")) == 20


@pytest.mark.parametrize(
    "limit,label",
    [("global_daily_yuan", "今日全局"), ("global_monthly_yuan", "本月全局")],
)
def test_either_budget_blocks_all_groups_and_independent_plugins_still_work(
    service, limit, label
):
    settings(service, global_mode=True, **{limit: 0.212})
    ticket = run(service.begin(Event(), "", PROVIDER, {}))
    run(service.settle(ticket, response(input_=300000, output=0), False))
    for event in (Event("u2", "g2"), Event("u3", "g3"), Event("u4", "")):
        with pytest.raises(RuntimeGateDenied, match=label):
            run(service.begin(event, "", PROVIDER, {}))
        assert run(service.begin(event, "astrbot_plugin_liuyao", PROVIDER, {})) is None
    assert run(service.ledger({}))["total"] == 1


@pytest.mark.parametrize("limit", ["global_daily_yuan", "global_monthly_yuan"])
def test_parallel_global_reservations_cannot_over_admit(service, limit):
    settings(service, global_mode=True, **{limit: 0.424})

    async def requests():
        return await asyncio.gather(
            *(
                service.begin(Event(str(i), str(i)), "", PROVIDER, {})
                for i in range(12)
            ),
            return_exceptions=True,
        )

    outcomes = run(requests())
    assert sum(isinstance(item, dict) for item in outcomes) == 2
    assert sum(isinstance(item, RuntimeGateDenied) for item in outcomes) == 10
    report = run(service.overview())
    assert (
        report["global"]["reserved_yuan"] == report["monthly"]["reserved_yuan"] == 0.424
    )


def test_daily_reset_does_not_reset_month_and_month_resets_at_beijing_midnight(
    service, monkeypatch
):
    settings(
        service, global_mode=True, global_daily_yuan=0.212, global_monthly_yuan=0.212
    )
    ticket = run(service.begin(Event(), "", PROVIDER, {}))
    run(service.settle(ticket, None, True))
    monkeypatch.setattr(economy, "now", lambda: WHEN + timedelta(days=1))
    report = run(service.overview())
    assert report["global"]["reserved_yuan"] == 0
    assert report["monthly"]["reserved_yuan"] == 0.212
    with pytest.raises(RuntimeGateDenied, match="本月全局"):
        run(service.begin(Event(), "", PROVIDER, {}))
    monkeypatch.setattr(
        economy, "now", lambda: datetime.fromisoformat("2026-11-01T00:00:00+08:00")
    )
    assert run(service.begin(Event(), "", PROVIDER, {}))


def test_unknown_usage_reconciliation_keeps_original_global_mode_after_switch(service):
    settings(service, global_mode=True, global_monthly_yuan=0.212)
    ticket = run(service.begin(Event(), "", PROVIDER, {}))
    run(service.settle(ticket, None, True))
    with pytest.raises(RuntimeGateDenied, match="本月全局"):
        run(service.begin(Event("u2"), "", PROVIDER, {}))
    settings(service, global_mode=False)
    award(service)
    row = run(
        service.resolve(
            {
                "request_id": ticket["request_id"],
                "reason": "核对账单",
                "usage": {
                    "cache_miss_tokens": 98000,
                    "cache_hit_tokens": 0,
                    "output_tokens": 2000,
                },
            },
            "admin",
        )
    )
    assert row["points_delta"] == 0
    assert row["cost_yuan"] == 0.106
    assert run(service.balance("qq", "u1")) == 20


def test_switch_preserves_old_costs_and_inflight_personal_deduction(service):
    award(service)
    ticket = run(service.begin(Event(), "", PROVIDER, {}))
    settings(service, global_mode=True, global_monthly_yuan=0.212)
    assert str(run(service.settle(ticket, response(), False))) == "2.12"
    with pytest.raises(RuntimeGateDenied, match="本月全局"):
        run(service.begin(Event("u2"), "", PROVIDER, {}))
    settings(service, global_mode=False, global_monthly_yuan=0)
    assert run(service.begin(Event(), "", PROVIDER, {}))
    with pytest.raises(RuntimeGateDenied, match="积分不足"):
        run(service.begin(Event("no-points", "g2"), "", PROVIDER, {}))


def test_legacy_saved_settings_gain_defaults_without_revision_or_balance_change(
    service,
):
    award(service)
    payload = run(service.settings())
    del payload["values"]["global_mode"]
    del payload["values"]["global_monthly_yuan"]
    service.store.save_settings(0, payload["values"], "old-client", WHEN.isoformat())
    loaded = run(service.settings())
    assert loaded["revision"] == 1
    assert loaded["values"]["global_mode"] is False
    assert loaded["values"]["global_monthly_yuan"] == 300
    assert run(service.balance("qq", "u1")) == 20


@pytest.mark.parametrize(
    "values",
    [
        {"global_mode": "true"},
        {"global_mode": 1},
        {"global_monthly_yuan": -1},
        {"global_monthly_yuan": float("nan")},
        {"global_monthly_yuan": True},
    ],
)
def test_global_settings_reject_invalid_values(service, values):
    with pytest.raises(ValueError):
        settings(service, **values)


def test_analytics_costs_peaks_rankings_and_pending_are_separate(service, monkeypatch):
    settings(service, global_mode=True)

    def consume(stamp, user, group, tokens):
        monkeypatch.setattr(economy, "now", lambda: datetime.fromisoformat(stamp))
        ticket = run(service.begin(Event(user, group), "", PROVIDER, {}))
        run(
            service.settle(
                ticket, response(input_=tokens, output=0) if tokens else None, False
            )
        )

    consume("2026-10-28T08:00:00+08:00", "old", "old", 100000)
    consume("2026-10-29T08:00:00+08:00", "u1", "g1", 100000)
    consume("2026-10-29T09:00:00+08:00", "u2", "g2", 200000)
    consume("2026-10-29T09:10:00+08:00", "u2", "", 50000)
    consume("2026-10-29T09:15:00+08:00", "u3", "g3", None)
    monkeypatch.setattr(
        economy, "now", lambda: datetime.fromisoformat("2026-10-29T09:30:00+08:00")
    )
    report = run(service.analytics())
    assert report["cost_yuan"] == pytest.approx(0.6)
    assert report["requests"] == 3
    assert report["total_tokens"] == 350000
    assert report["pending_count"] == 1
    assert report["reserved_yuan"] == 0.212
    assert report["recent_hour_yuan"] == 0.5
    assert report["series"][-1]["rate_yuan_per_hour"] == 1
    assert report["series"][-1]["partial"] is True
    assert report["peak_hours"][0] == 9
    assert report["quiet_hours"] == [0, 1, 2]
    assert report["hour_profile"][10]["average_yuan_per_hour"] is None
    assert report["users"][0]["user_id"] == "u2"
    assert report["users"][0]["cost_yuan"] == 0.5
    assert report["groups"][0]["group_id"] == "g2"
    assert any(group["group_id"] == "" for group in report["groups"])
    assert sum(group["cost_yuan"] for group in report["groups"]) == pytest.approx(0.6)
    assert run(service.analytics("7d"))["cost_yuan"] == pytest.approx(0.7)
    assert run(service.analytics("month"))["cost_yuan"] == pytest.approx(0.7)


def test_empty_analytics_and_period_validation(service, monkeypatch):
    monkeypatch.setattr(
        economy, "now", lambda: datetime.fromisoformat("2026-11-01T00:00:00+08:00")
    )
    report = run(service.analytics("month"))
    assert report["cost_yuan"] == 0
    assert (
        report["groups"]
        == report["users"]
        == report["peak_hours"]
        == report["quiet_hours"]
        == []
    )
    assert len(report["series"]) == 1
    assert report["series"][0]["rate_yuan_per_hour"] == 0
    with pytest.raises(ValueError):
        run(service.analytics("all' OR 1=1"))
