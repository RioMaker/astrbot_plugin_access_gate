// Run preview_credits.py first. Set NODE_PATH to a Playwright installation.
const assert = require("node:assert/strict");
const { chromium } = require("playwright");

async function checkPage() {
  const browser = await chromium.launch({headless: true, channel: "msedge"});
  try {
    const page = await browser.newPage({viewport: {width: 1280, height: 900}});
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("dialog", async (dialog) => { errors.push(dialog.message()); await dialog.dismiss(); });
    const open = async (view) => {
      await page.locator(`nav a[href="#${view}"]`).click();
      await page.locator(`#${view}:visible`).waitFor();
    };
    const textIncludes = (id, text) => page.waitForFunction(([id, text]) => document.getElementById(id).textContent.includes(text), [id, text]);
    const filters = () => page.evaluate(() => Object.fromEntries(new URLSearchParams(location.hash.split("?")[1])));
    const field = (form, name) => page.locator(`#${form} [name="${name}"]`);
    const submit = (id) => page.locator(`#${id} button`).filter({ hasNotText: "放弃" }).last();
    await page.goto(process.env.CREDITS_PREVIEW_URL || "http://127.0.0.1:8943/preview");
    await textIncludes("summary", "全局可用额度");
    assert.equal(await page.locator("main > section:visible").count(), 1);
    assert.match(await page.locator("#groups-body").innerText(), /群名称暂不可用[\s\S]*10004000/);
    await page.locator("#groups-body a").filter({hasText:"10003000"}).click();
    await textIncludes("ledger-body", "10003000");
    assert.match(await page.locator("#ledger-body").innerText(), /<img src=x onerror=alert\(1\)>/);
    assert.deepEqual(await filters(), {kind:"consumption", day:"2026-10-29", platform:"qq", group_id:"10003000"});

    await open("analytics");
    await page.locator("#cost-chart svg").waitFor();
    await textIncludes("peak-analysis", "消耗高峰");
    await page.locator("#analytics-period").selectOption("month");
    await page.waitForFunction(() => document.querySelector("#chart-hour").max === "692");
    if (process.env.CREDITS_ANALYTICS_SCREENSHOT) await page.screenshot({path: process.env.CREDITS_ANALYTICS_SCREENSHOT, fullPage:true});
    await page.locator("#group-ranking a").filter({hasText:"10002000"}).click();
    await textIncludes("ledger-body", "2026/10/28");
    const ranked = await filters();
    assert.equal(ranked.platform, "qq"); assert.equal(ranked.group_id, "10002000");
    assert.equal(ranked.status, "settled"); assert.equal(ranked.kind, "consumption");
    assert.equal(ranked.since, "2026-10-01T00:00:00+08:00");
    assert.equal(ranked.until, "2026-10-29T20:30:00.000001+08:00");
    assert.equal(await page.locator("#ledger-window").isVisible(), true);
    await page.reload();
    await textIncludes("ledger-body", "2026/10/28");
    assert.equal(await field("ledger-filter", "group_id").inputValue(), "10002000");
    await page.goBack();
    await page.locator("#group-ranking a").first().waitFor();
    assert.equal(await page.locator("#analytics-period").inputValue(), "month");
    await page.locator("#group-ranking a").filter({hasText:"私聊"}).click();
    await textIncludes("ledger-body", "私聊 / 无群");
    assert.equal((await filters()).scope, "private");
    assert.equal(await field("ledger-filter","group_id").isDisabled(), true);
    assert.equal(await page.locator("#ledger-body tr").count(), 1);
    await page.goBack();
    await page.locator("#user-ranking a").filter({hasText:"10001"}).click();
    await textIncludes("ledger-page", "共 2 条");
    assert.equal((await filters()).user_id, "10001");
    await page.goBack();
    await page.locator("#cost-chart svg").waitFor();
    await page.locator("#chart-hour").fill("0");
    assert.match(await page.locator("#chart-detail").innerText(), /2026\/10\/01/);
    await page.locator("#hour-ledger").click();
    await textIncludes("ledger-body","暂无记录");
    assert.equal((await filters()).until, "2026-10-01T01:00:00+08:00");

    await open("ledger");
    await textIncludes("ledger-page", "共 5 条");
    await page.locator("#ledger-next").click(); await textIncludes("ledger-page", "第 2 页");
    await page.goBack(); await textIncludes("ledger-page", "第 1 页");
    await open("overview"); await page.locator("#summary a").filter({hasText:"用量待核对"}).click();
    await page.getByRole("link",{name:"核对用量",exact:true}).click();
    await page.locator("#reconcile:visible").waitFor();
    assert.equal(await field("resolve-form","request_id").inputValue(), "fixture-unknown-request");
    await field("resolve-form","mode").selectOption("cancel");
    assert.equal(await page.locator("#usage-label").isVisible(), false);
    await field("resolve-form","reason").fill("演示核对");
    await submit("resolve-form").click(); await textIncludes("message", "核对结果已记录");
    await page.locator("#reconcile-back").click(); await textIncludes("ledger-body", "暂无记录");
    assert.equal((await filters()).status, "unknown");

    await open("settings"); await textIncludes("revision", "配置版本 1");
    assert.match(await page.locator("#mapping").innerText(), /19 \/ 20 \/ 21/);
    assert.match(await page.locator("#rates-table").innerText(), /deepseek-v4-pro/);
    assert.match(await page.locator("#rates-table").innerText(), /4\.24/);
    assert.match(await page.locator("#catalog-source").innerText(), /2026-11-24/);
    assert.equal(await page.locator("#plugins-list img, #plugins-list input").count(), 0);
    const blockUnpriced = page.getByRole("switch", {name:"未配置价格时拦截"});
    assert.equal(await blockUnpriced.isChecked(), true);
    await blockUnpriced.uncheck();
    await field("settings-form","points_per_yuan").fill("40");
    await textIncludes("rates-table","8.48");
    await open("overview"); await open("settings");
    assert.equal(await field("settings-form","points_per_yuan").inputValue(), "40");
    await page.getByText("编辑模型价目表",{exact:true}).click();
    const prices = field("settings-form","model_prices"), custom = JSON.parse(await prices.inputValue());
    custom.relay = structuredClone(custom["deepseek-flash"]); custom["deepseek-flash"].peak.output = 99;
    await prices.fill(JSON.stringify(custom)); await page.locator("#apply-catalog").click();
    const applied = JSON.parse(await prices.inputValue());
    assert.equal(applied["deepseek-flash"].peak.output, 8); assert.deepEqual(applied.relay, custom.relay);
    await page.getByRole("button",{name:"保存计费设置"}).click(); await textIncludes("revision", "配置版本 2");
    await open("overview"); await open("settings"); await textIncludes("revision", "配置版本 2");
    assert.equal(await blockUnpriced.isChecked(), false);
    await blockUnpriced.check();
    await field("settings-form","global_mode").selectOption("true"); await field("settings-form","global_monthly_yuan").fill("25");
    await page.getByRole("button",{name:"保存计费设置"}).click(); await textIncludes("revision", "配置版本 3");
    assert.match(await page.locator("#mapping").innerText(), /不扣个人积分/);
    assert.match(await page.locator("#rates-table").innerText(), /deepseek-v4-pro/);
    await field("settings-form","global_monthly_yuan").fill("99"); await page.locator("#discard-settings").click();
    await page.waitForFunction(() => document.querySelector('[name="global_monthly_yuan"]').value === "25");
    await open("overview"); await textIncludes("mode-status", "全局积分模式"); await textIncludes("summary","每月上限 ¥25");
    if (process.env.CREDITS_SCREENSHOT) await page.screenshot({path: process.env.CREDITS_SCREENSHOT, fullPage:true});

    await open("users"); await textIncludes("users-body","测试用户");
    await page.locator("#users-body a").filter({hasText:"测试用户"}).click(); await textIncludes("ledger-page","共 4 条");
    assert.deepEqual(await filters(), {platform:"qq",user_id:"10001"});
    await page.locator('#ledger-body a[title="查看用户余额"]').first().click();
    await textIncludes("users-page", "共 1 条");
    assert.match(await page.locator("#user-account").innerText(), /qq · 10001/);
    assert.equal(await page.locator("#users-body tr").count(), 1);
    await open("users"); await page.getByText("管理员调整积分",{exact:true}).click();
    await field("adjust-form","platform").fill("qq"); await field("adjust-form","user_id").fill("10001");
    await field("adjust-form","delta").fill("5"); await field("adjust-form","reason").fill("界面测试");
    await submit("adjust-form").click(); await textIncludes("users-body", "24.88");
    assert.equal(await page.locator("#users-body script, #ledger-body img, #group-ranking img, #groups-body img").count(), 0);
    await page.setViewportSize({width:390,height:844});
    await page.evaluate(() => document.documentElement.dataset.theme = "dark");
    for (const view of ["settings","ledger","overview","analytics"]) {
      await open(view); await page.waitForFunction(() => !document.querySelector("section:where(:not([hidden]))").hasAttribute("aria-busy"));
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `overflow in ${view}`);
    }
    if (process.env.CREDITS_MOBILE_SCREENSHOT) await page.screenshot({path: process.env.CREDITS_MOBILE_SCREENSHOT, fullPage:true});
    assert.deepEqual(errors, []);
    console.log("Credits UI passed: overview, rankings, scoped ledger, hourly windows, back/reload, pagination, drafts, rate cards, billing modes, adjustment, reconciliation and mobile dark layout.");
  } finally { await browser.close(); }
}
checkPage().catch((error) => { console.error(error); process.exitCode = 1; });
