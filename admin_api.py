"""Authenticated AstrBot Plugin Page endpoints for the credit ledger."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
from time import monotonic
from typing import Any

LOGGER = logging.getLogger(__name__)
PLUGIN_NAME = "astrbot_plugin_access_gate"


class _APIInactiveError(Exception):
    """The plugin instance owning this handler has been unloaded."""


async def _result(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class GroupNames:
    """Read display names through OneBot without touching billing identities."""

    QQ_PLATFORMS = (
        "",
        "qq",
        "aiocqhttp",
        "qq_official",
        "onebot",
        "onebot11",
        "napcat",
    )
    LOOKUP_TIMEOUT = 3

    def __init__(self, context: Any) -> None:
        self.context = context
        self.names: dict[str, str] = {}
        self.refresh_at = 0.0
        self.lock = asyncio.Lock()

    async def _fetch(self, client: Any, bot_id: str | None) -> dict[str, str]:
        try:
            params = {"self_id": bot_id} if bot_id is not None else {}
            rows = await asyncio.wait_for(
                client.call_action("get_group_list", **params),
                timeout=self.LOOKUP_TIMEOUT,
            )
            # CQHttp returns the data array; tolerate a wrapped OneBot response.
            if isinstance(rows, dict):
                rows = rows.get("data") if rows.get("status", "ok") == "ok" else None
            if not isinstance(rows, list):
                return {}
            names = {}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                group_id, name = row.get("group_id"), row.get("group_name")
                if type(group_id) not in (str, int) or not isinstance(name, str):
                    continue
                group_id = str(group_id).strip()
                name = "".join(char for char in name if char.isprintable()).strip()[
                    :200
                ]
                if group_id and name:
                    names[group_id] = name
            return names
        except Exception:  # noqa: BLE001 - optional metadata must not break credit data
            # Protocol errors may include connection credentials. Keep them out
            # of both the response and logs; a name lookup is optional.
            LOGGER.debug("群名称获取暂不可用，保留已缓存名称或群号")
            return {}

    async def _refresh(self) -> None:
        async with self.lock:
            if monotonic() < self.refresh_at:
                return
            calls = []
            try:
                manager = getattr(self.context, "platform_manager", None)
                platforms = manager.get_insts() if manager else []
            except Exception:  # noqa: BLE001 - platform discovery is best-effort
                LOGGER.debug("平台列表暂不可用，保留已缓存群名称")
                platforms = []
            for platform in platforms:
                try:
                    if platform.meta().name != "aiocqhttp":
                        continue
                    client = platform.get_client()
                    # A single CQHttp adapter can serve several reverse-WS bots.
                    # Snapshot only routing IDs; never read connection contents.
                    connections = getattr(client, "_wsr_api_clients", None)
                    bot_ids = (
                        list(connections) if isinstance(connections, dict) else [None]
                    )
                    calls.extend(self._fetch(client, bot_id) for bot_id in bot_ids)
                except Exception:  # noqa: BLE001 - isolate failing platform adapters
                    LOGGER.debug("跳过暂不可用的群名称来源")
                    continue
            for names in await asyncio.gather(*calls):
                self.names.update(names)
            # Retain last-known names for historical rows if a bot goes offline.
            while len(self.names) > 4096:
                self.names.pop(next(iter(self.names)))
            self.refresh_at = monotonic() + 60

    async def enrich(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        def supported(row: dict[str, Any]) -> bool:
            return (
                bool(row.get("group_id"))
                and str(row.get("platform") or "").casefold() in self.QQ_PLATFORMS
            )

        if any(supported(row) for row in rows):
            await self._refresh()
        return [
            {
                **row,
                "group_name": self.names.get(str(row["group_id"]), "")
                if supported(row)
                else "",
            }
            for row in rows
        ]


class AdminAPI:
    """Keep request parsing and Dashboard authorization outside the ledger."""

    def __init__(self, plugin: Any, web: Any = None) -> None:
        self.plugin = plugin
        self.active = True
        self.group_names = GroupNames(plugin.context)
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
            data["groups"] = await self.group_names.enrich(data.get("groups", []))
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
            data = dict(await _result(self.plugin.economy.ledger(filters)))
            data["items"] = await self.group_names.enrich(data.get("items", []))
            return data

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
