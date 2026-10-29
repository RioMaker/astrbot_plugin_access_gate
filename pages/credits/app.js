const $ = (id) => document.getElementById(id);
const bridge = window.AstrBotPluginView ?? window.AstrBotPluginPage;
const state = { values: null, revision: 0, userPage: 1, ledgerPage: 1, plugins: [], adjustment: null };
const numericKeys = ["points_per_yuan", "rp_base_points", "rp_step_points", "global_daily_yuan", "group_daily_yuan", "reserve_yuan"];
const jsonKeys = ["group_overrides", "holidays", "model_prices"];
const kinds = { rp: "RP 获得", consumption: "LLM 消耗", adjustment: "管理员调整" };
const statuses = { reserved: "在途预占", unknown: "用量待核对", settled: "已结算", cancelled: "已取消" };

function element(tag, text, className) {
  const item = document.createElement(tag);
  if (text !== undefined) item.textContent = String(text);
  if (className) item.className = className;
  return item;
}
function decimal(value, digits = 6) {
  if (value === null || value === undefined || value === "") return "—";
  const num = Number(value);
  return Number.isFinite(num) ? num.toLocaleString("zh-CN", { maximumFractionDigits: digits }) : "—";
}
function money(value) { return value === null || value === undefined ? "—" : `¥${decimal(value, 6)}`; }
function pretty(value) { return JSON.stringify(value, null, 2); }
function message(text, error = false) {
  $("message").textContent = text;
  $("message").className = error ? "error" : "success";
  $("message").hidden = false;
}
async function action(button, task) {
  if (button) button.disabled = true;
  try { await task(); } catch (error) { message(error?.message || "操作失败，请稍后重试", true); }
  finally { if (button) button.disabled = false; }
}
function empty(tbody, columns, text = "暂无记录") {
  tbody.replaceChildren();
  const cell = element("td", text, "empty"); cell.colSpan = columns;
  const row = element("tr"); row.append(cell); tbody.append(row);
}
function cells(row, values) { for (const value of values) row.append(element("td", value)); }
function pagination(prefix, data) {
  const page = Number(data.page || 1), total = Number(data.total || 0), size = Number(data.page_size || 30);
  $(`${prefix}-page`).textContent = `第 ${page} 页 · 共 ${total} 条`;
  $(`${prefix}-prev`).disabled = page <= 1;
  $(`${prefix}-next`).disabled = page * size >= total;
}
function card(label, value, detail, highlight = false) {
  const item = element("div", undefined, `card${highlight ? " highlight" : ""}`);
  item.append(element("div", label, "label"), element("div", value, "value"), element("div", detail, "detail"));
  return item;
}
async function loadOverview() {
  const data = await bridge.apiGet("credits/overview");
  const global = data.global || {};
  $("day").textContent = `${data.day || "—"} · 北京时间`;
  $("summary").replaceChildren(
    card("全局可用额度", money(global.remaining_yuan), `每日上限 ${money(global.limit_yuan)}`, true),
    card("今日已结算", money(global.spent_yuan), `在途预占 ${money(global.reserved_yuan)}`),
    card("用户积分总量", decimal(data.totals?.points), `${decimal(data.totals?.users, 0)} 位用户`),
    card("用量待核对", decimal(data.pending_count, 0), "未报告可靠用量，预留额度暂不释放")
  );
  const body = $("groups-body"); body.replaceChildren();
  for (const group of data.groups || []) {
    const row = element("tr");
    cells(row, [`${group.platform || "—"} / ${group.group_id}`, group.is_override ? "指定群额度" : "默认额度", money(group.limit_yuan), money(group.spent_yuan), money(group.reserved_yuan), money(group.remaining_yuan)]);
    body.append(row);
  }
  if (!body.children.length) empty(body, 6, "暂无群消费记录；默认额度和指定群额度可在下方设置");
  $("integration").textContent = pretty(data.integration || {});
}
async function loadUsers() {
  const data = await bridge.apiGet("credits/users", { query: $("user-query").value.trim(), page: state.userPage });
  const body = $("users-body"); body.replaceChildren();
  for (const user of data.items || []) {
    const row = element("tr");
    cells(row, [user.platform, user.name ? `${user.name} (${user.user_id})` : user.user_id, decimal(user.balance), money(user.amount_yuan)]);
    body.append(row);
  }
  if (!body.children.length) empty(body, 4);
  pagination("users", data);
}
function beijingTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(date);
}
async function loadLedger() {
  const filters = Object.fromEntries(new FormData($("ledger-filter")).entries());
  const data = await bridge.apiGet("credits/ledger", { ...filters, page: state.ledgerPage });
  const body = $("ledger-body"); body.replaceChildren();
  for (const entry of data.items || []) {
    const row = element("tr");
    cells(row, [beijingTime(entry.occurred_at), `${entry.platform || "—"} · ${entry.user_id || "无用户"}\n群 ${entry.group_id || "私聊 / 无群"}`, `${entry.source_plugin || "普通对话"}\n${kinds[entry.kind] || entry.kind || "—"}`]);
    const points = Number(entry.points_delta || 0);
    row.append(element("td", `${points > 0 ? "+" : ""}${decimal(entry.points_delta)}`, `numeric ${points > 0 ? "positive" : points < 0 ? "negative" : ""}`));
    row.append(element("td", money(entry.cost_yuan), "numeric"), element("td", decimal(entry.total_tokens, 0), "numeric"));
    const status = element("td"); status.append(element("span", statuses[entry.status] || entry.status || "—"));
    const details = element("details"); details.append(element("summary", "查看快照"), element("pre", pretty(entry)));
    status.append(details); row.append(status); body.append(row);
  }
  if (!body.children.length) empty(body, 7);
  pagination("ledger", data);
}
function renderPlugins() {
  const body = $("plugins-list"); body.replaceChildren();
  const selected = new Set(state.values?.charged_plugins || []);
  const plugins = [...state.plugins];
  for (const name of selected) if (!plugins.some((item) => item.name === name)) plugins.push({ name, display_name: name, activated: false, unavailable: true });
  for (const plugin of plugins) {
    if (!plugin.name) continue;
    const label = element("label", undefined, "plugin");
    const check = document.createElement("input"); check.type = "checkbox"; check.value = plugin.name; check.name = "charged_plugin"; check.checked = selected.has(plugin.name);
    const text = element("span", plugin.display_name || plugin.name);
    text.append(element("small", `${plugin.name}${plugin.version ? ` · ${plugin.version}` : ""} · ${plugin.unavailable ? "当前未检测到" : plugin.activated === false ? "已停用" : "已启用"}`));
    label.append(check, text); body.append(label);
  }
  if (!body.children.length) body.append(element("p", "未检测到可配置插件。", "muted"));
}
function renderMapping() {
  const form = $("settings-form"), target = $("mapping"); target.replaceChildren();
  const ratio = Number(form.elements.points_per_yuan.value);
  const base = Number(form.elements.rp_base_points.value), step = Number(form.elements.rp_step_points.value);
  if (!Number.isFinite(ratio) || ratio <= 0) { target.append(element("p", "填写有效积分比例后显示换算。")); return; }
  target.append(element("strong", `1 积分 = ${money(1 / ratio)} · ¥1 = ${decimal(ratio)} 积分`));
  target.append(element("p", `RP 0 / 50 / 100：分别获得 ${decimal(base)} / ${decimal(base + step * 50)} / ${decimal(base + step * 100)} 积分。`));
  let prices;
  try { prices = JSON.parse(form.elements.model_prices.value); } catch { target.append(element("p", "模型价格 JSON 尚未完成，修正后显示 Token 映射。")); return; }
  const flash = prices && typeof prices === "object" ? prices["deepseek-flash"] : null;
  if (!flash) { target.append(element("p", "未配置 deepseek-flash 价格；后端核验会显示当前模型映射。")); return; }
  const table = element("table"), head = element("tr");
  for (const name of ["时段", "1 积分：未缓存输入 / 缓存输入 / 输出", "100k 参考对话金额 / 积分"]) head.append(element("th", name));
  const thead = element("thead"); thead.append(head); table.append(thead);
  const tbody = element("tbody");
  for (const [tier, name] of [["offpeak", "空闲"], ["peak", "高峰"]]) {
    const rate = flash[tier]; if (!rate) continue;
    const equivalent = ["input", "cached", "output"].map((key) => Number(rate[key]) > 0 ? decimal(1e6 / (ratio * Number(rate[key])), 0) : "—").join(" / ");
    const cost = (98000 * Number(rate.input) + 2000 * Number(rate.output)) / 1e6;
    const row = element("tr"); cells(row, [name, equivalent, `${money(cost)} / ${decimal(cost * ratio)} 积分`]); tbody.append(row);
  }
  table.append(tbody); const wrapper = element("div", undefined, "table-wrap"); wrapper.append(table); target.append(wrapper);
  target.append(element("p", "100k 示例假设：98,000 未缓存输入 + 2,000 输出；仅供比例参考。实际扣分分别计算缓存输入、未缓存输入和输出，并使用请求时的峰谷价格。", "hint"));
}
async function loadSettings() {
  const [settings, plugins, quote] = await Promise.all([bridge.apiGet("credits/settings"), bridge.apiGet("credits/plugins"), bridge.apiGet("credits/quote")]);
  state.values = settings.values; state.revision = settings.revision; state.plugins = Array.isArray(plugins) ? plugins : plugins.items || [];
  const form = $("settings-form");
  for (const key of numericKeys) form.elements[key].value = state.values[key] ?? "";
  for (const key of jsonKeys) form.elements[key].value = pretty(state.values[key] ?? (key === "holidays" ? [] : {}));
  $("revision").textContent = `配置版本 ${state.revision}`;
  $("quote").textContent = pretty(quote);
  const calendarWarning = quote.calendar_warning || (quote.warnings || []).filter((item) => String(item).includes("节假日")).join(" ");
  $("calendar-warning").textContent = calendarWarning;
  $("calendar-warning").hidden = !calendarWarning;
  renderPlugins(); renderMapping();
}
async function refresh() { await Promise.all([loadOverview(), loadUsers(), loadLedger(), loadSettings()]); }
function parseJSONField(form, key) {
  try { return JSON.parse(form.elements[key].value); } catch { throw new Error(`${key} 不是有效 JSON，请检查后重试`); }
}
function requestId() {
  if (crypto.randomUUID) return crypto.randomUUID();
  const bytes = new Uint8Array(16); crypto.getRandomValues(bytes);
  return Array.from(bytes, (x) => x.toString(16).padStart(2, "0")).join("");
}
$("refresh").addEventListener("click", (event) => action(event.currentTarget, async () => { await refresh(); message("数据已刷新"); }));
$("user-search").addEventListener("submit", (event) => { event.preventDefault(); state.userPage = 1; action(event.submitter, loadUsers); });
$("ledger-filter").addEventListener("submit", (event) => { event.preventDefault(); state.ledgerPage = 1; action(event.submitter, loadLedger); });
for (const [prefix, field, loader] of [["users", "userPage", loadUsers], ["ledger", "ledgerPage", loadLedger]]) {
  for (const [direction, delta] of [["prev", -1], ["next", 1]]) $(`${prefix}-${direction}`).addEventListener("click", () => {
    const previous = state[field]; state[field] = Math.max(1, previous + delta);
    action(null, async () => { try { await loader(); } catch (error) { state[field] = previous; throw error; } });
  });
}
$("settings-form").addEventListener("input", renderMapping);
$("settings-form").addEventListener("submit", (event) => {
  event.preventDefault(); const form = event.currentTarget;
  action(event.submitter, async () => {
    if (!state.values) throw new Error("配置尚未加载，不能保存");
    const values = { ...state.values };
    for (const key of numericKeys) { const value = Number(form.elements[key].value); if (!Number.isFinite(value)) throw new Error(`${key} 必须为有效数字`); values[key] = value; }
    for (const key of jsonKeys) values[key] = parseJSONField(form, key);
    values.charged_plugins = Array.from(form.querySelectorAll('input[name="charged_plugin"]:checked'), (item) => item.value);
    await bridge.apiPost("credits/settings", { revision: state.revision, values });
    await Promise.all([loadSettings(), loadOverview()]); message("设置已保存，后续请求使用新配置");
  });
});
$("adjust-form").addEventListener("submit", (event) => {
  event.preventDefault(); const form = event.currentTarget;
  action(event.submitter, async () => {
    const payload = Object.fromEntries(new FormData(form).entries()); payload.delta = Number(payload.delta);
    if (!payload.reason.trim() || !Number.isFinite(payload.delta) || payload.delta === 0) throw new Error("填写非零调整积分和调整原因");
    const signature = pretty(payload);
    if (!state.adjustment || state.adjustment.signature !== signature) state.adjustment = { signature, request_id: requestId() };
    payload.request_id = state.adjustment.request_id;
    await bridge.apiPost("credits/adjust", payload); state.adjustment = null; form.reset();
    await Promise.all([loadOverview(), loadUsers(), loadLedger()]); message("积分已调整，操作已记入流水");
  });
});
$("resolve-form").elements.mode.addEventListener("change", (event) => { $("usage-label").hidden = event.target.value === "cancel"; });
$("resolve-form").addEventListener("submit", (event) => {
  event.preventDefault(); const form = event.currentTarget;
  action(event.submitter, async () => {
    const payload = { request_id: form.elements.request_id.value.trim(), reason: form.elements.reason.value.trim() };
    if (!payload.reason) throw new Error("请填写核对原因");
    if (form.elements.mode.value === "cancel") payload.cancel = true; else payload.usage = parseJSONField(form, "usage");
    await bridge.apiPost("credits/resolve", payload);
    await Promise.all([loadOverview(), loadUsers(), loadLedger()]); message("核对结果已记录");
  });
});

if (!bridge) {
  message("请在 AstrBot 的插件详情页打开「积分与模型额度」。此页面需要管理桥连接，未加载任何演示数据。", true);
  for (const button of document.querySelectorAll("button")) button.disabled = true;
} else {
  action($("refresh"), async () => { await bridge.ready(); await refresh(); });
}
