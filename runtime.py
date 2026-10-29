"""Bill only Access Gate's explicitly marked chat sessions.

Plugin-owned calls, unmarked sessions and background jobs pass through. A live
scope prevents delayed work from borrowing a completed chat's billing identity.
"""

# Runtime integrations may raise arbitrary plugin exceptions. Catch at the
# boundary, report the exception type, and preserve interruption semantics.
# ruff: noqa: BLE001

from __future__ import annotations

import asyncio
import copy
import importlib
import inspect
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import partial, wraps
from types import SimpleNamespace
from typing import Any


class RuntimeGateDenied(RuntimeError):
    """A safe, user-facing rejection raised before any upstream request."""


@dataclass
class _Lifetime:
    active: bool = True
    denied: bool = False


@dataclass
class _Scope:
    event: Any
    plugin: str | None
    lifetime: _Lifetime


@dataclass
class _Invocation:
    provider: Any
    task: Any
    active: bool = True


@dataclass
class _Patch:
    target: Any
    name: str
    original: Any
    wrapper: Any
    owned: bool


def _field(value: Any, key: str, default: Any = None) -> Any:
    return (
        value.get(key, default)
        if isinstance(value, dict)
        else getattr(value, key, default)
    )


class BillingRuntime:
    """Call begin(event, plugin, provider, kwargs), then settle(ticket, reply, failed).

    Both callbacks are asynchronous. Each accepted invocation settles exactly
    once, even after uninstall or caller cancellation. Settlement tasks remain
    owned until completion; ``drain`` can await them during plugin shutdown.
    Unknown/expired event attribution is outside this gateway's control scope.
    """

    def __init__(self, context, begin: Callable, settle: Callable, on_error=None):
        self.context = context
        self.begin = begin
        self.settle = settle
        self.on_error = on_error
        self.active = False
        self._installation: _Lifetime | None = None
        self._scope: ContextVar[_Scope | None] = ContextVar(
            f"access_gate_scope_{id(self)}", default=None
        )
        self._calls: ContextVar[tuple[_Invocation, ...]] = ContextVar(
            f"access_gate_calls_{id(self)}", default=()
        )
        self._patches: list[_Patch] = []
        self._provider_methods: dict[tuple[int, str], Any] = {}
        self._hook_methods: dict[int, Any] = {}
        self._hook_registry = None
        self._request_type = None
        self._request_owners: dict[int, tuple] = {}
        self._errors: list[str] = []
        self._pending: set[asyncio.Task] = set()
        self._inflight = 0
        self._scopes_ready = False
        self._hotload_ready = False
        self._hooks_ready = False
        self._requests_ready = False

    def _warn(self, message: str) -> None:
        if message in self._errors or len(self._errors) >= 40:
            return
        self._errors.append(message)
        if self.on_error:
            try:
                result = self.on_error(message)
                if inspect.iscoroutine(result):
                    result.close()
            except Exception as exc:
                if len(self._errors) < 40:
                    self._errors.append(f"错误通知回调失败（{type(exc).__name__}）。")

    def _enabled(self, installation: _Lifetime) -> bool:
        return (
            self.active and installation.active and self._installation is installation
        )

    def _patch(self, target, name, wrapper):
        self._patches.append(
            _Patch(target, name, getattr(target, name), wrapper, name in vars(target))
        )
        setattr(target, name, wrapper)

    def _providers(self):
        providers = self.context.get_all_providers()
        if inspect.isawaitable(providers):
            if inspect.iscoroutine(providers):
                providers.close()
            raise TypeError("get_all_providers must be synchronous")
        return list(providers)

    def install(self) -> bool:
        if self.active:
            return self.status()["complete"]
        try:
            scheduler = importlib.import_module("astrbot.core.pipeline.scheduler")
            handlers = importlib.import_module(
                "astrbot.core.pipeline.process_stage.method.star_request"
            )
            tool_exec = importlib.import_module("astrbot.core.astr_agent_tool_exec")
            registry = importlib.import_module(
                "astrbot.core.star.star_handler"
            ).star_handlers_registry
            request_stage = importlib.import_module(
                "astrbot.core.pipeline.process_stage.method.agent_request"
            ).AgentRequestSubStage
            request_type = importlib.import_module(
                "astrbot.core.provider.entities"
            ).ProviderRequest
            if not inspect.iscoroutinefunction(scheduler.PipelineScheduler.execute):
                raise TypeError("scheduler")
            if not inspect.isasyncgenfunction(handlers.call_handler):
                raise TypeError("handler")
            if not inspect.isasyncgenfunction(tool_exec.call_local_llm_tool):
                raise TypeError("tool")
            if not callable(registry.get_handlers_by_event_type):
                raise TypeError("hook registry")
            if not inspect.isasyncgenfunction(request_stage.process) or not isinstance(
                request_type, type
            ):
                raise TypeError("plugin provider request")
            manager = self.context.provider_manager
            if not inspect.iscoroutinefunction(manager.load_provider):
                raise TypeError("provider loading")
            providers = self._providers()
            self._installation = installation = _Lifetime()
            self.active = True
            self._patch(
                scheduler.PipelineScheduler,
                "execute",
                self._wrap_execute(scheduler.PipelineScheduler.execute, installation),
            )
            self._patch(
                handlers,
                "call_handler",
                self._wrap_handler(handlers.call_handler, installation, tool=False),
            )
            self._patch(
                tool_exec,
                "call_local_llm_tool",
                self._wrap_handler(
                    tool_exec.call_local_llm_tool, installation, tool=True
                ),
            )
            self._scopes_ready = True
            self._patch(
                manager,
                "load_provider",
                self._wrap_load(manager.load_provider, installation),
            )
            self._hotload_ready = True
            self._hook_registry = registry
            self._patch(
                registry,
                "get_handlers_by_event_type",
                self._wrap_hook_lookup(
                    registry.get_handlers_by_event_type, installation
                ),
            )
            self._refresh_hooks(registry, installation)
            self._hooks_ready = True
            self._request_type = request_type
            self._patch(
                request_stage,
                "process",
                self._wrap_request_process(request_stage.process, installation),
            )
            self._requests_ready = True
            self._refresh(providers, installation)
            return self.status()["complete"]
        except Exception as exc:
            self.uninstall()
            self._warn(f"LLM 额度网关安装失败（{type(exc).__name__}）；接口不兼容。")
            self._install_degraded_guard()
            return False

    def _install_degraded_guard(self):
        """Protect marked chats without disabling unrelated provider consumers."""
        self._installation = installation = _Lifetime()
        self.active = True
        try:
            self._refresh(self._providers(), installation)
        except Exception as exc:
            self._warn(
                f"降级保护无法读取 Provider（{type(exc).__name__}），门禁会话已暂停。"
            )
        try:
            manager = self.context.provider_manager
            original = manager.load_provider
            self._patch(
                manager,
                "load_provider",
                self._wrap_load(original, installation),
            )
            self._hotload_ready = True
        except Exception as exc:
            self._warn(
                f"无法跟踪 Provider 热加载（{type(exc).__name__}），门禁会话已暂停。"
            )

    def _refresh(self, providers, installation):
        for provider in providers:
            for name, stream in (("text_chat", False), ("text_chat_stream", True)):
                key = (id(provider), name)
                original = getattr(provider, name, None)
                if key in self._provider_methods:
                    expected = self._provider_methods[key]
                    # A cooperative outer wrapper retains __wrapped__. An
                    # unrelated replacement needs its own fresh billing wrapper.
                    candidate = original
                    seen = set()
                    while candidate is not None and id(candidate) not in seen:
                        if candidate is expected:
                            break
                        seen.add(id(candidate))
                        candidate = getattr(candidate, "__wrapped__", None)
                    if candidate is expected:
                        continue
                compatible = (
                    inspect.isasyncgenfunction(original)
                    if stream
                    else inspect.iscoroutinefunction(original)
                )
                if not compatible:
                    self._warn(
                        f"Provider {type(provider).__name__}.{name} 接口不兼容，将暂停门禁会话。"
                    )
                    if original is None:
                        continue
                    wrapper = self._block_method(original, installation, stream=stream)
                else:
                    wrapper = (
                        self._wrap_stream(provider, original, installation)
                        if stream
                        else self._wrap_chat(provider, original, installation)
                    )
                try:
                    self._patch(provider, name, wrapper)
                except Exception as exc:
                    self._warn(
                        f"Provider {type(provider).__name__}.{name} 无法安装网关（{type(exc).__name__}），门禁会话已暂停。"
                    )
                    continue
                self._provider_methods[key] = wrapper

    def refresh(self) -> int:
        if self.active and self._installation:
            try:
                self._refresh(self._providers(), self._installation)
                if self._hook_registry is not None:
                    self._refresh_hooks(self._hook_registry, self._installation)
            except Exception as exc:
                self._warn(f"Provider 列表刷新失败（{type(exc).__name__}）。")
        return len({key[0] for key in self._provider_methods})

    def uninstall(self) -> None:
        self.active = False
        if self._installation:
            self._installation.active = False
        for patch in reversed(self._patches):
            try:
                if getattr(patch.target, patch.name, None) is not patch.wrapper:
                    continue
                if patch.owned:
                    setattr(patch.target, patch.name, patch.original)
                else:
                    delattr(patch.target, patch.name)
            except Exception as exc:
                self._warn(f"网关卸载恢复失败（{type(exc).__name__}）。")
        self._patches.clear()
        self._provider_methods.clear()
        self._hook_methods.clear()
        self._hook_registry = None
        self._request_owners.clear()
        self._request_type = None
        self._scopes_ready = self._hotload_ready = False
        self._hooks_ready = False
        self._requests_ready = False

    def status(self) -> dict[str, Any]:
        return {
            "installed": self.active,
            "complete": self.active
            and self._scopes_ready
            and self._hotload_ready
            and self._hooks_ready
            and self._requests_ready
            and not self._errors,
            "provider_count": len({key[0] for key in self._provider_methods}),
            "scope_tracking": self._scopes_ready,
            "hotload_tracking": self._hotload_ready,
            "hook_tracking": self._hooks_ready,
            "plugin_request_tracking": self._requests_ready,
            "inflight": self._inflight,
            "pending_settlements": len(self._pending),
            "errors": list(self._errors),
            "coverage": "仅门禁插件触发的会话及提及判断；其他插件、未标记会话和后台调用直接放行。",
        }

    def _live_scope(self):
        scope = self._scope.get()
        return scope if scope and scope.lifetime.active else None

    def mark_chat(self, event):
        """Called only when Access Gate itself is about to initiate a chat."""
        event.set_extra("access_gate_credit_session", self._installation)

    def _controlled(self):
        scope = self._live_scope()
        return bool(
            scope
            and scope.plugin == ""
            and scope.event is not None
            and self._installation is not None
            and scope.event.get_extra("access_gate_credit_session")
            is self._installation
        )

    @contextmanager
    def billing_scope(self, event, plugin: str = ""):
        """Bounded execution identity; does not mark a session for billing."""
        lifetime = _Lifetime()
        token = self._scope.set(_Scope(event, self._normalize_plugin(plugin), lifetime))
        try:
            yield
        finally:
            lifetime.active = False
            self._scope.reset(token)

    @staticmethod
    def _normalize_plugin(plugin):
        return "" if plugin == "astrbot_plugin_access_gate" else str(plugin or "")

    def _plugin_for(self, handler):
        try:
            # StarManager binds commands, hooks and decorator tools with
            # partial(function, plugin_instance). Its own module is functools,
            # so resolve the underlying callable before checking plugin roots.
            seen = set()
            while handler is not None:
                if id(handler) in seen:
                    return None
                seen.add(id(handler))
                if isinstance(handler, partial):
                    handler = handler.func
                elif getattr(handler, "__wrapped__", None) is not None:
                    handler = handler.__wrapped__
                else:
                    break
            module = getattr(handler, "__module__", "") or ""
            matches = []
            for star in self.context.get_all_stars():
                root = getattr(star, "module_path", "") or ""
                # Metadata commonly identifies plugin.main while helpers live
                # beside main.py. Remove only that final conventional component.
                root = root.removesuffix(".main")
                name = getattr(star, "name", "") or ""
                if root and (module == root or module.startswith(root + ".")):
                    if "builtin_stars" in root.split(".") or getattr(
                        star, "reserved", False
                    ):
                        name = ""
                    matches.append((len(root), name))
            return self._normalize_plugin(max(matches)[1]) if matches else None
        except Exception as exc:
            self._warn(f"插件调用归属读取失败（{type(exc).__name__}）。")
            return None

    def _nested(self, provider):
        task = asyncio.current_task()
        return any(
            call.active and call.provider is provider and call.task is task
            for call in self._calls.get()
        )

    async def _denied(self, event, error):
        if event is None:
            return
        scope = self._live_scope()
        if scope and scope.lifetime.denied:
            return
        if scope:
            scope.lifetime.denied = True
        try:
            event.stop_event()
            await event.send(event.plain_result(str(error)[:240]))
        except Exception as exc:
            self._warn(f"额度拦截提示发送失败（{type(exc).__name__}）。")

    async def _begin(self, provider, kwargs):
        scope = self._live_scope()
        event = scope.event if scope else None
        plugin = scope.plugin if scope else ""
        try:
            if not self.status()["complete"]:
                raise RuntimeGateDenied(
                    "额度网关接入不完整，已暂停 LLM 请求，请管理员检查管理页面。"
                )
            return await self.begin(event, plugin, provider, dict(kwargs))
        except RuntimeGateDenied as exc:
            await self._denied(event, exc)
            raise

    def _settled(self, task):
        self._pending.discard(task)
        try:
            task.result()
        except BaseException as exc:
            self._warn(f"LLM 结算失败（{type(exc).__name__}），请核对未结算账目。")

    async def _finish(self, ticket, response, failed):
        task = asyncio.create_task(self.settle(ticket, response, failed))
        self._pending.add(task)
        task.add_done_callback(self._settled)
        # A second cancellation may interrupt this waiter, but never cancels the
        # owned settlement task. Uninstall does not drop these references.
        await asyncio.shield(task)

    async def drain(self):
        while self._pending:
            await asyncio.gather(
                *(asyncio.shield(task) for task in tuple(self._pending)),
                return_exceptions=True,
            )

    def _wrap_execute(self, original, installation):
        @wraps(original)
        async def execute(scheduler, event, *args, **kwargs):
            if not self._enabled(installation):
                return await original(scheduler, event, *args, **kwargs)
            self.refresh()
            with self.billing_scope(event):
                try:
                    return await original(scheduler, event, *args, **kwargs)
                except RuntimeGateDenied as exc:
                    await self._denied(event, exc)
                    return None

        return execute

    def _wrap_load(self, original, installation):
        @wraps(original)
        async def load_provider(*args, **kwargs):
            result = await original(*args, **kwargs)
            if self._enabled(installation):
                self.refresh()
            return result

        return load_provider

    def _block_method(self, original, installation, *, stream):
        async def reject():
            error = RuntimeGateDenied(
                "模型调用接口与积分网关不兼容，已拦截请求，请管理员检查。"
            )
            scope = self._live_scope()
            await self._denied(scope.event if scope else None, error)
            raise error

        if stream:

            @wraps(original)
            async def blocked_stream(*args, **kwargs):
                if self._enabled(installation) and self._controlled():
                    await reject()
                iterator = original(*args, **kwargs)
                try:
                    async for response in iterator:
                        yield response
                finally:
                    close = getattr(iterator, "aclose", None)
                    if callable(close):
                        await close()

            return blocked_stream

        @wraps(original)
        async def blocked_chat(*args, **kwargs):
            if self._enabled(installation) and self._controlled():
                await reject()
            result = original(*args, **kwargs)
            return await result if inspect.isawaitable(result) else result

        return blocked_chat

    def _refresh_hooks(self, metadata_items, installation):
        for metadata in metadata_items:
            event_name = getattr(getattr(metadata, "event_type", None), "name", "")
            if not event_name.startswith("On"):
                continue
            original = getattr(metadata, "handler", None)
            previous = self._hook_methods.get(id(metadata))
            candidate = original
            seen = set()
            while (
                previous is not None
                and candidate is not None
                and id(candidate) not in seen
            ):
                if candidate is previous:
                    break
                seen.add(id(candidate))
                candidate = getattr(candidate, "__wrapped__", None)
            if previous is not None and candidate is previous:
                continue
            plugin = self._plugin_for(original)
            if inspect.iscoroutinefunction(original):
                wrapper = self._wrap_hook(original, installation, plugin)
            elif inspect.isasyncgenfunction(original):
                wrapper = self._wrap_hook_generator(original, installation, plugin)
            else:
                self._warn("插件事件钩子不是异步函数，无法确定其计费归属。")
                continue
            self._patch(metadata, "handler", wrapper)
            self._hook_methods[id(metadata)] = wrapper

    def _wrap_hook_lookup(self, original, installation):
        @wraps(original)
        def get_handlers_by_event_type(*args, **kwargs):
            handlers = original(*args, **kwargs)
            if self._enabled(installation):
                self._refresh_hooks(handlers, installation)
            return handlers

        return get_handlers_by_event_type

    def _hook_scope(self, args, kwargs, plugin):
        event = kwargs.get("event", args[0] if args else None)
        # Lifecycle hooks can receive plugin metadata or a Context instead of a
        # message. Do not mistake one for a billable user identity.
        if not callable(getattr(event, "get_sender_id", None)):
            event = None
        parent = self._live_scope()
        lifetime = parent.lifetime if parent and parent.event is event else _Lifetime()
        return _Scope(event, plugin, lifetime), parent

    def _wrap_hook(self, original, installation, plugin):
        @wraps(original)
        async def hook(*args, **kwargs):
            if not self._enabled(installation):
                return await original(*args, **kwargs)
            scope, parent = self._hook_scope(args, kwargs, plugin)
            token = self._scope.set(scope)
            try:
                return await original(*args, **kwargs)
            finally:
                self._scope.reset(token)
                if not parent or parent.lifetime is not scope.lifetime:
                    scope.lifetime.active = False

        return hook

    def _wrap_hook_generator(self, original, installation, plugin):
        @wraps(original)
        async def hook(*args, **kwargs):
            scope, parent = self._hook_scope(args, kwargs, plugin)
            iterator = original(*args, **kwargs)
            try:
                while True:
                    token = (
                        self._scope.set(scope) if self._enabled(installation) else None
                    )
                    try:
                        response = await anext(iterator)
                    except StopAsyncIteration:
                        break
                    finally:
                        if token is not None:
                            self._scope.reset(token)
                    yield response
            finally:
                token = self._scope.set(scope) if self._enabled(installation) else None
                try:
                    await iterator.aclose()
                finally:
                    if token is not None:
                        self._scope.reset(token)
                    if not parent or parent.lifetime is not scope.lifetime:
                        scope.lifetime.active = False

        return hook

    def _wrap_handler(self, original, installation, *, tool):
        @wraps(original)
        async def handler_wrapper(*args, **kwargs):
            if tool:
                context = kwargs.get("context", args[0] if args else None)
                event = getattr(getattr(context, "context", None), "event", None)
            else:
                event = kwargs.get("event", args[0] if args else None)
            handler = kwargs.get("handler", args[1] if len(args) > 1 else None)
            parent = self._live_scope()
            lifetime = (
                parent.lifetime if parent and parent.event is event else _Lifetime()
            )
            scope = _Scope(event, self._plugin_for(handler), lifetime)
            iterator = original(*args, **kwargs)
            marked = []
            try:
                while True:
                    token = (
                        self._scope.set(scope) if self._enabled(installation) else None
                    )
                    try:
                        item = await iterator.__anext__()
                    except StopAsyncIteration:
                        break
                    finally:
                        if token is not None:
                            self._scope.reset(token)
                    if (
                        self._enabled(installation)
                        and self._request_type is not None
                        and isinstance(item, self._request_type)
                    ):
                        owner = (item, event, scope.plugin, installation)
                        self._request_owners[id(item)] = owner
                        marked.append(owner)
                    yield item
            finally:
                token = self._scope.set(scope) if self._enabled(installation) else None
                try:
                    await iterator.aclose()
                finally:
                    if token is not None:
                        self._scope.reset(token)
                    if not parent or parent.lifetime is not lifetime:
                        lifetime.active = False
                    for owner in marked:
                        if self._request_owners.get(id(owner[0])) is owner:
                            self._request_owners.pop(id(owner[0]), None)

        return handler_wrapper

    def _wrap_request_process(self, original, installation):
        @wraps(original)
        async def process(stage, event, *args, **kwargs):
            parent = self._live_scope()
            lifetime = (
                parent.lifetime if parent and parent.event is event else _Lifetime()
            )
            plugin = ""
            if self._enabled(installation):
                request = event.get_extra("provider_request")
                owner = self._request_owners.pop(id(request), None)
                if (
                    owner
                    and owner[0] is request
                    and owner[1] is event
                    and owner[3] is installation
                ):
                    plugin = owner[2]
            # A request origin is consumed once. Leaving provider_request in the
            # event's extras cannot exempt later ordinary-chat Agent runs.
            scope = _Scope(event, plugin, lifetime)
            iterator = original(stage, event, *args, **kwargs)
            try:
                while True:
                    token = (
                        self._scope.set(scope) if self._enabled(installation) else None
                    )
                    try:
                        response = await anext(iterator)
                    except StopAsyncIteration:
                        break
                    finally:
                        if token is not None:
                            self._scope.reset(token)
                    yield response
            finally:
                token = self._scope.set(scope) if self._enabled(installation) else None
                try:
                    await iterator.aclose()
                finally:
                    if token is not None:
                        self._scope.reset(token)
                    if not parent or parent.lifetime is not lifetime:
                        lifetime.active = False

        return process

    @staticmethod
    def _call_kwargs(original, args, kwargs):
        # Preserve positional model/options too, without persisting prompts here.
        try:
            arguments = dict(
                inspect.signature(original).bind_partial(*args, **kwargs).arguments
            )
            extra = arguments.pop("kwargs", {})
            if isinstance(extra, dict):
                arguments.update(extra)
            return arguments
        except (TypeError, ValueError):
            return dict(kwargs)

    def _wrap_chat(self, provider, original, installation):
        @wraps(original)
        async def text_chat(*args, **kwargs):
            if (
                not self._enabled(installation)
                or not self._controlled()
                or self._nested(provider)
            ):
                return await original(*args, **kwargs)
            ticket = await self._begin(
                provider, self._call_kwargs(original, args, kwargs)
            )
            call = _Invocation(provider, asyncio.current_task())
            token = self._calls.set(self._calls.get() + (call,))
            self._inflight += 1
            response = None
            failed = True
            try:
                response = await original(*args, **kwargs)
                failed = _field(response, "role") == "err"
                return response
            finally:
                call.active = False
                self._calls.reset(token)
                self._inflight -= 1
                await self._finish(ticket, response, failed)

        return text_chat

    def _snapshot(self, response):
        try:
            raw = _field(response, "raw_completion")
            return SimpleNamespace(
                usage=copy.deepcopy(_field(response, "usage")),
                raw_completion=SimpleNamespace(
                    usage=copy.deepcopy(_field(raw, "usage")),
                    id=_field(raw, "id"),
                    model=_field(raw, "model"),
                    created=_field(raw, "created"),
                )
                if raw is not None
                else None,
                role=_field(response, "role"),
                id=_field(response, "id"),
                is_chunk=bool(_field(response, "is_chunk", False)),
            )
        except Exception as exc:
            self._warn(f"流式用量快照失败（{type(exc).__name__}）。")
            return None

    def _wrap_stream(self, provider, original, installation):
        @wraps(original)
        async def text_chat_stream(*args, **kwargs):
            observe = (
                self._enabled(installation)
                and self._controlled()
                and not self._nested(provider)
            )
            if not observe:
                iterator = original(*args, **kwargs)
                try:
                    async for response in iterator:
                        yield response
                finally:
                    await iterator.aclose()
                return
            ticket = await self._begin(
                provider, self._call_kwargs(original, args, kwargs)
            )
            call = _Invocation(provider, asyncio.current_task())
            if observe:
                self._inflight += 1
            iterator = original(*args, **kwargs)
            final_response = None
            failed = True
            try:
                while True:
                    call.task = asyncio.current_task()
                    token = self._calls.set(self._calls.get() + (call,))
                    try:
                        response = await iterator.__anext__()
                    except StopAsyncIteration:
                        failed = _field(final_response, "role") == "err"
                        break
                    finally:
                        self._calls.reset(token)
                    if observe and not _field(response, "is_chunk", False):
                        final_response = self._snapshot(response)
                    yield response
            finally:
                call.task = asyncio.current_task()
                token = self._calls.set(self._calls.get() + (call,))
                try:
                    await iterator.aclose()
                finally:
                    self._calls.reset(token)
                    call.active = False
                    if observe:
                        self._inflight -= 1
                        await self._finish(ticket, final_response, failed)

        return text_chat_stream
