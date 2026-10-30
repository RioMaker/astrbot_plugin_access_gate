"""Billing invariants against AstrBot-shaped interfaces, without model access."""

import asyncio
import inspect
import sys
import tempfile
import unittest
from decimal import Decimal
from functools import partial, wraps
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_access_gate.economy import Economy
from astrbot_plugin_access_gate.runtime import BillingRuntime, RuntimeGateDenied


def reply(tokens=10, *, chunk=False):
    return SimpleNamespace(
        role="assistant",
        is_chunk=chunk,
        usage=SimpleNamespace(input_other=tokens, input_cached=20, output=3),
        raw_completion=SimpleNamespace(
            usage={
                "prompt_tokens": tokens + 20,
                "prompt_cache_hit_tokens": 20,
                "prompt_cache_miss_tokens": tokens,
                "completion_tokens": 3,
            },
            model="deepseek-flash",
        ),
    )


class Event:
    def __init__(self):
        self.sent = []
        self.stopped = False
        self.extras = {}

    def stop_event(self):
        self.stopped = True

    def is_stopped(self):
        return self.stopped

    def get_sender_id(self):
        return "10001"

    def get_group_id(self):
        return "20001"

    def get_platform_name(self):
        return "aiocqhttp"

    def get_extra(self, key):
        return self.extras.get(key)

    def set_extra(self, key, value):
        self.extras[key] = value

    def plain_result(self, text):
        return text

    async def send(self, text):
        self.sent.append(text)
        self._has_send_oper = True


class ProviderRequest:
    pass


class Plain:
    def __init__(self, text):
        self.text = text


class Result:
    def __init__(self, text="", *, kind="LLM_RESULT", stream=None):
        self.chain = [Plain(text)] if text else []
        self.result_content_type = SimpleNamespace(name=kind)
        self.async_stream = stream

    def text(self):
        return "".join(part.text for part in self.chain if isinstance(part, Plain))


class ResultEvent(Event):
    result = None

    def get_result(self):
        return self.result

    def plain_result(self, text):
        return Result(text, kind="GENERAL_RESULT")


class Provider:
    def __init__(self):
        self.requests = 0
        self.input_tokens = 10
        self.tools_call_name = []

    async def text_chat(self, prompt=None, model=None, **kwargs):
        self.requests += 1
        response = reply(self.input_tokens)
        response.tools_call_name = self.tools_call_name
        return response

    async def text_chat_stream(self, **kwargs):
        yield reply(1, chunk=True)
        yield await self.text_chat(**kwargs)


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.provider = Provider()
        self.providers = [self.provider]
        self.started = []
        self.finished = []
        self.event = Event()

        class Scheduler:
            async def execute(_, event):
                return await event.action()

        class Manager:
            async def load_provider(_, provider):
                self.providers.append(provider)

        class HookRegistry(list):
            def get_handlers_by_event_type(registry, event_type):
                return [item for item in registry if item.event_type.name == event_type]

        class AgentRequestStage:
            async def process(_, event):
                await self.provider.text_chat()
                yield None

        class RespondStage:
            async def process(_, event):
                result = event.get_result()
                sent_texts = event.get_extra(
                    "_send_message_to_user_current_session_plain_texts"
                )
                if (
                    result
                    and isinstance(sent_texts, list)
                    and result.text().strip() in sent_texts
                ):
                    return
                if (
                    result
                    and result.chain
                    and result.result_content_type.name != "STREAMING_FINISH"
                ):
                    await event.send(result)

        async def call_handler(event, handler, **kwargs):
            result = handler(event, **kwargs)
            if inspect.isasyncgen(result):
                async for item in result:
                    yield item
            else:
                yield await result

        async def call_local_llm_tool(context, handler, method_name, **kwargs):
            event = context.context.event
            async for item in call_handler(event, handler, **kwargs):
                yield item

        self.scheduler = Scheduler()
        self.manager = Manager()
        self.handlers = ModuleType("star_request")
        self.handlers.call_handler = call_handler
        self.tool_exec = ModuleType("tool_exec")
        self.tool_exec.call_local_llm_tool = call_local_llm_tool
        scheduler_module = ModuleType("scheduler")
        scheduler_module.PipelineScheduler = Scheduler
        self.hooks = HookRegistry()
        registry_module = ModuleType("star_handler")
        registry_module.star_handlers_registry = self.hooks
        request_stage_module = ModuleType("agent_request")
        request_stage_module.AgentRequestSubStage = AgentRequestStage
        request_entities = ModuleType("entities")
        request_entities.ProviderRequest = ProviderRequest
        self.agent_stage = AgentRequestStage()
        respond_module = ModuleType("respond")
        respond_module.RespondStage = RespondStage
        self.respond_stage = RespondStage()
        modules = {
            "astrbot.core.pipeline.scheduler": scheduler_module,
            "astrbot.core.pipeline.process_stage.method.star_request": self.handlers,
            "astrbot.core.astr_agent_tool_exec": self.tool_exec,
            "astrbot.core.star.star_handler": registry_module,
            "astrbot.core.pipeline.process_stage.method.agent_request": request_stage_module,
            "astrbot.core.provider.entities": request_entities,
            "astrbot.core.pipeline.respond.stage": respond_module,
        }
        self.modules = patch.dict(sys.modules, modules)
        self.modules.start()
        self.stars = [
            SimpleNamespace(
                name="astrbot_plugin_liuyao", module_path="data.plugins.liuyao.main"
            ),
            SimpleNamespace(
                name="astrbot_plugin_access_gate",
                module_path="data.plugins.access_gate.main",
            ),
            SimpleNamespace(name="core", module_path="astrbot.builtin_stars.core.main"),
        ]
        self.context = SimpleNamespace(
            get_all_providers=lambda: self.providers,
            provider_manager=self.manager,
            get_all_stars=lambda: self.stars,
        )

        async def llm_generate(*, chat_provider_id, **kwargs):
            return await self.provider.text_chat(**kwargs)

        self.context.llm_generate = llm_generate

        async def begin(event, plugin, provider, kwargs):
            ticket = len(self.started)
            self.started.append((event, plugin, provider, kwargs))
            return ticket

        async def settle(ticket, response, failed):
            self.finished.append((ticket, response, failed))

        self.runtime = BillingRuntime(self.context, begin, settle)
        self.assertTrue(self.runtime.install())
        self.runtime.mark_chat(self.event)

    async def asyncTearDown(self):
        self.runtime.uninstall()
        await self.runtime.drain()
        self.modules.stop()

    def _loaded_plugin_command(self):
        async def command(plugin, event, content="测试问题"):
            yield await asyncio.wait_for(
                plugin.context.llm_generate(chat_provider_id="test", prompt=content),
                timeout=2,
            )

        command.__module__ = "data.plugins.liuyao.main"
        instance = SimpleNamespace(context=self.context)
        self.stars[0].star_cls = instance
        # StarManager binds registered functions to the loaded plugin this way.
        return partial(command, instance)

    async def _attach_economy(self, **changes):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        service = Economy(Path(directory.name) / "credits.db")
        self.addCleanup(service.store.close)
        self.provider.provider_config = {"id": "test", "model": "deepseek-flash"}
        self.provider.input_tokens = 98000
        self.runtime.begin, self.runtime.settle = service.begin, service.settle
        if changes:
            payload = await service.settings()
            payload["values"].update(changes)
            await service.update_settings(payload, "admin")
        return service

    async def test_loaded_partial_command_and_tool_keep_plugin_identity(self):
        command = self._loaded_plugin_command()
        async for _ in self.handlers.call_handler(self.event, command):
            pass
        context = SimpleNamespace(context=SimpleNamespace(event=self.event))
        async for _ in self.tool_exec.call_local_llm_tool(
            context, command, "decorator_handler"
        ):
            pass
        self.assertEqual(self.provider.requests, 2)
        self.assertEqual(self.started, [])

    async def test_decorated_partial_command_restores_chat_scope_at_yield(self):
        command = self._loaded_plugin_command()

        @wraps(command)
        async def decorated(*args, **kwargs):
            async for item in command(*args, **kwargs):
                yield item

        with self.runtime.billing_scope(self.event):
            iterator = self.handlers.call_handler(self.event, partial(decorated))
            await anext(iterator)
            await self.provider.text_chat()
            await iterator.aclose()
        self.assertEqual([row[1] for row in self.started], [""])

    async def test_loaded_partial_hook_keeps_plugin_identity(self):
        async def hook(plugin, event):
            return await plugin.context.llm_generate(chat_provider_id="test")

        hook.__module__ = "data.plugins.liuyao.main"
        self.hooks.append(
            SimpleNamespace(
                event_type=SimpleNamespace(name="OnLLMResponseEvent"),
                handler=partial(hook, SimpleNamespace(context=self.context)),
            )
        )
        with self.runtime.billing_scope(self.event):
            for metadata in self.hooks.get_handlers_by_event_type("OnLLMResponseEvent"):
                await metadata.handler(self.event)
            await self.provider.text_chat()
        self.assertEqual([row[1] for row in self.started], [""])

    async def test_zero_balance_plugin_passes_but_gate_chat_still_requires_points(self):
        service = await self._attach_economy()
        command = self._loaded_plugin_command()
        with self.runtime.billing_scope(self.event):
            iterator = self.handlers.call_handler(self.event, command)
            try:
                await anext(iterator)
                with self.assertRaisesRegex(RuntimeGateDenied, "积分不足"):
                    await self.provider.text_chat()
            finally:
                await iterator.aclose()
        self.assertEqual(self.provider.requests, 1)
        self.assertEqual(await service.balance("qq", "10001"), 0)
        records = await service.ledger({"kind": "consumption"})
        self.assertEqual(records["total"], 0)
        overview = await service.overview()
        self.assertEqual(overview["global"]["spent_yuan"], 0)
        self.assertEqual(overview["global"]["reserved_yuan"], 0)

    async def test_plugins_ignore_legacy_charge_settings_and_billing_faults(self):
        service = await self._attach_economy(
            charged_plugins=["astrbot_plugin_liuyao"],
            global_daily_yuan=0,
            group_daily_yuan=0,
        )
        service.fault = "账本故障"
        self.provider.provider_config["model"] = "unpriced-model"
        self.runtime._warn("模拟网关兼容故障")
        command = self._loaded_plugin_command()
        with self.runtime.billing_scope(self.event):
            async for _ in self.handlers.call_handler(self.event, command):
                pass
        self.assertEqual(self.provider.requests, 1)
        self.assertEqual((await service.ledger({}))["total"], 0)

    async def test_marked_chat_records_cost_and_deducts_points(self):
        service = await self._attach_economy()
        await service.adjust(
            {
                "platform": "qq",
                "user_id": "10001",
                "delta": 20,
                "reason": "验证门禁会话扣分",
                "request_id": "runtime_plugin_credit_test",
            },
            "admin",
        )
        self.event.action = self.provider.text_chat
        await self.scheduler.execute(self.event)
        self.assertEqual(self.provider.requests, 1)
        self.assertLess(await service.balance("qq", "10001"), 20)
        records = await service.ledger({"kind": "consumption"})
        self.assertEqual(records["total"], 1)
        item = records["items"][0]
        self.assertEqual(item["source_plugin"], "chat")
        self.assertEqual(item["total_tokens"], 98023)
        self.assertLess(item["points_delta"], 0)
        overview = await service.overview()
        self.assertEqual(overview["global"]["spent_yuan"], item["cost_yuan"])
        self.assertEqual(overview["groups"][0]["spent_yuan"], item["cost_yuan"])

    async def test_global_budget_only_blocks_gate_chat(self):
        await self._attach_economy(global_daily_yuan=0)
        with self.runtime.billing_scope(self.event):
            async for _ in self.handlers.call_handler(
                self.event, self._loaded_plugin_command()
            ):
                pass
            with self.assertRaisesRegex(RuntimeGateDenied, "全局"):
                await self.provider.text_chat()
        self.assertEqual(self.provider.requests, 1)

    async def test_group_override_only_blocks_gate_chat(self):
        await self._attach_economy(group_overrides={"qq:20001": 0})
        with self.runtime.billing_scope(self.event):
            async for _ in self.handlers.call_handler(
                self.event, self._loaded_plugin_command()
            ):
                pass
            with self.assertRaisesRegex(RuntimeGateDenied, "本群"):
                await self.provider.text_chat()
        self.assertEqual(self.provider.requests, 1)

    async def test_unmarked_session_and_background_calls_pass_through(self):
        service = await self._attach_economy(global_daily_yuan=0)
        self.event.extras.clear()
        self.event.action = self.provider.text_chat
        await self.scheduler.execute(self.event)
        await self.provider.text_chat()
        async for _ in self.provider.text_chat_stream():
            pass
        self.assertEqual(self.provider.requests, 3)
        self.assertEqual((await service.ledger({}))["total"], 0)

    async def test_unknown_handler_on_marked_event_is_not_assumed_to_be_chat(self):
        await self._attach_economy(global_daily_yuan=0)

        async def handler(event):
            return await self.provider.text_chat()

        with self.runtime.billing_scope(self.event):
            async for _ in self.handlers.call_handler(self.event, handler):
                pass
        self.assertEqual(self.provider.requests, 1)

    async def test_main_chat_has_real_event_and_positional_options(self):
        self.event.action = lambda: self.provider.text_chat("hello", "flash-alias")
        response = await self.scheduler.execute(self.event)
        self.assertIs(self.started[0][0], self.event)
        self.assertEqual(self.started[0][1], "")
        self.assertEqual(self.started[0][3]["model"], "flash-alias")
        self.assertIs(self.finished[0][1], response)
        self.assertFalse(self.finished[0][2])

    async def test_plugin_commands_and_tools_use_plugin_scope(self):
        async def command(event):
            return await self.provider.text_chat()

        command.__module__ = "data.plugins.liuyao.agent_analysis"
        async for _ in self.handlers.call_handler(self.event, command):
            pass
        context = SimpleNamespace(context=SimpleNamespace(event=self.event))
        async for _ in self.tool_exec.call_local_llm_tool(
            context, command, "decorator_handler"
        ):
            pass
        await self.provider.text_chat()
        self.assertEqual(self.provider.requests, 3)
        self.assertEqual(self.started, [])

    async def test_gate_and_builtin_helpers_are_chat_not_exempt_plugin(self):
        async def command(plugin, event):
            return await self.provider.text_chat()

        for module in (
            "data.plugins.access_gate.main",
            "astrbot.builtin_stars.core.main",
        ):
            command.__module__ = module
            async for _ in self.handlers.call_handler(
                self.event, partial(command, object())
            ):
                pass
        self.assertEqual([row[1] for row in self.started], ["", ""])

    async def test_plugin_scope_does_not_leak_across_generator_yield(self):
        async def command(event):
            yield await self.provider.text_chat()
            yield await self.provider.text_chat()

        command.__module__ = "data.plugins.liuyao.main"
        with self.runtime.billing_scope(self.event):
            stream = self.handlers.call_handler(self.event, command)
            await anext(stream)
            await self.provider.text_chat()
            await anext(stream)
            await stream.aclose()
        self.assertEqual(
            [row[1] for row in self.started],
            [""],
        )

    async def test_deny_never_calls_provider_or_settles_and_notifies_once(self):
        async def deny(*args):
            raise RuntimeGateDenied("今日群额度已用完")

        self.runtime.begin = deny
        self.event.action = self.provider.text_chat
        self.assertIsNone(await self.scheduler.execute(self.event))
        self.assertEqual(self.provider.requests, 0)
        self.assertEqual(self.finished, [])
        self.assertTrue(self.event.stopped)
        self.assertEqual(self.event.sent, ["今日群额度已用完"])

    async def test_unknown_calls_never_reach_admission(self):
        async def reject_unknown(event, *args):
            self.fail("Background calls must not reach the gate")

        self.runtime.begin = reject_unknown
        await self.provider.text_chat()
        self.assertEqual(self.provider.requests, 1)

    async def test_finished_event_is_not_inherited_by_delayed_child(self):
        release = asyncio.Event()

        async def child():
            await release.wait()
            await self.provider.text_chat()

        with self.runtime.billing_scope(self.event):
            task = asyncio.create_task(child())
        release.set()
        await task
        self.assertEqual(self.started, [])
        self.assertEqual(self.provider.requests, 1)

    async def test_child_task_using_same_provider_is_a_separate_bill(self):
        provider = Provider()
        base = provider.text_chat

        async def delegated(prompt=None, **kwargs):
            if prompt == "outer":
                await asyncio.create_task(provider.text_chat("child"))
            return await base(prompt, **kwargs)

        provider.text_chat = delegated
        await self.manager.load_provider(provider)
        with self.runtime.billing_scope(self.event):
            await provider.text_chat("outer")
        self.assertEqual(len(self.started), 2)
        self.assertEqual(len(self.finished), 2)

    async def test_same_task_nested_provider_calls_settle_once(self):
        with self.runtime.billing_scope(self.event):
            outputs = [response async for response in self.provider.text_chat_stream()]
        self.assertEqual(len(outputs), 2)
        self.assertEqual(len(self.started), 1)
        self.assertEqual(len(self.finished), 1)
        self.assertEqual(self.finished[0][1].usage.input_other, 10)

    async def test_stream_anext_in_different_tasks_still_has_no_nested_double_charge(
        self,
    ):
        with self.runtime.billing_scope(self.event):
            stream = self.provider.text_chat_stream()
            await asyncio.create_task(anext(stream))
            await asyncio.create_task(anext(stream))
            with self.assertRaises(StopAsyncIteration):
                await asyncio.create_task(anext(stream))
        self.assertEqual(len(self.started), 1)
        self.assertEqual(len(self.finished), 1)

    async def test_stream_snapshot_does_not_change_when_caller_mutates_response(self):
        with self.runtime.billing_scope(self.event):
            stream = self.provider.text_chat_stream()
            await anext(stream)
            final = await anext(stream)
            final.usage.input_other = 99999
            final.raw_completion.usage["prompt_tokens"] = 99999
            with self.assertRaises(StopAsyncIteration):
                await anext(stream)
        saved = self.finished[0][1]
        self.assertEqual(saved.usage.input_other, 10)
        self.assertEqual(saved.raw_completion.usage["prompt_tokens"], 30)

    async def test_stream_early_close_marks_unknown_usage_failed(self):
        with self.runtime.billing_scope(self.event):
            stream = self.provider.text_chat_stream()
            await anext(stream)
            await stream.aclose()
        self.assertEqual(len(self.finished), 1)
        self.assertIsNone(self.finished[0][1])
        self.assertTrue(self.finished[0][2])

    async def test_exception_and_cancellation_each_settle_once(self):
        provider = Provider()

        async def fails(**kwargs):
            raise ValueError("upstream")

        provider.text_chat = fails
        await self.manager.load_provider(provider)
        with self.runtime.billing_scope(self.event), self.assertRaises(ValueError):
            await provider.text_chat()
        entered = asyncio.Event()

        async def pending(**kwargs):
            entered.set()
            await asyncio.Event().wait()

        provider.text_chat = pending
        self.runtime.refresh()
        with self.runtime.billing_scope(self.event):
            task = asyncio.create_task(provider.text_chat())
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(len(self.finished), 2)
        self.assertTrue(all(row[2] for row in self.finished))

    async def test_uninstall_preserves_inflight_settlement_but_stops_new_billing(self):
        entered, release = asyncio.Event(), asyncio.Event()
        provider = Provider()

        async def pending(**kwargs):
            entered.set()
            await release.wait()
            return reply()

        provider.text_chat = pending
        await self.manager.load_provider(provider)
        with self.runtime.billing_scope(self.event):
            task = asyncio.create_task(provider.text_chat())
            await entered.wait()
            self.runtime.uninstall()
            release.set()
            await task
        await self.provider.text_chat()
        self.assertEqual(len(self.started), 1)
        self.assertEqual(len(self.finished), 1)

    async def test_second_cancel_does_not_cancel_owned_settlement(self):
        settling, release = asyncio.Event(), asyncio.Event()

        async def settle(ticket, response, failed):
            settling.set()
            await release.wait()
            self.finished.append((ticket, response, failed))

        self.runtime.settle = settle
        with self.runtime.billing_scope(self.event):
            task = asyncio.create_task(self.provider.text_chat())
            await settling.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.runtime.status()["pending_settlements"], 1)
        self.runtime.uninstall()
        release.set()
        await self.runtime.drain()
        self.assertEqual(len(self.finished), 1)

    async def test_hotloaded_instance_override_is_guarded_before_first_call(self):
        provider = Provider()
        original = provider.text_chat

        async def override(**kwargs):
            return await original(**kwargs)

        provider.text_chat = override
        await self.manager.load_provider(provider)
        with self.runtime.billing_scope(self.event):
            await provider.text_chat()
        self.assertIs(self.started[0][2], provider)

    async def test_incompatible_methods_only_close_marked_chat(self):
        provider = Provider()
        provider.text_chat_stream = lambda **kwargs: None
        await self.manager.load_provider(provider)
        self.assertFalse(self.runtime.status()["complete"])
        self.assertIn("text_chat_stream", self.runtime.status()["errors"][0])
        with self.runtime.billing_scope(self.event):
            with self.assertRaises(RuntimeGateDenied):
                await anext(provider.text_chat_stream())
            with self.assertRaises(RuntimeGateDenied):
                await provider.text_chat()
            with self.assertRaises(RuntimeGateDenied):
                await self.provider.text_chat()
        self.assertEqual(len(self.finished), 0)
        self.assertEqual(provider.requests, 0)
        await provider.text_chat()
        self.assertEqual(provider.requests, 1)

    async def test_installation_failure_does_not_block_other_consumers_or_loading(self):
        self.runtime.uninstall()
        self.handlers.call_handler = lambda *args, **kwargs: None
        self.assertFalse(self.runtime.install())
        self.assertTrue(self.runtime.status()["installed"])
        self.assertFalse(self.runtime.status()["complete"])
        self.runtime.mark_chat(self.event)
        with self.runtime.billing_scope(self.event):
            with self.assertRaises(RuntimeGateDenied):
                await self.provider.text_chat()
            with self.assertRaises(RuntimeGateDenied):
                await anext(self.provider.text_chat_stream())
        await self.provider.text_chat()
        await self.manager.load_provider(Provider())
        await self.providers[-1].text_chat()
        self.assertEqual(self.provider.requests, 1)
        self.assertEqual(len(self.providers), 2)
        self.assertEqual(self.finished, [])

    async def test_reinstall_does_not_reactivate_old_wrapper(self):
        old_wrapper = self.provider.text_chat
        self.runtime.uninstall()
        self.assertTrue(self.runtime.install())
        with self.runtime.billing_scope(self.event):
            await old_wrapper()
            await self.provider.text_chat()
            self.assertEqual(len(self.started), 0)
            self.runtime.mark_chat(self.event)
            await self.provider.text_chat()
        self.assertEqual(len(self.started), 1)

    async def test_hotloaded_hook_is_exempt_and_restores_main_chat_scope(self):
        async def hook(event):
            # Child tasks inherit the hook's identity, including wait_for calls.
            await asyncio.wait_for(self.provider.text_chat(), timeout=1)

        hook.__module__ = "data.plugins.liuyao.main"
        self.hooks.append(
            SimpleNamespace(
                event_type=SimpleNamespace(name="OnLLMResponseEvent"), handler=hook
            )
        )

        async def action():
            for metadata in self.hooks.get_handlers_by_event_type("OnLLMResponseEvent"):
                await metadata.handler(self.event)
            await self.provider.text_chat()

        self.event.action = action
        await self.scheduler.execute(self.event)
        self.assertEqual([row[1] for row in self.started], [""])
        self.assertTrue(all(row[0] is self.event for row in self.started))

    async def test_generator_hook_scope_is_reset_between_yields(self):
        async def hook(event):
            yield await self.provider.text_chat()
            yield await self.provider.text_chat()

        hook.__module__ = "data.plugins.liuyao.main"
        self.hooks.append(
            SimpleNamespace(
                event_type=SimpleNamespace(name="OnCallingFuncToolEvent"), handler=hook
            )
        )
        metadata = self.hooks.get_handlers_by_event_type("OnCallingFuncToolEvent")[0]
        with self.runtime.billing_scope(self.event):
            iterator = metadata.handler(self.event)
            await anext(iterator)
            await self.provider.text_chat()
            await anext(iterator)
            await iterator.aclose()
        self.assertEqual(
            [row[1] for row in self.started],
            [""],
        )

    async def test_lifecycle_hook_does_not_borrow_an_unrelated_message_identity(self):
        async def hook(metadata):
            await self.provider.text_chat()

        hook.__module__ = "data.plugins.liuyao.main"
        self.hooks.append(
            SimpleNamespace(
                event_type=SimpleNamespace(name="OnPluginLoadedEvent"), handler=hook
            )
        )
        metadata = self.hooks.get_handlers_by_event_type("OnPluginLoadedEvent")[0]
        with self.runtime.billing_scope(self.event):
            await metadata.handler(SimpleNamespace(name="some_plugin"))
        self.assertEqual(self.started, [])
        self.assertEqual(self.provider.requests, 1)

    async def test_legacy_plugin_request_exempts_only_its_own_agent_run(self):
        async def command(event):
            yield ProviderRequest()

        command.__module__ = "data.plugins.liuyao.main"

        async def action():
            async for request in self.handlers.call_handler(self.event, command):
                self.event.extras["provider_request"] = request
                async for _ in self.agent_stage.process(self.event):
                    # Work after a yielded Agent result is not plugin work.
                    await self.provider.text_chat()
            # Even if Core keeps the old request in extras, its consumed origin
            # must not exempt an ordinary chat run that follows the command.
            async for _ in self.agent_stage.process(self.event):
                pass

        self.event.action = action
        await self.scheduler.execute(self.event)
        self.assertEqual([row[1] for row in self.started], ["", ""])
        self.assertTrue(all(row[0] is self.event for row in self.started))

    async def test_legacy_request_origin_cannot_transfer_to_another_event(self):
        async def command(event):
            yield ProviderRequest()

        command.__module__ = "data.plugins.liuyao.main"
        other_event = Event()
        self.runtime.mark_chat(other_event)
        stream = self.handlers.call_handler(self.event, command)
        other_event.extras["provider_request"] = await anext(stream)
        async for _ in self.agent_stage.process(other_event):
            pass
        await stream.aclose()
        self.assertEqual(self.started[0][1], "")
        self.assertIs(self.started[0][0], other_event)

    async def test_abandoned_legacy_request_does_not_retain_plugin_exemption(self):
        async def command(event):
            yield ProviderRequest()

        command.__module__ = "data.plugins.liuyao.main"
        stream = self.handlers.call_handler(self.event, command)
        self.event.extras["provider_request"] = await anext(stream)
        await stream.aclose()
        async for _ in self.agent_stage.process(self.event):
            pass
        self.assertEqual(self.started[0][1], "")

    async def _notice_setup(self):
        service = await self._attach_economy()
        event = ResultEvent()
        self.runtime.mark_chat(event)
        await service.adjust(
            {
                "platform": "qq",
                "user_id": "10001",
                "delta": 20,
                "reason": "验证回复实际扣分",
                "request_id": "runtime_credit_notice_test",
            },
            "admin",
        )
        return service, event

    async def _notice_stage(self, process, event, *, deliver=True):
        wrapped = self.runtime._wrap_request_process(
            process, self.runtime._installation
        )
        async for item in wrapped(None, event):
            if deliver:
                await self.respond_stage.process(event)
            yield item

    async def test_final_notice_sums_ledger_and_excludes_independent_tool_cost(self):
        service, event = await self._notice_setup()
        with self.runtime.billing_scope(event):
            await self.provider.text_chat()  # Gate mention classifier.
        self.runtime.mark_chat(event)  # A second mark must retain that receipt.
        history_part = Plain("最后的回答")

        async def process(_, event):
            self.provider.tools_call_name = ["cast_liuyao"]
            await self.provider.text_chat()
            event.result = Result("准备调用工具")
            yield
            with self.runtime.billing_scope(event, "astrbot_plugin_liuyao"):
                await self.provider.text_chat()
            event.result = Result("插件结果", kind="GENERAL_RESULT")
            yield
            self.provider.tools_call_name = []
            await self.provider.text_chat()
            event.result = Result()
            event.result.chain = [history_part]
            yield
            yield  # Re-processing the same result must not add another footer.

        outputs = [event.result async for _ in self._notice_stage(process, event)]
        records = (await service.ledger({"kind": "consumption"}))["items"]
        total = sum(-Decimal(str(row["points_delta"])) for row in records)
        self.assertEqual(len(records), 3)
        self.assertEqual(outputs[0].text(), "准备调用工具")
        self.assertEqual(outputs[1].text(), "插件结果")
        self.assertEqual(outputs[2].text(), f"最后的回答\n[积分-{total:.2f}]")
        self.assertEqual(outputs[3].text().count("[积分-"), 1)
        self.assertEqual(history_part.text, "最后的回答")
        self.assertAlmostEqual(await service.balance("qq", "10001"), 20 - float(total))

    async def test_stream_tail_settles_before_final_response_and_is_not_repeated(self):
        service, event = await self._notice_setup()
        upstream = self.provider.text_chat_stream()
        chunks = []

        async def stream():
            first = await anext(upstream)
            self.assertTrue(first.is_chunk)
            yield Result("流式回答")
            final = await anext(upstream)
            self.assertFalse(final.is_chunk)
            # Mimic AstrBot breaking at the final provider result, without
            # advancing the provider iterator through StopAsyncIteration.
            records = (await service.ledger({"kind": "consumption"}))["items"]
            self.assertEqual(records[0]["status"], "settled")

        async def process(_, event):
            event.result = Result(kind="STREAMING_RESULT", stream=stream())
            yield
            event.result = Result("流式回答", kind="STREAMING_FINISH")
            yield

        try:
            async for _ in self._notice_stage(process, event):
                if event.result.async_stream:
                    chunks.extend([part async for part in event.result.async_stream])
                else:
                    self.assertEqual(event.result.text(), "流式回答")
        finally:
            await upstream.aclose()
        records = (await service.ledger({"kind": "consumption"}))["items"]
        self.assertEqual(len(records), 1)
        deduction = -records[0]["points_delta"]
        self.assertEqual(
            [part.text() for part in chunks], ["流式回答", f"\n[积分-{deduction:.2f}]"]
        )

    async def test_plugin_owned_stream_keeps_its_exemption_and_has_no_notice(self):
        service, event = await self._notice_setup()
        with self.runtime.billing_scope(event):
            await self.provider.text_chat()

        async def command(event):
            yield ProviderRequest()

        command.__module__ = "data.plugins.liuyao.main"

        async def stream():
            async for _ in self.provider.text_chat_stream():
                pass
            yield Result("独立插件回答")

        async def process(_, event):
            event.result = Result(kind="STREAMING_RESULT", stream=stream())
            yield

        chunks = []
        with self.runtime.billing_scope(event):
            async for request in self.handlers.call_handler(event, command):
                event.set_extra("provider_request", request)
                async for _ in self._notice_stage(process, event):
                    chunks.extend([part async for part in event.result.async_stream])
        self.assertEqual([part.text() for part in chunks], ["独立插件回答"])
        self.assertEqual((await service.ledger({"kind": "consumption"}))["total"], 1)

    async def test_unknown_usage_does_not_display_reserved_credit_as_spent(self):
        service, event = await self._notice_setup()
        provider = Provider()
        provider.provider_config = self.provider.provider_config

        async def no_usage(**kwargs):
            return SimpleNamespace(role="assistant", usage=None)

        provider.text_chat = no_usage
        await self.manager.load_provider(provider)

        async def process(_, event):
            await provider.text_chat()
            event.result = Result("没有用量的回答")
            yield

        async for _ in self._notice_stage(process, event):
            self.assertEqual(event.result.text(), "没有用量的回答")
        self.assertEqual(await service.balance("qq", "10001"), 20)
        self.assertEqual((await service.overview())["pending_count"], 1)

    async def test_concurrent_messages_keep_separate_notices_and_tiny_charges(self):
        amounts = [Decimal("0.21"), Decimal("0.000001"), Decimal(0)]

        async def settle(ticket, response, failed):
            await asyncio.sleep(0)
            return amounts[ticket]

        self.runtime.settle = settle

        async def run():
            event = ResultEvent()
            self.runtime.mark_chat(event)

            async def process(_, event):
                await self.provider.text_chat()
                event.result = Result("回答")
                yield

            async for _ in self._notice_stage(process, event):
                pass
            return event.result.text()

        outputs = await asyncio.gather(run(), run(), run())
        self.assertCountEqual(
            outputs, ["回答\n[积分-0.21]", "回答\n[积分-0.000001]", "回答"]
        )

    async def test_interrupted_output_stream_closes_without_a_success_notice(self):
        service, event = await self._notice_setup()
        closed = []

        async def stream():
            try:
                async for response in self.provider.text_chat_stream():
                    if response.is_chunk:
                        yield Result("只输出一部分")
            finally:
                closed.append(True)

        async def process(_, event):
            event.result = Result(kind="STREAMING_RESULT", stream=stream())
            yield

        stage = self._notice_stage(process, event)
        await anext(stage)
        output = event.result.async_stream
        self.assertEqual((await anext(output)).text(), "只输出一部分")
        await output.aclose()
        await stage.aclose()
        self.assertEqual(closed, [True])
        await self.runtime.drain()
        self.assertEqual(await service.balance("qq", "10001"), 20)

    async def test_plain_reply_rebuilt_by_decorator_keeps_its_credit_notice(self):
        service, event = await self._notice_setup()

        async def process(_, event):
            await self.provider.text_chat()
            event.result = Result("原始正文")
            yield

        async def action():
            async for _ in self._notice_stage(process, event, deliver=False):
                # on_decorating_result may rebuild even an ordinary text chain.
                event.result = Result("整理后的文字", kind="GENERAL_RESULT")
                await self.respond_stage.process(event)

        event.action = action
        await self.scheduler.execute(event)
        records = (await service.ledger({"kind": "consumption"}))["items"]
        deduction = -records[0]["points_delta"]
        self.assertEqual(
            [result.text() for result in event.sent],
            [f"整理后的文字\n[积分-{deduction:.2f}]"],
        )
        self.assertNotIn("send", vars(event))

    async def test_direct_text_delivery_gets_one_final_receipt(self):
        service, event = await self._notice_setup()

        async def action():
            self.provider.tools_call_name = ["send_message_to_user"]
            await self.provider.text_chat()
            await event.send(event.plain_result("直接发送的文字"))

        event.action = action
        await self.scheduler.execute(event)
        await self.runtime._send_missing_notice(event, self.runtime._installation)
        records = (await service.ledger({"kind": "consumption"}))["items"]
        deduction = -records[0]["points_delta"]
        self.assertEqual(
            [result.text() for result in event.sent],
            ["直接发送的文字", f"[积分-{deduction:.2f}]"],
        )

    async def test_duplicate_direct_text_is_not_resent_just_to_attach_receipt(self):
        service, event = await self._notice_setup()

        async def process(_, event):
            await self.provider.text_chat()
            event.result = Result("已经发送的文字")
            yield

        async def action():
            await event.send(event.plain_result("已经发送的文字"))
            event.set_extra(
                "_send_message_to_user_current_session_plain_texts", ["已经发送的文字"]
            )
            async for _ in self._notice_stage(process, event):
                pass

        event.action = action
        await self.scheduler.execute(event)
        records = (await service.ledger({"kind": "consumption"}))["items"]
        deduction = -records[0]["points_delta"]
        self.assertEqual(
            [result.text() for result in event.sent],
            ["已经发送的文字", f"[积分-{deduction:.2f}]"],
        )

    async def test_result_left_when_agent_returns_still_gets_a_notice(self):
        _, event = await self._notice_setup()

        async def process(_, event):
            yield
            await self.provider.text_chat()
            event.result = Result("末尾直接留下的文字")

        async def action():
            async for _ in self._notice_stage(process, event):
                pass
            await self.respond_stage.process(event)

        event.action = action
        await self.scheduler.execute(event)
        self.assertEqual(len(event.sent), 1)
        self.assertIn("[积分-", event.sent[0].text())

    async def test_notice_is_confirmed_only_after_send_and_retry_does_not_duplicate(
        self,
    ):
        _, event = await self._notice_setup()
        original_send = event.send

        async def failed_send(message):
            raise OSError("temporary platform failure")

        async def process(_, event):
            await self.provider.text_chat()
            event.result = Result("正文")
            yield
            yield

        stage = self._notice_stage(process, event, deliver=False)
        await anext(stage)
        event.send = failed_send
        with self.assertRaises(OSError):
            await self.respond_stage.process(event)
        self.assertIs(event.send, failed_send)
        self.assertFalse(event.get_extra("access_gate_credit_notice").reported)
        event.send = original_send
        await anext(stage)
        await self.respond_stage.process(event)
        await stage.aclose()
        self.assertEqual(len(event.sent), 1)
        self.assertEqual(event.sent[0].text().count("[积分-"), 1)
        self.assertTrue(event.get_extra("access_gate_credit_notice").reported)
        self.assertTrue(self.runtime.status()["complete"])

    async def test_blocked_or_silent_events_do_not_emit_a_standalone_receipt(self):
        for stopped in (True, False):
            with self.subTest(stopped=stopped):
                _, event = await self._notice_setup()

                async def action(*, current_event=event, should_stop=stopped):
                    await self.provider.text_chat()
                    if should_stop:
                        current_event.stop_event()
                        await current_event.send(current_event.plain_result("已停止"))

                event.action = action
                await self.scheduler.execute(event)
                self.assertEqual(
                    [result.text() for result in event.sent],
                    ["已停止"] if stopped else [],
                )

    async def test_denial_cooldown_suppresses_sends_but_always_blocks_provider(self):
        from test_access_gate import main as plugin_main
        from test_group_rest import GroupEvent
        from test_wake import At, Context
        from test_wake import Plain as EventPlain

        await self._attach_economy()
        gate = plugin_main.AccessGatePlugin(Context("", {}), {})
        self.runtime.can_notify = gate._credit_denial_notice_allowed
        events = []

        async def attempt():
            event = GroupEvent()
            event.message_obj.message = [At(), EventPlain("你好")]
            self.runtime.mark_chat(event)
            event.action = self.provider.text_chat
            events.append(event)
            await self.scheduler.execute(event)

        with patch.object(plugin_main, "monotonic", return_value=1000):
            await asyncio.gather(*(attempt() for _ in range(8)))
        self.assertEqual(sum(len(event.sent) for event in events), 1)
        self.assertTrue(all(event.is_stopped() for event in events))
        self.assertEqual(self.provider.requests, 0)
        with patch.object(plugin_main, "monotonic", return_value=1300):
            await attempt()
        self.assertEqual(len(events[-1].sent), 1)
        self.assertEqual(self.provider.requests, 0)


if __name__ == "__main__":
    unittest.main()
