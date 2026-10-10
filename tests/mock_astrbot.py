"""Small AstrBot API shim used by the plugin unit tests."""

import sys
from types import ModuleType, SimpleNamespace


class FakeLogger:
    def __init__(self) -> None:
        self.records: list[tuple[object, ...]] = []

    def info(self, *args: object) -> None:
        self.records.append(args)

    def error(self, *args: object) -> None:
        self.records.append(args)


class AstrBotConfig(dict):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.saved = False

    def save_config(self) -> None:
        self.saved = True


class Context:
    def __init__(self, config: dict[str, object] | None = None) -> None:
        self._config = AstrBotConfig(config or {})

    def get_config(self) -> AstrBotConfig:
        return self._config


class Star:
    def __init__(self, context: Context) -> None:
        self.context = context


class AstrMessageEvent:
    pass


class EventMessageType:
    ALL = "all"


class FilterNamespace:
    EventMessageType = EventMessageType

    @staticmethod
    def event_message_type(event_type: object, priority: int = 0):
        def decorator(func):
            func.__event_type__ = event_type
            func.__priority__ = priority
            return func

        return decorator

    @staticmethod
    def command(_name: str, priority: int = 0):
        def decorator(func):
            func.__priority__ = priority
            return func

        return decorator


def register(*_args: object, **_kwargs: object):
    def decorator(cls):
        return cls

    return decorator


def install() -> FakeLogger:
    logger = FakeLogger()
    astrbot = ModuleType("astrbot")
    api = ModuleType("astrbot.api")
    event = ModuleType("astrbot.api.event")
    star = ModuleType("astrbot.api.star")
    core = ModuleType("astrbot.core")
    core_star = ModuleType("astrbot.core.star")
    core_filter = ModuleType("astrbot.core.star.filter")
    command = ModuleType("astrbot.core.star.filter.command")

    api.AstrBotConfig = AstrBotConfig
    api.logger = logger
    event.AstrMessageEvent = AstrMessageEvent
    event.filter = FilterNamespace
    star.Context = Context
    star.Star = Star
    star.register = register
    command.GreedyStr = str

    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event
    sys.modules["astrbot.api.star"] = star
    sys.modules["astrbot.core"] = core
    sys.modules["astrbot.core.star"] = core_star
    sys.modules["astrbot.core.star.filter"] = core_filter
    sys.modules["astrbot.core.star.filter.command"] = command
    return logger


class MockEvent:
    def __init__(
        self,
        *,
        private: bool,
        message: str = "hello",
        sender_id: str = "10001",
        group_id: str = "",
        umo: str = "",
        admin: bool = False,
        platform: str = "aiocqhttp",
        original_message: str | None = None,
    ) -> None:
        self._private = private
        self._sender_id = sender_id
        self._group_id = group_id
        self._admin = admin
        self._platform = platform
        self.message_str = message
        self.message_obj = SimpleNamespace(
            message_str=original_message if original_message is not None else message
        )
        self.unified_msg_origin = umo or (
            f"aiocqhttp:FriendMessage:{sender_id}"
            if private
            else f"aiocqhttp:GroupMessage:{group_id}"
        )
        self.stopped = False
        self.extras: dict[str, object] = {}

    def is_private_chat(self) -> bool:
        return self._private

    def is_admin(self) -> bool:
        return self._admin

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_group_id(self) -> str:
        return self._group_id

    def get_platform_name(self) -> str:
        return self._platform

    def stop_event(self) -> None:
        self.stopped = True

    def is_stopped(self) -> bool:
        return self.stopped

    def get_extra(self, key: str, default=None):
        return self.extras.get(key, default)

    def plain_result(self, text: str) -> str:
        return text
