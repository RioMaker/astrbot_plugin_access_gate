import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from test_access_gate import main
from test_group_rest import GroupEvent
from test_wake import At, Context


def setup(handler=None):
    context = Context('{"should_reply":true}', {})
    plugin = main.AccessGatePlugin(context, {"group_whitelist": ["20001"]})
    event = GroupEvent("")
    event.message_obj.message = [At()]
    plugin._enforce_access = AsyncMock()
    plugin._enforce_group_rest = AsyncMock()
    plugin._handle_poke = AsyncMock(return_value=False)
    calls = []

    async def help_reply(event):
        calls.append("help")
        event.should_call_llm(True)
        event.stop_event()
        await event.send(event.plain_result("模板帮助图"))
        return True

    callback = AsyncMock(side_effect=handler or help_reply)
    star = SimpleNamespace(
        name="astrbot_plugin_custom_help",
        activated=True,
        module_path="custom.main",
        star_cls=SimpleNamespace(handle_help_shortcut=callback),
    )
    context.get_all_stars = lambda: [star]
    event.extras["activated_handlers"] = [
        SimpleNamespace(handler_module_path="custom.main")
    ]
    return plugin, event, callback, calls, star


@pytest.mark.parametrize("entry", ["enforce_access", "route_wake"])
def test_help_runs_after_access_rest_before_silent_poke_reward(entry):
    plugin, event, callback, calls, _ = setup()

    async def access(event):
        calls.append("access")

    async def rest(event):
        calls.append("rest")

    async def poke(event, *, reply=True):
        assert reply is False
        calls.append("poke_reward")
        return True

    plugin._enforce_access.side_effect = access
    plugin._enforce_group_rest.side_effect = rest
    plugin._handle_poke.side_effect = poke
    asyncio.run(getattr(plugin, entry)(event))
    assert calls == ["access", "rest", "help", "poke_reward"]
    assert event.sent == ["模板帮助图"]
    assert event.is_stopped()
    callback.assert_awaited_once_with(event)


@pytest.mark.parametrize("blocked_at", ["access", "rest"])
def test_permissions_and_rest_never_get_bypassed(blocked_at):
    plugin, event, callback, _, _ = setup()

    async def stop(event):
        event.stop_event()

    getattr(
        plugin, "_enforce_" + ("access" if blocked_at == "access" else "group_rest")
    ).side_effect = stop
    asyncio.run(plugin.enforce_access(event))
    callback.assert_not_awaited()
    plugin._handle_poke.assert_not_awaited()


def test_disabled_or_session_filtered_plugin_is_not_invoked():
    for disabled in ("global", "session"):
        plugin, event, callback, _, star = setup()
        if disabled == "global":
            star.activated = False
        else:
            event.extras["activated_handlers"] = []
        asyncio.run(plugin.enforce_access(event))
        callback.assert_not_awaited()
        assert event.sent == ["我在，请继续说。"]


def test_unconfigured_shortcut_retains_existing_empty_at_reply():
    async def disabled(event):
        return False

    plugin, event, callback, _, _ = setup(disabled)
    asyncio.run(plugin.enforce_access(event))
    callback.assert_awaited_once()
    assert event.sent == ["我在，请继续说。"]


def test_failed_help_does_not_disable_original_wake():
    async def fail(event):
        raise RuntimeError("offline help")

    plugin, event, _, _, _ = setup(fail)
    asyncio.run(plugin.enforce_access(event))
    assert event.sent == ["我在，请继续说。"]


@pytest.mark.parametrize("reason", ["missing", "closed"])
def test_unavailable_runtime_reports_reload_hint_once(monkeypatch, reason):
    plugin, event, callback, _, star = setup()
    logger = Mock()
    monkeypatch.setattr(main, "logger", logger)
    star.version = "2.3.0"
    if reason == "missing":
        star.star_cls = SimpleNamespace()
    else:
        star.star_cls.closed = True

    async def run():
        assert await plugin._handle_custom_help(event) is False
        assert await plugin._handle_custom_help(event) is False

    asyncio.run(run())
    callback.assert_not_awaited()
    logger.warning.assert_called_once()
    assert "重载" in logger.warning.call_args.args[1]


def test_stopped_help_failure_does_not_add_fallback_reply():
    async def fail_after_stop(event):
        event.stop_event()
        raise OSError("send failed")

    plugin, event, _, _, _ = setup(fail_after_stop)
    asyncio.run(plugin.enforce_access(event))
    assert event.sent == []
    plugin._handle_poke.assert_awaited_once_with(event, reply=False)
