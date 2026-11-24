/* 打开最近一个会话，验证消息流与引用来源卡的新样式 */
const { chromium } = require("playwright-core");
const BASE = "http://127.0.0.1:8000";
const EDGE = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
async function loginToken() {
  const cap = await fetch(`${BASE}/api/v1/auth/captcha`).then((r) => r.json());
  const resp = await fetch(`${BASE}/api/v1/auth/login`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: "admin", password: "admin123", captcha_id: cap.captcha_id, captcha_text: cap.debug_answer }),
  });
  if (!resp.ok) throw new Error(`login ${resp.status} ${await resp.text()}`);
  return (await resp.json()).access_token;
}
(async () => {
  const token = await loginToken();
  const browser = await chromium.launch({ executablePath: EDGE, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.addInitScript((t) => {
    localStorage.setItem("tradrag.token", t);
    localStorage.setItem("tradrag.user", JSON.stringify({ username: "admin", role: "admin", role_name: "系统管理员", biz_group: "default", permissions: ["admin:users", "kb:manage", "audit:read", "debug:retrieval", "sql:manage", "compliance:manage"] }));
  }, token);
  await page.goto(BASE + "/app/", { waitUntil: "networkidle" });
  await page.waitForSelector(".session-item", { timeout: 10000 });
  await page.click(".session-item");
  await page.waitForTimeout(1500);
  // 展开第一条引用卡（若存在）
  const more = await page.$("[data-ref-more]");
  if (more) await more.click();
  await page.waitForTimeout(500);
  await page.screenshot({ path: "../ui-preview/live-session.png" });
  console.log("ok live-session.png | pageerror =", errors.length);
  await browser.close();
})();
