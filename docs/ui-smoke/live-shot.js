/* 真实页面截图（换色验收用） */
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
  for (const [path, out, h] of [["/", "../ui-preview/live-chat.png", 900], ["/admin.html", "../ui-preview/live-admin.png", 900]]) {
    const page = await browser.newPage({ viewport: { width: 1440, height: h } });
    await page.addInitScript((t) => { localStorage.setItem("tradrag.token", t); localStorage.setItem("tradrag.user", JSON.stringify({ username: "admin", role: "admin" })); }, token);
    await page.goto(BASE + "/app/" + path, { waitUntil: "networkidle" });
    await page.waitForTimeout(1200);
    await page.screenshot({ path: out });
    console.log("ok", out);
    await page.close();
  }
  await browser.close();
})();
