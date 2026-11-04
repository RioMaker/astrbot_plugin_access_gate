"""Exercise Dashboard authorization and the service boundary without AstrBot."""

import asyncio
import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from admin_api import AdminAPI, GroupNames


class FakeRequest:
    username = "dashboard-admin"

    def __init__(self):
        self.query = {}
        self.payload = {}

    async def json(self, default=None):
        return self.payload


class AdminAPITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.request = FakeRequest()
        self.routes = []
        self.economy = SimpleNamespace(
            **{
                name: AsyncMock(return_value={"ok": True})
                for name in (
                    "overview",
                    "analytics",
                    "users",
                    "ledger",
                    "settings",
                    "update_settings",
                    "quote",
                    "adjust",
                    "resolve",
                )
            }
        )
        self.plugin = SimpleNamespace(
            economy=self.economy,
            context=SimpleNamespace(
                register_web_api=lambda *args: self.routes.append(args)
            ),
            credit_status=lambda: {"rp_connected": True},
            credit_plugins=lambda: [{"name": "example", "charged": False}],
        )
        web = SimpleNamespace(
            request=self.request,
            json_response=lambda data: {"status": 200, "body": data},
            error_response=lambda message, status_code: {
                "status": status_code,
                "error": message,
            },
        )
        self.api = AdminAPI(self.plugin, web)

    def connect_bot(self, rows, *, bot_ids=("90001",), platform="aiocqhttp"):
        client = SimpleNamespace(
            call_action=AsyncMock(return_value=rows),
            _wsr_api_clients=dict.fromkeys(bot_ids),
        )
        adapter = SimpleNamespace(
            meta=lambda: SimpleNamespace(name=platform), get_client=lambda: client
        )
        self.plugin.context.platform_manager = SimpleNamespace(
            get_insts=lambda: [adapter]
        )
        return client

    def test_registers_distinct_get_and_post_routes(self):
        self.api.register()
        signatures = {(path, tuple(methods)) for path, _, methods, _ in self.routes}
        self.assertEqual(len(signatures), 10)
        self.assertIn(
            ("/astrbot_plugin_access_gate/credits/settings", ("GET",)), signatures
        )
        self.assertIn(
            ("/astrbot_plugin_access_gate/credits/settings", ("POST",)), signatures
        )

    async def test_anonymous_cannot_read_or_mutate(self):
        self.request.username = " "
        for method in (
            self.api.overview,
            self.api.analytics,
            self.api.users,
            self.api.ledger,
            self.api.settings,
            self.api.quote,
            self.api.plugins,
            self.api.save_settings,
            self.api.adjust,
            self.api.resolve,
        ):
            self.assertEqual((await method())["status"], 403)
        self.economy.overview.assert_not_called()
        self.economy.adjust.assert_not_called()

    async def test_unregistered_old_handlers_cannot_read_or_mutate(self):
        self.api.register()
        self.api.unregister()
        for _, handler, _, _ in self.routes:
            self.assertEqual((await handler())["status"], 503)
        self.economy.overview.assert_not_called()
        self.economy.update_settings.assert_not_called()
        self.economy.adjust.assert_not_called()
        self.economy.resolve.assert_not_called()

    async def test_disable_during_body_parse_prevents_queued_mutation(self):
        async def delayed_body(default=None):
            self.api.unregister()
            return {"request_id": "queued-request-123", "delta": 5}

        self.request.json = delayed_body
        self.assertEqual((await self.api.adjust())["status"], 503)
        self.economy.adjust.assert_not_called()

    async def test_register_can_reactivate_instance(self):
        self.api.unregister()
        self.api.register()
        self.assertEqual((await self.api.overview())["status"], 200)

    async def test_api_key_is_not_dashboard_identity(self):
        self.api.api_key_prefix = "fixture-api-key:"
        self.request.username = "fixture-api-key:abc"
        self.assertEqual((await self.api.settings())["status"], 403)
        self.economy.settings.assert_not_called()
        self.assertEqual((await self.api.analytics())["status"], 403)
        self.economy.analytics.assert_not_called()

    async def test_analytics_passes_range_and_enriches_group_names(self):
        self.connect_bot([{"group_id": 123, "group_name": "消耗群"}])
        self.request.query = {"period": "month"}
        source = {
            "groups": [{"platform": "qq", "group_id": "123", "cost_yuan": 2}],
            "users": [],
            "series": [],
        }
        self.economy.analytics.return_value = source
        result = await self.api.analytics()
        self.economy.analytics.assert_awaited_once_with("month")
        self.assertEqual(result["body"]["groups"][0]["group_name"], "消耗群")
        self.assertNotIn("group_name", source["groups"][0])

    async def test_analytics_rejects_oversized_range_and_surfaces_validation(self):
        self.request.query = {"period": "x" * 201}
        self.assertEqual((await self.api.analytics())["status"], 400)
        self.economy.analytics.assert_not_called()
        self.request.query = {"period": "invalid"}
        self.economy.analytics.side_effect = ValueError("统计范围无效")
        self.assertEqual((await self.api.analytics())["status"], 400)

    async def test_overview_adds_integration_without_mutating_service_data(self):
        source = {"day": "2026-10-29"}
        self.economy.overview.return_value = source
        result = await self.api.overview()
        self.assertEqual(result["body"]["integration"], {"rp_connected": True})
        self.assertNotIn("integration", source)

    async def test_group_names_enrich_overview_and_history_without_mutating_records(
        self,
    ):
        client = self.connect_bot(
            [{"group_id": 123456, "group_name": "  太鼓交流群  "}]
        )
        groups = [
            {"platform": platform, "group_id": "123456", "spent_yuan": 0.106}
            for platform in ("qq", "aiocqhttp", "", "telegram")
        ]
        groups.append({"platform": "qq", "group_id": "999999"})
        entries = [
            {"platform": "qq", "group_id": "123456", "details": {"cost": 0.106}},
            {"platform": "qq", "group_id": ""},
        ]
        before = copy.deepcopy((groups, entries))
        self.economy.overview.return_value = {"groups": groups}
        self.economy.ledger.return_value = {"items": entries, "page": 2, "total": 2}
        overview, ledger = await asyncio.gather(self.api.overview(), self.api.ledger())
        self.assertEqual(
            [row["group_name"] for row in overview["body"]["groups"]],
            ["太鼓交流群", "太鼓交流群", "太鼓交流群", "", ""],
        )
        self.assertEqual(
            [row["group_name"] for row in ledger["body"]["items"]], ["太鼓交流群", ""]
        )
        self.assertEqual(ledger["body"]["page"], 2)
        self.assertEqual((groups, entries), before)
        client.call_action.assert_awaited_once_with("get_group_list", self_id="90001")

    async def test_group_names_refresh_after_cache_expiry_and_keep_last_known_on_failure(
        self,
    ):
        client = self.connect_bot([{"group_id": 123456, "group_name": "原群名称"}])
        self.economy.overview.return_value = {
            "groups": [{"platform": "qq", "group_id": "123456"}]
        }
        with patch("admin_api.monotonic", return_value=100) as clock:
            await self.api.overview()
            client.call_action.return_value = [
                {"group_id": 123456, "group_name": "新群名称"}
            ]
            clock.return_value = 159
            self.assertEqual(
                (await self.api.overview())["body"]["groups"][0]["group_name"],
                "原群名称",
            )
            clock.return_value = 161
            self.assertEqual(
                (await self.api.overview())["body"]["groups"][0]["group_name"],
                "新群名称",
            )
            client.call_action.side_effect = RuntimeError("private connection token")
            clock.return_value = 222
            result = await self.api.overview()
            self.assertEqual(result["status"], 200)
            self.assertEqual(result["body"]["groups"][0]["group_name"], "新群名称")
            self.assertNotIn("private connection", str(result))

    async def test_group_lookup_queries_each_connected_bot_and_skips_malformed_names(
        self,
    ):
        client = self.connect_bot([], bot_ids=("90001", "90002"))
        client.call_action.side_effect = [
            [
                {"group_id": 123456, "group_name": "第一群"},
                None,
                {"group_id": True, "group_name": "bad"},
            ],
            {
                "status": "ok",
                "data": [
                    {"group_id": "654321", "group_name": "第二群"},
                    {"group_id": "7", "group_name": []},
                ],
            },
        ]
        self.economy.overview.return_value = {
            "groups": [
                {"platform": "qq", "group_id": value}
                for value in ("123456", "654321", "7")
            ]
        }
        result = await self.api.overview()
        self.assertEqual(
            [row["group_name"] for row in result["body"]["groups"]],
            ["第一群", "第二群", ""],
        )
        self.assertEqual(client.call_action.await_count, 2)
        self.assertEqual(
            {call.kwargs["self_id"] for call in client.call_action.await_args_list},
            {"90001", "90002"},
        )

    async def test_group_lookup_timeout_does_not_break_credit_data(self):
        client = self.connect_bot([])
        cancelled = asyncio.Event()

        async def unavailable(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        client.call_action.side_effect = unavailable
        self.economy.overview.return_value = {
            "global": {"spent_yuan": 0.106},
            "groups": [{"platform": "qq", "group_id": "123456"}],
        }
        with patch.object(GroupNames, "LOOKUP_TIMEOUT", 0.01):
            result = await asyncio.wait_for(self.api.overview(), timeout=1)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["body"]["global"]["spent_yuan"], 0.106)
        self.assertEqual(result["body"]["groups"][0]["group_name"], "")

    async def test_unauthorized_requests_do_not_query_bot_metadata(self):
        client = self.connect_bot([])
        self.request.username = ""
        self.assertEqual((await self.api.overview())["status"], 403)
        self.assertEqual((await self.api.ledger())["status"], 403)
        client.call_action.assert_not_called()

    async def test_unsupported_or_disconnected_platform_keeps_group_id(self):
        self.economy.overview.return_value = {
            "groups": [{"platform": "qq", "group_id": "123456"}]
        }
        for platform, bot_ids in (("telegram", ("90001",)), ("aiocqhttp", ())):
            client = self.connect_bot([], platform=platform, bot_ids=bot_ids)
            self.api.group_names.refresh_at = 0
            row = (await self.api.overview())["body"]["groups"][0]
            self.assertEqual(
                row, {"platform": "qq", "group_id": "123456", "group_name": ""}
            )
            client.call_action.assert_not_called()

    async def test_search_and_pagination_are_bounded(self):
        self.request.query = {"query": "  用户1  ", "page": "2"}
        self.assertEqual((await self.api.users())["status"], 200)
        self.economy.users.assert_awaited_once_with("用户1", 2)
        for raw in ("0", "-1", "nan", "1.5", "1000001"):
            self.request.query["page"] = raw
            self.assertEqual((await self.api.users())["status"], 400)
        self.request.query = {"query": "x" * 201}
        self.assertEqual((await self.api.users())["status"], 400)

    async def test_history_filters_use_supported_keys_only(self):
        self.request.query = {
            "day": "2026-10-29",
            "kind": "rp",
            "page": "3",
            "sql": "ignored",
        }
        self.assertEqual((await self.api.ledger())["status"], 200)
        filters = self.economy.ledger.await_args.args[0]
        self.assertEqual(filters["day"], "2026-10-29")
        self.assertEqual(filters["page"], 3)
        self.assertNotIn("sql", filters)

    async def test_settings_require_object_and_integer_revision(self):
        for payload in (
            None,
            [],
            {},
            {"values": {}, "revision": True},
            {"values": [], "revision": 1},
        ):
            self.request.payload = payload
            self.assertEqual((await self.api.save_settings())["status"], 400)
        payload = {"values": {"points_per_yuan": 20}, "revision": 1}
        self.request.payload = payload
        self.assertEqual((await self.api.save_settings())["status"], 200)
        self.economy.update_settings.assert_awaited_once_with(
            payload, "dashboard-admin"
        )

    async def test_adjust_and_resolve_never_trust_client_actor(self):
        for method, service in (
            (self.api.adjust, self.economy.adjust),
            (self.api.resolve, self.economy.resolve),
        ):
            self.request.payload = {
                "request_id": "fixture-request-123",
                "actor": "forged",
            }
            self.assertEqual((await method())["status"], 200)
            service.assert_awaited_once_with(
                {"request_id": "fixture-request-123"}, "dashboard-admin"
            )
            self.assertEqual(self.request.payload["actor"], "forged")

    async def test_validation_errors_are_explained_but_internal_errors_are_hidden(self):
        self.economy.overview.side_effect = ValueError("页码无效")
        self.assertEqual((await self.api.overview())["error"], "页码无效")
        self.economy.overview.side_effect = RuntimeError(
            "private database file /secret/path"
        )
        with self.assertLogs("admin_api", level="ERROR"):
            result = await self.api.overview()
        self.assertEqual(result["status"], 500)
        self.assertNotIn("secret", str(result))


if __name__ == "__main__":
    unittest.main()
