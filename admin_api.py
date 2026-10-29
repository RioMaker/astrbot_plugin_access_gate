"""Authenticated AstrBot Plugin Page endpoints for the credit ledger."""

from __future__ import annotations

import importlib
import inspect
import logging
from typing import Any

LOGGER = logging.getLogger(__name__)
PLUGIN_NAME = "astrbot_plugin_access_gate"


class _APIInactiveError(Exception):
    """The plugin instance owning this handler has been unloaded."""


async def _result(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class AdminAPI:
    """Keep request parsing and Dashboard authorization outside the ledger."""

    def __init__(self, plugin: Any, web: Any = None) -> None:
        self.plugin = plugin
        self.active = True
        self.web = web or importlib.import_module("astrbot.api.web")
        try:
            workspace = importlib.import_module("astrbot.core.workspace")
            self.api_key_prefix = workspace.API_KEY_USERNAME_PREFIX
        except (ImportError, AttributeError):
            self.api_key_prefix = None

    def register(self) -> None:
        self.active = True
        for name in ("overview", "users", "ledger", "settings", "quote", "plugins"):
            self.plugin.context.register_web_api(
                f"/{PLUGIN_NAME}/credits/{name}",
                getattr(self, name),
                ["GET"],
                f"积分管理：{name}",
            )
        for name in ("settings", "adjust", "resolve"):
            handler = self.save_settings if name == "settings" else getattr(self, name)
            self.plugin.context.register_web_api(
                f"/{PLUGIN_NAME}/credits/{name}", handler, ["POST"], f"积分管理：{name}"
            )

    def unregister(self) -> None:
        """Disable stale handlers without editing AstrBot's route registry.

        AstrBot replaces same-path/same-method routes on the next register().
        The inactive flag also protects old handler references during reload.
        Operations already admitted to the ledger may finish normally.
        """
        self.active = False

    def _ensure_active(self) -> None:
        if not self.active:
            raise _APIInactiveError

    def _actor(self) -> str:
        username = self.web.request.username
        if not isinstance(username, str) or not username.strip():
            raise PermissionError("请先登录 AstrBot 管理页面")
        if self.api_key_prefix and username.startswith(self.api_key_prefix):
            raise PermissionError("积分管理仅允许 AstrBot 管理页面登录用户访问")
        return username.strip()

    async def _run(self, operation: Any) -> Any:
        try:
            self._ensure_active()
            actor = self._actor()
            data = await operation(actor)
            return self.web.json_response(data)
        except _APIInactiveError:
            return self.web.error_response(
                "插件已停用或正在重载，请刷新后重试", status_code=503
            )
        except PermissionError as exc:
            return self.web.error_response(str(exc), status_code=403)
        except ValueError as exc:
            return self.web.error_response(str(exc), status_code=400)
        except Exception:
            LOGGER.exception("积分管理接口执行失败")
            return self.web.error_response(
                "积分管理暂不可用，请检查 AstrBot 日志", status_code=500
            )

    def _query(self, key: str, default: str = "") -> str:
        value = self.web.request.query.get(key, default)
        if not isinstance(value, str) or len(value) > 200:
            raise ValueError(f"{key} 参数格式错误或过长")
        return value.strip()

    def _page(self) -> int:
        raw = self._query("page", "1")
        if not raw.isdecimal() or not 1 <= int(raw) <= 1_000_000:
            raise ValueError("页码必须为 1 至 1000000 的整数")
        return int(raw)

    async def _payload(self) -> dict[str, Any]:
        value = await self.web.request.json(default=None)
        self._ensure_active()
        if not isinstance(value, dict):
            raise ValueError("请求正文必须为 JSON 对象")  # noqa: TRY004 - public validation error
        return dict(value)

    async def overview(self) -> Any:
        async def operation(_actor: str) -> Any:
            data = dict(await _result(self.plugin.economy.overview()))
            data["integration"] = await _result(self.plugin.credit_status())
            return data

        return await self._run(operation)

    async def users(self) -> Any:
        async def operation(_actor: str) -> Any:
            return await _result(
                self.plugin.economy.users(self._query("query"), self._page())
            )

        return await self._run(operation)

    async def ledger(self) -> Any:
        async def operation(_actor: str) -> Any:
            filters = {
                key: self._query(key)
                for key in ("platform", "user_id", "group_id", "kind", "status", "day")
            }
            filters["page"] = self._page()
            return await _result(self.plugin.economy.ledger(filters))

        return await self._run(operation)

    async def settings(self) -> Any:
        async def operation(_actor: str) -> Any:
            return await _result(self.plugin.economy.settings())

        return await self._run(operation)

    async def save_settings(self) -> Any:
        async def operation(actor: str) -> Any:
            payload = await self._payload()
            if not isinstance(payload.get("values"), dict):
                raise ValueError("values 必须为配置对象")  # noqa: TRY004 - public validation error
            revision = payload.get("revision")
            if (
                isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 0
            ):
                raise ValueError("revision 必须为非负整数，请刷新页面后重试")
            return await _result(self.plugin.economy.update_settings(payload, actor))

        return await self._run(operation)

    async def quote(self) -> Any:
        async def operation(_actor: str) -> Any:
            return await _result(self.plugin.economy.quote())

        return await self._run(operation)

    async def plugins(self) -> Any:
        async def operation(_actor: str) -> Any:
            return await _result(self.plugin.credit_plugins())

        return await self._run(operation)

    async def adjust(self) -> Any:
        async def operation(actor: str) -> Any:
            payload = await self._payload()
            payload.pop("actor", None)
            return await _result(self.plugin.economy.adjust(payload, actor))

        return await self._run(operation)

    async def resolve(self) -> Any:
        async def operation(actor: str) -> Any:
            payload = await self._payload()
            payload.pop("actor", None)
            return await _result(self.plugin.economy.resolve(payload, actor))

        return await self._run(operation)
