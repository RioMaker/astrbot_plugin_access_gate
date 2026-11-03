"""Poke reward policy and authenticated OneBot notice parsing."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_REPLY = "嘿嘿，送你一点小幸运～\n积分+{points}"


def _number(config, key, default, minimum, maximum, *, integer=False):
    value = config.get(key, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not minimum <= value <= maximum
        or not math.isfinite(value)
        or (integer and not isinstance(value, int))
    ):
        raise ValueError(f"{key} 必须为 {minimum}～{maximum} 的有效数值")
    return value


@dataclass(frozen=True)
class PokeSettings:
    probability: float
    minimum: int
    maximum: int
    hours: float
    limit: int
    count_mode: str
    reply_mode: str
    reply: str

    @classmethod
    def from_config(cls, config):
        probability = _number(config, "poke_reward_probability", 30, 0, 100)
        minimum = _number(config, "poke_reward_min", 1, 1, 10000, integer=True)
        maximum = _number(config, "poke_reward_max", 6, 1, 10000, integer=True)
        hours = _number(config, "poke_reward_cooldown_hours", 8, 0, 8760)
        limit = _number(config, "poke_reward_max_times", 1, 1, 1000, integer=True)
        if minimum > maximum:
            raise ValueError("戳一戳奖励下限不能大于上限")
        count_mode = config.get("poke_reward_count_mode", "success")
        if count_mode not in ("success", "attempt"):
            raise ValueError("戳一戳计次方式必须为 success 或 attempt")
        reply_mode = config.get("poke_reward_reply_mode", "image")
        if reply_mode not in ("text", "image", "both"):
            raise ValueError("戳一戳回复方式必须为 text、image 或 both")
        reply = config.get("poke_reward_reply", DEFAULT_REPLY)
        if not isinstance(reply, str) or len(reply) > 1000:
            raise ValueError("戳一戳回复文案必须为最多 1000 字的文本")
        return cls(
            probability, minimum, maximum, hours, limit, count_mode, reply_mode, reply
        )

    @property
    def chance(self):
        return round(self.probability * 10000)

    def text(self, receipt, name):
        template = self.reply.strip()
        if "{points}" not in template:
            template += ("\n" if template else "") + "积分+{points}"
        values = {
            "points": str(receipt["points"]),
            "balance": f"{receipt['balance']:.6f}".rstrip("0").rstrip("."),
            "nickname": name,
        }
        # Replace in one pass so a nickname cannot introduce template fields.
        return re.sub(
            r"\{(points|balance|nickname)\}", lambda m: values[m[1]], template
        )


def parse_notice(event):
    """Only trust a protocol notice aimed at this bot, never CQ/text lookalikes."""
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    if not isinstance(raw, Mapping):
        return None
    if (raw.get("post_type"), raw.get("notice_type"), raw.get("sub_type")) != (
        "notice",
        "notify",
        "poke",
    ):
        return None
    if str(event.get_platform_name()).casefold() not in {
        "aiocqhttp",
        "onebot",
        "onebot11",
        "napcat",
    }:
        return None
    bot = str(event.get_self_id() or "")
    user = str(event.get_sender_id() or "")
    group = str(event.get_group_id() or "")
    if (
        not bot
        or not user
        or user == bot
        or str(raw.get("target_id", "")) != bot
        or str(raw.get("self_id", "")) != bot
        or str(raw.get("user_id", "")) != user
        or str(raw.get("group_id") or "") != group
    ):
        return None
    stamp = raw.get("time")
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        return None
    if not 0 <= stamp <= 253402300799 or not math.isfinite(stamp):
        return None
    # AstrBot assigns a fresh random message_id when converting each notice.
    # Use upstream fields so a repeated delivery cannot roll or award twice.
    key = json.dumps(
        [
            bot,
            user,
            group,
            float(stamp),
            str(raw.get("event_id") or raw.get("message_id") or ""),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return {"event_id": hashlib.sha256(key.encode()).hexdigest(), "bot_id": bot}
