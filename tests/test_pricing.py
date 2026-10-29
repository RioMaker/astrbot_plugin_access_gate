"""Financial boundary checks against independently calculated expectations."""

import copy
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pricing import (
    CHINA_TZ,
    DEFAULTS,
    cost_yuan,
    normalize_usage,
    price_snapshot,
    quote,
    validate_settings,
)


def stamp(day, hour, minute=0):
    return datetime.fromisoformat(f"2026-{day}T{hour:02d}:{minute:02d}:00+08:00")


@pytest.mark.parametrize(
    ("day", "hour", "minute", "tier"),
    [
        ("10-29", 8, 59, "offpeak"),
        ("10-29", 9, 0, "peak"),
        ("10-29", 11, 59, "peak"),
        ("10-29", 12, 0, "offpeak"),
        ("10-29", 13, 59, "offpeak"),
        ("10-29", 14, 0, "peak"),
        ("10-29", 17, 59, "peak"),
        ("10-29", 18, 0, "offpeak"),
        ("10-31", 10, 0, "offpeak"),  # Saturday
        ("10-10", 10, 0, "offpeak"),  # Saturday make-up workday
        ("10-01", 10, 0, "offpeak"),  # National Day
        ("02-23", 10, 0, "offpeak"),  # Spring Festival vacation
    ],
)
def test_beijing_peak_boundaries_weekends_and_holidays(day, hour, minute, tier):
    snapshot = price_snapshot({}, "deepseek-flash", stamp(day, hour, minute))
    assert snapshot["tier"] == tier
    assert snapshot["calendar_warning"] is None


def test_request_start_and_timezone_determine_rate_and_snapshot_is_independent():
    settings = validate_settings({})
    start = stamp("10-29", 9).astimezone(timezone.utc)
    snapshot = price_snapshot(settings, "deepseek-flash", start)
    settings["points_per_yuan"] = 10
    settings["model_prices"]["deepseek-flash"]["peak"]["input"] = 99
    assert snapshot["tier"] == "peak"
    assert snapshot["rates"]["input"] == 2
    assert snapshot["points_per_yuan"] == 20
    with pytest.raises(ValueError, match="时区"):
        price_snapshot({}, "deepseek-flash", start.replace(tzinfo=None))


def test_unknown_models_fail_and_only_documented_aliases_get_flash_rates():
    for alias in ("deepseek-v4-flash", "deepseek-v4-flash-vision-exp"):
        assert (
            price_snapshot({}, alias, stamp("10-29", 9))["priced_model"]
            == "deepseek-flash"
        )
    for unknown in ("deepseek-v4-pro", "deepseek-chat", "deepseek-flash-fake"):
        with pytest.raises(ValueError, match="尚未配置"):
            price_snapshot({}, unknown, stamp("10-29", 9))


def test_custom_model_and_holiday_are_supported_without_assuming_complete_calendar():
    values = {
        "holidays": ["2027-01-01"],
        "model_prices": {
            "mine": {
                "offpeak": {"input": 3, "cached": 1, "output": 8},
                "peak": {"input": 4, "cached": 2, "output": 10},
            }
        },
    }
    snapshot = price_snapshot(values, "mine", datetime(2027, 1, 1, 10, tzinfo=CHINA_TZ))
    assert snapshot["tier"] == "offpeak"
    assert snapshot["rates"]["input"] == 3
    assert "2027" in snapshot["calendar_warning"]


@pytest.mark.parametrize(
    "invalid",
    [
        {"point_per_yuan": 1000},
        {"points_per_yuan": float("nan")},
        {"points_per_yuan": float("inf")},
        {"points_per_yuan": 0},
        {"points_per_yuan": True},
        {"global_daily_yuan": -1},
        {"global_daily_yuan": "10"},
        {"reserve_yuan": 0},
        {"reserve_yuan": 1e-50},
        {"group_overrides": {"g": float("inf")}},
        {"group_overrides": {" g": 2}},
        {"charged_plugins": "anything"},
        {"charged_plugins": ["a\nb"]},
        {"charged_plugins": ["a"] * 501},
        {"holidays": ["2026-02-30"]},
        {"holidays": ["20260101"]},
        {"model_prices": {}},
        {"model_prices": {"x": {"offpeak": {}, "peak": {}, "wrong": {}}}},
        {
            "model_prices": {
                "x": {
                    "offpeak": {"input": 1, "cached": 1, "output": 1},
                    "peak": {"input": 1, "cached": 1, "output": float("nan")},
                }
            }
        },
    ],
)
def test_invalid_admin_configuration_rejected(invalid):
    with pytest.raises(ValueError):
        validate_settings(invalid)


def test_defaults_are_not_mutated_and_config_is_json_safe():
    old = copy.deepcopy(DEFAULTS)
    settings = validate_settings({"points_per_yuan": Decimal("1000.5")})
    settings["holidays"].append("2027-01-01")
    assert DEFAULTS == old
    json.dumps(settings, allow_nan=False)
    assert validate_settings({"global_daily_yuan": 0})["global_daily_yuan"] == 0


def test_raw_deepseek_cache_wins_over_lossy_astrbot_usage_and_cost_is_exact():
    response = SimpleNamespace(
        usage=SimpleNamespace(input_other=98_000, input_cached=0, output=2000),
        raw_completion={
            "usage": {
                "prompt_tokens": 98_000,
                "prompt_cache_hit_tokens": 88_200,
                "prompt_cache_miss_tokens": 9800,
                "completion_tokens": 2000,
                "total_tokens": 100_000,
            }
        },
    )
    usage = normalize_usage(response)
    assert usage["cache_hit_tokens"] == 88_200
    assert usage["cache_miss_tokens"] == 9800
    assert usage["input_tokens"] == 98_000
    assert usage["total_tokens"] == 100_000
    off = price_snapshot({}, "deepseek-flash", stamp("10-29", 20))
    peak = price_snapshot({}, "deepseek-flash", stamp("10-29", 10))
    assert cost_yuan(usage, off) == Decimal("0.019564")
    assert cost_yuan(usage, peak) == Decimal("0.039128")


@pytest.mark.parametrize("raw", [None, {}, {"usage": None}])
def test_astrbot_empty_usage_sentinel_is_unknown(raw):
    assert (
        normalize_usage(
            SimpleNamespace(
                usage=SimpleNamespace(input_other=0, input_cached=0, output=0),
                raw_completion=raw,
            )
        )
        is None
    )
    assert (
        normalize_usage({"usage": {"prompt_tokens": 0, "completion_tokens": 0}}) is None
    )


def test_explicit_upstream_zero_is_measured_zero():
    usage = normalize_usage(
        {
            "raw_completion": {
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                }
            }
        }
    )
    assert usage["total_tokens"] == 0


@pytest.mark.parametrize(
    "wire",
    [
        {"prompt_tokens": 12, "completion_tokens": 2, "prompt_cache_hit_tokens": 13},
        {"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 99},
        {"prompt_tokens": True, "completion_tokens": 2},
        {"prompt_tokens": 12.0, "completion_tokens": 2},
        {"prompt_tokens": 12, "completion_tokens": -2},
        {"prompt_tokens": 12},
        {
            "prompt_tokens": 12,
            "completion_tokens": 2,
            "prompt_cache_hit_tokens": 10,
            "prompt_cache_miss_tokens": 4,
        },
    ],
)
def test_partial_inconsistent_or_invalid_usage_is_unknown(wire):
    assert normalize_usage({"raw_completion": {"usage": wire}}) is None


def test_deepseek_missing_counter_infers_only_from_reported_input():
    usage = normalize_usage(
        {
            "raw_completion": {
                "usage": {
                    "prompt_tokens": 100,
                    "prompt_cache_hit_tokens": 80,
                    "completion_tokens": 2,
                }
            }
        }
    )
    assert usage["cache_miss_tokens"] == 20
    assert usage["total_tokens"] == 102


def test_openai_responses_anthropic_and_astrbot_cache_semantics():
    for response in (
        {
            "raw_completion": {
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 2,
                    "input_tokens_details": {"cached_tokens": 80},
                }
            }
        },
        {
            "raw_completion": {
                "usage": {
                    "input_tokens": 15,
                    "output_tokens": 2,
                    "cache_read_input_tokens": 80,
                    "cache_creation_input_tokens": 5,
                }
            }
        },
        SimpleNamespace(
            usage=SimpleNamespace(input_other=20, input_cached=80, output=2)
        ),
    ):
        usage = normalize_usage(response)
        assert usage["input_tokens"] == 100
        assert usage["cache_hit_tokens"] == 80
        assert usage["cache_miss_tokens"] == 20
        assert usage["total_tokens"] == 102


def test_fractional_cost_not_rounded_to_free():
    usage = {"cache_hit_tokens": 1, "cache_miss_tokens": 0, "output_tokens": 0}
    assert cost_yuan(
        usage, {"rates": {"input": 1, "cached": 0.02, "output": 4}}
    ) == Decimal("0.00000002")


def test_quote_explains_100k_assumptions_and_ratio_changes_points_only():
    normal = quote({})
    double = quote({"points_per_yuan": 40})
    tiers = normal["models"][0]["tiers"]
    assert normal["yuan_per_point"] == 0.05
    assert tiers["offpeak"]["example_100k"]["yuan"] == 0.106
    assert tiers["peak"]["example_100k"]["yuan"] == 0.212
    assert tiers["peak"]["example_100k"]["points"] == 4.24
    assert double["models"][0]["tiers"]["peak"]["example_100k"]["points"] == 8.48
    assert tiers["offpeak"]["one_point_tokens"] == {
        "input": 50000,
        "cached": 2500000,
        "output": 12500,
    }
    assert normal["rp_rewards"][0]["points"] == 19
    assert normal["rp_rewards"][2]["points"] == 20
    assert normal["rp_rewards"][-1]["points"] == 21
    json.dumps(normal, allow_nan=False)
