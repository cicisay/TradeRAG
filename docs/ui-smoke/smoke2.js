const { chromium } = require("playwright-core");
const BASE = "http://127.0.0.1:8000";
const EDGE = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
(async () => {
  const cap = await fetch(`${BASE}/api/v1/auth/captcha`).then((r) => r.json());
  const token = (await fetch(`${BASE}/api/v1/auth/login`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: "admin", password: "admin123", captcha_id: cap.captcha_id, captcha_text: cap.debug_answer }) }).then(r => r.json())).access_token;
  const browser = await chromium.launch({ executablePath: EDGE, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));

  // 先注入登录态再访问页面（否则会弹登录门，找不到 tab）
  await page.goto(`${BASE}/app/`, { waitUntil: "domcontentloaded" });
  await page.evaluate((t) => {
    localStorage.setItem("tradrag.token", t);
    localStorage.setItem("tradrag.user", JSON.stringify({ id: 1, username: "admin", role: "admin", role_name: "系统管理员", biz_group: "default", permissions: ["admin:users", "kb:manage", "audit:read", "debug:retrieval", "sql:manage", "compliance:manage"] }));
  }, token);
  await page.goto(`${BASE}/app/admin.html`, { waitUntil: "networkidle" });
  await page.waitForTimeout(800);
  await page.click('[data-tab="llm"]');
  await page.click("#llm-add");
  await page.waitForTimeout(400);
  // 自绘下拉：点开厂商 → 选 DeepSeek → base_url 应联动
  await page.click(".modal .dd-trigger");
  await page.waitForTimeout(300);
  await page.click('.dd-menu .dd-item[data-value="deepseek"]');
  await page.waitForTimeout(300);
  const url = await page.$eval("#llm-base-url", (el) => el.value);
  const model = await page.$eval("#llm-model", (el) => el.value);
  const label = await page.$eval(".modal .dd-trigger .dd-label", (el) => el.textContent);
  console.log("厂商联动:", label, "->", url, "|", model);

  // 聊天页：发一问 → 引用卡片应带"展开全部"
  await page.goto(`${BASE}/app/`, { waitUntil: "networkidle" });
  await page.fill("#input", "不锈钢法兰的材质和压力等级有什么要求？详细说明");
  await page.click("#send");
  await page.waitForSelector(".ref-card", { timeout: 60000 });
  await page.waitForTimeout(800);
  const hasMore = await page.$$eval(".ref-more", (els) => els.length);
  const collapsed = await page.$$eval(".ref-card:not(.ref-open)", (els) => els.length);
  console.log("引用卡片收起态 =", collapsed, "| 展开按钮 =", hasMore);
  // 点第一个展开
  const btn = await page.$(".ref-more");
  if (btn) {
    await btn.click();
    await page.waitForTimeout(400);
    const opened = await page.$$eval(".ref-card.ref-open", (els) => els.length);
    const btnText = await page.$eval(".ref-card.ref-open .ref-more", (el) => el.textContent.trim());
    console.log("点击展开后 ref-open =", opened, "| 按钮文案 =", JSON.stringify(btnText));
  }
  await page.screenshot({ path: "shot-refs.png" });
  console.log("控制台错误 =", errors.length ? errors.slice(0, 3) : "0");
  await browser.close();
})().catch((e) => { console.error("FAIL:", e); process.exit(1); });
