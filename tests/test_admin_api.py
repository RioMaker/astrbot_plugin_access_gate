"""Exercise Dashboard authorization and the service boundary without AstrBot."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from admin_api import AdminAPI


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

    def test_registers_distinct_get_and_post_routes(self):
        self.api.register()
        signatures = {(path, tuple(methods)) for path, _, methods, _ in self.routes}
        self.assertEqual(len(signatures), 9)
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

    async def test_overview_adds_integration_without_mutating_service_data(self):
        source = {"day": "2026-10-29"}
        self.economy.overview.return_value = source
        result = await self.api.overview()
        self.assertEqual(result["body"]["integration"], {"rp_connected": True})
        self.assertNotIn("integration", source)

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
