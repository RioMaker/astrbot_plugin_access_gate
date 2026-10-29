"""Optional adapter for the installed taiko RP plugin's real daily records."""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
RP_NAMES = frozenset({"astrbot_plugin_taiko_rp", "taiko_rp"})
METHOD = "_get_today_rp_record"
_MISSING = object()


def _field(obj: Any, name: str, default: Any = None) -> Any:
    return (
        obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)
    )


class RPBridge:
    """Wrap instances only; uninstall restores exactly the previous attribute.

    The ledger callback must atomically deduplicate (source, sender, record.date).
    Both /rp and the existing get_today_rp tool pass through this shared method.
    """

    def __init__(self, context, on_award, on_error=None):
        self.context = context
        self.on_award = on_award
        self.on_error = on_error
        self._bindings: dict[int, tuple[Any, Any, Any]] = {}
        self._active = False
        self._last_error: str | None = None
        self._tasks: set[asyncio.Task] = set()

    @staticmethod
    def _today() -> str:
        return datetime.now(CHINA_TZ).date().isoformat()

    def _report(self, message: str) -> None:
        self._last_error = message
        if self.on_error is None:
            logging.getLogger(__name__).warning(message)
            return
        try:
            result = self.on_error(message)
            if inspect.isawaitable(result):
                try:
                    task = asyncio.get_running_loop().create_task(result)
                except RuntimeError:
                    if inspect.iscoroutine(result):
                        result.close()
                    return
                self._tasks.add(task)
                task.add_done_callback(self._finish_error_task)
        except Exception:
            logging.getLogger(__name__).exception("RP 关联错误回调失败")

    def _finish_error_task(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()

    @staticmethod
    def _instance(metadata):
        name = _field(metadata, "name")
        if name not in RP_NAMES:
            return None
        instance = _field(metadata, "star_cls")
        if instance is None:
            instance = _field(metadata, "instance")
        if instance is None or isinstance(instance, type):
            return None
        if _field(metadata, "activated", True) is False:
            return None
        return instance

    def install(self) -> dict:
        self._active = True
        return self.refresh()

    def refresh(self) -> dict:
        if not self._active:
            return self.status()
        try:
            metadata = self.context.get_all_stars()
            if not isinstance(metadata, (list, tuple)):
                raise TypeError("插件列表格式不兼容")
        except Exception as exc:  # noqa: BLE001 - optional external plugin boundary
            self._report(f"无法检测 RP 插件：{type(exc).__name__}")
            return self.status()
        found = {}
        for item in metadata:
            instance = self._instance(item)
            if instance is not None:
                found[id(instance)] = instance
        for key in list(self._bindings):
            if key not in found:
                self._restore(key)
        for key, instance in found.items():
            if key in self._bindings:
                if getattr(instance, METHOD, None) is self._bindings[key][2]:
                    continue
                # A newer adapter/extension replaced the attribute. Leave it
                # intact; do not create a recursively stacked wrapper.
                self._report("RP 获取方法被其他组件替换，请重载积分插件以重新关联")
                continue
            original = getattr(instance, METHOD, None)
            if not callable(original) or not inspect.iscoroutinefunction(original):
                self._report("已检测到 RP 插件，但每日记录接口不兼容")
                continue
            if getattr(original, "__access_gate_rp_bridge__", None) is not None:
                self._report("RP 已被另一个积分插件实例关联，请先卸载旧实例")
                continue
            previous = vars(instance).get(METHOD, _MISSING)
            wrapped = self._make_wrapper(original)
            setattr(instance, METHOD, wrapped)
            self._bindings[key] = (instance, previous, wrapped)
        return self.status()

    def _make_wrapper(self, original):
        @functools.wraps(original)
        async def wrapped(event, *args, **kwargs):
            started_day = self._today()
            record = await original(event, *args, **kwargs)
            # Presentation receipts are per request; never retain an earlier
            # caller's success notice in an RP-owned cached record.
            if isinstance(record, dict) and "access_gate_credit" in record:
                record = {
                    key: value
                    for key, value in record.items()
                    if key != "access_gate_credit"
                }
            if not self._active:
                return record
            eligible = False
            try:
                # A request crossing midnight may legitimately return either
                # day's record. Historical/preview records are not accepted.
                allowed_days = {started_day, self._today()}
                if (
                    not isinstance(record, dict)
                    or record.get("date") not in allowed_days
                ):
                    raise ValueError("RP 返回的不是本次请求日期的真实记录")
                score = record.get("luck_value")
                if type(score) is not int or not 0 <= score <= 100:
                    raise ValueError("RP 分数必须为 0～100 的整数")
                record_id = record.get("id")
                if type(record_id) is not int or record_id <= 0:
                    raise ValueError("RP 缺少持久化记录编号")
                sender = str(event.get_sender_id() or "").strip()
                if not sender:
                    raise ValueError("RP 缺少发送者标识")
                eligible = True
                receipt = await self.on_award(event, dict(record))
                if isinstance(receipt, dict) and receipt.get("status") in {
                    "awarded",
                    "claimed",
                }:
                    return {**record, "access_gate_credit": dict(receipt)}
            except Exception as exc:  # noqa: BLE001 - rewards must not break RP
                self._report(f"RP 积分登记失败：{type(exc).__name__}: {exc}")
                if eligible:
                    return {
                        **record,
                        "access_gate_credit": {
                            "status": "error",
                            "date": record["date"],
                        },
                    }
            return record

        wrapped.__access_gate_rp_bridge__ = self
        return wrapped

    def _restore(self, key: int) -> None:
        instance, previous, wrapped = self._bindings.pop(key)
        if getattr(instance, METHOD, None) is not wrapped:
            return
        if previous is _MISSING:
            delattr(instance, METHOD)
        else:
            setattr(instance, METHOD, previous)

    def uninstall(self) -> None:
        self._active = False
        for key in list(self._bindings):
            self._restore(key)
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()

    def status(self) -> dict:
        count = sum(
            getattr(instance, METHOD, None) is wrapped
            for instance, _previous, wrapped in self._bindings.values()
        )
        return {
            "enabled": self._active,
            "connected": self._active and count > 0,
            "instances": count,
            "plugin": "astrbot_plugin_taiko_rp",
            "last_error": self._last_error,
            "message": "已关联真实每日 RP 记录" if count else "未关联兼容的 RP 插件",
        }
