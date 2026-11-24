/* 前端 UI 冒烟：登录页（角色切换+验证码图）→ 注入 token → 下拉组件 → 会话删除 → 模型配置页。 */
const { chromium } = require("playwright-core");

const BASE = "http://127.0.0.1:8000";
const EDGE = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";

async function loginToken() {
  const cap = await fetch(`${BASE}/api/v1/auth/captcha`).then((r) => r.json());
  const resp = await fetch(`${BASE}/api/v1/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
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
  page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
  page.on("pageerror", (e) => errors.push(String(e)));

  // ---- 登录页（未注入 token）----
  await page.goto(`${BASE}/app/`, { waitUntil: "networkidle" });
  await page.waitForSelector(".login-page", { timeout: 8000 });
  const hasTabs = await page.$$("#role-tabs button[data-role]");
  const captchaSrc = await page.$eval("#captcha-img", (el) => el.src.slice(0, 22));
  console.log("登录页: 角色切换按钮 =", hasTabs.length, "| 验证码图 =", captchaSrc.startsWith("data:image/png") ? "PNG OK" : "FAIL");
  // 切到管理员 tab，检查预填用户名变化
  await page.click('#role-tabs button[data-role="admin"]');
  const presetUser = await page.$eval("#login-form input[name=username]", (el) => el.value);
  console.log("管理员 tab 预填 =", presetUser);
  await page.screenshot({ path: "shot-login.png" });

  // ---- 注入 token 跳过登录 ----
  await page.evaluate((t) => {
    localStorage.setItem("tradrag.token", t);
    localStorage.setItem("tradrag.user", JSON.stringify({ id: 1, username: "admin", role: "admin", role_name: "系统管理员", biz_group: "default", permissions: ["admin:users", "kb:manage", "audit:read", "debug:retrieval", "sql:manage", "compliance:manage"] }));
  }, token);
  await page.reload({ waitUntil: "networkidle" });

  // ---- 下拉组件自动增强 ----
  await page.goto(`${BASE}/app/knowledge.html`, { waitUntil: "networkidle" });
  await page.waitForTimeout(1200);
  const ddCount = await page.$$eval(".dd-trigger", (els) => els.length);
  const nativeVisible = await page.$$eval("select:not(.dd-hidden)", (els) => els.filter((e) => e.offsetParent !== null).length);
  console.log("知识库页: 自绘下拉 =", ddCount, "个 | 未被接管的可见原生 select =", nativeVisible);
  // 打开一个下拉看弹层位置
  const trig = await page.$(".dd-trigger");
  if (trig) {
    const box = await trig.boundingBox();
    await trig.click();
    await page.waitForTimeout(300);
    const menu = await page.$(".dd-menu:not([hidden])");
    const mbox = menu ? await menu.boundingBox() : null;
    const anchored = mbox && box && Math.abs(mbox.x - box.x) < 60 && mbox.y > box.y;
    console.log("下拉弹层锚定在触发器下方 =", anchored ? "OK" : `FAIL menu=${JSON.stringify(mbox)} trigger=${JSON.stringify(box)}`);
    await page.screenshot({ path: "shot-dropdown.png" });
  } else {
    console.log("下拉弹层: 页面没有可测的下拉（可能都在表单内）");
  }

  // ---- 会话列表删除按钮 ----
  await page.goto(`${BASE}/app/`, { waitUntil: "networkidle" });
  await page.waitForSelector(".session-item", { timeout: 10000 });
  const delBtns = await page.$$("[data-sdel]");
  console.log("会话删除按钮 =", delBtns.length, "个");
  await page.screenshot({ path: "shot-chat.png" });

  // ---- 管理后台模型配置页 ----
  await page.goto(`${BASE}/app/admin.html`, { waitUntil: "networkidle" });
  await page.waitForTimeout(800);
  const llmTab = await page.$('[data-tab="llm"]');
  if (llmTab) {
    await llmTab.click();
    await page.waitForTimeout(800);
    const hasAdd = await page.$("#llm-add");
    console.log("模型配置 tab:", hasAdd ? "渲染 OK" : "FAIL");
    // 打开新增模态验证厂商下拉被组件接管
    await page.click("#llm-add");
    await page.waitForTimeout(500);
    const modalDd = await page.$$eval(".modal .dd-trigger", (els) => els.length);
    console.log("模态内厂商下拉（自绘）=", modalDd, "个");
    await page.screenshot({ path: "shot-llm-admin.png" });
  } else {
    console.log("模型配置 tab: 未找到（权限或渲染问题）");
  }

  console.log("控制台错误 =", errors.length ? errors.slice(0, 3) : "0");
  await browser.close();
})().catch((e) => { console.error("UI 测试失败:", e); process.exit(1); });
