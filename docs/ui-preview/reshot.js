/* 设计稿重截图：file:// 打开三个 preview 页面 */
const { chromium } = require("../ui-smoke/node_modules/playwright-core");
const EDGE = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
(async () => {
  const browser = await chromium.launch({ executablePath: EDGE, headless: true });
  const jobs = [
    ["login.html", "shot-login.png", 1080, 660],
    ["chat.html", "shot-chat.png", 1080, 666],
    ["admin.html", "shot-admin.png", 1080, 843],
  ];
  for (const [src, out, w, h] of jobs) {
    const page = await browser.newPage({ viewport: { width: w, height: h } });
    await page.goto("file:///" + process.cwd().replace(/\\/g, "/") + "/" + src, { waitUntil: "networkidle" });
    await page.waitForTimeout(400);
    await page.screenshot({ path: out, fullPage: src !== "chat.html" });
    console.log("ok", out);
    await page.close();
  }
  await browser.close();
})();
