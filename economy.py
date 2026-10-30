"""Pricing, account identity and asynchronous ledger service for Access Gate."""

from __future__ import annotations

import asyncio
import copy
import math
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .ledger import FAILED_REQUESTS, Ledger
from .pricing import (
    DEFAULTS,
    cost_yuan,
    normalize_usage,
    price_snapshot,
    quote,
    validate_settings,
)
from .runtime import RuntimeGateDenied

BEIJING = timezone(timedelta(hours=8))


def now():
    return datetime.now(BEIJING)


def field(obj, name, default=None):
    return (
        obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)
    )


def identifier(value, label, *, required=True):
    if (
        not isinstance(value, str)
        or len(value) > 200
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError(f"{label}格式无效。")
    if required and not value.strip():
        raise ValueError(f"{label}不能为空。")
    return value.strip()


def identity(event):
    if event is None:
        raise ValueError(
            "此 LLM 请求没有有效的用户/群归属，请管理员接入 billing_scope 后再调用。"
        )
    platform = str(event.get_platform_name() or "")
    # QQ identifiers belong to the same account across adapters and bot instances.
    if platform.casefold() in {
        "aiocqhttp",
        "qq",
        "qq_official",
        "onebot",
        "onebot11",
        "napcat",
    }:
        platform = "qq"
    user = str(event.get_sender_id() or "")
    group = str(event.get_group_id() or "")
    name_getter = getattr(event, "get_sender_name", None)
    return {
        "platform": identifier(platform, "平台"),
        "user_id": identifier(user, "用户"),
        "group_id": identifier(group, "群号", required=False),
        "name": str(name_getter() or user)[:80] if callable(name_getter) else user,
    }


class Economy:
    def __init__(self, path):
        self.store = Ledger(path)
        self._settings_lock = asyncio.Lock()
        self.fault = ""

    def _settings(self):
        payload = self.store.settings(copy.deepcopy(DEFAULTS))
        # Retain the legacy key for old databases/clients, but it no longer
        # enables billing for independent plugin calls.
        payload["values"]["charged_plugins"] = []
        return payload

    async def settings(self):
        return await asyncio.to_thread(self._settings)

    async def update_settings(self, payload, actor):
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("revision"), int)
            or isinstance(payload.get("revision"), bool)
        ):
            raise ValueError("请提交设置版本号及完整 values 对象。")  # noqa: TRY004 - public API validation
        values = validate_settings(payload.get("values"))
        async with self._settings_lock:
            return await asyncio.to_thread(
                self.store.save_settings,
                payload["revision"],
                values,
                identifier(actor, "管理员"),
                now().isoformat(),
            )

    async def award(self, event, record, *, with_receipt=False):
        today = now().date()
        day = record.get("date")
        rp = record.get("luck_value")
        if (
            day not in {today.isoformat(), (today - timedelta(days=1)).isoformat()}
            or not isinstance(rp, int)
            or isinstance(rp, bool)
            or not 0 <= rp <= 100
        ):
            return None if with_receipt else False
        person = identity(event)
        settings = (await self.settings())["values"]
        points = (
            Decimal(str(settings["rp_base_points"]))
            + Decimal(str(settings["rp_step_points"])) * rp
        )
        return await asyncio.to_thread(
            self.store.award, person, day, rp, points, now().isoformat(), with_receipt
        )

    @staticmethod
    def provider_info(provider, kwargs):
        config = getattr(provider, "provider_config", {}) or {}
        model = kwargs.get("model")
        if not model:
            getter = getattr(provider, "get_model", None)
            model = (
                getter() if callable(getter) else getattr(provider, "model_name", None)
            )
        model = str(model or config.get("model") or "")
        provider_id = str(
            config.get("id") or getattr(provider, "id", "") or type(provider).__name__
        )
        return model, provider_id

    async def begin(self, event, plugin, provider, kwargs):
        if plugin:
            return None
        if self.fault or FAILED_REQUESTS:
            raise RuntimeGateDenied(
                "积分账本写入异常，已暂停新 LLM 请求，请管理员检查存储。"
            )
        try:
            person = identity(event)
            settings = (await self.settings())["values"]
            started = now()
            model, provider_id = self.provider_info(provider, kwargs)
            price = price_snapshot(settings, model, started)
            charged = True
            overrides = settings["group_overrides"]
            group_limit = overrides.get(
                f"{person['platform']}:{person['group_id']}",
                overrides.get(person["group_id"], settings["group_daily_yuan"]),
            )
            request_id = uuid.uuid4().hex
            details = {
                "price": price,
                "provider_id": provider_id,
                "model": model,
                "charged": charged,
                "estimate_note": "预留为可配置估值，实际用量结算后可能超出本次预留及日额度。",
            }
            # Reserve admission itself must finish before cancellation can abandon
            # it; an admitted but cancelled request is conservatively unresolved.
            task = asyncio.create_task(
                asyncio.to_thread(
                    self.store.reserve,
                    request_id,
                    person,
                    started.isoformat(),
                    "chat",
                    charged,
                    details,
                    {"global": settings["global_daily_yuan"], "group": group_limit},
                    settings["reserve_yuan"],
                )
            )
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                await asyncio.to_thread(
                    self.store.settle, request_id, None, None, True, now().isoformat()
                )
                raise
            return {"request_id": request_id, "price": price}
        except ValueError as exc:
            raise RuntimeGateDenied(str(exc)) from None

    async def settle(self, ticket, response, failed):
        if ticket is None:
            return
        try:
            usage = normalize_usage(response)
            cost = cost_yuan(usage, ticket["price"]) if usage is not None else None
            settled = await asyncio.to_thread(
                self.store.settle,
                ticket["request_id"],
                usage,
                cost,
                failed,
                now().isoformat(),
            )
            if settled:
                receipt = await asyncio.to_thread(
                    self.store.get_request, ticket["request_id"]
                )
                if receipt and receipt["status"] == "settled":
                    # Use the committed deduction, including ledger rounding and
                    # the request's original exchange rate, not a new estimate.
                    return -Decimal(str(receipt["points_delta"]))
        except Exception:
            # Preserve this terminal failure across plugin module reloads, while
            # live requests from the same process retain their reserved state.
            FAILED_REQUESTS.add(ticket["request_id"])
            self.fault = "账本结算写入失败；重启后未结算请求将保留为待核对。"
            raise

    async def overview(self):
        values = (await self.settings())["values"]
        result = await asyncio.to_thread(
            self.store.overview, now().date().isoformat(), values
        )
        result["global"] = result.pop("global_")
        return result

    @staticmethod
    def page(value):
        try:
            number = int(value)
        except (ValueError, TypeError):
            raise ValueError("页码必须为正整数。") from None
        if not 1 <= number <= 1_000_000:
            raise ValueError("页码超出范围。")
        return number

    async def users(self, query="", page=1):
        query = identifier(query, "搜索内容", required=False)
        ratio = (await self.settings())["values"]["points_per_yuan"]
        return await asyncio.to_thread(self.store.users, query, self.page(page), ratio)

    async def balance(self, platform, user_id):
        return await asyncio.to_thread(self.store.balance, platform, user_id)

    async def ledger(self, filters):
        clean = {
            key: identifier(str(value), "筛选条件", required=False)
            for key, value in filters.items()
            if key in {"day", "user_id", "group_id", "platform", "kind", "status"}
        }
        clean["page"] = self.page(filters.get("page", 1))
        return await asyncio.to_thread(self.store.history, clean)

    async def quote(self):
        return quote((await self.settings())["values"])

    async def adjust(self, payload, actor):
        if not isinstance(payload, dict):
            raise ValueError("调整参数必须为对象。")  # noqa: TRY004 - public API validation
        person = {
            "platform": identifier(payload.get("platform"), "平台"),
            "user_id": identifier(payload.get("user_id"), "用户"),
            "name": "",
            "group_id": "",
        }
        delta = payload.get("delta")
        if (
            isinstance(delta, bool)
            or not isinstance(delta, (int, float))
            or not math.isfinite(delta)
            or not 0 < abs(delta) <= 1_000_000
        ):
            raise ValueError("调整积分必须是非零有限数，绝对值不超过 1000000。")
        reason = identifier(payload.get("reason"), "调整原因")
        operation_id = identifier(payload.get("request_id"), "操作编号")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{16,80}", operation_id):
            raise ValueError("操作编号必须是 16～80 位字母数字或横线。")
        return await asyncio.to_thread(
            self.store.adjust,
            person,
            delta,
            reason,
            "admin:" + operation_id,
            identifier(actor, "管理员"),
            now().isoformat(),
        )

    async def resolve(self, payload, actor):
        if not isinstance(payload, dict):
            raise ValueError("核对参数必须为对象。")  # noqa: TRY004 - public API validation
        request_id = identifier(payload.get("request_id"), "请求编号")
        reason = identifier(payload.get("reason"), "核对依据")
        row = await asyncio.to_thread(self.store.get_request, request_id)
        if not row or row["kind"] != "consumption" or row["status"] != "unknown":
            raise ValueError("只有用量未知的 LLM 请求可人工核对。")
        cancel = payload.get("cancel", False)
        if not isinstance(cancel, bool):
            raise ValueError("cancel 必须是布尔值。")  # noqa: TRY004 - public API validation
        usage, cost = None, None
        if not cancel:
            supplied = payload.get("usage")
            if not isinstance(supplied, dict):
                raise ValueError("请提交缓存未命中/命中/输出 token 数。")
            usage = {}
            for key in ("cache_miss_tokens", "cache_hit_tokens", "output_tokens"):
                value = supplied.get(key)
                if (
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or not 0 <= value <= 100_000_000
                ):
                    raise ValueError(f"{key} 必须是 0～100000000 的整数。")
                usage[key] = value
            usage["input_tokens"] = (
                usage["cache_miss_tokens"] + usage["cache_hit_tokens"]
            )
            usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
            usage["usage_source"] = "admin_reconciliation"
            cost = cost_yuan(usage, row["details"]["price"])
        done = await asyncio.to_thread(
            self.store.settle,
            request_id,
            usage,
            cost,
            False,
            now().isoformat(),
            identifier(actor, "管理员"),
            reason,
            cancel,
        )
        if not done:
            raise ValueError("请求已被其他管理员处理，请刷新。")
        return await asyncio.to_thread(self.store.get_request, request_id)
