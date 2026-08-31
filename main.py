"""Apply independent group and private-chat allowlists before other handlers."""

from collections.abc import Iterable
import json
import re
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr


PLUGIN_NAME = "astrbot_plugin_access_gate"
PLUGIN_AUTHOR = "Rio"
PLUGIN_DESC = "独立过滤群聊和私聊白名单，并允许指定私聊指令绕过"
PLUGIN_VERSION = "1.0.0"
PLUGIN_REPO = ""

# Keep this above ordinary command/listener priorities so denied events are
# stopped before another plugin or the LLM can consume them.
GATE_PRIORITY = 10_000
DEFAULT_PRIVATE_BYPASS_COMMANDS = ("rtlink",)
ENTRY_SEPARATOR = re.compile(r"[\r\n,，;；]+")
COMMAND_PREFIXES = "/!！"
IMPORT_KEYS = ("id_whitelist", "group_whitelist", "private_whitelist", "ids")


@register(
    PLUGIN_NAME,
    PLUGIN_AUTHOR,
    PLUGIN_DESC,
    PLUGIN_VERSION,
    PLUGIN_REPO,
)
class AccessGatePlugin(Star):
    """Stop messages that do not match the configured access policy."""

    def __init__(
        self,
        context: Context,
        config: AstrBotConfig | None = None,
    ) -> None:
        super().__init__(context)
        fallback_config = context.get_config() if config is None else config
        self.config: dict[str, Any] = (
            fallback_config if fallback_config is not None else {}
        )
        logger.info(
            "[access_gate] 已加载；请确保 AstrBot 核心 enable_id_white_list 已关闭"
        )

    def _bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, str):
            normalized = value.strip().casefold()
            if normalized in {"true", "1", "yes", "on", "是", "开"}:
                return True
            if normalized in {"false", "0", "no", "off", "否", "关"}:
                return False
        return bool(value)

    def _entries(
        self,
        key: str,
        default: Iterable[str] = (),
    ) -> set[str]:
        raw = self.config.get(key, list(default))
        if isinstance(raw, str):
            values: Iterable[object] = ENTRY_SEPARATOR.split(raw)
        elif isinstance(raw, Iterable) and not isinstance(raw, (dict, bytes)):
            values = raw
        else:
            values = ()
        return set(self._dedupe_entries(values))

    @staticmethod
    def _dedupe_entries(values: Iterable[object]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            if value is None:
                continue
            entry = str(value).strip()
            if not entry or entry in seen:
                continue
            seen.add(entry)
            result.append(entry)
        return result

    @classmethod
    def _entries_from_json(cls, value: object) -> list[str]:
        if isinstance(value, dict):
            for key in IMPORT_KEYS:
                if key in value:
                    return cls._entries_from_json(value[key])
            return []
        if isinstance(value, str):
            return cls._dedupe_entries(ENTRY_SEPARATOR.split(value))
        if isinstance(value, Iterable) and not isinstance(value, (dict, bytes)):
            return cls._dedupe_entries(value)
        if value is None:
            return []
        return cls._dedupe_entries((value,))

    @classmethod
    def _parse_batch_entries(cls, payload: str) -> list[str]:
        text = str(payload or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
            text = re.sub(r"\s*```$", "", text).strip()
        if not text:
            return []

        candidates = (text, "{" + text.rstrip("\r\n ,") + "}")
        for candidate in candidates:
            try:
                decoded = json.loads(candidate)
            except (json.JSONDecodeError, TypeError):
                continue
            entries = cls._entries_from_json(decoded)
            if entries:
                return entries

        # Accept plain lines, comma-separated IDs and JSON-like fragments.
        # Only numeric IDs or full UMO-style values containing ':' are kept,
        # so labels such as id_whitelist are not accidentally imported.
        tokens = re.split(r"[\s,，;；\[\]{}]+", text)
        cleaned = (
            token.strip("\"'")
            for token in tokens
            if token.strip("\"'")
        )
        return cls._dedupe_entries(
            token for token in cleaned if token.isdigit() or token.count(":") >= 2
        )

    def _current_entries(self, key: str) -> list[str]:
        raw = self.config.get(key, [])
        if isinstance(raw, str):
            values: Iterable[object] = ENTRY_SEPARATOR.split(raw)
        elif isinstance(raw, Iterable) and not isinstance(raw, (dict, bytes)):
            values = raw
        else:
            values = ()
        return self._dedupe_entries(values)

    def _save_import(self, key: str, imported: list[str]) -> tuple[int, int]:
        existing = self._current_entries(key)
        merged = self._dedupe_entries((*existing, *imported))
        had_key = key in self.config
        previous = self.config.get(key)
        self.config[key] = merged
        try:
            save_config = getattr(self.config, "save_config", None)
            if not callable(save_config):
                raise RuntimeError("当前配置对象不支持 save_config()")
            save_config()
        except Exception:
            if had_key:
                self.config[key] = previous
            else:
                self.config.pop(key, None)
            raise
        return len(merged) - len(existing), len(merged)

    @staticmethod
    def _event_identifiers(event: AstrMessageEvent, private: bool) -> set[str]:
        primary = event.get_sender_id() if private else event.get_group_id()
        unified_origin = getattr(event, "unified_msg_origin", "")
        return {
            str(value).strip()
            for value in (primary, unified_origin)
            if value is not None and str(value).strip()
        }

    @staticmethod
    def _message_candidates(event: AstrMessageEvent) -> tuple[str, ...]:
        candidates: list[str] = []
        for value in (
            getattr(event, "message_str", ""),
            getattr(getattr(event, "message_obj", None), "message_str", ""),
        ):
            text = str(value or "").strip()
            if text and text not in candidates:
                candidates.append(text)
        return tuple(candidates)

    @staticmethod
    def _root_command(text: str) -> str:
        first_token = text.strip().split(maxsplit=1)[0] if text.strip() else ""
        return first_token.lstrip(COMMAND_PREFIXES).casefold()

    def _is_private_bypass_command(self, event: AstrMessageEvent) -> bool:
        commands = {
            self._root_command(value)
            for value in self._entries(
                "private_bypass_commands",
                DEFAULT_PRIVATE_BYPASS_COMMANDS,
            )
        }
        commands.discard("")
        return any(
            self._root_command(text) in commands
            for text in self._message_candidates(event)
        )

    def _is_webchat(self, event: AstrMessageEvent) -> bool:
        platform = str(event.get_platform_name() or "").strip().casefold()
        return platform == "webchat"

    def _log_denied(self, event: AstrMessageEvent, scope: str) -> None:
        if not self._bool("log_denied", True):
            return
        sender_id = event.get_sender_id()
        group_id = event.get_group_id()
        unified_origin = getattr(event, "unified_msg_origin", "")
        logger.info(
            f"[access_gate] 已拦截{scope}：sender={sender_id} "
            f"group={group_id} umo={unified_origin}"
        )

    @filter.command("accessgate", priority=GATE_PRIORITY + 1)
    async def accessgate_command(
        self,
        event: AstrMessageEvent,
        content: GreedyStr,
    ):
        """管理员批量导入群聊或私聊白名单。"""
        # This recovery command must run before the gate itself. Non-admins are
        # silently stopped to preserve normal whitelist-denial behavior.
        event.stop_event()
        if not event.is_admin():
            return

        text = str(content or "").strip()
        if text.casefold() in {"help", "帮助"}:
            yield event.plain_result(
                "批量导入白名单：\n"
                "/accessgate import group <JSON、逐行或逗号分隔的群号>\n"
                "/accessgate import private <JSON、逐行或逗号分隔的用户ID>\n"
                "中文同义写法：/accessgate 导入 群聊|私聊 <内容>\n"
                "导入会与现有名单合并并自动去重。"
            )
            return

        parts = text.split(None, 2)
        if len(parts) != 3 or parts[0].casefold() not in {"import", "导入"}:
            yield event.plain_result("格式错误，发送 /accessgate help 查看用法。")
            return

        scope = parts[1].casefold()
        targets = {
            "group": ("group_whitelist", "群聊"),
            "群聊": ("group_whitelist", "群聊"),
            "private": ("private_whitelist", "私聊"),
            "私聊": ("private_whitelist", "私聊"),
        }
        target = targets.get(scope)
        if target is None:
            yield event.plain_result("导入目标只能是 group/群聊 或 private/私聊。")
            return

        imported = self._parse_batch_entries(parts[2])
        if not imported:
            yield event.plain_result("没有识别到可导入的 ID，请检查粘贴内容。")
            return

        config_key, scope_label = target
        try:
            added, total = self._save_import(config_key, imported)
        except Exception as exc:
            logger.error(f"[access_gate] 保存{scope_label}白名单失败：{exc}")
            yield event.plain_result(f"导入失败：配置未保存（{exc}）。")
            return
        yield event.plain_result(
            f"{scope_label}白名单导入完成：识别 {len(imported)} 条，"
            f"新增 {added} 条，当前共 {total} 条。"
        )

    @filter.event_message_type(filter.EventMessageType.ALL, priority=GATE_PRIORITY)
    async def enforce_access(self, event: AstrMessageEvent) -> None:
        """Apply the access gate before normal commands and message handlers."""
        if self._bool("webchat_bypass", True) and self._is_webchat(event):
            return

        if event.is_private_chat():
            if self._is_private_bypass_command(event):
                return
            if self._bool("private_admin_bypass", True) and event.is_admin():
                return
            if not self._bool("private_whitelist_enabled", True):
                return
            allowed = self._event_identifiers(event, private=True)
            whitelist = self._entries("private_whitelist")
            if allowed & whitelist:
                return
            self._log_denied(event, "私聊")
            event.stop_event()
            return

        # EventMessageType.ALL can also include non-chat platform events. Only
        # apply the group policy when AstrBot supplies an actual group ID.
        if not str(event.get_group_id() or "").strip():
            return
        if self._bool("group_admin_bypass", True) and event.is_admin():
            return
        if not self._bool("group_whitelist_enabled", True):
            return
        allowed = self._event_identifiers(event, private=False)
        whitelist = self._entries("group_whitelist")
        if allowed & whitelist:
            return
        self._log_denied(event, "群聊")
        event.stop_event()
