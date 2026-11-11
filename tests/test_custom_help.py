import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
def test_help_runs_after_access_rest_and_poke_before_default_wake(entry):
    plugin, event, callback, calls, _ = setup()

    async def access(event):
        calls.append("access")

    async def rest(event):
        calls.append("rest")

    async def poke(event):
        calls.append("poke_reward")
        return True

    plugin._enforce_access.side_effect = access
    plugin._enforce_group_rest.side_effect = rest
    plugin._handle_poke.side_effect = poke
    asyncio.run(getattr(plugin, entry)(event))
    assert calls == ["access", "rest", "poke_reward", "help"]
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
