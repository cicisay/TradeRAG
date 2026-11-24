import { api, escapeHtml, fmtMs, fmtTime, streamChat, toast } from "./api.js";
import { boot } from "./ui.js?v=20261110b";

const REASONS = [
  ["wrong_answer", "答案错误"],
  ["incomplete", "答案不完整"],
  ["off_topic", "答非所问"],
  ["bad_citation", "引用无关/译文有误"],
  ["too_slow", "响应太慢"],
  ["other", "其他"],
];
const INTENT_NAMES = { greeting: "问候", kb_question: "知识库提问", data_query: "数据查询" };

const state = { sessionId: null, streaming: false, controller: null, sessions: [], search: "" };

const EMPTY_HTML = `
  <div class="empty-state" id="empty-state">
    <div class="es-mark">TR</div>
    <div class="big">外贸知识，一问即达</div>
    <div class="sub">单证、认证、报价、合规都可以问；回答会附上引用原文</div>
  </div>`;

const $ = (sel) => document.querySelector(sel);
const messagesEl = $("#messages");
const inputEl = $("#input");
const sendBtn = $("#send");

function scrollBottom() {
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function hideEmpty() {
  const empty = $("#empty-state");
  if (empty) empty.classList.add("hidden");
}

function renderRefs(refs) {
  if (!refs || !refs.length) return "";
  const cards = refs.map((r, i) => {
    const idx = r.index ?? i + 1;
    const hasTrans = Boolean(r.trans_snapshot);
    const page = r.page_or_section ? `<span class="chip">${escapeHtml(r.page_or_section)}</span>` : "";
    const lang = r.language ? `<span class="chip">${escapeHtml(r.language)}</span>` : "";
    const score = r.score !== null && r.score !== undefined ? `<span class="faint small">score ${Number(r.score).toFixed(3)}</span>` : "";
    return `
      <div class="ref-card">
        <div class="ref-head">
          <span class="idx">${idx}</span>
          <span class="doc" title="${escapeHtml(r.doc_name)}">${escapeHtml(r.doc_name || "未知文档")}</span>
          ${page}${lang}
          ${hasTrans ? `<span class="ref-tabs"><button class="on" data-ref-toggle="orig">原文</button><button data-ref-toggle="trans">译文</button></span>` : ""}
        </div>
        <div class="ref-body" data-orig="${escapeHtml(r.content_snapshot || "")}" ${hasTrans ? `data-trans="${escapeHtml(r.trans_snapshot)}"` : ""}>${escapeHtml(r.content_snapshot || "")}</div>
        <div class="row" style="margin-top:6px">${score}${hasTrans ? `<span class="chip warn">机器翻译</span>` : ""}</div>
      </div>`;
  });
  return `
    <div class="refs">
      <div class="refs-title">引用原文 · ${refs.length} 条</div>
      ${cards.join("")}
    </div>`;
}

/** 引用卡片收起/展开：内容超过折叠高度才显示「展开全部」，否则保持原样。 */
function enhanceRefCards(scope) {
  (scope || messagesEl).querySelectorAll(".ref-card").forEach((card) => {
    const body = card.querySelector(".ref-body");
    if (!body) return;
    const over = body.scrollHeight > body.clientHeight + 2;
    card.classList.toggle("has-more", over);
    let btn = card.querySelector(".ref-more");
    if (!over) {
      btn?.remove();
      return;
    }
    if (!btn) {
      btn = document.createElement("button");
      btn.className = "ref-more";
      btn.dataset.refMore = "1";
      body.after(btn);
    }
    btn.innerHTML = card.classList.contains("ref-open") ? `<span class="arr"></span> 收起` : `<span class="arr"></span> 展开全部`;
  });
}

/* 反馈按钮：Uiverse like-unlike-radio 风格（图标 + 文字竖排，悬停放大，选中弹跳动画） */
const FB_ICONS = {
  up: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M7 10v12"/><path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88Z"/></svg>`,
  down: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M17 14V2"/><path d="M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.76a2 2 0 0 0-1.79 1.11L12 22a3.13 3.13 0 0 1-3-3.88Z"/></svg>`,
};

function feedbackButtons(messageId, rating) {
  if (!messageId) return "";
  const label = (kind, name, tip) => `
    <span class="feedback-label ${rating === kind ? "on" : ""}" data-feedback="${kind}" data-mid="${messageId}"
          role="button" tabindex="0" title="${tip}" aria-pressed="${rating === kind}">
      <span class="icon">${FB_ICONS[kind]}</span><span class="fb-text">${name}</span>
    </span>`;
  return `
    <div class="fb-rate" data-mid="${messageId}">
      ${label("up", "有帮助", "回答有帮助")}
      ${label("down", "有问题", "回答有问题")}
    </div>`;
}

function metaChips(meta = {}) {
  const chips = [];
  if (meta.intent) chips.push(`<span class="chip">${INTENT_NAMES[meta.intent] || meta.intent}</span>`);
  if (meta.took_ms !== undefined) chips.push(`<span class="chip">检索 ${fmtMs(meta.took_ms)}</span>`);
  if (meta.refs?.length) chips.push(`<span class="chip brand">引用 ${meta.refs.length}</span>`);
  if (meta.degraded) chips.push(`<span class="chip warn">降级</span>`);
  if (meta.sql_degraded) chips.push(`<span class="chip warn">SQL 降级</span>`);
  return chips.join("");
}

function renderSql(sql) {
  if (!sql) return "";
  const rows = sql.rows || [];
  const cols = rows.length ? Object.keys(rows[0]) : [];
  const head = cols.map((c) => `<th>${escapeHtml(c)}</th>`).join("");
  const body = rows.slice(0, 8).map((r) => `<tr>${cols.map((c) => `<td>${escapeHtml(r[c] ?? "")}</td>`).join("")}</tr>`).join("");
  return `
    <div class="card" style="margin-top:10px;box-shadow:none">
      <div class="card-head" style="padding:9px 12px">
        <h3 style="font-size:12px">${escapeHtml(sql.template_id || "")} · ${escapeHtml(sql.template_name || "")}</h3>
        <span class="chip">${sql.row_count ?? rows.length} 行</span>
      </div>
      ${rows.length ? `<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>` : `<div class="card-body small muted">查询成功，但当前数据源无记录。</div>`}
    </div>`;
}

function appendUser(text) {
  hideEmpty();
  const el = document.createElement("div");
  el.className = "msg user";
  el.innerHTML = `<div class="avatar">我</div><div class="bubble">${escapeHtml(text)}</div>`;
  messagesEl.appendChild(el);
  scrollBottom();
}

function appendAssistant() {
  hideEmpty();
  const el = document.createElement("div");
  el.className = "msg assistant";
  el.innerHTML = `
    <div class="avatar" style="background:var(--brand)">AI</div>
    <div class="bubble">
      <div class="msg-name">知识库助手
        <span class="chip brand" data-role="state"><span class="dots"><i></i><i></i><i></i></span><span data-role="stage">正在连接…</span></span>
        <span data-role="chips" class="row" style="gap:4px"></span>
      </div>
      <div class="answer-text" data-role="text"></div>
      <div data-role="compliance"></div>
      <div data-role="sql"></div>
      <div data-role="refs"></div>
      <div class="msg-foot" data-role="foot"></div>
    </div>`;
  messagesEl.appendChild(el);
  scrollBottom();
  return {
    el,
    text: el.querySelector('[data-role="text"]'),
    state: el.querySelector('[data-role="state"]'),
    chips: el.querySelector('[data-role="chips"]'),
    compliance: el.querySelector('[data-role="compliance"]'),
    sql: el.querySelector('[data-role="sql"]'),
    refs: el.querySelector('[data-role="refs"]'),
    foot: el.querySelector('[data-role="foot"]'),
  };
}

function setStreaming(on) {
  state.streaming = on;
  // 圆形发送键：生成中变成"停止"方块，避免文字把圆按钮撑破
  sendBtn.innerHTML = on ? `<span class="stop-ic"></span>` : "↑";
  sendBtn.title = on ? "停止生成" : "发送（Enter）";
  sendBtn.classList.toggle("danger", on);
  sendBtn.disabled = false;
}

function send() {
  if (state.streaming) {
    state.controller?.abort();
    return;
  }
  const query = inputEl.value.trim();
  if (!query) return;
  inputEl.value = "";
  inputEl.focus();
  appendUser(query);
  const view = appendAssistant();
  state.controller = new AbortController();
  setStreaming(true);
  let answerText = "";
  let messageId = null;
  let meta = null;

  const finish = () => {
    setStreaming(false);
    state.controller = null;
    scrollBottom();
  };

  streamChat(query, state.sessionId, (event, data) => {
    if (event === "stage") {
      // 后端在检索/预填充这些"没有字可吐"的阶段补一帧提示，
      // 否则界面会停在静态的"生成中"两三秒，看起来像卡死。
      if (view.state.classList.contains("danger")) return;
      view.state.className = "chip brand";
      view.state.innerHTML = `<span class="dots"><i></i><i></i><i></i></span><span>${escapeHtml(data.message || "处理中…")}</span>`;
      if (data.session_id) decideSession(data.session_id, query);
    } else if (event === "meta") {
      meta = data;
      if (data.session_id) decideSession(data.session_id, query);
      view.chips.innerHTML = metaChips(data);
      if (data.refs?.length) {
        view.refs.innerHTML = renderRefs(data.refs);
        enhanceRefCards(view.refs);
      }
      if (data.blocked) {
        view.state.className = "chip danger";
        view.state.textContent = "合规拦截";
      }
      if (data.sql) view.sql.innerHTML = renderSql(data.sql);
    } else if (event === "delta") {
      answerText += data.content || "";
      view.text.textContent = answerText;
      if (!view.state.classList.contains("danger")) {
        view.state.className = "chip brand";
        view.state.textContent = "输出中";
      }
      scrollBottom();
    } else if (event === "override") {
      answerText = data.content || answerText;
      view.text.textContent = answerText;
      view.state.className = "chip danger";
      view.state.textContent = "已拦截改写";
    } else if (event === "done") {
      messageId = data.message_id;
      view.state.className = "chip ok";
      view.state.textContent = data.compliance_action === "block" ? "已拦截" : "完成";
      if (meta?.compliance?.action === "annotate" && meta.compliance.notice && !answerText.includes("【合规提示】")) {
        view.compliance.innerHTML = `<div class="compliance-block"><div class="h">合规提示（不可关闭）</div>${escapeHtml(meta.compliance.notice)}</div>`;
      }
      const foot = [];
      if (data.request_id) foot.push(`<span class="mono" title="点击复制 request_id" data-copy="${data.request_id}" style="cursor:pointer">req ${data.request_id}</span>`);
      if (data.refs) foot.push(`<span>${data.refs} 条引用</span>`);
      // 三种来源分开标注：结构化数据查询 / 大模型生成 / 确实没配模型。
      // 只有后端明确回报 none 才提示未接模型——字段缺失不能当作未配置（SQL 通道曾因此被误判）。
      if (data.sql_template) foot.push(`<span class="chip">数据查询 ${escapeHtml(data.sql_template)}</span>`);
      else if (data.llm_provider && data.llm_provider !== "none") foot.push(`<span>${escapeHtml(data.llm_provider)}</span>`);
      else if (data.llm_provider === "none") foot.push(`<span class="chip">未接大模型 · 展示检索依据</span>`);
      if (data.llm_failed) foot.push(`<span class="chip warn">模型降级</span>`);
      if (meta?.rerank?.provider) foot.push(`<span class="chip">重排 ${escapeHtml(meta.rerank.provider.split(":")[0])}</span>`);
      foot.push(feedbackButtons(messageId, null));
      view.foot.innerHTML = foot.join("");
      loadSessions();
    } else if (event === "error") {
      view.state.className = "chip danger";
      view.state.textContent = "出错";
      view.text.innerHTML = `<span style="color:var(--danger)">${escapeHtml(data.message || "服务异常，请稍后重试")}</span>`;
      view.foot.innerHTML = data.error_code ? `<span class="chip danger">${escapeHtml(data.error_code)}</span>` : "";
      finish();
    }
  }, state.controller.signal)
    .catch((err) => {
      if (err.name === "AbortError") {
        view.state.className = "chip warn";
        view.state.textContent = "已停止";
        if (!answerText) view.text.textContent = "（已停止生成）";
      } else {
        view.state.className = "chip danger";
        view.state.textContent = "失败";
        view.text.innerHTML = `<span style="color:var(--danger)">${escapeHtml(err.message)}</span>`;
      }
    })
    .finally(finish);
}

function decideSession(sessionId, query) {
  if (state.sessionId === null) {
    state.sessionId = sessionId;
    $("#session-title").textContent = query.slice(0, 30);
  }
}

/** 会话按「今天 / 昨天 / 更早」分组，和设计稿侧栏一致。 */
function dayGroup(iso) {
  const d = iso ? new Date(iso) : null;
  if (!d || Number.isNaN(d.getTime())) return "更早";
  const now = new Date();
  if (d.toDateString() === now.toDateString()) return "今天";
  const y = new Date(now);
  y.setDate(now.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return "昨天";
  return "更早";
}

function renderSessionList() {
  const list = $("#session-list");
  if (!list) return;
  const q = state.search.trim().toLowerCase();
  const items = q
    ? state.sessions.filter((s) => String(s.title || "").toLowerCase().includes(q))
    : state.sessions;
  if (!items.length) {
    list.innerHTML = `<div class="small faint" style="padding:12px 10px">${q ? "没有匹配的会话" : "还没有会话"}</div>`;
    return;
  }
  const groups = new Map();
  for (const s of items) {
    const key = dayGroup(s.updated_at);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(s);
  }
  list.innerHTML = Array.from(groups)
    .map(
      ([label, arr]) => `
      <div class="group-label">${label}</div>
      ${arr
        .map(
          (s) => `
        <div class="session-item ${s.id === state.sessionId ? "active" : ""}" data-sid="${s.id}">
          <div class="title">${escapeHtml(s.title || "新会话")}</div>
          <div class="meta">${fmtTime(s.updated_at)} · ${s.message_count} 条</div>
          <button class="session-edit" data-sedit="${s.id}" title="重命名">✎</button>
          <button class="session-del" data-sdel="${s.id}" title="删除会话">✕</button>
        </div>`
        )
        .join("")}`
    )
    .join("");
}

async function loadSessions() {
  try {
    state.sessions = await api("/api/v1/sessions");
  } catch {
    return;
  }
  renderSessionList();
}

/** 就地重命名：Enter/失焦保存，Esc 取消。失败会回滚原文。 */
function startRename(item, sessionId) {
  const titleEl = item.querySelector(".title");
  const original = titleEl.textContent;
  const input = document.createElement("input");
  input.className = "session-rename";
  input.value = original;
  input.maxLength = 60;
  input.spellcheck = false;
  titleEl.replaceWith(input);
  input.focus();
  input.select();

  let settled = false;
  const restore = () => {
    if (input.isConnected) input.replaceWith(titleEl);
  };
  const commit = async () => {
    if (settled) return;
    settled = true;
    const title = input.value.trim();
    if (!title || title === original) {
      // 空标题直接当取消：标题列显示空白比"改失败"更让人困惑
      restore();
      if (!title && original !== input.value) toast("标题不能为空，已保留原名", "err");
      return;
    }
    titleEl.textContent = title;
    restore();
    try {
      await api(`/api/v1/sessions/${sessionId}`, { method: "PATCH", body: { title } });
      if (state.sessionId === sessionId) $("#session-title").textContent = title;
      toast("已重命名", "ok");
    } catch (e) {
      toast(e.message, "err");
    }
    loadSessions();
  };
  const cancel = () => {
    if (settled) return;
    settled = true;
    restore();
  };

  input.addEventListener("keydown", (ev) => {
    ev.stopPropagation(); // 别让 Enter 冒到聊天输入框的快捷键
    if (ev.key === "Enter") { ev.preventDefault(); commit(); }
    else if (ev.key === "Escape") { ev.preventDefault(); cancel(); }
  });
  input.addEventListener("click", (ev) => ev.stopPropagation());
  input.addEventListener("blur", commit);
}

let delConfirmTimer = null;

async function deleteSession(sessionId) {
  try {
    await api(`/api/v1/sessions/${sessionId}`, { method: "DELETE" });
    if (state.sessionId === sessionId) {
      state.sessionId = null;
      $("#session-title").textContent = "新会话";
      messagesEl.innerHTML = EMPTY_HTML;
    }
    await loadSessions();
    toast("会话已删除", "ok");
  } catch (e) {
    toast(e.message, "err");
  }
}

async function openSession(sessionId) {
  if (state.streaming) state.controller?.abort();
  state.sessionId = sessionId;
  const items = await api(`/api/v1/sessions/${sessionId}/messages`);
  messagesEl.innerHTML = "";
  if (!items.length) {
    messagesEl.innerHTML = `<div class="empty-state"><div class="big">这个会话还没有消息</div></div>`;
  }
  for (const m of items) {
    if (m.role === "user") {
      appendUser(m.content);
    } else {
      const view = appendAssistant();
      view.text.textContent = m.content;
      view.state.className = `chip ${m.compliance_flag === "block" ? "danger" : m.compliance_flag === "annotate" ? "warn" : "ok"}`;
      view.state.textContent = m.compliance_flag === "block" ? "合规拦截" : m.compliance_flag === "annotate" ? "含合规提示" : "完成";
      if (m.refs?.length) {
        view.refs.innerHTML = renderRefs(m.refs);
        enhanceRefCards(view.refs);
      }
      const foot = [];
      if (m.request_id) foot.push(`<span class="mono">req ${escapeHtml(m.request_id)}</span>`);
      if (m.retrieval?.took_ms !== undefined) foot.push(`<span>检索 ${fmtMs(m.retrieval.took_ms)}</span>`);
      foot.push(feedbackButtons(m.id, m.feedback));
      view.foot.innerHTML = foot.join("");
    }
  }
  const s = state.sessions.find((x) => x.id === sessionId);
  $("#session-title").textContent = s?.title || "会话";
  loadSessions();
  scrollBottom();
}

function openFeedbackModal(messageId, rating) {
  if (rating === "up") {
    api("/api/v1/feedback", { method: "POST", body: { message_id: messageId, rating: "up" } })
      .then(() => {
        toast("已记录：回答有帮助", "ok");
        document.querySelectorAll(`[data-feedback="up"][data-mid="${messageId}"]`).forEach((b) => b.classList.add("on"));
        document.querySelectorAll(`[data-feedback="down"][data-mid="${messageId}"]`).forEach((b) => b.classList.remove("on"));
      })
      .catch((e) => toast(e.message, "err"));
    return;
  }
  const root = $("#modal-root");
  root.innerHTML = `
    <div class="modal-mask">
      <div class="modal">
        <div class="spread" style="margin-bottom:12px"><h3>这条回答哪里有问题？</h3><button class="btn sm" data-close>关闭</button></div>
        <div class="stack" style="gap:10px">
          <label class="field">原因（必选）
            <select id="fb-reason">${REASONS.map(([v, n]) => `<option value="${v}">${n}</option>`).join("")}</select>
          </label>
          <label class="field">补充说明（可选）
            <textarea id="fb-comment" placeholder="例如：引用的是旧版规格书 / 漏掉了页码 / 译文把 DN50 翻错了"></textarea>
          </label>
          <div class="infobox small">提交后会自动保存当时的检索快照（问题、回答、引用、候选与耗时），供质量同学复现与回归。</div>
          <div class="row" style="justify-content:flex-end">
            <button class="btn" data-close>取消</button>
            <button class="btn danger" id="fb-submit">提交点踩</button>
          </div>
        </div>
      </div>
    </div>`;
  const close = () => (root.innerHTML = "");
  root.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", close));
  root.querySelector("#fb-submit").addEventListener("click", async (ev) => {
    ev.target.disabled = true;
    try {
      await api("/api/v1/feedback", {
        method: "POST",
        body: {
          message_id: messageId,
          rating: "down",
          reason: root.querySelector("#fb-reason").value,
          comment: root.querySelector("#fb-comment").value.trim() || null,
        },
      });
      toast("已记录，感谢反馈（已生成待处理工单）", "ok");
      document.querySelectorAll(`[data-feedback="down"][data-mid="${messageId}"]`).forEach((b) => b.classList.add("on"));
      document.querySelectorAll(`[data-feedback="up"][data-mid="${messageId}"]`).forEach((b) => b.classList.remove("on"));
      close();
    } catch (e) {
      toast(e.message, "err");
      ev.target.disabled = false;
    }
  });
}

function bindEvents() {
  sendBtn.addEventListener("click", send);
  inputEl.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) {
      ev.preventDefault();
      send();
    }
  });
  // 快捷问句：只填入输入框，用户还能改；不直接发送，避免误触
  document.querySelectorAll("[data-hint]").forEach((btn) => {
    btn.addEventListener("click", () => {
      inputEl.value = btn.dataset.hint || "";
      inputEl.focus();
    });
  });
  // 会话搜索：本地过滤，不额外打后端
  const searchEl = $("#session-search");
  if (searchEl) {
    searchEl.addEventListener("input", () => {
      state.search = searchEl.value || "";
      renderSessionList();
    });
  }
  $("#new-session").addEventListener("click", () => {
    if (state.streaming) state.controller?.abort();
    state.sessionId = null;
    messagesEl.innerHTML = EMPTY_HTML;
    $("#session-title").textContent = "新会话";
    loadSessions();
    inputEl.focus();
  });
  $("#session-list").addEventListener("click", (ev) => {
    const edit = ev.target.closest("[data-sedit]");
    if (edit) {
      ev.stopPropagation();
      startRename(edit.closest(".session-item"), Number(edit.dataset.sedit));
      return;
    }
    const del = ev.target.closest("[data-sdel]");
    if (del) {
      ev.stopPropagation();
      const item = del.closest(".session-item");
      // 两段式确认：第一次点变红色「确认」，3 秒内再点才真删，防止误触
      if (!item.classList.contains("confirm-del")) {
        item.classList.add("confirm-del");
        del.textContent = "确认";
        clearTimeout(delConfirmTimer);
        delConfirmTimer = setTimeout(() => {
          item.classList.remove("confirm-del");
          del.textContent = "✕";
        }, 3000);
        return;
      }
      clearTimeout(delConfirmTimer);
      deleteSession(Number(del.dataset.sdel));
      return;
    }
    const item = ev.target.closest(".session-item");
    if (item) openSession(Number(item.dataset.sid));
  });
  $("#session-list").addEventListener("dblclick", (ev) => {
    const titleEl = ev.target.closest(".title");
    if (!titleEl) return;
    const item = titleEl.closest(".session-item");
    startRename(item, Number(item.dataset.sid));
  });
  messagesEl.addEventListener("click", (ev) => {
    const fb = ev.target.closest("[data-feedback]");
    if (fb) {
      openFeedbackModal(Number(fb.dataset.mid), fb.dataset.feedback);
      return;
    }
    const toggle = ev.target.closest("[data-ref-toggle]");
    if (toggle) {
      const card = toggle.closest(".ref-card");
      const body = card.querySelector(".ref-body");
      const isTrans = toggle.dataset.refToggle === "trans";
      body.textContent = isTrans ? body.dataset.trans || "" : body.dataset.orig || "";
      card.querySelectorAll("[data-ref-toggle]").forEach((b) => b.classList.toggle("on", b === toggle));
      // 切换原文/译文后内容长度可能变化，重新判断是否需要「展开全部」
      enhanceRefCards(card);
      return;
    }
    const more = ev.target.closest("[data-ref-more]");
    if (more) {
      const card = more.closest(".ref-card");
      card.classList.toggle("ref-open");
      more.innerHTML = card.classList.contains("ref-open") ? `<span class="arr"></span> 收起` : `<span class="arr"></span> 展开全部`;
      return;
    }
    const copy = ev.target.closest("[data-copy]");
    if (copy) {
      navigator.clipboard?.writeText(copy.dataset.copy);
      toast("已复制 request_id", "ok");
    }
  });
}

async function loadCapabilities() {
  try {
    const cap = await api("/api/v1/system/capabilities");
    const emb = cap.embedder || {};
    const rr = cap.reranker || {};
    const chip = $("#model-chip");
    const embLabel = emb.active || emb.name || "unknown";
    chip.textContent = embLabel.includes("hash") ? "哈希嵌入（降级）" : embLabel.replace("st:", "");
    chip.className = `chip ${cap.fallback_warning ? "warn" : "ok"}`;
    const capBadges = [
      `<span class="cap-badge">${escapeHtml(String(embLabel).replace("st:", ""))}${emb.dim ? ` · ${emb.dim}` : ""}</span>`,
      `<span class="cap-badge">${escapeHtml(cap.vector_store?.backend || "—")}</span>`,
      `<span class="cap-badge">${escapeHtml(cap.ocr?.engine || "—")}</span>`,
    ];
    $("#capability-hint").innerHTML = capBadges.join("") +
      (cap.fallback_warning ? `<span class="cap-badge warn">⚠ 嵌入未加载</span>` : "");
  } catch {
    $("#capability-hint").textContent = "能力探针不可用";
  }
}

async function main() {
  const u = await boot("chat");
  if (!u) return;
  bindEvents();
  await loadSessions();
  loadCapabilities();
  inputEl.focus();
}

main();
