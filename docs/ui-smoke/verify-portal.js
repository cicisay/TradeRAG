/* 验证登录入口隔离：用户 tab 输入 admin → 自动切到管理员 tab 并给出提示 */
const { chromium } = require("playwright-core");
const BASE = "http://127.0.0.1:8000";
const EDGE = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
(async () => {
  const browser = await chromium.launch({ executablePath: EDGE, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  let answer = null;
  await page.route("**/api/v1/auth/captcha", async (route) => {
    const resp = await route.fetch();
    const body = await resp.json();
    answer = body.debug_answer;
    await route.fulfill({ response: resp, body: JSON.stringify(body) });
  });
  await page.goto(BASE + "/app/", { waitUntil: "networkidle" });
  await page.waitForSelector(".login-page");
  await page.waitForTimeout(600);
  // 停在「用户登录」tab，用管理员账号登录
  await page.fill("#login-form input[name=username]", "admin");
  await page.fill("#login-form input[name=password]", "admin123");
  await page.fill("#login-form input[name=captcha_text]", answer);
  await page.click(".btn-login");
  await page.waitForTimeout(1500);
  const activeTab = await page.$eval("#role-tabs button.on", (el) => el.dataset.role);
  const errText = await page.$eval("#login-error", (el) => el.textContent.trim());
  const stillGate = await page.$(".login-page");
  console.log("管理员在用户入口提交 → 仍在登录页:", !!stillGate);
  console.log("自动切换到的 tab:", activeTab);
  console.log("提示文案:", errText);
  await page.screenshot({ path: "../ui-preview/fix-portal.png" });
  await browser.close();
})();
