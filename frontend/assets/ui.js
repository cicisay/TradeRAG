/* 共享界面骨架：登录门、顶栏、权限判断。 */
import { api, clearSession, escapeHtml, getToken, getUser, setSession, toast } from "./api.js";

const ROLE_NAMES = {
  admin: "系统管理员",
  auditor: "安全审计员",
  compliance: "合规管理员",
  knowledge: "知识管理员",
  user: "普通用户",
};

let currentUser = null;

export function user() {
  return currentUser || getUser();
}

export function can(permission) {
  const u = user();
  return Boolean(u?.permissions?.includes(permission));
}

export function roleName(code) {
  return ROLE_NAMES[code] || code || "—";
}

/** 侧栏品牌区 + 导航 + 底部用户卡。三个页面共用同一个外壳。 */
function renderShell(active) {
  const u = user();
  const showAdmin = can("audit:read") || can("debug:retrieval") || can("kb:manage") || can("compliance:manage");

  const head = document.getElementById("side-head");
  if (head) {
    head.innerHTML = `
      <div class="brand-mark">TR</div>
      <div>
        <div class="brand-name">TradeRAG</div>
        <div class="brand-sub">${active === "admin" ? "管理后台" : "外贸知识中枢"}</div>
      </div>`;
  }

  const nav = document.getElementById("side-nav");
  if (nav) {
    // 方案 C「底部切换坞」：导航沉底，图标 + 文字竖排（svg-wrapper 结构按用户给定模板）。
    const icons = {
      chat: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
      knowledge:
        '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>',
      admin: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
    };
    const items = [
      { id: "chat", name: "问答", href: "index.html" },
      { id: "knowledge", name: "知识库", href: "knowledge.html" },
      ...(showAdmin ? [{ id: "admin", name: "管理后台", href: "admin.html" }] : []),
    ];
    nav.innerHTML = items
      .map(
        (it) => `<a class="nav-item ${active === it.id ? "on" : ""}" href="${it.href}" title="${it.name}">
          <span class="svg-wrapper">
            <svg class="icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="19" height="19"
                 fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              ${icons[it.id]}
            </svg>
          </span>
          <span>${it.name}</span>
        </a>`
      )
      .join("");
  }

  const foot = document.getElementById("side-foot");
  if (foot) {
    foot.innerHTML = `
      <div class="ava">${(u?.username || "?").slice(0, 1).toUpperCase()}</div>
      <div class="who">
        <b>${u?.username || ""}</b>
        <span>${roleName(u?.role)}${u?.biz_group ? ` · ${u.biz_group}` : ""}</span>
      </div>
      <button class="logout-mini" id="logout-btn" title="退出登录">退出</button>`;
    foot.querySelector("#logout-btn").addEventListener("click", () => {
      clearSession();
      location.reload();
    });
  }
}

function showLogin(message = "") {
  return new Promise((resolve) => {
    const gate = document.createElement("div");
    gate.className = "login-page";
    gate.innerHTML = `
      <section class="login-left">
        <div class="ll-brand">
          <div class="brand-mark">TR</div>
          <div>
            <b>TradeRAG</b>
            <span>FOREIGN TRADE KNOWLEDGE</span>
          </div>
        </div>

        <div class="ll-hero">
          <h1>外贸知识，<br /><em>一问即达</em></h1>
          <p>把单证、认证、报价与合规要求沉淀成可提问的知识库。每条结论都带原文引用，查得到出处，也追得回批注。</p>
          <div class="ll-points">
            <div class="ll-point">
              <div class="ic">01</div>
              <div><b>跨语言检索</b><span>中文提问，自动匹配英、西、阿语单证原文</span></div>
            </div>
            <div class="ll-point">
              <div class="ic">02</div>
              <div><b>答案必有出处</b><span>每个结论标注引用编号，可一键回看原文与页码</span></div>
            </div>
            <div class="ll-point">
              <div class="ic">03</div>
              <div><b>合规先于生成</b><span>敏感问题在检索前拦截，不给出任何规避建议</span></div>
            </div>
          </div>
        </div>

        <div class="ll-foot">TradeRAG v1.0 · 演示环境 · 数据仅在本机处理</div>
      </section>

      <section class="login-right">
        <div class="login-card">
          <h2>欢迎回来</h2>
          <div class="lead">登录后即可提问、上传单证、查看引用原文</div>

          <div class="seg" id="role-tabs">
            <button type="button" class="on" data-role="user">用户登录</button>
            <button type="button" data-role="admin">管理员登录</button>
          </div>

          <form id="login-form">
            <label class="field">
              <span>账号</span>
              <div class="input-box">
                <input type="text" name="username" value="demo_sales" autocomplete="username" required />
              </div>
            </label>

            <label class="field">
              <span>密码</span>
              <div class="input-box">
                <input type="password" name="password" placeholder="请输入密码" autocomplete="current-password" required />
                <span class="ic pw-toggle" id="pw-toggle" role="button" tabindex="0">显示</span>
              </div>
            </label>

            <label class="field">
              <span>验证码 <span class="faint hint-inline">· 不区分大小写</span></span>
              <div class="row gap-10">
                <div class="input-box" style="flex:1">
                  <input type="text" name="captcha_text" placeholder="输入右侧 4 位字符" maxlength="8" autocomplete="off" required />
                </div>
                <img class="captcha-img" id="captcha-img" title="点击换一张" alt="验证码" />
              </div>
            </label>

            <div class="form-error" id="login-error">${message}</div>

            <div class="login-row">
              <label class="check"><input type="checkbox" id="remember-me" checked /><i>✓</i>记住账号</label>
              <span class="link-muted" id="forgot-link">忘记密码？</span>
            </div>

            <button class="btn-login" type="submit">登录</button>
          </form>

          <div class="divider">演示账号一览</div>
          <div class="demo-chips" id="demo-hint"></div>
        </div>
      </section>`;
    document.body.appendChild(gate);

    const form = gate.querySelector("#login-form");
    const errorEl = gate.querySelector("#login-error");
    const captchaImg = gate.querySelector("#captcha-img");
    const demoHint = gate.querySelector("#demo-hint");
    const DEMO_HINTS = {
      user: `<div class="demo-chip"><b>demo_sales</b>普通用户 · demo123456</div>
             <div class="demo-chip"><b>demo_sales_b</b>普通用户（B组） · demo123456</div>`,
      admin: `<div class="demo-chip"><b>admin</b>系统管理员 · admin123</div>
              <div class="demo-chip"><b>demo_kb</b>知识管理员 · demo123456</div>`,
    };
    const PRESET = { user: "demo_sales", admin: "admin" };
    const LAST_USER_KEY = "tradrag.lastuser";
    let currentRole = "user";  // 当前登录入口：user / admin（随 tab 切换）

    const refreshCaptcha = async () => {
      captchaImg.removeAttribute("src");
      try {
        const cap = await fetch("/api/v1/auth/captcha").then((r) => r.json());
        captchaImg.dataset.cid = cap.captcha_id;
        captchaImg.src = cap.image;
      } catch {
        captchaImg.alt = "验证码加载失败，点击重试";
      }
    };
    captchaImg.addEventListener("click", refreshCaptcha);
    captchaImg.addEventListener("error", () => refreshCaptcha());

    // 密码可见性切换：演示环境里方便核对输入
    const pwInput = form.password;
    const pwToggle = gate.querySelector("#pw-toggle");
    const togglePw = () => {
      const show = pwInput.type === "password";
      pwInput.type = show ? "text" : "password";
      pwToggle.textContent = show ? "隐藏" : "显示";
    };
    pwToggle.addEventListener("click", (ev) => { ev.preventDefault(); togglePw(); });
    pwToggle.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); togglePw(); }
    });

    gate.querySelector("#forgot-link").addEventListener("click", () => {
      toast("演示环境请在下方选用演示账号登录", "ok");
    });

    // keepUser=true 时保留已输入的账号（入口被后端纠正时用，不让用户重打一遍）
    const applyRole = (role, keepUser = false) => {
      currentRole = role;
      gate.querySelectorAll("#role-tabs button").forEach((b) => b.classList.toggle("on", b.dataset.role === role));
      demoHint.innerHTML = DEMO_HINTS[role];
      if (keepUser) return;
      const remembered = localStorage.getItem(`${LAST_USER_KEY}.${role}`);
      form.username.value = remembered || PRESET[role];
    };
    gate.querySelector("#role-tabs").addEventListener("click", (ev) => {
      const btn = ev.target.closest("button[data-role]");
      if (btn) applyRole(btn.dataset.role);
    });
    applyRole("user");
    refreshCaptcha();

    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      errorEl.textContent = "";
      const btn = form.querySelector("button[type=submit]");
      btn.disabled = true;
      btn.textContent = "登录中…";
      try {
        const data = new FormData(form);
        const resp = await api("/api/v1/auth/login", {
          method: "POST",
          body: {
            username: data.get("username"),
            password: data.get("password"),
            captcha_id: captchaImg.dataset.cid || null,
            captcha_text: (data.get("captcha_text") || "").trim() || null,
            role: currentRole,  // 后端据此校验账号与入口是否匹配
          },
        });
        if (gate.querySelector("#remember-me").checked) {
          localStorage.setItem(`${LAST_USER_KEY}.${currentRole}`, String(data.get("username") || ""));
        } else {
          localStorage.removeItem(`${LAST_USER_KEY}.${currentRole}`);
        }
        setSession(resp.access_token, null);
        const me = await api("/api/v1/auth/me");
        setSession(null, me);
        currentUser = me;
        gate.remove();
        resolve(me);
      } catch (err) {
        const detail = err.data?.detail;
        if (detail?.error_code === "E6006" && detail.portal) {
          // 入口选错：自动切到正确 tab 并保留账号，提示补一次验证码即可
          const label = detail.portal === "admin" ? "管理员登录" : "用户登录";
          applyRole(detail.portal, true);
          errorEl.textContent = `已为你切换到「${label}」入口，请输入验证码重试`;
        } else {
          errorEl.textContent = err.message;
        }
        btn.disabled = false;
        btn.textContent = "登录";
        refreshCaptcha();  // 一次一用：失败后旧码已作废，必须换新图
        form.captcha_text.value = "";
        form.captcha_text.focus();
      }
    });
  });
}

/**
 * 启动页面：校验登录、渲染顶栏。
 * @param {"chat"|"knowledge"|"admin"} active
 * @returns {Promise<object|null>} 当前用户
 */
export async function boot(active) {
  if (!getToken()) {
    await showLogin();
  }
  try {
    currentUser = await api("/api/v1/auth/me");
    setSession(null, currentUser);
  } catch (err) {
    if (err.status === 401) {
      clearSession();
      currentUser = await showLogin("登录已过期，请重新登录");
    } else {
      toast(err.message, "err");
      return null;
    }
  }
  renderShell(active);
  return currentUser;
}

window.addEventListener("auth:expired", () => {
  if (document.querySelector(".login-page, .gate")) return;
  toast("登录已过期，请重新登录", "err");
  setTimeout(() => location.reload(), 800);
});

/* ================= 自绘下拉组件 =================
 *
 * 为什么不用原生 <select> 的弹出层：Windows 显示缩放 125%/150% 时，
 * Chrome 的原生下拉弹层会按错误坐标渲染（“漂移”到页面角落），CSS 无法根治。
 * 这里把 select 隐藏但保留在表单流里（FormData 照常提交），另画一个受控弹层：
 * 弹层用 position:fixed 锚定触发器的实时坐标，不受任何 transform/overflow/缩放影响。
 * MutationObserver 自动增强所有（含动态插入的）select，业务代码零改动。
 */
const ddMenu = document.createElement("div");
ddMenu.className = "dd-menu";
ddMenu.hidden = true;
let ddOpenFor = null;   // 当前打开的下拉对应的（隐藏）select
let ddTriggerEl = null; // 可见的触发器：弹层定位的锚点（select 是 display:none，量不出坐标）

function ddOptionsOf(select) {
  return Array.from(select.options).map((o, i) => ({
    value: o.value,
    label: o.textContent,
    disabled: o.disabled,
    index: i,
  }));
}

function ddLabelOf(select) {
  const o = select.options[select.selectedIndex];
  return o ? o.textContent : "";
}

function ddClose() {
  ddMenu.hidden = true;
  ddMenu.innerHTML = "";
  ddOpenFor = null;
  ddTriggerEl = null;
}

function ddPlace(trigger) {
  /** fixed 定位锚定触发器实时坐标：优先向下展开，放不下就向上。 */
  const rect = trigger.getBoundingClientRect();
  const mh = Math.min(ddMenu.scrollHeight || 200, 280);
  const below = rect.bottom + 6 + mh <= window.innerHeight - 8;
  ddMenu.style.left = `${rect.left}px`;
  ddMenu.style.top = below ? `${rect.bottom + 4}px` : `${Math.max(8, rect.top - 4 - mh)}px`;
}

function ddOpen(select, trigger) {
  ddOpenFor = select;
  ddTriggerEl = trigger;
  ddMenu.innerHTML = ddOptionsOf(select)
    .map(
      (o) => `
      <div class="dd-item ${o.value === select.value ? "on" : ""} ${o.disabled ? "disabled" : ""}"
           data-value="${escapeHtml(o.value)}" data-index="${o.index}">${escapeHtml(o.label)}</div>`
    )
    .join("");
  ddMenu.style.minWidth = `${trigger.getBoundingClientRect().width}px`;
  ddMenu.hidden = false;
  ddPlace(trigger);
}

function ddPick(item) {
  const select = ddOpenFor;
  if (!select || item.classList.contains("disabled")) return;
  const idx = Number(item.dataset.index);
  if (select.selectedIndex !== idx) {
    select.selectedIndex = idx;
    select.dispatchEvent(new Event("change", { bubbles: true }));
  }
  if (ddTriggerEl) ddTriggerEl.querySelector(".dd-label").textContent = ddLabelOf(select);
  ddClose();
}

function ddEnhance(select) {
  if (select.dataset.ddReady || select.tagName !== "SELECT") return;
  select.dataset.ddReady = "1";
  const trigger = document.createElement("div");
  trigger.className = "dd-trigger";
  trigger.tabIndex = 0;
  trigger.setAttribute("role", "combobox");
  trigger.innerHTML = `<span class="dd-label">${escapeHtml(ddLabelOf(select))}</span><span class="dd-arrow"></span>`;
  const cs = getComputedStyle(select);
  trigger.style.width = cs.width;
  trigger.style.height = `${cs.height === "auto" ? select.offsetHeight : select.offsetHeight}px`;
  select.classList.add("dd-hidden");
  select.before(trigger);
  trigger.addEventListener("click", (ev) => {
    // preventDefault 必须有：trigger 常被插在 <label class="field"> 内部（原 select 的位置），
    // label 的默认行为会把这次点击"转发"给隐藏的 select，合成第二个 click 冒泡到
    // document 的关闭监听 → 菜单刚打开就被关掉（实测踩过）。
    ev.preventDefault();
    ev.stopPropagation();
    if (ddOpenFor === select) ddClose();
    else ddOpen(select, trigger);
  });
  trigger.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" || ev.key === " ") {
      ev.preventDefault();
      ddOpenFor === select ? ddClose() : ddOpen(select, trigger);
    } else if (ev.key === "Escape") ddClose();
  });
}

// 同步外部代码对 select.value/options 的改动（如脚本 setSelected）到触发器文案
const ddObserver = new MutationObserver((mutations) => {
  for (const m of mutations) {
    if (m.type === "childList") {
      m.addedNodes.forEach((n) => {
        if (n.nodeType !== 1) return;
        n.querySelectorAll?.("select").forEach(ddEnhance);
        if (n.tagName === "SELECT") ddEnhance(n);
      });
    } else if (m.type === "attributes" && m.target === ddOpenFor && m.attributeName === "style") {
      // 忽略
    }
  }
});

function initDropdowns() {
  ddObserver.observe(document.body, { childList: true, subtree: true });
  document.querySelectorAll("select").forEach(ddEnhance);
  ddMenu.addEventListener("click", (ev) => {
    const item = ev.target.closest(".dd-item");
    if (item) ddPick(item);
  });
  document.addEventListener("click", (ev) => {
    // dd-hidden（隐藏 select 自身）也要忽略：label 转发的合成 click 的 target 就是它
    if (
      !ddMenu.hidden && !ddMenu.contains(ev.target)
      && !ev.target.closest?.(".dd-trigger") && !ev.target.classList?.contains("dd-hidden")
    ) ddClose();
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && !ddMenu.hidden) ddClose();
  });
  // 滚动/缩放时跟随触发器重新定位（而不是关掉）：用户在长表单里滚动时下拉不消失
  window.addEventListener("scroll", () => {
    if (!ddMenu.hidden && ddTriggerEl) ddPlace(ddTriggerEl);
  }, true);
  window.addEventListener("resize", () => {
    if (!ddMenu.hidden && ddTriggerEl) ddPlace(ddTriggerEl);
    else if (!ddMenu.hidden) ddClose();
  });
  document.body.appendChild(ddMenu);
}
// ui.js 被每个页面 import，模块加载即接管所有 select
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initDropdowns);
} else {
  initDropdowns();
}
