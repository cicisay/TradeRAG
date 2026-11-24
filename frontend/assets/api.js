/* 统一 API 客户端：token 管理、错误提示、SSE 流式解析。
 * 前端与后端同源（FastAPI 直接托管本目录），因此路径写相对地址即可。 */

const TOKEN_KEY = "tradrag.token";
const USER_KEY = "tradrag.user";

export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || "";
}

export function setSession(token, user) {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  if (user) localStorage.setItem(USER_KEY, JSON.stringify(user));
}

export function getUser() {
  try {
    return JSON.parse(localStorage.getItem(USER_KEY) || "null");
  } catch {
    return null;
  }
}

export function clearSession() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
}

export function toast(message, kind = "") {
  let wrap = document.querySelector(".toast-wrap");
  if (!wrap) {
    wrap = document.createElement("div");
    wrap.className = "toast-wrap";
    document.body.appendChild(wrap);
  }
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  wrap.appendChild(el);
  setTimeout(() => {
    el.style.opacity = "0";
    el.style.transform = "translateY(6px)";
    el.style.transition = "all .25s";
    setTimeout(() => el.remove(), 260);
  }, 3600);
}

function errorMessage(status, data) {
  const detail = data?.detail;
  if (typeof detail === "string") return detail;
  if (detail?.message) return detail.message;
  if (Array.isArray(detail) && detail[0]?.msg) return detail[0].msg;
  if (data?.message) return data.message;
  return `请求失败（HTTP ${status}）`;
}

export async function api(path, { method = "GET", body, form, headers = {}, raw = false } = {}) {
  const opts = { method, headers: { ...headers } };
  const token = getToken();
  if (token) opts.headers.Authorization = `Bearer ${token}`;
  if (form) {
    opts.body = form;
  } else if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(path, opts);
  if (resp.status === 401) {
    clearSession();
    window.dispatchEvent(new CustomEvent("auth:expired"));
  }
  if (!resp.ok) {
    let data = null;
    try {
      data = await resp.json();
    } catch {
      /* 非 JSON 错误体 */
    }
    const err = new Error(errorMessage(resp.status, data));
    err.status = resp.status;
    err.data = data;
    throw err;
  }
  if (raw) return resp;
  const type = resp.headers.get("content-type") || "";
  if (type.includes("application/json")) return resp.json();
  return resp.text();
}

/**
 * SSE 流式提问：POST /api/v1/chat（EventSource 不支持 POST + 自定义头，所以用 fetch 流）。
 * @param {string} query
 * @param {number|null} sessionId
 * @param {(event: string, data: any) => void} onEvent
 */
export async function streamChat(query, sessionId, onEvent, signal) {
  const resp = await fetch("/api/v1/chat", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${getToken()}`,
    },
    body: JSON.stringify({ query, session_id: sessionId }),
    signal,
  });
  if (!resp.ok || !resp.body) {
    let data = null;
    try {
      data = await resp.json();
    } catch {
      /* ignore */
    }
    throw new Error(errorMessage(resp.status, data));
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const chunk = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      let event = "message";
      const dataLines = [];
      for (const line of chunk.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      }
      if (!dataLines.length) continue;
      let data;
      try {
        data = JSON.parse(dataLines.join("\n"));
      } catch {
        data = { raw: dataLines.join("\n") };
      }
      onEvent(event, data);
    }
  }
}

export function escapeHtml(text) {
  return String(text ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

export function fmtPct(value, digits = 1) {
  if (value === null || value === undefined) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

export function fmtMs(ms) {
  if (ms === null || ms === undefined) return "—";
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)}s` : `${Math.round(ms)}ms`;
}

export function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : `${iso}Z`);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
