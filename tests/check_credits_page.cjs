// Run preview_credits.py first. Set NODE_PATH to a Playwright installation.
const assert = require("node:assert/strict");
const { chromium } = require("playwright");

(async () => {
  const browser = await chromium.launch({ headless: true, channel: "msedge" });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("dialog", async (dialog) => { errors.push(`unexpected dialog: ${dialog.message()}`); await dialog.dismiss(); });
    await page.goto("http://127.0.0.1:8943/preview");
    await page.locator('#settings-form input[name="points_per_yuan"]').waitFor();
    await page.waitForFunction(() => document.querySelector("#mapping").textContent.includes("2.12"));
    assert.match(await page.locator("#mapping").innerText(), /19 \/ 20 \/ 21/);
    assert.match(await page.locator("#mapping").innerText(), /4\.24/);
    assert.equal(await page.locator("#plugins-list img").count(), 0);
    assert.equal(await page.locator("#users-body script").count(), 0);
    assert.equal(await page.locator('#plugins-list input').count(), 0);
    assert.match(await page.locator('#plugins-list').innerText(), /六爻[\s\S]*独立调用放行/);
    assert.match(await page.locator('#overview').innerText(), /不扣积分、不占额度、不新增流水/);
    await page.locator('#settings-form input[name="points_per_yuan"]').fill("40");
    assert.match(await page.locator("#mapping").innerText(), /8\.48/);
    await page.locator('#settings-form button[type="submit"]').click();
    await page.waitForFunction(() => document.querySelector("#revision").textContent.includes("2"));
    assert.equal(await page.locator('#plugins-list input').count(), 0);
    assert.deepEqual((await page.evaluate(() => window.AstrBotPluginPage.apiGet('credits/settings'))).values.charged_plugins, []);
    await page.locator('#users details summary').click();
    await page.locator('#adjust-form input[name="platform"]').fill("qq");
    await page.locator('#adjust-form input[name="user_id"]').fill("10001");
    await page.locator('#adjust-form input[name="delta"]').fill("5");
    await page.locator('#adjust-form input[name="reason"]').fill("界面测试");
    await page.locator('#adjust-form button').click();
    await page.waitForFunction(() => document.querySelector("#users-body").textContent.includes("24.88"));
    await page.locator('#resolve-form input[name="request_id"]').fill("fixture-unknown-request");
    await page.locator('#resolve-form input[name="reason"]').fill("演示核对");
    await page.locator('#resolve-form select[name="mode"]').selectOption("cancel");
    assert.equal(await page.locator("#usage-label").isVisible(), false);
    await page.locator('#resolve-form button').click();
    await page.waitForFunction(() => document.querySelector("#message").textContent.includes("核对结果已记录"));
    await page.setViewportSize({ width: 390, height: 844 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= 390));
    await page.evaluate(() => document.documentElement.dataset.theme = "dark");
    assert.equal(errors.length, 0, errors.join("\n"));
    console.log("Credits UI: mapping, plugin passthrough, adjustment, reconciliation, XSS text, mobile layout passed.");
  } finally {
    await browser.close();
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
