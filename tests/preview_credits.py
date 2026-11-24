"""Explicit isolated UI fixture. Run then open http://127.0.0.1:8943/preview.

The production page never loads this bridge. Writes below affect browser memory
only, and the server binds localhost and serves three fixed production assets.
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PAGE = Path(__file__).resolve().parents[1] / "pages" / "credits"
sys.path.insert(0, str(PAGE.parents[1]))
from pricing import DEFAULTS, quote

BRIDGE = r"""
const fixture = {
  values: { ...__DEFAULTS__, group_overrides: { "qq:10002000": 3 } },
  official: __CATALOG__, revision: 1, balance: 19.88, spent: 0.106,
  groupNames: { "10002000": "太鼓达人交流①群 🥁", "10003000": "<img src=x onerror=alert(1)>" }
};
const records = [
  ["fixture-unknown-request", "2026-10-29T15:30:00+08:00", "10001", "10002000", "consumption", "unknown"],
  ["fixture-request-2", "2026-10-29T08:30:00+08:00", "10001", "10003000", "consumption", "settled"],
  ["private-request", "2026-10-29T20:10:00+08:00", "10002", "", "consumption", "settled"],
  ["group-request", "2026-10-28T09:00:00+08:00", "10001", "10002000", "consumption", "settled"],
  ["rp-request", "2026-10-29T07:00:00+08:00", "10001", "", "rp", "settled"],
].map(([request_id, occurred_at, user_id, group_id, kind, status], index) => ({
  id: index + 1, request_id, occurred_at, user_id, group_id, kind, status, platform: "qq",
  group_name: fixture.groupNames[group_id] || "", source_plugin: "chat",
  cost_yuan: kind === "rp" ? 0 : status === "unknown" ? null : 0.106,
  points_delta: kind === "rp" ? 20 : status === "unknown" ? null : -2.12,
  total_tokens: status === "unknown" ? null : 100000, details: { model: "deepseek-flash" }
}));
const users = [
  { platform: "qq", user_id: "10001", name: "测试用户" },
  { platform: "qq", user_id: "10002", name: "<script>不可执行的昵称</script>" }
];
function analyticsFixture(period) {
  const startDay = period === "month" ? "01" : period === "7d" ? "23" : "29";
  const start = "2026-10-" + startDay + "T00:00:00+08:00", end = "2026-10-29T20:30:00+08:00";
  const until = "2026-10-29T20:30:00.000001+08:00", recent_since = "2026-10-29T19:30:00.000001+08:00";
  const stamp = (t) => new Date(t + 8 * 3600000).toISOString().slice(0, 19) + "+08:00";
  const rows = records.filter((r) => r.kind === "consumption" && r.status === "settled" && r.occurred_at >= start);
  const series = [];
  for (let t = Date.parse(start); t < Date.parse(end); t += 3600000) {
    const hour = stamp(t), items = rows.filter((r) => r.occurred_at.slice(0, 13) === hour.slice(0, 13));
    const cost = items.length * 0.106, partial = t + 3600000 > Date.parse(end);
    series.push({start:hour, cost_yuan:cost, requests:items.length, total_tokens:items.length * 100000, rate_yuan_per_hour:cost * (partial ? 2 : 1), partial});
  }
  const ranking = (key) => [...new Set(rows.map((r) => r[key]))].map((id) => {
    const n = rows.filter((r) => r[key] === id).length;
    return { platform:"qq", [key]: id, group_name:fixture.groupNames[id], name:users.find((u) => u.user_id === id)?.name, cost_yuan:n * 0.106, requests:n, share:n / rows.length };
  });
  const pending = records.filter((r) => r.status === "unknown" && r.occurred_at >= start).length;
  return {period, start, end, until, recent_since, series, cost_yuan:rows.length * 0.106, requests:rows.length, total_tokens:rows.length * 100000,
    recent_hour_yuan:0.106, pending_count:pending, reserved_yuan:pending * 0.212,
    peak_hours:[20], quiet_hours:[0], hour_profile:[{hour:20, average_yuan_per_hour:0.212}, {hour:0, average_yuan_per_hour:0}], groups:ranking("group_id"), users:ranking("user_id")};
}
function pageOf(rows, query) {
  const page = Number(query.page || 1), size = 2;
  return {page, page_size:size, total:rows.length, items:rows.slice((page - 1) * size, page * size)};
}
window.AstrBotPluginPage = {
  ready: async () => ({isDark:false}),
  apiGet: async (path, query = {}) => {
    if (path === "credits/settings") return {revision:fixture.revision, values:structuredClone(fixture.values)};
    if (path === "credits/plugins") return [{name:"astrbot_plugin_liuyao",display_name:"六爻",activated:true}, {name:"astrbot_plugin_test",display_name:"<img src=x onerror=alert(1)>",activated:false}];
    if (path === "credits/quote") return {official_catalog:fixture.official, points_per_yuan:fixture.values.points_per_yuan};
    if (path === "credits/overview") return {
      day:"2026-10-29", global_mode:fixture.values.global_mode,
      monthly:{limit_yuan:fixture.values.global_monthly_yuan,spent_yuan:8.3,reserved_yuan:0.212,remaining_yuan:fixture.values.global_monthly_yuan-8.512},
      global:{limit_yuan:10,spent_yuan:fixture.spent,reserved_yuan:0.212,remaining_yuan:9.682},
      groups:["10002000","10003000","10004000"].map((group_id) => ({platform:"qq",group_id,group_name:fixture.groupNames[group_id] || "",limit_yuan:3,spent_yuan:fixture.spent,reserved_yuan:0.212,remaining_yuan:2.682,is_override:true})),
      totals:{spent_yuan:58.2,users:2,points:fixture.balance + 20.54}, pending_count:records.filter((r) => r.status === "unknown").length, integration:{fixture:true,rp_connected:true}
    };
    if (path === "credits/analytics") return analyticsFixture(query.period || "today");
    if (path === "credits/users") {
      const matches = users.filter((u) => query.user_id ? u.user_id === query.user_id && u.platform === query.platform : (u.name + u.user_id).includes(query.query || ""));
      return pageOf(matches.map((u) => {
        const balance = u.user_id === "10001" ? fixture.balance : 20.54;
        return {...u, balance, amount_yuan:balance / fixture.values.points_per_yuan, spent_yuan:0.106};
      }), query);
    }
    if (path === "credits/ledger") {
      const rows = records.filter((r) => {
        if (["platform","user_id","group_id","kind"].some((key) => query[key] && r[key] !== query[key])) return false;
        if (query.status === "pending" ? !["unknown","reserved"].includes(r.status) : query.status && r.status !== query.status) return false;
        return !(query.scope === "private" && r.group_id || query.day && r.occurred_at.slice(0,10) !== query.day ||
          query.since && r.occurred_at < query.since || query.until && r.occurred_at >= query.until);
      });
      return pageOf(rows, query);
    }
    throw Error("fixture endpoint missing: " + path);
  },
  apiPost: async (path, payload) => {
    if (path === "credits/settings") {
      if (payload.revision !== fixture.revision) throw Error("配置版本冲突");
      fixture.values = structuredClone(payload.values);
      return {values:structuredClone(fixture.values),revision:++fixture.revision};
    }
    if (path === "credits/adjust") { fixture.balance += payload.delta; return {saved:true}; }
    if (path === "credits/resolve") {
      const row = records.find((r) => r.request_id === payload.request_id && r.status === "unknown");
      if (!row) throw Error("请求未处于待核对状态");
      row.status = payload.cancel ? "cancelled" : "settled"; return {saved:true};
    }
    throw Error("fixture endpoint missing: " + path);
  }
};
"""


class PreviewHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/preview":
            body = (PAGE / "index.html").read_text(encoding="utf-8")
            body = body.replace(
                "<body>",
                '<body><p style="padding:12px;background:#fff2cc;color:#513c00">本地演示数据 · 所有修改仅保存在浏览器内存，不连接真实账本</p>',
            )
            body = body.replace(
                '<script type="module" src="./app.js">',
                '<script src="./fixture-bridge.js"></script><script type="module" src="./app.js">',
            )
            content_type = "text/html; charset=utf-8"
        elif path == "/fixture-bridge.js":
            body = BRIDGE.replace("__DEFAULTS__", json.dumps(DEFAULTS)).replace(
                "__CATALOG__", json.dumps(quote({})["official_catalog"])
            )
            content_type = "text/javascript; charset=utf-8"
        elif path in {"/app.js", "/style.css"}:
            body = (PAGE / path.lstrip("/")).read_text(encoding="utf-8")
            content_type = (
                "text/javascript; charset=utf-8"
                if path.endswith(".js")
                else "text/css; charset=utf-8"
            )
        else:
            self.send_error(404)
            return
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)


if __name__ == "__main__":
    print("Credits fixture: http://127.0.0.1:8943/preview", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 8943), PreviewHandler).serve_forever()
