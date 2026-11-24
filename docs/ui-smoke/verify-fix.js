/* 验证三处修复：退出按钮 / 登录表单无图标+验证码完整 / 输入框无内框 */
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
  // 1) 聊天页侧栏底部（退出按钮）
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.addInitScript((t) => {
    localStorage.setItem("tradrag.token", t);
    localStorage.setItem("tradrag.user", JSON.stringify({ username: "admin", role: "admin", role_name: "系统管理员", biz_group: "default", permissions: ["admin:users", "kb:manage", "audit:read", "debug:retrieval"] }));
  }, token);
  await page.goto(BASE + "/app/", { waitUntil: "networkidle" });
  await page.waitForTimeout(800);
  // 3) 输入框聚焦，验证无内框
  await page.focus("#input");
  await page.type("#input", "测试输入");
  const foot = await page.$("#side-foot");
  if (foot) await foot.screenshot({ path: "../ui-preview/fix-foot.png" });
  const composer = await page.$(".composer-wrap");
  if (composer) await composer.screenshot({ path: "../ui-preview/fix-composer.png" });
  await page.close();
  // 2) 登录页（无图标 + 验证码完整）
  const p2 = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await p2.goto(BASE + "/app/", { waitUntil: "networkidle" });
  await p2.waitForSelector(".login-page", { timeout: 8000 });
  await p2.waitForTimeout(1200);
  await p2.screenshot({ path: "../ui-preview/fix-login.png" });
  console.log("ok");
  await browser.close();
})();
