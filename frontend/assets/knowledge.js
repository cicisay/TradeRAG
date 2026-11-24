import { api, escapeHtml, fmtTime, toast } from "./api.js";
import { boot, can, user } from "./ui.js?v=20261110b";

const DOC_TYPES = { spec: "产品规格", customs: "报关单证", compliance: "合规文件", policy: "制度流程", other: "其他" };
const STATUS = {
  uploaded: ["已上传", ""],
  parsing: ["解析中", "warn"],
  parsed: ["已解析", "ok"],
  failed: ["失败", "danger"],
};
const state = { kbs: [], kbId: null, docs: [] };

// ---- 异步摄入任务（M6 步骤10-2）----
const JOB_STAGE = {
  queued: ["排队中", "info"],
  parsing: ["解析中", "warn"],
  chunking: ["切分中", "warn"],
  embedding: ["向量化中", "warn"],
  indexed: ["已完成", "ok"],
  failed: ["失败", "danger"],
  canceled: ["已取消", ""],
};
const TERMINAL = new Set(["indexed", "failed", "canceled"]);
const jobState = { items: [], signature: "", pollers: new Map() };

function progressBar(pct, kind = "") {
  const v = Math.max(0, Math.min(100, Number(pct) || 0));
  return `<div class="bar"><div class="bar-fill ${kind}" style="width:${v}%"></div></div>`;
}

function jobRow(j) {
  const [name, kind] = JOB_STAGE[j.stage] || [j.stage || "未知", ""];
  const terminal = TERMINAL.has(j.stage);
  const action = terminal
    ? (j.stage === "indexed" ? "" : `<button class="btn sm" data-retry="${j.id}">重试</button>`)
    : `<button class="btn sm" data-cancel="${j.id}">取消</button>`;
  const err = j.error
    ? ` · <span style="color:var(--danger)">${escapeHtml(String(j.error).slice(0, 160))}</span>`
    : "";
  return `<div class="job-row">
    <div class="grow" style="min-width:0">
      <div class="spread" style="gap:10px">
        <div style="min-width:0"><b>${escapeHtml(j.doc_name || "文档 #" + j.doc_id)}</b>
          <span class="small faint">#${j.id} · ${escapeHtml(j.broker || "—")} · 第 ${j.attempts}/${j.max_attempts} 次</span></div>
        <span class="chip ${kind}">${name} ${j.progress}%</span>
      </div>
      ${progressBar(j.progress, kind)}
      <div class="small muted" style="margin-top:4px">${escapeHtml(j.message || "")}${err}</div>
    </div>
    <div class="row" style="flex:0 0 auto">${action}</div>
  </div>`;
}

function renderJobBanner(job) {
  const [name, kind] = JOB_STAGE[job.stage] || [job.stage, ""];
  $("#upload-result").innerHTML = `<div class="infobox small">
    <div class="spread"><b>${escapeHtml(job.doc_name || "文档")}</b>
      <span class="chip ${kind}">${name} ${job.progress}%</span></div>
    ${progressBar(job.progress, kind)}
    <div class="muted" style="margin-top:4px">${escapeHtml(job.message || "已进入处理队列")}</div>
  </div>`;
}

const $ = (s) => document.querySelector(s);

function statusChip(status) {
  const [name, kind] = STATUS[status] || [status || "未知", ""];
  return `<span class="chip ${kind}">${escapeHtml(name)}</span>`;
}

function renderKbs() {
  const grid = $("#kb-grid");
  if (!state.kbs.length) {
    grid.innerHTML = `<div class="card"><div class="card-body muted">还没有可见的知识库。管理员可新建，或运行 seed_demo_corpus.py 生成演示数据。</div></div>`;
    return;
  }
  grid.innerHTML = state.kbs.map((kb) => `
    <div class="card" data-kb="${kb.id}" style="cursor:pointer;${kb.id === state.kbId ? "border-color:var(--brand-2);box-shadow:0 0 0 3px rgba(47,111,165,.12)" : ""}">
      <div class="card-body">
        <div class="spread"><h3 style="font-size:14px">${escapeHtml(kb.name)}</h3><span class="chip brand">${escapeHtml(kb.biz_group)}</span></div>
        <div class="row" style="margin-top:10px;gap:18px">
          <div><div class="small faint">文档</div><div style="font-size:20px;font-weight:650">${kb.documents}</div></div>
          <div><div class="small faint">分片</div><div style="font-size:20px;font-weight:650">${kb.chunks}</div></div>
          <div class="grow" style="text-align:right"><div class="small faint">嵌入模型</div><div class="small">${escapeHtml(kb.embedding_model)}</div></div>
        </div>
      </div>
    </div>`).join("");
  grid.querySelectorAll("[data-kb]").forEach((el) => {
    el.addEventListener("click", () => selectKb(Number(el.dataset.kb)));
  });
}

function selectKb(kbId) {
  state.kbId = kbId;
  const kb = state.kbs.find((k) => k.id === kbId);
  $("#upload-kb-chip").textContent = kb ? `${kb.name} · ${kb.biz_group}` : "—";
  $("#doc-kb-filter").value = String(kbId || "");
  renderKbs();
  loadDocs();
}

function docRow(d) {
  const langs = (d.languages || []).join(",") || "—";
  const ocr = d.ocr_used
    ? `<span class="chip ${(d.ocr_confidence || 0) < 0.85 ? "warn" : "info"}">OCR ${d.ocr_confidence ? d.ocr_confidence.toFixed(3) : "—"}</span>`
    : `<span class="chip">文本层</span>`;
  const low = (d.ocr_low_pages || []).length ? `<span class="chip warn">低置信页 ${d.ocr_low_pages.join(",")}</span>` : "";
  return `
    <tr>
      <td><div style="font-weight:600">${escapeHtml(d.doc_name)}</div><div class="small faint">#${d.id} · ${escapeHtml(d.doc_type || "other")}</div></td>
      <td>${statusChip(d.status)}${d.chunks ? `<div class="small faint">${d.chunks} 分片</div>` : ""}</td>
      <td><div class="row wrap" style="gap:4px">${ocr}${low}</div></td>
      <td class="small">${escapeHtml(langs)}<div class="faint">${escapeHtml(d.biz_group || "")} · T${d.authority_level ?? "?"}</div></td>
      <td class="small">${d.page_count ?? "—"}</td>
      <td><button class="btn sm" data-doc="${d.id}">分片</button></td>
    </tr>`;
}

async function loadDocs() {
  const box = $("#doc-table");
  box.innerHTML = `<div class="card-body small muted">加载中…</div>`;
  try {
    const qs = state.kbId ? `?kb_id=${state.kbId}` : "";
    state.docs = await api(`/api/v1/documents${qs}`);
  } catch (err) {
    box.innerHTML = `<div class="card-body small" style="color:var(--danger)">${escapeHtml(err.message)}</div>`;
    return;
  }
  if (!state.docs.length) {
    box.innerHTML = `<div class="card-body small muted">该知识库还没有文档，上传一份试试。</div>`;
    return;
  }
  box.innerHTML = `
    <table>
      <thead><tr><th>文档</th><th>状态</th><th>解析质量</th><th>语种/归属</th><th>页数</th><th></th></tr></thead>
      <tbody>${state.docs.map(docRow).join("")}</tbody>
    </table>`;
  box.querySelectorAll("[data-doc]").forEach((btn) => {
    btn.addEventListener("click", () => openDoc(Number(btn.dataset.doc)));
  });
}

async function openDoc(docId) {
  const doc = state.docs.find((d) => d.id === docId);
  const root = $("#drawer-root");
  root.innerHTML = `<div class="drawer-mask"><div class="drawer" id="doc-drawer"><div class="muted small">加载版本…</div></div></div>`;
  const close = () => (root.innerHTML = "");
  root.querySelector(".drawer-mask").addEventListener("click", (ev) => {
    if (ev.target.classList.contains("drawer-mask")) close();
  });
  try {
    const versions = await api(`/api/v1/documents/${docId}/versions`);
    const current = versions.find((v) => v.current) || versions[0];
    const drawer = root.querySelector("#doc-drawer");
    drawer.innerHTML = `
      <div class="drawer-head">
        <div><h3>${escapeHtml(doc?.doc_name || `文档 #${docId}`)}</h3><div class="small muted">版本链 ${versions.length} 个 · 当前 v${current?.version_no ?? "?"}</div></div>
        <button class="btn sm" id="drawer-close">关闭</button>
      </div>
      <div class="stack" style="gap:12px">
        <div class="row wrap" style="gap:6px">
          ${versions.map((v) => `<button class="btn sm ${v.id === current?.id ? "primary" : ""}" data-vid="${v.id}">v${v.version_no}${v.current ? " · 当前" : ""}</button>`).join("")}
        </div>
        <div class="infobox small">
          解析：${escapeHtml(current?.parse_status || "—")} · 页数 ${current?.page_count ?? "—"} ·
          OCR ${current?.ocr_used ? `已启用（平均置信度 ${current?.ocr_confidence?.toFixed?.(3) ?? "—"}${(current?.ocr_low_pages || []).length ? `，低置信页 ${current.ocr_low_pages.join(",")}` : ""}）` : "未启用"} ·
          上传 ${fmtTime(current?.uploaded_at)}
        </div>
        <div class="spread"><h3 style="font-size:13px">分片内容</h3><span class="small faint" id="chunk-count">—</span></div>
        <div id="chunk-list" class="stack" style="gap:8px"></div>
      </div>`;
    drawer.querySelector("#drawer-close").addEventListener("click", close);
    drawer.querySelectorAll("[data-vid]").forEach((btn) => {
      btn.addEventListener("click", () => loadChunks(Number(btn.dataset.vid), drawer));
    });
    if (current) await loadChunks(current.id, drawer);
  } catch (err) {
    root.innerHTML = `<div class="drawer-mask"><div class="drawer"><div class="warnbox">${escapeHtml(err.message)}</div><button class="btn" onclick="this.closest('.drawer-mask').remove()">关闭</button></div></div>`;
  }
}

async function loadChunks(versionId, drawer) {
  const box = drawer.querySelector("#chunk-list");
  box.innerHTML = `<div class="small muted">加载分片…</div>`;
  try {
    const data = await api(`/api/v1/doc-versions/${versionId}/chunks?limit=100`);
    drawer.querySelector("#chunk-count").textContent = `共 ${data.total} 片`;
    if (!data.items.length) {
      box.innerHTML = `<div class="small muted">暂无分片（文档可能未成功切分）</div>`;
      return;
    }
    box.innerHTML = data.items.map((c) => `
      <div class="hit">
        <div class="top">
          <span class="rank">${c.chunk_index + 1}</span>
          <span class="chip">${escapeHtml(c.language)}</span>
          ${c.page_no ? `<span class="chip info">第 ${c.page_no} 页</span>` : ""}
          ${c.ocr_confidence ? `<span class="chip warn">OCR ${c.ocr_confidence.toFixed(3)}</span>` : ""}
        </div>
        <div class="content" style="max-height:120px;white-space:pre-wrap">${escapeHtml(c.content)}</div>
      </div>`).join("");
  } catch (err) {
    box.innerHTML = `<div class="small" style="color:var(--danger)">${escapeHtml(err.message)}</div>`;
  }
}

async function upload(ev) {
  ev.preventDefault();
  const form = ev.target;
  const file = form.querySelector('input[name="file"]').files[0];
  if (!file) return;
  if (!state.kbId) {
    toast("请先选择知识库", "err");
    return;
  }
  const btn = $("#upload-btn");
  btn.disabled = true;
  btn.textContent = "上传中…";
  $("#upload-result").innerHTML = `<div class="infobox small">正在上传并登记任务…</div>`;
  const fd = new FormData();
  fd.append("kb_id", String(state.kbId));
  fd.append("file", file);
  fd.append("doc_type", form.querySelector('[name="doc_type"]').value);
  fd.append("authority_level", form.querySelector('[name="authority_level"]').value);
  fd.append("languages", form.querySelector('[name="languages"]').value);
  try {
    const doc = await api("/api/v1/documents/upload", { method: "POST", form: fd });
    form.reset();
    if (doc.queued && doc.job_id) {
      // 异步：接口已返回，真正的工作在后台，进度靠轮询
      $("#upload-result").innerHTML = `<div class="infobox small">已入队（任务 #${doc.job_id}），后台处理中…</div>`;
      toast("已入队，正在后台处理", "ok");
      pollJob(doc.job_id);
      await loadJobs(true);
      await loadDocs();
    } else {
      $("#upload-result").innerHTML = `
        <div class="infobox small">
          <b>${escapeHtml(doc.doc_name)}</b> 已入库：#${doc.id} · ${doc.chunks ?? 0} 个分片 ·
          ${doc.page_count ?? "—"} 页 · OCR ${doc.ocr_used ? `是（${doc.ocr_confidence?.toFixed?.(3) ?? "—"}）` : "否"}
          ${(doc.ocr_low_pages || []).length ? ` · <span style="color:var(--warn)">低置信页 ${doc.ocr_low_pages.join(",")}</span>` : ""}
        </div>`;
      toast("上传并索引完成", "ok");
      await loadKbs();
      await loadDocs();
    }
  } catch (err) {
    $("#upload-result").innerHTML = `<div class="warnbox small">${escapeHtml(err.message)}</div>`;
    toast(err.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = "上传并入队";
  }
}

// ---- 任务面板 ----
async function loadJobs(force = false) {
  const box = $("#jobs-body");
  if (!box) return;
  try {
    const [stats, list] = await Promise.all([
      api("/api/v1/jobs/stats"),
      api("/api/v1/jobs?limit=8"),
    ]);
    const chip = $("#queue-mode");
    const mode = stats.mode || "—";
    const brokerOk = !!stats.broker?.available;
    chip.textContent = mode === "celery"
      ? `celery · 队列 ${stats.broker?.queued_in_broker ?? "—"}`
      : mode === "thread" ? "同进程线程（内存向量库）" : mode;
    chip.className = "chip " + (mode === "celery" ? (brokerOk ? "ok" : "danger") : "info");

    const items = list.items || [];
    const sig = items.map((i) => `${i.id}:${i.stage}:${i.progress}`).join("|");
    if (!force && sig === jobState.signature) return;
    jobState.signature = sig;
    jobState.items = items;
    if (!items.length) {
      box.innerHTML = `<div class="small muted">暂无任务。上传文档后会在这里看到解析与向量化进度。</div>`;
      return;
    }
    box.innerHTML = items.map(jobRow).join("");
  } catch (err) {
    box.innerHTML = `<div class="small" style="color:var(--danger)">${escapeHtml(err.message)}</div>`;
  }
}

function pollJob(jobId, withBanner = true) {
  if (jobState.pollers.has(jobId)) return;
  const timer = setInterval(async () => {
    let job;
    try {
      job = await api(`/api/v1/jobs/${jobId}`);
    } catch {
      clearInterval(timer);
      jobState.pollers.delete(jobId);
      return;
    }
    if (withBanner) renderJobBanner(job);
    loadJobs(true);
    if (TERMINAL.has(job.stage)) {
      clearInterval(timer);
      jobState.pollers.delete(jobId);
      if (job.stage === "indexed") {
        toast(`《${job.doc_name || "文档"}》处理完成，${job.result?.chunks ?? 0} 个分片`, "ok");
        await loadKbs();
        await loadDocs();
      } else if (job.stage === "failed") {
        toast(`处理失败：${String(job.error || "").slice(0, 80)}`, "err");
        await loadDocs();
      }
    }
  }, 1200);
  jobState.pollers.set(jobId, timer);
}

async function reindex() {
  const btn = $("#reindex-btn");
  if (!confirm("把全部已解析文档重新排队索引？后台执行，进度见下方「处理任务」面板。")) return;
  btn.disabled = true;
  btn.textContent = "入队中…";
  try {
    const res = await api("/api/v1/system/reindex", { method: "POST" });
    const failed = (res.failed || []).length;
    toast(`已排队 ${res.queued}/${res.total} 篇${failed ? `，${failed} 篇入队失败` : ""}`, failed ? "err" : "ok");
    await loadJobs(true);
  } catch (err) {
    toast(err.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = "重建索引";
  }
}

function newKbModal() {
  const root = $("#modal-root");
  const isAdmin = user()?.role === "admin";
  root.innerHTML = `
    <div class="modal-mask"><div class="modal">
      <div class="spread" style="margin-bottom:12px"><h3>新建知识库</h3><button class="btn sm" data-close>关闭</button></div>
      <div class="stack" style="gap:10px">
        <label class="field">名称<input type="text" id="kb-name" placeholder="例如：欧洲区产品资料" /></label>
        <label class="field">业务组<input type="text" id="kb-group" value="${escapeHtml(user()?.biz_group || "default")}" ${isAdmin ? "" : "disabled"} /></label>
        <label class="field">产品线<input type="text" id="kb-line" value="general" /></label>
        <div class="infobox small">业务组决定谁能检索该库；非管理员只能建自己业务组的库（FR-603）。</div>
        <div class="row" style="justify-content:flex-end"><button class="btn" data-close>取消</button><button class="btn primary" id="kb-create">创建</button></div>
      </div>
    </div></div>`;
  const close = () => (root.innerHTML = "");
  root.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", close));
  root.querySelector("#kb-create").addEventListener("click", async () => {
    try {
      const kb = await api("/api/v1/knowledge-bases", {
        method: "POST",
        body: {
          name: root.querySelector("#kb-name").value.trim(),
          biz_group: root.querySelector("#kb-group").value.trim() || null,
          product_line: root.querySelector("#kb-line").value.trim() || "general",
        },
      });
      toast("知识库已创建", "ok");
      close();
      await loadKbs();
      selectKb(kb.id);
    } catch (err) {
      toast(err.message, "err");
    }
  });
}

async function loadKbs() {
  state.kbs = await api("/api/v1/knowledge-bases");
  const filter = $("#doc-kb-filter");
  filter.innerHTML = `<option value="">全部可见文档</option>` + state.kbs.map((k) => `<option value="${k.id}">${escapeHtml(k.name)}</option>`).join("");
  if (!state.kbId && state.kbs.length) state.kbId = state.kbs[0].id;
  filter.value = state.kbId ? String(state.kbId) : "";
  renderKbs();
  const kb = state.kbs.find((k) => k.id === state.kbId);
  $("#upload-kb-chip").textContent = kb ? `${kb.name} · ${kb.biz_group}` : "—";
}

async function main() {
  const u = await boot("knowledge");
  if (!u) return;
  if (!can("kb:manage")) {
    $("#upload-card").classList.add("hidden");
    $("#reindex-btn").classList.add("hidden");
    $("#new-kb-btn").classList.add("hidden");
  }
  $("#upload-form").addEventListener("submit", upload);
  $("#reindex-btn").addEventListener("click", reindex);
  $("#refresh-jobs")?.addEventListener("click", () => loadJobs(true));
  $("#jobs-body")?.addEventListener("click", async (ev) => {
    const retryId = ev.target.closest("[data-retry]")?.dataset.retry;
    const cancelId = ev.target.closest("[data-cancel]")?.dataset.cancel;
    try {
      if (retryId) {
        await api(`/api/v1/jobs/${retryId}/retry`, { method: "POST" });
        toast("已重新排队", "ok");
        pollJob(Number(retryId), false);
        await loadJobs(true);
      } else if (cancelId) {
        await api(`/api/v1/jobs/${cancelId}/cancel`, { method: "POST" });
        toast("已取消（正在执行的批次结束后停止）", "ok");
        await loadJobs(true);
      }
    } catch (err) {
      toast(err.message, "err");
    }
  });
  $("#refresh-docs").addEventListener("click", loadDocs);
  $("#new-kb-btn").addEventListener("click", newKbModal);
  $("#doc-kb-filter").addEventListener("change", (ev) => {
    state.kbId = ev.target.value ? Number(ev.target.value) : null;
    renderKbs();
    loadDocs();
  });
  await loadKbs();
  await loadDocs();
  await loadJobs();
  // 有任务在跑时每 4 秒刷新面板；没有任务也保持低频刷新，便于别人入队时看到
  setInterval(() => loadJobs(), 4000);
}

main();
