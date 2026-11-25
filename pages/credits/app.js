const $ = (id) => document.getElementById(id);
const bridge = window.AstrBotPluginView ?? window.AstrBotPluginPage;
const state = {
  ready: false, view: "", params: {}, request: 0,
  values: null, revision: 0, catalog: null, dirty: false, adjustment: null,
  series: [], until: "", inspected: ""
};
const views = new Set([...document.querySelectorAll("main > section")].map((section) => section.id));
const ledgerFields = ["platform", "user_id", "group_id", "scope", "kind", "status", "day"];
const settledCosts = { kind: "consumption", status: "settled" };
const numericKeys = ["points_per_yuan", "rp_base_points", "rp_step_points", "global_daily_yuan", "global_monthly_yuan", "group_daily_yuan", "reserve_yuan"];
const jsonKeys = ["group_overrides", "holidays", "model_prices"];
const kinds = { rp: "RP 获得", poke: "戳一戳奖励", consumption: "LLM 消耗", adjustment: "管理员调整" };
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
function money(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  const digits = Math.abs(Number(value)) < 0.000001 ? 9 : 6;
  return `¥${decimal(value, digits)}`;
}
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
function groupIdentity(group, showPlatform = false) {
  const item = element("div", undefined, "group-identity");
  if (!group.group_id) {
    item.append("私聊 / 无群");
    if (showPlatform) item.append(element("small", group.platform, "group-id"));
    return item;
  }
  item.append(element("span", group.group_name || "群名称暂不可用", "group-name"));
  item.append(element("small", `${showPlatform ? `${group.platform || "—"} · ` : ""}群号 ${group.group_id}`, "group-id"));
  return item;
}
function pagination(prefix, data) {
  const page = Number(data.page || 1), total = Number(data.total || 0), size = Number(data.page_size || 30);
  $(`${prefix}-page`).textContent = `第 ${page} 页 · 共 ${total} 条`;
  $(`${prefix}-prev`).disabled = page <= 1;
  $(`${prefix}-next`).disabled = page * size >= total;
}

function routeHref(view, params = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "" && !(key === "page" && Number(value) === 1)) query.set(key, value);
  }
  return `#${view}${query.size ? `?${query}` : ""}`;
}
function link(href, content) {
  const anchor = element("a");
  anchor.href = href;
  anchor.append(content);
  return anchor;
}
function navigate(view, params = {}) {
  const hash = routeHref(view, params);
  if (location.hash === hash) showRoute();
  else location.hash = hash;
}
function groupFilter(group) {
  return group.group_id ? { platform: group.platform, group_id: group.group_id } : { platform: group.platform, scope: "private" };
}
function card(label, value, detail, href, highlight = false) {
  const item = link(href, element("span", label, "label"));
  item.className = `card${highlight ? " highlight" : ""}`;
  item.append(element("strong", value, "value"), element("span", detail, "detail"));
  return item;
}
async function loadOverview(request) {
  const data = await bridge.apiGet("credits/overview");
  if (request !== state.request) return;
  const global = data.global || {}, monthly = data.monthly || {};
  $("day").textContent = `${data.day} · 北京时间`;
  $("mode-status").textContent = data.global_mode
    ? "全局积分模式 · 所有群共享日、月预算，个人免扣积分"
    : "个人积分模式 · 按个人余额、全局日额度和群日额度结算";
  $("summary").replaceChildren(
    card("全局可用额度", money(global.remaining_yuan), `每日上限 ${money(global.limit_yuan)}`, "#settings", true),
    card("今日已结算", money(global.spent_yuan), `在途预占 ${money(global.reserved_yuan)}`, routeHref("ledger", { ...settledCosts, day: data.day })),
    card("本月已结算", money(monthly.spent_yuan), `在途预占 ${money(monthly.reserved_yuan)}`, "#analytics?period=month"),
    card("本月可用额度", data.global_mode ? money(monthly.remaining_yuan) : "月限额未启用", `每月上限 ${money(monthly.limit_yuan)}`, "#settings"),
    card("累计已结算", money(data.totals?.spent_yuan), "查看全部消费流水", routeHref("ledger", settledCosts)),
    card("用户积分总量", decimal(data.totals?.points), `${decimal(data.totals?.users, 0)} 位用户`, "#users"),
    card("用量待核对", decimal(data.pending_count, 0), "查看待核对请求", routeHref("ledger", { kind: "consumption", status: "unknown" }))
  );
  const body = $("groups-body"); body.replaceChildren();
  for (const group of data.groups || []) {
    const row = element("tr"), identity = element("td");
    identity.append(link(routeHref("ledger", { kind: "consumption", day: data.day, ...groupFilter(group) }), groupIdentity(group, true)));
    row.append(identity);
    cells(row, [data.global_mode ? "使用全局预算" : group.is_override ? "指定群额度" : "默认额度", data.global_mode ? "—" : money(group.limit_yuan), money(group.spent_yuan), money(group.reserved_yuan), data.global_mode ? "共享全局余额" : money(group.remaining_yuan)]);
    body.append(row);
  }
  if (!body.children.length) empty(body, 6, "暂无群消费记录");
  $("integration").textContent = pretty(data.integration || {});
}
function svgElement(tag, attributes = {}, text) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  if (text !== undefined) node.textContent = text;
  return node;
}
function inspectHour(index) {
  const entry = state.series[index];
  if (!entry) return;
  $("chart-hour").value = index;
  state.inspected = entry.start;
  $("hour-ledger").href = routeHref("ledger", { ...settledCosts, since: entry.start, until: state.series[index + 1]?.start || state.until });
  $("hour-ledger").hidden = false;
  $("chart-detail").textContent = `${beijingTime(entry.start)} 起 · 已结算 ${money(entry.cost_yuan)} · ${decimal(entry.rate_yuan_per_hour)} 元/小时 · ${decimal(entry.requests, 0)} 次请求 · ${decimal(entry.total_tokens, 0)} Token${entry.partial ? " · 当前小时尚未结束" : ""}`;
  const cursor = $("chart-cursor");
  if (cursor) { cursor.setAttribute("x1", state.chartX(index)); cursor.setAttribute("x2", state.chartX(index)); }
}
function renderChart(series) {
  state.series = series;
  const target = $("cost-chart"); target.replaceChildren();
  $("chart-hour").max = Math.max(0, series.length - 1);
  $("chart-hour").disabled = !series.length;
  $("hour-ledger").hidden = !series.length;
  if (!series.length) { target.append(element("p", "暂无可绘制的时段", "empty")); $("chart-detail").textContent = ""; return; }
  const width = Math.max(235, target.clientWidth - 16), height = 260, left = 82, right = width - 18, top = 20, bottom = 218;
  const x = (index) => left + index / Math.max(series.length - 1, 1) * (right - left);
  const max = Math.max(0.001, ...series.map((item) => item.rate_yuan_per_hour));
  const y = (rate) => bottom - rate / max * (bottom - top);
  state.chartX = x;
  const svg = svgElement("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "每小时已结算消耗速度曲线，可用下方滑块查看数值" });
  for (let i = 0; i <= 4; i++) {
    const value = max * i / 4;
    svg.append(svgElement("line", { x1: left, x2: right, y1: y(value), y2: y(value), class: "chart-grid" }), svgElement("text", { x: left - 10, y: y(value) + 4, "text-anchor": "end" }, decimal(value, 6)));
  }
  const points = series.map((entry, index) => `${x(index)},${y(entry.rate_yuan_per_hour)}`);
  svg.append(svgElement("path", { d: `M${left},${bottom} L${points.join(" L")} L${x(series.length - 1)},${bottom} Z`, class: "chart-area" }));
  svg.append(svgElement("polyline", { points: points.join(" "), class: "chart-line" }));
  if (series.length === 1) svg.append(svgElement("circle", { cx: x(0), cy: y(series[0].rate_yuan_per_hour), r: 4, class: "chart-dot" }));
  const labels = width < 600 ? [0, series.length - 1] : [0, Math.floor((series.length - 1) / 2), series.length - 1];
  for (const index of new Set(labels)) {
    const label = series[index].start.slice(5, 16).replace("T", " ");
    svg.append(svgElement("text", { x: x(index), y: 248, "text-anchor": index === 0 ? "start" : index === series.length - 1 ? "end" : "middle" }, label));
  }
  svg.append(svgElement("line", { id: "chart-cursor", x1: right, x2: right, y1: top, y2: bottom, class: "chart-cursor" }));
  svg.addEventListener("pointermove", (event) => {
    const bounds = svg.getBoundingClientRect();
    const position = (event.clientX - bounds.left) / bounds.width * width;
    inspectHour(Math.max(0, Math.min(series.length - 1, Math.round((position - left) / (right - left) * (series.length - 1)))));
  });
  target.append(svg);
  const selected = series.findIndex((entry) => entry.start === state.inspected);
  inspectHour(selected < 0 ? series.length - 1 : selected);
}

function renderRanking(id, entries, group, range) {
  const body = $(id); body.replaceChildren();
  for (const entry of entries) {
    const row = element("tr"), identity = element("td");
    const subject = group ? groupFilter(entry) : { platform: entry.platform, user_id: entry.user_id };
    const content = group ? groupIdentity(entry, true) : element("span", entry.name || entry.user_id);
    if (!group) content.append(element("small", `${entry.platform} · ${entry.user_id}`, "group-id"));
    identity.append(link(routeHref("ledger", { ...settledCosts, ...range, ...subject }), content));
    row.append(identity);
    cells(row, [money(entry.cost_yuan), `${decimal(entry.share * 100, 1)}%`, decimal(entry.requests, 0)]);
    body.append(row);
  }
  if (!entries.length) empty(body, 4, "所选范围暂无已结算消费");
}
async function loadAnalytics(request) {
  const data = await bridge.apiGet("credits/analytics", { period: $("analytics-period").value });
  if (request !== state.request) return;
  state.until = data.until;
  const range = { since: data.start, until: data.until };
  const settled = routeHref("ledger", { ...settledCosts, ...range });
  $("analytics-time").textContent = `${beijingTime(data.start)} 至 ${beijingTime(data.end)} · 北京时间`;
  $("analytics-summary").replaceChildren(
    card("最近 60 分钟消耗速度", `${money(data.recent_hour_yuan)} / 小时`, "最近一小时的已结算请求", routeHref("ledger", { ...settledCosts, since: data.recent_since, until: data.until })),
    card("所选范围已结算", money(data.cost_yuan), `${decimal(data.requests, 0)} 次请求`, settled, true),
    card("所选范围 Token", decimal(data.total_tokens, 0), "服务商报告的已结算用量", settled),
    card("在途或待核对", `${decimal(data.pending_count, 0)} 笔`, `预留 ${money(data.reserved_yuan)}`, routeHref("ledger", { kind: "consumption", status: "pending", ...range }))
  );
  renderChart(data.series || []);
  const peak = $("peak-analysis"); peak.replaceChildren();
  const hourLabel = (hour) => `${String(hour).padStart(2, "0")}:00–${String(hour + 1).padStart(2, "0")}:00`;
  const describe = (hour) => `${hourLabel(hour)}（${money(data.hour_profile.find((item) => item.hour === hour)?.average_yuan_per_hour)}/小时）`;
  if (data.peak_hours?.length) {
    peak.append(element("p", `消耗高峰　${data.peak_hours.map(describe).join("、")}`), element("p", `相对低峰　${data.quiet_hours.map(describe).join("、")}`));
  } else peak.append(element("p", "当前时段的消耗差异不足以区分高低峰。"));
  renderRanking("group-ranking", data.groups || [], true, range);
  renderRanking("user-ranking", data.users || [], false, range);
}
async function loadUsers(request) {
  const data = await bridge.apiGet("credits/users", { ...state.params, page: state.params.page || 1 });
  if (request !== state.request) return;
  const body = $("users-body"); body.replaceChildren();
  for (const user of data.items || []) {
    const row = element("tr"), name = element("td"), cost = element("td");
    const subject = { platform: user.platform, user_id: user.user_id };
    name.append(link(routeHref("ledger", subject), user.name ? `${user.name} (${user.user_id})` : user.user_id));
    cost.append(link(routeHref("ledger", { ...settledCosts, ...subject }), money(user.spent_yuan)));
    row.append(element("td", user.platform), name);
    cells(row, [decimal(user.balance), money(user.amount_yuan)]);
    row.append(cost); body.append(row);
  }
  if (!body.children.length) empty(body, 5);
  pagination("users", data);
}
function beijingTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(date);
}
async function loadLedger(request) {
  const data = await bridge.apiGet("credits/ledger", { ...state.params, page: state.params.page || 1 });
  if (request !== state.request) return;
  const body = $("ledger-body"); body.replaceChildren();
  for (const entry of data.items || []) {
    const row = element("tr");
    cells(row, [beijingTime(entry.occurred_at)]);
    const identity = element("td");
    if (entry.user_id && entry.platform) {
      const account = link(routeHref("users", { platform: entry.platform, user_id: entry.user_id }), `${entry.platform} · ${entry.user_id}`);
      account.title = "查看用户余额"; identity.append(account);
    } else identity.append(element("span", entry.user_id || "无用户"));
    const group = link(routeHref("ledger", { ...state.params, user_id: "", group_id: "", scope: "", page: 1, ...groupFilter(entry) }), groupIdentity(entry));
    group.title = entry.group_id ? "查看该群流水" : "查看私聊流水";
    identity.append(group); row.append(identity);
    cells(row, [`${entry.source_plugin || "普通对话"}\n${kinds[entry.kind] || entry.kind || "—"}`]);
    const points = Number(entry.points_delta || 0);
    row.append(element("td", entry.kind === "consumption" && entry.details?.global_mode ? "全局模式免扣" : `${points > 0 ? "+" : ""}${decimal(entry.points_delta)}`, `numeric ${points > 0 ? "positive" : points < 0 ? "negative" : ""}`));
    row.append(element("td", money(entry.cost_yuan), "numeric"), element("td", decimal(entry.total_tokens, 0), "numeric"));
    const status = element("td"); status.append(element("span", statuses[entry.status] || entry.status || "—"));
    const details = element("details"); details.append(element("summary", "查看快照"), element("pre", pretty(entry)));
    if (entry.kind === "consumption" && entry.status === "unknown" && entry.request_id) {
      status.append(element("br"), link(routeHref("reconcile", { request_id: entry.request_id }), "核对用量"));
    }
    status.append(details); row.append(status); body.append(row);
  }
  if (!body.children.length) empty(body, 7);
  pagination("ledger", data);
}
function renderPlugins(plugins) {
  const body = $("plugins-list"); body.replaceChildren();
  for (const plugin of plugins) {
    if (!plugin.name) continue;
    const label = element("div", undefined, "plugin");
    const text = element("span", plugin.display_name || plugin.name);
    text.append(element("small", `${plugin.name}${plugin.version ? ` · ${plugin.version}` : ""} · ${plugin.activated === false ? "已停用" : "已启用"} · 独立调用放行`));
    label.append(text); body.append(label);
  }
  if (!body.children.length) body.append(element("p", "未检测到其他插件。", "muted"));
}

function setDirty(value) {
  state.dirty = value;
  $("draft-status").textContent = value ? "有未保存的修改" : "";
  $("discard-settings").hidden = !value;
}
function renderMapping() {
  const form = $("settings-form"), target = $("mapping"), rates = $("rates-table");
  const ratio = Number(form.elements.points_per_yuan.value);
  const base = Number(form.elements.rp_base_points.value), step = Number(form.elements.rp_step_points.value);
  const global = form.elements.global_mode.value === "true";
  target.replaceChildren();
  if (global) target.append(element("strong", "全局模式使用共享预算，不扣个人积分，RP 签到暂停发分。"));
  else if (ratio > 0) target.append(element("strong", `1 积分 = ${money(1 / ratio)} · ¥1 = ${decimal(ratio)} 积分`),
    element("p", `RP 为 0 / 50 / 100 时分别获得 ${decimal(base)} / ${decimal(base + step * 50)} / ${decimal(base + step * 100)} 积分。`));
  else target.append(element("p", "填写每元对应积分后可查看换算。"));
  rates.replaceChildren();
  let prices;
  try { prices = parseJSONField(form, "model_prices"); } catch { /* show feedback below */ }
  if (!prices || typeof prices !== "object" || Array.isArray(prices) || !Object.keys(prices).length) {
    rates.append(element("p", "填写有效的模型价格 JSON 后可预览费率。", "hint")); return;
  }
  const table = element("table"), head = element("tr"), thead = element("thead"), tbody = element("tbody");
  for (const title of ["模型", "时段", "未命中输入", "命中输入", "输出", "100k 示例费用", ...(global ? [] : ["示例积分", "每积分 Token¹"])]) head.append(element("th", title));
  thead.append(head); table.append(thead, tbody);
  for (const [model, tiers] of Object.entries(prices)) {
    for (const [key, tier] of [["offpeak", "空闲"], ["peak", "高峰"]]) {
      const rate = tiers?.[key] || {};
      const row = element("tr");
      const valid = [rate.input, rate.cached, rate.output].every((n) => Number.isFinite(n) && n >= 0);
      const cost = valid ? (98000 * rate.input + 2000 * rate.output) / 1e6 : null;
      cells(row, [model, tier, money(rate.input), money(rate.cached), money(rate.output), money(cost)]);
      if (!global) {
        const tokens = [rate.input, rate.cached, rate.output].map((price) => valid && price > 0 && ratio > 0 ? decimal(1e6 / (ratio * price), 0) : "—");
        cells(row, [cost !== null && ratio > 0 ? decimal(cost * ratio) : "—", tokens.join(" / ")]);
      }
      tbody.append(row);
    }
  }
  rates.append(table, element("p", "100k 示例含 98,000 未命中输入和 2,000 输出 Token。" + (global ? "" : "¹ 每积分可购买的 Token 依次按纯未命中输入、纯命中输入、纯输出换算。"), "hint"));
}
async function loadSettings(request, discardDraft = false) {
  if (state.dirty && !discardDraft) return;
  const [settings, plugins, quote] = await Promise.all([bridge.apiGet("credits/settings"), bridge.apiGet("credits/plugins"), bridge.apiGet("credits/quote")]);
  if (request !== state.request || (state.dirty && !discardDraft)) return;
  state.values = settings.values; state.revision = settings.revision; state.catalog = quote.official_catalog;
  const form = $("settings-form");
  form.elements.global_mode.value = String(Boolean(state.values.global_mode));
  form.elements.block_unpriced_models.checked = state.values.block_unpriced_models ?? true;
  for (const key of numericKeys) form.elements[key].value = state.values[key] ?? "";
  for (const key of jsonKeys) form.elements[key].value = pretty(state.values[key] ?? (key === "holidays" ? [] : {}));
  $("revision").textContent = `配置版本 ${state.revision}`;
  $("quote").textContent = pretty(quote);
  const warning = quote.calendar_warning || (quote.warnings || []).filter((item) => String(item).includes("节假日")).join(" ");
  $("calendar-warning").textContent = warning; $("calendar-warning").hidden = !warning;
  const source = $("catalog-source"); source.replaceChildren();
  if (state.catalog) {
    const official = link(state.catalog.source, "DeepSeek 官方人民币价目表");
    official.target = "_blank"; official.rel = "noopener noreferrer";
    source.append(`${state.catalog.checked_on} 核验 · `, official);
  }
  $("apply-catalog").disabled = !state.catalog;
  setDirty(false); form.inert = false;
  renderPlugins(Array.isArray(plugins) ? plugins : plugins.items || []); renderMapping();
}
function parseJSONField(form, key) {
  try { return JSON.parse(form.elements[key].value); } catch { throw new Error(`${key} 不是有效 JSON，请检查后重试`); }
}
function requestId() {
  if (crypto.randomUUID) return crypto.randomUUID();
  const bytes = new Uint8Array(16); crypto.getRandomValues(bytes);
  return Array.from(bytes, (x) => x.toString(16).padStart(2, "0")).join("");
}

function syncScope() {
  const form = $("ledger-filter");
  const privateOnly = form.elements.scope.value === "private";
  if (privateOnly) form.elements.group_id.value = "";
  form.elements.group_id.disabled = privateOnly;
}
function syncResolveMode() {
  const form = $("resolve-form"), cancel = form.elements.mode.value === "cancel";
  $("usage-label").hidden = cancel;
  form.elements.usage.disabled = cancel;
}
async function refreshView(discardDraft = false) {
  const request = ++state.request;
  const panel = $(state.view);
  const loaders = { overview: loadOverview, analytics: loadAnalytics, users: loadUsers, ledger: loadLedger, settings: loadSettings };
  const load = loaders[state.view];
  if (!state.ready || !load) return;
  panel.setAttribute("aria-busy", "true");
  try { await load(request, discardDraft); }
  catch (error) {
    if (request === state.request) message(error?.message || "数据加载失败，请刷新重试", true);
  } finally {
    if (request === state.request) panel.removeAttribute("aria-busy");
  }
}
function showRoute() {
  const [name, query] = location.hash.slice(1).split("?");
  const view = views.has(name) ? name : "overview";
  const params = Object.fromEntries(new URLSearchParams(query));
  const previous = state.view, previousParams = state.params;
  const page = Number(params.page);
  state.params = {};
  if ((view === "ledger" || view === "users") && Number.isInteger(page) && page > 1 && page <= 1000000) state.params.page = page;
  $("message").hidden = true;
  if (previous) $(previous).removeAttribute("aria-busy");
  for (const section of document.querySelectorAll("main > section")) section.hidden = section.id !== view;
  for (const item of document.querySelectorAll("nav a")) {
    if (item.hash === `#${view}`) item.setAttribute("aria-current", "page"); else item.removeAttribute("aria-current");
  }
  state.view = view;
  $("refresh").hidden = view === "reconcile";
  if (view === "ledger") {
    const form = $("ledger-filter"); form.reset();
    for (const key of ledgerFields) {
      form.elements[key].value = params[key] || "";
      if (form.elements[key].value) state.params[key] = form.elements[key].value;
    }
    syncScope();
    if (state.params.scope === "private") delete state.params.group_id;
    for (const key of ["since", "until"]) if (params[key]) state.params[key] = params[key];
    const filterWindow = $("ledger-window");
    filterWindow.hidden = !params.since && !params.until;
    filterWindow.querySelector("span").textContent = `统计时段（北京）　${beijingTime(params.since)} 至 ${beijingTime(params.until)}`;
    $("clear-window").href = routeHref("ledger", { ...state.params, since: "", until: "", page: 1 });
    empty($("ledger-body"), 7, "正在加载流水…");
  } else if (view === "analytics") {
    const period = ["today", "7d", "month"].includes(params.period) ? params.period : "today";
    $("analytics-period").value = period; state.params.period = period;
    if (previous !== view || previousParams.period !== period) {
      state.series = []; state.inspected = "";
      $("analytics-time").textContent = "正在加载统计…";
      $("analytics-summary").replaceChildren(); $("peak-analysis").replaceChildren();
      renderChart([]); empty($("group-ranking"), 4, "正在加载排行…"); empty($("user-ranking"), 4, "正在加载排行…");
    }
  } else if (view === "users") {
    $("user-query").value = params.query || "";
    const exact = Boolean(params.platform && params.user_id);
    $("user-account").hidden = !exact;
    if (exact) {
      state.params.platform = params.platform; state.params.user_id = params.user_id;
      $("user-account").querySelector("span").textContent = `当前账户　${params.platform} · ${params.user_id}`;
    } else if (params.query) state.params.query = params.query;
    empty($("users-body"), 5, "正在加载用户…");
  } else if (view === "settings" && !state.values) {
    $("settings-form").inert = true;
  } else if (view === "reconcile") {
    const form = $("resolve-form");
    form.reset(); syncResolveMode(); form.elements.request_id.value = params.request_id || "";
    $("reconcile-back").href = previous === "ledger" ? routeHref("ledger", previousParams) : routeHref("ledger", { kind: "consumption", status: "unknown" });
    $("reconcile-back").textContent = previous === "ledger" ? "返回来源流水" : "查看待核对流水";
  }
  if (view === "users" || view === "ledger") {
    $(`${view}-prev`).disabled = true; $(`${view}-next`).disabled = true; $(`${view}-page`).textContent = "";
  }
  if (view !== previous && previous) {
    $(view).querySelector("h2").focus({ preventScroll: true });
    window.scrollTo(0, 0);
  }
  if (!bridge) {
    message("请从 AstrBot 插件详情打开「积分与模型额度」，以连接管理账本。", true);
    return;
  }
  return refreshView();
}
$("refresh").addEventListener("click", (event) => action(event.currentTarget, async () => {
  if (state.view === "settings" && state.dirty) {
    message("当前有未保存的设置。可先保存，或选择「放弃修改并重载」。"); return;
  }
  await refreshView();
}));
window.addEventListener("hashchange", showRoute);
document.addEventListener("click", (event) => {
  const anchor = event.target.closest("a[href^='#']");
  if (anchor && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey) {
    event.preventDefault();
    if (anchor.hash === location.hash) showRoute(); else location.hash = anchor.hash;
  }
});
$("analytics-period").addEventListener("change", (event) => navigate("analytics", { period: event.target.value }));
$("chart-hour").addEventListener("input", (event) => inspectHour(Number(event.target.value)));
window.addEventListener("resize", () => { if (state.view === "analytics" && state.series.length) renderChart(state.series); });
$("user-search").addEventListener("submit", (event) => {
  event.preventDefault(); navigate("users", { query: $("user-query").value.trim() });
});
$("ledger-filter").elements.scope.addEventListener("change", syncScope);
$("ledger-filter").addEventListener("submit", (event) => {
  event.preventDefault();
  const filters = Object.fromEntries(new FormData(event.currentTarget));
  navigate("ledger", { ...filters, since: state.params.since, until: state.params.until });
});
for (const view of ["users", "ledger"]) {
  for (const [direction, delta] of [["prev", -1], ["next", 1]]) {
    $(`${view}-${direction}`).addEventListener("click", () => navigate(view, { ...state.params, page: Math.max(1, (state.params.page || 1) + delta) }));
  }
}
$("settings-form").addEventListener("input", () => { if (state.values) { setDirty(true); renderMapping(); } });
$("settings-form").addEventListener("invalid", (event) => {
  const details = event.target.closest("details");
  if (details) details.open = true;
}, true);
$("discard-settings").addEventListener("click", (event) => action(event.currentTarget, async () => {
  const form = $("settings-form"); form.inert = true;
  try { await refreshView(true); } finally { form.inert = false; }
}));
$("apply-catalog").addEventListener("click", () => action(null, async () => {
  const form = $("settings-form"), prices = parseJSONField(form, "model_prices");
  if (!prices || typeof prices !== "object" || Array.isArray(prices)) throw Error("请先修正模型价格 JSON。");
  form.elements.model_prices.value = pretty({ ...prices, ...state.catalog.models });
  setDirty(true); renderMapping(); message("已将本版 Flash 和 Pro 官方价填入草稿，保存后生效。");
}));
$("settings-form").addEventListener("submit", (event) => {
  event.preventDefault(); const form = event.currentTarget;
  action(event.submitter, async () => {
    if (!state.values) throw new Error("配置尚未加载，不能保存");
    const values = {
      ...state.values,
      global_mode: form.elements.global_mode.value === "true",
      block_unpriced_models: form.elements.block_unpriced_models.checked,
      charged_plugins: [],
    };
    for (const key of numericKeys) values[key] = Number(form.elements[key].value);
    for (const key of jsonKeys) {
      try { values[key] = parseJSONField(form, key); }
      catch (error) { form.elements[key].closest("details").open = true; form.elements[key].focus(); throw error; }
    }
    form.inert = true;
    try {
      const saved = await bridge.apiPost("credits/settings", { revision: state.revision, values });
      state.values = saved.values; state.revision = saved.revision;
      setDirty(false);
      if (state.view === "settings") {
        message("计费设置已保存，后续请求使用新配置。"); await refreshView();
      }
    } finally { form.inert = false; }
  });
});
$("adjust-form").addEventListener("submit", (event) => {
  event.preventDefault(); const form = event.currentTarget;
  action(event.submitter, async () => {
    const payload = Object.fromEntries(new FormData(form)); payload.delta = Number(payload.delta);
    if (!payload.reason.trim() || !Number.isFinite(payload.delta) || payload.delta === 0) throw new Error("填写非零调整积分和调整原因");
    const signature = pretty(payload);
    if (!state.adjustment || state.adjustment.signature !== signature) state.adjustment = { signature, request_id: requestId() };
    payload.request_id = state.adjustment.request_id;
    await bridge.apiPost("credits/adjust", payload); state.adjustment = null; form.reset();
    message("积分已调整，操作已记入流水。"); await refreshView();
  });
});
$("resolve-form").elements.mode.addEventListener("change", syncResolveMode);
$("resolve-form").addEventListener("submit", (event) => {
  event.preventDefault(); const form = event.currentTarget;
  action(event.submitter, async () => {
    const payload = { request_id: form.elements.request_id.value.trim(), reason: form.elements.reason.value.trim() };
    if (!payload.reason) throw new Error("请填写核对原因");
    if (form.elements.mode.value === "cancel") payload.cancel = true; else payload.usage = parseJSONField(form, "usage");
    await bridge.apiPost("credits/resolve", payload);
    form.reset(); syncResolveMode(); message("核对结果已记录，可返回流水查看。");
  });
});

showRoute();
if (!bridge) {
  for (const button of document.querySelectorAll("button")) button.disabled = true;
} else {
  action($("refresh"), async () => {
    await bridge.ready(); state.ready = true; await refreshView();
    setInterval(() => {
      if (!document.hidden && ["overview", "analytics"].includes(state.view) && !$(state.view).hasAttribute("aria-busy")) refreshView();
    }, 60000);
  });
}
