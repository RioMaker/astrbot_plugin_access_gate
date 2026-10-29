"""Validated CNY price cards and reported-token accounting, without API calls."""

# All invalid settings use ValueError so the management API has one error contract.
# ruff: noqa: TRY004

from __future__ import annotations

import copy
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
PRICE_SOURCE = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
HOLIDAY_SOURCE = (
    "https://www.beijing.gov.cn/zhengce/zhengcefagui/202511/t20251104_4258873.html"
)
# The State Council's published 2026 holiday periods. Weekend make-up workdays
# remain off-peak because DeepSeek defines every Saturday/Sunday as off-peak.
HOLIDAYS_2026 = [
    f"2026-{month:02d}-{day:02d}"
    for month, first, last in (
        (1, 1, 3),
        (2, 15, 23),
        (4, 4, 6),
        (5, 1, 5),
        (6, 19, 21),
        (9, 25, 27),
        (10, 1, 7),
    )
    for day in range(first, last + 1)
]
MODEL_ALIASES = {
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek-v4-flash-vision-exp": "deepseek-flash",
}
DEFAULTS = {
    "points_per_yuan": 20,
    "rp_base_points": 19,
    "rp_step_points": 0.02,
    "global_daily_yuan": 10,
    "group_daily_yuan": 2,
    "reserve_yuan": 0.212,
    "charged_plugins": [],
    "group_overrides": {},
    "holidays": HOLIDAYS_2026.copy(),
    "model_prices": {
        "deepseek-flash": {
            "offpeak": {"input": 1, "cached": 0.02, "output": 4},
            "peak": {"input": 2, "cached": 0.04, "output": 8},
        }
    },
}
MAX_COUNTER = 2**53 - 1
MAX_AMOUNT = Decimal(1000000000)


def _number(value: Any, name: str, *, positive: bool = False) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"{name} 必须是数值")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{name} 数值无效") from exc
    if not result.is_finite() or result < 0 or result > MAX_AMOUNT:
        raise ValueError(f"{name} 必须为 0 至 10 亿之间的有限数值")
    if positive and result <= 0:
        raise ValueError(f"{name} 必须大于 0")
    if result and result < Decimal("0.000001"):
        raise ValueError(f"{name} 精度不能小于百万分之一")
    return int(result) if result == result.to_integral_value() else float(result)


def _label(value: Any, name: str, limit: int = 200) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > limit
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(f"{name} 必须为 1～{limit} 个字符且不含控制字符或首尾空格")
    return value


def validate_settings(values: dict) -> dict:
    """Return an independent, JSON-safe full config; reject misspelled keys."""
    if not isinstance(values, dict):
        raise ValueError("积分配置必须是对象")
    unknown = set(values) - set(DEFAULTS)
    if unknown:
        raise ValueError("未知积分配置字段：" + ", ".join(sorted(map(str, unknown))))
    result = copy.deepcopy(DEFAULTS)
    result.update(copy.deepcopy(values))
    for name in (
        "points_per_yuan",
        "rp_base_points",
        "rp_step_points",
        "global_daily_yuan",
        "group_daily_yuan",
        "reserve_yuan",
    ):
        result[name] = _number(
            result[name], name, positive=name in {"points_per_yuan", "reserve_yuan"}
        )
    plugins = result["charged_plugins"]
    if not isinstance(plugins, list) or len(plugins) > 500:
        raise ValueError("charged_plugins 必须是最多 500 项的插件名称列表")
    result["charged_plugins"] = list(
        dict.fromkeys(_label(value, "插件名称") for value in plugins)
    )
    overrides = result["group_overrides"]
    if not isinstance(overrides, dict) or len(overrides) > 5000:
        raise ValueError("group_overrides 必须是最多 5000 项的群额度对象")
    result["group_overrides"] = {
        _label(key, "群标识"): _number(value, "群额度")
        for key, value in overrides.items()
    }
    holidays = result["holidays"]
    if not isinstance(holidays, list) or len(holidays) > 3660:
        raise ValueError("holidays 必须是最多 3660 项的 YYYY-MM-DD 日期列表")
    dates = []
    for value in holidays:
        if not isinstance(value, str) or len(value) != 10:
            raise ValueError("节假日必须采用 YYYY-MM-DD 格式")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("节假日必须是有效的 YYYY-MM-DD 日期") from exc
        if parsed.isoformat() != value:
            raise ValueError("节假日必须采用 YYYY-MM-DD 格式")
        dates.append(value)
    result["holidays"] = sorted(set(dates))
    prices = result["model_prices"]
    if not isinstance(prices, dict) or not prices or len(prices) > 100:
        raise ValueError("model_prices 必须包含 1～100 个模型价目表")
    cards = {}
    for model, card in prices.items():
        model = _label(model, "模型名称")
        if not isinstance(card, dict) or set(card) != {"offpeak", "peak"}:
            raise ValueError(f"{model} 必须同时包含且仅包含 offpeak 和 peak")
        cards[model] = {}
        for tier, rates in card.items():
            if not isinstance(rates, dict) or set(rates) != {
                "input",
                "cached",
                "output",
            }:
                raise ValueError(
                    f"{model}/{tier} 需包含 input、cached、output 三种单价"
                )
            cards[model][tier] = {
                key: _number(value, f"{model}/{tier}/{key}")
                for key, value in rates.items()
            }
    result["model_prices"] = cards
    return result


def _calendar_warning(settings: dict, year: int) -> str | None:
    dates = settings["holidays"]
    if year == 2026 and set(HOLIDAYS_2026).issubset(dates):
        return None
    return (
        f"{year} 年节假日表未经本版本完整核验；按配置日期及周末判定低谷，"
        "请依据官方日历维护 holidays。"
    )


def price_snapshot(settings: dict, model: str, started_datetime: datetime) -> dict:
    """Freeze the request-start price; callers persist it with the ledger row."""
    settings = validate_settings(settings)
    model = _label(model, "模型名称")
    cards = settings["model_prices"]
    priced_model = model if model in cards else MODEL_ALIASES.get(model, model)
    if priced_model not in cards:
        raise ValueError(f"模型 {model} 尚未配置价格，无法核算成本")
    if (
        not isinstance(started_datetime, datetime)
        or started_datetime.tzinfo is None
        or started_datetime.utcoffset() is None
    ):
        raise ValueError("请求时间必须包含时区")
    local = started_datetime.astimezone(CHINA_TZ)
    minute = local.hour * 60 + local.minute
    peak = (
        local.weekday() < 5
        and local.date().isoformat() not in settings["holidays"]
        and (9 * 60 <= minute < 12 * 60 or 14 * 60 <= minute < 18 * 60)
    )
    tier = "peak" if peak else "offpeak"
    return {
        "model": model,
        "priced_model": priced_model,
        "tier": tier,
        "rates": copy.deepcopy(cards[priced_model][tier]),
        "points_per_yuan": settings["points_per_yuan"],
        "currency": "CNY",
        "price_unit_tokens": 1_000_000,
        "started_at": local.isoformat(),
        "price_source": PRICE_SOURCE
        if priced_model == "deepseek-flash"
        else "configured",
        "calendar_warning": _calendar_warning(settings, local.year),
    }


def _field(obj: Any, key: str, default: Any = None) -> Any:
    return (
        obj.get(key, default)
        if isinstance(obj, Mapping)
        else getattr(obj, key, default)
    )


def _counter(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= MAX_COUNTER else None


def _count(obj: Any, key: str) -> int | None:
    return _counter(_field(obj, key))


def _pack_usage(
    input_tokens: int | None,
    cached: int | None,
    output: int | None,
    source: str,
    reported_total: Any = None,
) -> dict | None:
    if any(value is None for value in (input_tokens, cached, output)):
        return None
    if cached > input_tokens or input_tokens + output > MAX_COUNTER:
        return None
    total = input_tokens + output
    if reported_total is not None and _counter(reported_total) != total:
        return None
    return {
        "input_tokens": input_tokens,
        "cache_hit_tokens": cached,
        "cache_miss_tokens": input_tokens - cached,
        "output_tokens": output,
        "total_tokens": total,
        "usage_source": source,
    }


def _read_wire_usage(usage: Any, source: str) -> dict | None:
    if usage is None:
        return None
    # DeepSeek's two explicit counters take precedence over the framework's
    # generic usage, which can lose its cache-hit information.
    has_hit = _field(usage, "prompt_cache_hit_tokens") is not None
    has_miss = _field(usage, "prompt_cache_miss_tokens") is not None
    if has_hit or has_miss:
        hit = _count(usage, "prompt_cache_hit_tokens")
        miss = _count(usage, "prompt_cache_miss_tokens")
        input_ = _count(usage, "prompt_tokens")
        if input_ is None and hit is not None and miss is not None:
            input_ = hit + miss
        if input_ is None or (has_hit and hit is None) or (has_miss and miss is None):
            return None
        hit = input_ - miss if hit is None and miss is not None else hit
        miss = input_ - hit if miss is None and hit is not None else miss
        if hit is None or miss is None or min(hit, miss) < 0 or hit + miss != input_:
            return None
        return _pack_usage(
            input_,
            hit,
            _count(usage, "completion_tokens"),
            source + ":deepseek",
            _field(usage, "total_tokens"),
        )
    # Anthropic input_tokens excludes cache reads and writes. Cache writes are
    # treated as uncached input by this three-rate card, not added to cache hits.
    if any(
        _field(usage, key) is not None
        for key in ("cache_read_input_tokens", "cache_creation_input_tokens")
    ):
        other = _count(usage, "input_tokens")
        hit = _counter(_field(usage, "cache_read_input_tokens", 0))
        created = _counter(_field(usage, "cache_creation_input_tokens", 0))
        if any(value is None for value in (other, hit, created)):
            return None
        return _pack_usage(
            other + hit + created,
            hit,
            _count(usage, "output_tokens"),
            source + ":anthropic",
            _field(usage, "total_tokens"),
        )
    input_ = _count(usage, "prompt_tokens")
    if input_ is None:
        input_ = _count(usage, "input_tokens")
    output = _count(usage, "completion_tokens")
    if output is None:
        output = _count(usage, "output_tokens")
    details = _field(usage, "prompt_tokens_details")
    if details is None:
        details = _field(usage, "input_tokens_details")
    cached = _counter(_field(details, "cached_tokens", 0))
    return _pack_usage(
        input_, cached, output, source + ":openai", _field(usage, "total_tokens")
    )


def normalize_usage(response: Any) -> dict | None:
    """Read complete reported usage. Unknown or inconsistent counters are None."""
    raw = _field(response, "raw_completion")
    raw_usage = _field(raw, "usage")
    if raw_usage is not None:
        return _read_wire_usage(raw_usage, "raw_completion")
    usage = _field(response, "usage")
    other = _count(usage, "input_other")
    cached = _count(usage, "input_cached")
    output = _count(usage, "output")
    if other is not None and cached is not None and output is not None:
        # AstrBot creates TokenUsage() even when the upstream omitted usage.
        if not (other or cached or output):
            return None
        return _pack_usage(other + cached, cached, output, "astrbot")
    result = _read_wire_usage(usage, "response")
    return result if result and result["total_tokens"] else None


def cost_yuan(usage: dict, snapshot: dict) -> Decimal:
    """Exact Decimal cost; never round each token, call, or rate to whole cents."""
    if not isinstance(usage, dict) or not isinstance(snapshot, dict):
        raise ValueError("计费必须提供有效用量与价格快照")
    values = {}
    for name in ("cache_miss_tokens", "cache_hit_tokens", "output_tokens"):
        value = _counter(usage.get(name))
        if value is None:
            raise ValueError(f"{name} 必须是非负整数")
        values[name] = value
    rates = snapshot.get("rates")
    if not isinstance(rates, dict):
        raise ValueError("价格快照缺少 rates")
    amounts = {
        name: Decimal(str(_number(rates.get(name), f"rates.{name}")))
        for name in ("input", "cached", "output")
    }
    return (
        values["cache_miss_tokens"] * amounts["input"]
        + values["cache_hit_tokens"] * amounts["cached"]
        + values["output_tokens"] * amounts["output"]
    ) / Decimal(1_000_000)


def quote(settings: dict) -> dict:
    """JSON-safe illustrative mappings; token equivalents depend on token type."""
    settings = validate_settings(settings)
    ratio = Decimal(str(settings["points_per_yuan"]))
    unit_yuan = Decimal(1) / ratio
    models = []
    for model, card in settings["model_prices"].items():
        tiers = {}
        for tier, rates in card.items():
            sample = {
                "input_tokens": 98_000,
                "cache_hit_tokens": 0,
                "cache_miss_tokens": 98_000,
                "output_tokens": 2000,
                "total_tokens": 100_000,
            }
            cost = cost_yuan(sample, {"rates": rates})
            cached_sample = dict(
                sample, cache_hit_tokens=88_200, cache_miss_tokens=9800
            )
            cached_cost = cost_yuan(cached_sample, {"rates": rates})
            tiers[tier] = {
                "rates": rates,
                "one_point_tokens": {
                    name: float(unit_yuan * 1_000_000 / Decimal(str(value)))
                    if value
                    else None
                    for name, value in rates.items()
                },
                "example_100k": {
                    **sample,
                    "yuan": float(cost),
                    "points": float(cost * ratio),
                },
                "example_100k_90_percent_cached": {
                    **cached_sample,
                    "yuan": float(cached_cost),
                    "points": float(cached_cost * ratio),
                },
            }
        models.append({"model": model, "tiers": tiers})
    return {
        "currency": "CNY",
        "points_per_yuan": settings["points_per_yuan"],
        "yuan_per_point": float(unit_yuan),
        "rp_rewards": [
            {
                "rp": rp,
                "points": settings["rp_base_points"] + rp * settings["rp_step_points"],
                "yuan": float(
                    (
                        Decimal(str(settings["rp_base_points"]))
                        + rp * Decimal(str(settings["rp_step_points"]))
                    )
                    / ratio
                ),
            }
            for rp in (0, 25, 50, 75, 100)
        ],
        "models": models,
        "warnings": [
            "100k 示例假设输入 98k、输出 2k；实际按每次返回的缓存命中/未命中及输出分别计费。",
            "一积分对应的 token 数分别按纯输入、纯缓存输入、纯输出展示，不能相加。",
            "金额为依据所配置单价计算的成本，不是上游账户余额或官方账单。",
            "内置已核实的节假日日历仅覆盖 2026 年；下一年度需更新 holidays。",
        ],
    }
