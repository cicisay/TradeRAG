import { api, escapeHtml, fmtMs, fmtPct, fmtTime, toast } from "./api.js";
import { boot, can, user } from "./ui.js?v=20261110b";

const $ = (s) => document.querySelector(s);
const panel = () => $("#panel");

const state = { tab: "overview", datasets: [], caps: null };
let monitorTimer = null;

/* ------------------------------------------------------------------ */
/* 通用组件                                                            */
/* ------------------------------------------------------------------ */
const loading = (text = "加载中…") => `<div class="card"><div class="card-body muted">${text}</div></div>`;
const errorBox = (msg) => `<div class="card"><div class="card-body"><div class="warnbox">${escapeHtml(msg)}</div></div></div>`;

function metric(label, value, hint = "", kind = "") {
  return `<div class="card metric"><div class="label">${escapeHtml(label)}</div>
    <div class="value ${kind}">${value}</div>${hint ? `<div class="hint">${escapeHtml(hint)}</div>` : ""}</div>`;
}

function funnelRows(funnel = {}) {
  return Object.entries(funnel).map(([name, s]) => {
    const rate = s.entered ? s.succeeded / s.entered : 0;
    return `<div class="funnel-row">
      <div class="name">${escapeHtml(name)}</div>
      <div class="bar"><span style="width:${(rate * 100).toFixed(1)}%"></span></div>
      <div class="small" style="text-align:right">${s.succeeded}/${s.entered} · ${fmtPct(rate, 1)}</div>
    </div>`;
  }).join("");
}

function hitsHtml(hits = [], showRerank = false) {
  if (!hits.length) return `<div class="small muted">无结果</div>`;
  return `<div class="hit-list">${hits.map((h, i) => `
    <div class="hit">
      <div class="top">
        <span class="rank">${i + 1}</span>
        <span class="doc" title="${escapeHtml(h.doc_name)}" style="font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:180px">${escapeHtml(h.doc_name)}</span>
        ${h.page_no ? `<span class="chip info">p${h.page_no}</span>` : ""}
        <span class="chip">${escapeHtml(h.language || "")}</span>
        ${(h.channels || []).map((c) => `<span class="chip brand">${escapeHtml(c)}</span>`).join("")}
        <span class="score">${Number(h.score ?? 0).toFixed(3)}</span>
      </div>
      ${showRerank && h.rerank_score !== null && h.rerank_score !== undefined ? `<div class="small faint">rerank ${Number(h.rerank_score).toFixed(3)}</div>` : ""}
      <div class="content">${escapeHtml(h.content || "")}</div>
    </div>`).join("")}</div>`;
}

function table(headers, rows, empty = "暂无数据") {
  if (!rows.length) return `<div class="card-body small muted">${empty}</div>`;
  return `<div class="table-wrap"><table><thead><tr>${headers.map((h) => `<th>${h}</th>`).join("")}</tr></thead>
    <tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}

/* ------------------------------------------------------------------ */
/* 概览                                                                */
/* ------------------------------------------------------------------ */
async function renderOverview() {
  const caps = await api("/api/v1/system/capabilities");
  state.caps = caps;
  let summary = null;
  let alerts = [];
  try {
    summary = await api("/api/v1/monitor/summary");
    alerts = (await api("/api/v1/monitor/alerts")).alerts || [];
  } catch {
    summary = null; // 知识管理员没有 audit:read，降级为只看能力
  }
  const emb = caps.embedder || {};
  const rr = caps.reranker || {};
  const ocr = caps.ocr || {};
  const rates = summary?.metrics?.rates || {};
  const latest = summary?.latest_eval;

  panel().innerHTML = `
    ${caps.fallback_warning ? `<div class="warnbox" style="margin-bottom:14px"><b>⚠ 嵌入模型降级：</b>当前使用哈希嵌入，语义检索与跨语言能力会显著下降。请确认 models/bge-m3 已就位，必要时重启服务。</div>` : ""}
    <div class="grid cols-4" style="margin-bottom:16px">
      ${metric("累计请求", summary ? summary.metrics.requests_accepted : "—", "进程启动以来")}
      ${metric("完成率", summary ? fmtPct(rates.completion) : "—", "目标 ≥ 99%", rates.completion != null && rates.completion < 0.97 ? "warn" : "ok")}
      ${metric("空答率", summary ? fmtPct(rates.empty_answer) : "—", "目标 ≤ 8%", rates.empty_answer > 0.15 ? "danger" : "")}
      ${metric("待处理 badcase", summary ? summary.badcases.open : "—", summary ? `7 日新增 ${summary.badcases.week_new}` : "需 audit:read 权限")}
    </div>

    <div class="grid cols-2" style="align-items:start">
      <div class="card">
        <div class="card-head"><h3>能力探针</h3>
          <button class="btn sm" id="probe-btn">深探测（加载模型）</button></div>
        <div class="card-body stack" style="gap:10px" id="caps-body">
          ${capRow("嵌入", emb.active || emb.name, `${emb.dim ? emb.dim + " 维" : ""} ${emb.is_fallback ? "· 降级" : ""}`, emb.is_fallback)}
          ${capRow("重排", rr.active || rr.name || "未启用", rr.is_fallback ? "启发式（未加载模型）" : "交叉编码器", rr.is_fallback)}
          ${capRow("OCR", ocr.engine || "未启用", ocr.loaded ? "已加载" : "未加载", false)}
          ${capRow("向量库", caps.vector_store?.backend, caps.vector_store?.collection, caps.vector_store?.backend === "memory")}
          ${capRow("LLM", caps.llm?.provider, caps.llm?.configured ? "已配置 Key" : "未配置（走诚实兜底）", !caps.llm?.configured)}
        </div>
      </div>

      <div class="card">
        <div class="card-head"><h3>完成率漏斗</h3><span class="chip">FR-801</span></div>
        <div class="card-body">
          ${summary ? funnelRows(summary.metrics.funnel) : `<div class="small muted">需要 audit:read 权限查看漏斗。</div>`}
          ${summary ? `<div class="divider"></div>
          <div class="grid cols-3" style="gap:8px">
            <div><div class="small faint">首 Token P95</div><b>${fmtMs(summary.metrics.latency_ms.first_token?.p95)}</b></div>
            <div><div class="small faint">合规拦截</div><b>${summary.metrics.compliance.block}</b></div>
            <div><div class="small faint">SQL 拒绝</div><b>${summary.metrics.sql.denied}</b></div>
          </div>` : ""}
        </div>
      </div>
    </div>

    <div class="grid cols-2" style="margin-top:16px;align-items:start">
      <div class="card">
        <div class="card-head"><h3>告警</h3><span class="chip ${alerts.length ? "warn" : "ok"}">${alerts.length ? `${alerts.length} 条` : "正常"}</span></div>
        <div class="card-body stack" style="gap:8px">
          ${alerts.length ? alerts.map((a) => `<div class="warnbox small"><b>${a.level}</b> · ${escapeHtml(a.message)}</div>`).join("") : `<div class="small muted">当前没有触发阈值告警。</div>`}
        </div>
      </div>
      <div class="card">
        <div class="card-head"><h3>最近一次评测</h3><button class="btn sm" data-goto="eval">去跑分</button></div>
        <div class="card-body">
          ${latest ? `<div class="kv">
            <dt>数据集</dt><dd>${escapeHtml(latest.dataset)}（${latest.cases} 条）</dd>
            <dt>Recall@${latest.top_k || 5}</dt><dd><b>${latest.recall != null ? fmtPct(latest.recall) : "—"}</b> · MRR ${latest.mrr?.toFixed(3) ?? "—"} · nDCG ${latest.ndcg?.toFixed(3) ?? "—"}</dd>
            <dt>合规准确率</dt><dd>${latest.accuracy != null ? fmtPct(latest.accuracy) : "—"}</dd>
            <dt>耗时</dt><dd>${fmtMs(latest.duration_ms)} · ${fmtTime(latest.created_at)}</dd>
          </div>` : `<div class="small muted">还没有评测记录。到「批量评测」跑一次，结果会存档并可对比趋势。</div>`}
        </div>
      </div>
    </div>`;

  $("#probe-btn").addEventListener("click", async (ev) => {
    ev.target.disabled = true;
    ev.target.textContent = "探测中…（可能几十秒）";
    try {
      const probed = await api("/api/v1/system/capabilities?probe=true");
      state.caps = probed;
      toast("探测完成，模型状态已刷新", "ok");
      renderOverview();
    } catch (err) {
      toast(err.message, "err");
      ev.target.disabled = false;
      ev.target.textContent = "深探测（加载模型）";
    }
  });
  panel().querySelectorAll("[data-goto]").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.goto)));
}

function capRow(name, value, hint, warn) {
  return `<div class="spread" style="padding:6px 0;border-bottom:1px solid var(--border)">
    <div><b>${escapeHtml(name)}</b><div class="small faint">${escapeHtml(String(value || "—"))}</div></div>
    <span class="chip ${warn ? "warn" : "ok"}">${escapeHtml(hint || "")}</span>
  </div>`;
}

/* ------------------------------------------------------------------ */
/* 召回测试台                                                          */
/* ------------------------------------------------------------------ */
async function renderDebug() {
  panel().innerHTML = `
    <div class="card" style="margin-bottom:16px">
      <div class="card-head"><h3>五通道召回对照</h3><span class="chip">FR-701 / FR-704</span></div>
      <div class="card-body">
        <form id="dbg-form" class="stack" style="gap:10px">
          <label class="field">查询
            <input type="text" id="dbg-query" placeholder="例如：法兰用什么材质 / DAP 和 DDP 的区别" value="不锈钢法兰用什么材质" />
          </label>
          <div class="row wrap" style="gap:14px">
            <label class="field" style="width:100px">Top K<input type="number" id="dbg-topk" value="10" min="1" max="50" /></label>
            <label class="toggle"><input type="checkbox" id="dbg-cross" checked /> 跨语言变体</label>
            <label class="toggle"><input type="checkbox" id="dbg-rerank" checked /> 重排</label>
            <label class="field grow" style="max-width:260px">RBAC 模拟用户（可选）
              <input type="text" id="dbg-sim" placeholder="demo_sales_b（验证隔离）" />
            </label>
            <button class="btn primary" type="submit" id="dbg-run">执行检索</button>
          </div>
          <div class="small faint">提示：模拟用户只有管理员可用；越权业务组的内容应 0 召回。</div>
        </form>
      </div>
    </div>
    <div id="dbg-result"><div class="card"><div class="card-body muted">执行一次检索，查看向量 / BM25 / 混合 / 跨语言 / SQL 五通道对照。</div></div></div>`;

  $("#dbg-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const btn = $("#dbg-run");
    btn.disabled = true;
    btn.textContent = "检索中…";
    $("#dbg-result").innerHTML = loading("检索中（首次会加载模型）…");
    try {
      const res = await api("/api/v1/debug/retrieval", {
        method: "POST",
        body: {
          query: $("#dbg-query").value.trim(),
          top_k: Number($("#dbg-topk").value) || 10,
          crosslingual: $("#dbg-cross").checked,
          rerank: $("#dbg-rerank").checked,
          simulate_user: $("#dbg-sim").value.trim() || null,
        },
      });
      renderDebugResult(res);
    } catch (err) {
      $("#dbg-result").innerHTML = errorBox(err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = "执行检索";
    }
  });
  $("#dbg-form").dispatchEvent(new Event("submit"));
}

function renderDebugResult(res) {
  const rerank = res.rerank || {};
  $("#dbg-result").innerHTML = `
    <div class="grid cols-4" style="margin-bottom:14px">
      ${metric("耗时", fmtMs(res.server_took_ms), `检索 ${fmtMs(res.took_ms)}`)}
      ${metric("重排", rerank.applied ? "已启用" : "未启用", rerank.provider || "—", rerank.is_fallback ? "warn" : "")}
      ${metric("跨语言变体", Object.keys(res.variants || {}).length, res.degraded ? "翻译降级" : "正常")}
      ${metric("身份", escapeHtml(res.effective_user?.username || "—"), `${res.effective_user?.role} · ${res.effective_user?.biz_group}`)}
    </div>
    <div class="grid cols-2" style="margin-bottom:14px;align-items:start">
      <div class="card">
        <div class="card-head"><h3>SQL 通道路由</h3><span class="chip ${res.sql?.routed ? "ok" : ""}">${res.sql?.routed ? "已命中模板" : "未命中"}</span></div>
        <div class="card-body">
          ${res.sql?.routed ? `<div class="kv"><dt>模板</dt><dd class="mono">${escapeHtml(res.sql.template_id)}</dd>
            <dt>槽位参数</dt><dd class="mono">${escapeHtml(JSON.stringify(res.sql.params))}</dd></div>
            <div class="small faint" style="margin-top:6px">到「SQL 通道」页可查看渲染后的只读 SQL（此处不执行）。</div>`
            : `<div class="small muted">该问题不涉及结构化数据，走文档检索。</div>`}
        </div>
      </div>
      <div class="card">
        <div class="card-head"><h3>跨语言变体</h3><span class="chip">${res.degraded ? "降级" : "FR-202a"}</span></div>
        <div class="card-body">
          ${Object.keys(res.variants || {}).length
            ? Object.entries(res.variants).map(([lang, text]) => `<div class="small" style="margin-bottom:6px"><span class="chip info">${escapeHtml(lang)}</span> ${escapeHtml(text)}</div>`).join("")
            : `<div class="small muted">未生成变体（未配置翻译模型时，bge-m3 本身仍支持跨语言语义匹配）。</div>`}
        </div>
      </div>
    </div>
    <div class="grid cols-3" style="align-items:start">
      <div class="card"><div class="card-head"><h3>向量通道</h3><span class="chip">${(res.vector || []).length} 条</span></div><div class="card-body tight">${hitsHtml(res.vector)}</div></div>
      <div class="card"><div class="card-head"><h3>BM25 通道</h3><span class="chip">${(res.bm25 || []).length} 条</span></div><div class="card-body tight">${hitsHtml(res.bm25)}</div></div>
      <div class="card"><div class="card-head"><h3>混合（RRF）</h3><span class="chip brand">${(res.hybrid || []).length} 条</span></div><div class="card-body tight">${hitsHtml(res.hybrid, true)}</div></div>
    </div>`;
}

/* ------------------------------------------------------------------ */
/* 批量评测                                                            */
/* ------------------------------------------------------------------ */
async function renderEval() {
  state.datasets = await api("/api/v1/debug/eval/datasets");
  const runs = await api("/api/v1/debug/eval/runs?limit=10").catch(() => []);
  panel().innerHTML = `
    <div class="card" style="margin-bottom:16px">
      <div class="card-head"><h3>评测跑分</h3><span class="chip">FR-705 / FR-706</span></div>
      <div class="card-body">
        <form id="eval-form" class="row wrap" style="gap:12px;align-items:flex-end">
          <label class="field" style="min-width:240px">评测集
            <select id="eval-dataset">${state.datasets.length
              ? state.datasets.map((d) => `<option value="${escapeHtml(d.name)}">${escapeHtml(d.name)}（${d.cases} 条 · ${d.kind === "compliance" ? "合规" : "检索"}）</option>`).join("")
              : `<option value="">— 无可用评测集 —</option>`}</select>
          </label>
          <label class="field" style="width:94px">Top K<input type="number" id="eval-topk" value="5" min="1" max="20" /></label>
          <label class="field" style="width:110px">限制条数<input type="number" id="eval-limit" placeholder="全部" min="1" max="500" /></label>
          <label class="toggle"><input type="checkbox" id="eval-rerank" checked /> 重排</label>
          <button class="btn primary" type="submit" id="eval-run"${state.datasets.length ? "" : " disabled"}>开始跑分</button>
          <span class="small faint" id="eval-dataset-hint">${datasetHint() || (state.datasets.length ? "" : "eval/ 目录下没有 *.jsonl，或容器未挂载该目录")}</span>
        </form>
      </div>
    </div>
    <div id="eval-result"><div class="card"><div class="card-body muted">跑分结果会显示各通道对比、分组明细与未命中用例；并写入历史记录。</div></div></div>
    <div class="card" style="margin-top:16px">
      <div class="card-head"><h3>历史跑分</h3><span class="chip">每周回归 / 发版前</span></div>
      ${table(["时间", "数据集", "条数", "Recall@K", "MRR", "nDCG", "准确率", "耗时", "执行人"],
        runs.map((r) => [
          fmtTime(r.created_at), escapeHtml(r.dataset), r.cases,
          r.accuracy != null ? "—" : `<b>${fmtPct(r.recall)}</b>`,
          r.accuracy != null ? "—" : r.mrr?.toFixed(3), r.accuracy != null ? "—" : r.ndcg?.toFixed(3),
          r.accuracy != null ? fmtPct(r.accuracy) : "—", fmtMs(r.duration_ms), escapeHtml(r.created_by || ""),
        ]), "还没有跑分记录")}
    </div>`;

  $("#eval-dataset").addEventListener("change", () => ($("#eval-dataset-hint").textContent = datasetHint()));
  $("#eval-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const dsName = $("#eval-dataset")?.value || "";
    if (!dsName) {
      // 空值不发给后端：否则 FastAPI 会以 "String should have at least 1 character" 报错，看不懂
      $("#eval-result").innerHTML = errorBox("没有可用评测集：请确认宿主机 eval/ 目录存在 *.jsonl，且 compose 已挂载到容器 /eval");
      return;
    }
    const btn = $("#eval-run");
    btn.disabled = true;
    btn.textContent = "跑分中…";
    $("#eval-result").innerHTML = loading("正在逐题检索并计算指标，请稍候（几十秒）…");
    try {
      const limit = $("#eval-limit").value ? Number($("#eval-limit").value) : null;
      const report = await api("/api/v1/debug/eval/run", {
        method: "POST",
        body: {
          dataset: dsName,
          top_k: Number($("#eval-topk").value) || 5,
          limit,
          rerank: $("#eval-rerank").checked,
          include_details: true,
        },
      });
      renderEvalReport(report);
      const refreshed = await api("/api/v1/debug/eval/runs?limit=10");
      refreshRunsTable(refreshed);
    } catch (err) {
      $("#eval-result").innerHTML = errorBox(err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = "开始跑分";
    }
  });
}

function datasetHint() {
  const d = state.datasets.find((x) => x.name === $("#eval-dataset")?.value);
  if (!d) return "";
  return `语种：${Object.entries(d.languages || {}).map(([k, v]) => `${k}×${v}`).join(" / ")}`;
}

function refreshRunsTable(runs) {
  const card = panel().querySelector(".card:last-child");
  if (!card) return;
  card.innerHTML = `<div class="card-head"><h3>历史跑分</h3><span class="chip">每周回归 / 发版前</span></div>
    ${table(["时间", "数据集", "条数", "Recall@K", "MRR", "nDCG", "准确率", "耗时", "执行人"],
      runs.map((r) => [
        fmtTime(r.created_at), escapeHtml(r.dataset), r.cases,
        r.accuracy != null ? "—" : `<b>${fmtPct(r.recall)}</b>`,
        r.accuracy != null ? "—" : r.mrr?.toFixed(3), r.accuracy != null ? "—" : r.ndcg?.toFixed(3),
        r.accuracy != null ? fmtPct(r.accuracy) : "—", fmtMs(r.duration_ms), escapeHtml(r.created_by || ""),
      ]), "还没有跑分记录")}`;
}

function renderEvalReport(report) {
  if (report.kind === "compliance") {
    const s = report.summary;
    const fails = (report.details || []).filter((d) => !d.ok || !d.notice_ok);
    $("#eval-result").innerHTML = `
      <div class="grid cols-4" style="margin-bottom:14px">
        ${metric("准确率", fmtPct(s.accuracy), `${report.cases} 条`, s.accuracy >= 0.95 ? "ok" : "warn")}
        ${metric("拦截召回", s.block_recall != null ? fmtPct(s.block_recall) : "—", "该拦的拦住", s.redline_missed?.length ? "danger" : "ok")}
        ${metric("拦截精确率", s.block_precision != null ? fmtPct(s.block_precision) : "—", "不误拦", s.false_blocks?.length ? "warn" : "ok")}
        ${metric("提示附着", fmtPct(s.notice_accuracy), "提示级需带提示块")}
      </div>
      ${s.redline_missed?.length ? `<div class="warnbox" style="margin-bottom:12px"><b>红线漏拦：</b>${s.redline_missed.map(escapeHtml).join("、")}（发版阻断项）</div>` : `<div class="infobox" style="margin-bottom:12px">红线项 0 发生：expected=block 的用例全部被拦截。</div>`}
      ${s.false_blocks?.length ? `<div class="warnbox" style="margin-bottom:12px"><b>误拦截：</b>${s.false_blocks.map(escapeHtml).join("、")}</div>` : ""}
      <div class="card">
        <div class="card-head"><h3>未通过用例</h3><span class="chip">${fails.length} 条</span></div>
        ${table(["ID", "问题", "期望", "实际", "原因"], fails.map((d) => [
          escapeHtml(d.id), escapeHtml(d.query), `<span class="chip">${escapeHtml(d.expected)}</span>`,
          `<span class="chip ${d.actual === d.expected ? "ok" : "danger"}">${escapeHtml(d.actual)}</span>`,
          escapeHtml((d.hit_terms || []).join("、") || (d.notice_ok ? "" : "缺少提示块")),
        ]), "全部通过")}
      </div>`;
    return;
  }
  const sm = report.summary;
  const hybrid = sm.hybrid || {};
  const details = report.details || [];
  const misses = details.filter((d) => !d.channels?.hybrid?.hit);
  $("#eval-result").innerHTML = `
    <div class="grid cols-4" style="margin-bottom:14px">
      ${metric(`Recall@${report.top_k}`, fmtPct(hybrid.recall), `${report.cases} 条 · 命中率口径`, hybrid.recall >= 0.85 ? "ok" : "warn")}
      ${metric("MRR", hybrid.mrr?.toFixed(3), "首个命中位置倒数")}
      ${metric("nDCG", hybrid.ndcg?.toFixed(3), "排序质量")}
      ${metric("耗时", fmtMs(report.duration_ms), `运行 #${report.run_id ?? "—"}`)}
    </div>
    <div class="grid cols-2" style="align-items:start;margin-bottom:14px">
      <div class="card">
        <div class="card-head"><h3>通道对比</h3><span class="chip">重排前 vs 后</span></div>
        ${table(["通道", "Recall", "MRR", "nDCG"], ["vector", "bm25", "hybrid"].map((c) => [
          `<b>${c}</b>`, fmtPct(sm[c]?.recall), sm[c]?.mrr?.toFixed(3) ?? "—", sm[c]?.ndcg?.toFixed(3) ?? "—",
        ]))}
      </div>
      <div class="card">
        <div class="card-head"><h3>分组明细</h3><span class="chip">按语种 / 类别</span></div>
        ${table(["维度", "分组", "Recall", "MRR", "条数"], [
          ...Object.entries(sm.by_lang || {}).map(([k, v]) => ["语种", escapeHtml(k), fmtPct(v.recall), v.mrr.toFixed(3), v.cases]),
          ...Object.entries(sm.by_category || {}).map(([k, v]) => ["类别", escapeHtml(k), fmtPct(v.recall), v.mrr.toFixed(3), v.cases]),
        ])}
      </div>
    </div>
    <div class="card">
      <div class="card-head"><h3>未命中用例</h3><span class="chip">${misses.length} 条</span></div>
      ${table(["ID", "问题", "期望文档", "实际 Top 文档"], misses.map((d) => [
        escapeHtml(d.id), escapeHtml(d.query),
        escapeHtml((d.expected_docs || []).join("、") || (d.expected_keywords || []).join("、")),
        escapeHtml((d.top_docs || []).slice(0, 3).join(" / ")),
      ]), "全部命中")}
    </div>`;
}

/* ------------------------------------------------------------------ */
/* Badcase                                                             */
/* ------------------------------------------------------------------ */
async function renderBadcase() {
  const [stats, data] = await Promise.all([
    api("/api/v1/admin/badcases/stats"),
    api("/api/v1/admin/badcases?limit=50&with_snapshot=true"),
  ]);
  const s = stats.by_status || {};
  panel().innerHTML = `
    <div class="grid cols-4" style="margin-bottom:16px">
      ${metric("待处理", s.pending ?? 0, "点踩自动生成")}
      ${metric("处理中", (s.located || 0) + (s.fixing || 0), "已定位 + 修复中")}
      ${metric("待回归", s.regressed ?? 0, "已转评测样本")}
      ${metric("平均闭环", stats.avg_close_hours != null ? `${stats.avg_close_hours}h` : "—", "目标 ≤ 5 个工作日")}
    </div>
    <div class="grid cols-2" style="align-items:start;margin-bottom:16px">
      <div class="card">
        <div class="card-head"><h3>7 日知识缺口</h3><span class="chip">FR-804</span></div>
        <div class="card-body">
          ${stats.gaps_7d?.length
            ? stats.gaps_7d.map((g) => `<div class="spread" style="padding:6px 0;border-bottom:1px solid var(--border)"><span class="small">${escapeHtml(g.query)}</span><span class="chip danger">${g.count} 次</span></div>`).join("")
            : `<div class="small muted">暂无重复缺口（同一问题 7 日内 ≥2 次点踩会进入榜单）。</div>`}
        </div>
      </div>
      <div class="card">
        <div class="card-head"><h3>归因分布</h3><span class="chip">FR-803c</span></div>
        <div class="card-body">
          ${Object.keys(stats.by_attribution || {}).length
            ? Object.entries(stats.by_attribution).map(([k, v]) => `<div class="spread" style="padding:4px 0"><span class="small">${escapeHtml(attributionName(k))}</span><b>${v}</b></div>`).join("")
            : `<div class="small muted">还没有归因数据。</div>`}
        </div>
      </div>
    </div>
    <div class="card">
      <div class="card-head"><h3>工单列表</h3>
        <div class="row">
          <select id="bc-filter" style="width:150px">
            <option value="">全部状态</option>
            ${Object.entries(STATUS_NAMES).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}
          </select>
          <button class="btn sm" id="bc-refresh">刷新</button>
        </div>
      </div>
      <div id="bc-table">${badcaseTable(data.items)}</div>
    </div>`;
  $("#bc-refresh").addEventListener("click", renderBadcase);
  $("#bc-filter").addEventListener("change", async (ev) => {
    const status = ev.target.value;
    const res = await api(`/api/v1/admin/badcases?limit=50&with_snapshot=true${status ? `&status=${status}` : ""}`);
    $("#bc-table").innerHTML = badcaseTable(res.items);
    bindBadcaseRows();
  });
  bindBadcaseRows();
}

const STATUS_NAMES = { pending: "待处理", located: "已定位", fixing: "修复中", regressed: "待回归", closed: "已关闭", archived: "已归档" };
const ATTRS = {
  "": "未归因",
  retrieval_miss: "检索未命中",
  chunking: "切分不当",
  missing_knowledge: "知识缺失",
  hallucination: "生成幻觉",
  translation: "翻译错误",
  intent: "意图误判",
  permission: "权限误伤",
};
const attributionName = (k) => ATTRS[k] || k || "未归因";

function badcaseTable(items) {
  return table(["ID / 时间", "问题", "状态", "归因", "操作"], items.map((b) => [
    `<b>#${b.id}</b><div class="small faint">${fmtTime(b.created_at)}</div>`,
    `<div style="max-width:360px">${escapeHtml((b.query || "").slice(0, 90))}</div><div class="small faint">${escapeHtml(b.reason || "")}</div>`,
    `<select data-bc-status="${b.id}" style="width:110px">${Object.entries(STATUS_NAMES).map(([k, v]) => `<option value="${k}" ${b.status === k ? "selected" : ""}>${v}</option>`).join("")}</select>`,
    `<select data-bc-attr="${b.id}" style="width:130px">${Object.entries(ATTRS).map(([k, v]) => `<option value="${k}" ${(b.attribution || "") === k ? "selected" : ""}>${v}</option>`).join("")}</select>`,
    `<div class="row" style="gap:4px"><button class="btn sm" data-bc-view="${b.id}">详情</button><button class="btn sm primary" data-bc-eval="${b.id}">转评测</button></div>`,
  ]), "暂无工单");
}

function bindBadcaseRows() {
  const box = $("#bc-table");
  box.querySelectorAll("[data-bc-view]").forEach((btn) => btn.addEventListener("click", () => viewBadcase(Number(btn.dataset.bcView))));
  box.querySelectorAll("[data-bc-eval]").forEach((btn) => btn.addEventListener("click", () => toEvalModal(Number(btn.dataset.bcEval))));
  box.querySelectorAll("[data-bc-status]").forEach((sel) => sel.addEventListener("change", () => updateBadcase(Number(sel.dataset.bcStatus), { status: sel.value })));
  box.querySelectorAll("[data-bc-attr]").forEach((sel) => sel.addEventListener("change", () => updateBadcase(Number(sel.dataset.bcAttr), { attribution: sel.value || null })));
}

async function updateBadcase(id, patch) {
  try {
    await api(`/api/v1/admin/badcases/${id}`, { method: "PATCH", body: patch });
    toast("已更新", "ok");
  } catch (err) {
    toast(err.message, "err");
  }
}

async function viewBadcase(id) {
  const item = await api(`/api/v1/admin/badcases/${id}`);
  if (!item) {
    toast("工单不存在", "err");
    return;
  }
  const snap = item.snapshot || {};
  const refs = snap.refs || [];
  const root = $("#drawer-root");
  root.innerHTML = `
    <div class="drawer-mask"><div class="drawer">
      <div class="drawer-head"><div><h3>Badcase #${item.id}</h3><div class="small muted">${fmtTime(item.created_at)} · ${escapeHtml(item.status_name || "")}</div></div><button class="btn sm" id="bc-close">关闭</button></div>
      <div class="stack" style="gap:14px">
        <div class="card"><div class="card-head"><h3>问题</h3><span class="chip">${escapeHtml(item.reason || "未标注")}</span></div><div class="card-body">${escapeHtml(snap.question || item.query)}</div></div>
        <div class="card"><div class="card-head"><h3>回答</h3>${snap.request_id ? `<span class="chip mono">${escapeHtml(snap.request_id)}</span>` : ""}</div><div class="card-body"><div style="white-space:pre-wrap;max-height:260px;overflow:auto">${escapeHtml(snap.answer || item.answer)}</div></div></div>
        <div class="card"><div class="card-head"><h3>引用快照</h3><span class="chip">${refs.length} 条</span></div><div class="card-body stack" style="gap:8px">
          ${refs.length ? refs.map((r, i) => `<div class="hit"><div class="top"><span class="rank">${i + 1}</span><b>${escapeHtml(r.doc_name)}</b>${r.page_or_section ? `<span class="chip">${escapeHtml(r.page_or_section)}</span>` : ""}<span class="score">${r.score ?? ""}</span></div><div class="content" style="max-height:120px;white-space:pre-wrap">${escapeHtml(r.content_snapshot || "")}</div></div>`).join("") : `<div class="small muted">无引用（可能是 SQL 通道或空答）。</div>`}
        </div></div>
        <div class="card"><div class="card-head"><h3>检索过程快照</h3><span class="chip">复现用</span></div><div class="card-body"><pre class="sql" style="max-height:240px">${escapeHtml(JSON.stringify(snap.retrieval || {}, null, 2))}</pre></div></div>
        <div class="card"><div class="card-head"><h3>处理备注</h3></div><div class="card-body">
          <textarea id="bc-note" placeholder="修复说明 / 根因 / 回归结果">${escapeHtml(item.note || "")}</textarea>
          <div class="row" style="justify-content:flex-end;margin-top:8px"><button class="btn primary" id="bc-note-save">保存备注</button></div>
        </div></div>
      </div>
    </div></div>`;
  const close = () => (root.innerHTML = "");
  root.querySelector("#bc-close").addEventListener("click", close);
  root.querySelector(".drawer-mask").addEventListener("click", (ev) => { if (ev.target.classList.contains("drawer-mask")) close(); });
  root.querySelector("#bc-note-save").addEventListener("click", async () => {
    await updateBadcase(id, { note: root.querySelector("#bc-note").value });
    close();
  });
}

function toEvalModal(id) {
  const root = $("#modal-root");
  root.innerHTML = `
    <div class="modal-mask"><div class="modal">
      <div class="spread" style="margin-bottom:12px"><h3>转评测样本 #${id}</h3><button class="btn sm" data-close>关闭</button></div>
      <div class="stack" style="gap:10px">
        <label class="field">期望命中文档（逗号分隔，可留空进草稿）
          <input type="text" id="te-docs" placeholder="例如：法兰规格书_zh.txt" />
        </label>
        <label class="field">期望关键词（逗号分隔，可留空）
          <input type="text" id="te-kw" placeholder="例如：304 / 316L" />
        </label>
        <div class="grid cols-2" style="gap:10px">
          <label class="field">数据集<input type="text" id="te-dataset" value="draft_badcases" /></label>
          <label class="field">类别<input type="text" id="te-category" value="badcase" /></label>
        </div>
        <div class="infobox small">不填期望文档时会写入 draft_badcases.jsonl，等修复后补全再并入正式评测集。</div>
        <div class="row" style="justify-content:flex-end"><button class="btn" data-close>取消</button><button class="btn primary" id="te-submit">写入评测样本</button></div>
      </div>
    </div></div>`;
  const close = () => (root.innerHTML = "");
  root.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", close));
  root.querySelector("#te-submit").addEventListener("click", async (ev) => {
    ev.target.disabled = true;
    try {
      const res = await api(`/api/v1/admin/badcases/${id}/to-eval`, {
        method: "POST",
        body: {
          expected_docs: splitList(root.querySelector("#te-docs").value),
          expected_keywords: splitList(root.querySelector("#te-kw").value),
          dataset: root.querySelector("#te-dataset").value.trim() || "draft_badcases",
          category: root.querySelector("#te-category").value.trim() || "badcase",
        },
      });
      toast(`已写入 ${res.file}（${res.case_id}）`, "ok");
      close();
      renderBadcase();
    } catch (err) {
      toast(err.message, "err");
      ev.target.disabled = false;
    }
  });
}

const splitList = (text) => text.split(/[,，\n]/).map((s) => s.trim()).filter(Boolean);

/* ------------------------------------------------------------------ */
/* 合规                                                                */
/* ------------------------------------------------------------------ */
async function renderCompliance() {
  const [summary, terms, rules, events] = await Promise.all([
    api("/api/v1/compliance/summary"),
    api("/api/v1/compliance/terms"),
    api("/api/v1/compliance/rules"),
    api("/api/v1/compliance/events?limit=30"),
  ]);
  panel().innerHTML = `
    <div class="grid cols-4" style="margin-bottom:16px">
      ${metric("事件总数", summary.events_total, `留存 ${summary.retention_days} 天`)}
      ${metric("拦截", summary.blocks, "block", summary.blocks ? "warn" : "")}
      ${metric("提示", summary.annotates, "annotate")}
      ${metric("待复核词条", summary.pending_terms, "双人复核后生效", summary.pending_terms ? "warn" : "ok")}
    </div>
    <div class="grid cols-2" style="align-items:start;margin-bottom:16px">
      <div class="card">
        <div class="card-head"><h3>新增词条</h3><span class="chip">提交后需他人复核</span></div>
        <div class="card-body">
          <form id="term-form" class="stack" style="gap:9px">
            <div class="grid cols-2" style="gap:9px">
              <label class="field">词条<input type="text" id="term-text" placeholder="例如：伊朗" required /></label>
              <label class="field">类型
                <select id="term-type">
                  <option value="sanctioned_country">受制裁国别</option>
                  <option value="controlled_item">管制物项</option>
                  <option value="restricted_org">受限机构</option>
                  <option value="evasion_phrase">规避话术</option>
                </select>
              </label>
            </div>
            <div class="grid cols-2" style="gap:9px">
              <label class="field">级别
                <select id="term-sev"><option value="annotate">提示</option><option value="block">拦截</option></select>
              </label>
              <label class="field">变体（逗号分隔）<input type="text" id="term-variants" placeholder="Iran,Irán" /></label>
            </div>
            <label class="field">提示语<textarea id="term-notice" placeholder="风险 + 建议，将原样展示给用户"></textarea></label>
            <button class="btn primary" type="submit">提交（草稿）</button>
          </form>
        </div>
      </div>
      <div class="card">
        <div class="card-head"><h3>规则</h3><span class="chip">国别 × 物项 × 意图</span></div>
        ${table(["规则", "动作", "优先级", "状态", ""], rules.map((r) => [
          `<b>${escapeHtml(r.rule_name)}</b><div class="small faint">${escapeHtml((r.countries || []).join(","))} × ${escapeHtml((r.items || []).join(","))}</div>`,
          `<span class="chip ${r.action === "block" ? "danger" : "warn"}">${r.action}</span>`,
          r.priority, r.status === "active" ? `<span class="chip ok">生效 v${r.version}</span>` : `<span class="chip">草稿</span>`,
          r.status === "draft" ? `<button class="btn sm" data-rule-approve="${r.id}">复核生效</button>` : "",
        ]))}
      </div>
    </div>
    <div class="card" style="margin-bottom:16px">
      <div class="card-head"><h3>词库</h3><span class="chip">${terms.length} 条</span></div>
      ${table(["词条", "类型", "级别", "状态", "版本", "操作"], terms.map((t) => [
        `<b>${escapeHtml(t.term)}</b><div class="small faint">${escapeHtml((t.variants || []).join(" / "))}</div>`,
        escapeHtml(t.term_type), `<span class="chip ${t.severity === "block" ? "danger" : "warn"}">${t.severity}</span>`,
        t.status === "active" ? `<span class="chip ok">生效</span>` : `<span class="chip">草稿</span>`,
        `v${t.version}`,
        t.status === "draft" ? `<button class="btn sm" data-term-approve="${t.id}">复核生效</button>` : "",
      ]))}
    </div>
    <div class="card">
      <div class="card-head"><h3>合规事件</h3><span class="chip">摘要脱敏</span></div>
      ${table(["时间", "层", "动作", "命中", "说明"], events.map((e) => [
        fmtTime(e.created_at), `<span class="chip">${escapeHtml(e.layer)}</span>`,
        `<span class="chip ${e.action === "block" ? "danger" : "warn"}">${escapeHtml(e.action)}</span>`,
        escapeHtml((e.hit_terms || []).join("、") || e.region_or_item || ""),
        escapeHtml((e.detail || "").slice(0, 80)),
      ]), "暂无事件")}
    </div>`;

  $("#term-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    try {
      await api("/api/v1/compliance/terms", {
        method: "POST",
        body: {
          term: $("#term-text").value.trim(),
          term_type: $("#term-type").value,
          severity: $("#term-sev").value,
          variants: splitList($("#term-variants").value),
          notice: $("#term-notice").value.trim() || null,
        },
      });
      toast("已提交草稿，需另一位合规管理员复核后生效", "ok");
      renderCompliance();
    } catch (err) {
      toast(err.message, "err");
    }
  });
  panel().querySelectorAll("[data-term-approve]").forEach((btn) => btn.addEventListener("click", async () => {
    try {
      await api(`/api/v1/compliance/terms/${btn.dataset.termApprove}/approve`, { method: "POST" });
      toast("已生效", "ok");
      renderCompliance();
    } catch (err) {
      toast(err.message, "err");
    }
  }));
  panel().querySelectorAll("[data-rule-approve]").forEach((btn) => btn.addEventListener("click", async () => {
    try {
      await api(`/api/v1/compliance/rules/${btn.dataset.ruleApprove}/approve`, { method: "POST" });
      toast("已生效", "ok");
      renderCompliance();
    } catch (err) {
      toast(err.message, "err");
    }
  }));
}

/* ------------------------------------------------------------------ */
/* SQL 通道                                                            */
/* ------------------------------------------------------------------ */
async function renderSql() {
  const templates = await api("/api/v1/sql/templates");
  panel().innerHTML = `
    <div class="card" style="margin-bottom:16px">
      <div class="card-head"><h3>SQL 通道测试</h3><span class="chip">FR-703 · 只读模板</span></div>
      <div class="card-body">
        <form id="sql-form" class="row wrap" style="gap:10px;align-items:flex-end">
          <label class="field grow" style="max-width:520px">自然语言问题
            <input type="text" id="sql-query" value="按今天汇率把1000欧元换算成美元" />
          </label>
          <button class="btn primary" type="submit" id="sql-run">路由并执行</button>
          <span class="small faint">只会选已注册模板，参数严格校验；无自由 SQL 入口。</span>
        </form>
        <div id="sql-result" style="margin-top:12px"></div>
      </div>
    </div>
    <div class="card">
      <div class="card-head"><h3>已注册模板</h3><span class="chip">${templates.length} 个</span></div>
      ${table(["模板", "说明", "只读视图", "最大行数", "可见角色", "版本"], templates.map((t) => [
        `<b class="mono">${escapeHtml(t.template_id)}</b>`,
        `<div style="max-width:380px">${escapeHtml(t.name)}<div class="small faint">${escapeHtml(t.description || "")}</div></div>`,
        `<span class="mono">${escapeHtml(t.view_name)}</span>`, t.max_rows,
        escapeHtml((t.visible_roles || []).join(",")), `v${t.version}`,
      ]))}
    </div>`;
  $("#sql-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const btn = $("#sql-run");
    btn.disabled = true;
    btn.textContent = "执行中…";
    try {
      const res = await api("/api/v1/sql/route", { method: "POST", body: { query: $("#sql-query").value.trim() } });
      renderSqlResult(res);
    } catch (err) {
      $("#sql-result").innerHTML = `<div class="warnbox">${escapeHtml(err.message)}</div>`;
    } finally {
      btn.disabled = false;
      btn.textContent = "路由并执行";
    }
  });
}

function renderSqlResult(res) {
  if (!res.routed) {
    $("#sql-result").innerHTML = `<div class="infobox">${escapeHtml(res.message || "未匹配到模板，会走文档检索。")}</div>`;
    return;
  }
  const rows = res.rows || [];
  const cols = rows.length ? Object.keys(rows[0]) : [];
  $("#sql-result").innerHTML = `
    <div class="row wrap" style="gap:8px;margin-bottom:8px">
      <span class="chip brand">${escapeHtml(res.template_id)}</span>
      <span class="chip ${res.status === "ok" ? "ok" : "danger"}">${escapeHtml(res.status || "")}</span>
      ${res.error_code ? `<span class="chip danger">${escapeHtml(res.error_code)}</span>` : ""}
      ${res.elapsed_ms !== undefined ? `<span class="chip">${fmtMs(res.elapsed_ms)}</span>` : ""}
      ${res.degraded ? `<span class="chip warn">已降级到文档检索</span>` : ""}
    </div>
    <div class="small muted" style="margin-bottom:6px">槽位参数：<span class="mono">${escapeHtml(JSON.stringify(res.params || {}))}</span></div>
    ${res.rendered_sql ? `<pre class="sql">${escapeHtml(res.rendered_sql)}</pre>` : ""}
    ${rows.length ? `<div class="table-wrap" style="margin-top:8px"><table><thead><tr>${cols.map((c) => `<th>${escapeHtml(c)}</th>`).join("")}</tr></thead><tbody>${rows.slice(0, 20).map((r) => `<tr>${cols.map((c) => `<td>${escapeHtml(r[c] ?? "")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>` : `<div class="small muted" style="margin-top:8px">${escapeHtml(res.message || "无记录")}</div>`}`;
}

/* ------------------------------------------------------------------ */
/* 监控                                                                */
/* ------------------------------------------------------------------ */
async function renderMonitor() {
  if (monitorTimer) clearInterval(monitorTimer);
  const [summary, alerts] = await Promise.all([
    api("/api/v1/monitor/summary"),
    api("/api/v1/monitor/alerts"),
  ]);
  const m = summary.metrics;
  const lat = m.latency_ms || {};
  panel().innerHTML = `
    <div class="row" style="justify-content:space-between;margin-bottom:14px">
      <div class="row"><span class="chip ${alerts.alerts.length ? "warn" : "ok"}">${alerts.alerts.length ? `${alerts.alerts.length} 条告警` : "无告警"}</span>
        <span class="small faint">更新于 ${new Date().toLocaleTimeString()}</span></div>
      <div class="row">
        <label class="toggle"><input type="checkbox" id="mon-auto" checked /> 10s 自动刷新</label>
        <button class="btn sm" id="mon-refresh">刷新</button>
        <a class="btn sm" href="/metrics" target="_blank">Prometheus /metrics</a>
      </div>
    </div>
    ${alerts.alerts.map((a) => `<div class="warnbox small" style="margin-bottom:8px"><b>${a.level}</b> · ${escapeHtml(a.message)}</div>`).join("")}
    <div class="grid cols-4" style="margin-bottom:16px">
      ${metric("完成率", fmtPct(m.rates.completion), "≥ 99%", m.rates.completion < 0.97 ? "warn" : "ok")}
      ${metric("空答率", fmtPct(m.rates.empty_answer), "≤ 8%", m.rates.empty_answer > 0.15 ? "danger" : "")}
      ${metric("降级率", fmtPct(m.rates.degraded), "≤ 2%", m.rates.degraded > 0.02 ? "warn" : "ok")}
      ${metric("合规拦截率", fmtPct(m.rates.compliance_block), "观测项")}
    </div>
    <div class="grid cols-2" style="align-items:start;margin-bottom:16px">
      <div class="card"><div class="card-head"><h3>漏斗</h3><span class="chip">${m.requests_accepted} 请求</span></div><div class="card-body">${funnelRows(m.funnel)}</div></div>
      <div class="card"><div class="card-head"><h3>阶段延迟</h3><span class="chip">毫秒</span></div>
        ${table(["阶段", "P50", "P95", "P99", "均值"], Object.entries(lat).map(([k, v]) => [
          `<b>${escapeHtml(k)}</b>`, v.p50, v.p95, v.p99, v.avg,
        ]), "暂无样本")}
      </div>
    </div>
    <div class="grid cols-2" style="align-items:start">
      <div class="card"><div class="card-head"><h3>业务存量</h3></div>
        <div class="card-body"><div class="kv">
          <dt>文档 / 分片</dt><dd>${summary.inventory.documents} / ${summary.inventory.chunks}</dd>
          <dt>会话 / 消息</dt><dd>${summary.inventory.sessions} / ${summary.inventory.messages}</dd>
          <dt>今日合规事件</dt><dd>${summary.today.compliance_events}</dd>
          <dt>今日 SQL 执行</dt><dd>${summary.today.sql_execs}（拒绝 ${summary.today.sql_denied}）</dd>
          <dt>今日新增 badcase</dt><dd>${summary.today.badcases}</dd>
        </div></div>
      </div>
      <div class="card"><div class="card-head"><h3>错误码</h3><span class="chip">${Object.values(m.errors || {}).reduce((a, b) => a + b, 0)} 次</span></div>
        <div class="card-body">${Object.keys(m.errors || {}).length ? Object.entries(m.errors).map(([k, v]) => `<div class="spread" style="padding:4px 0"><span class="mono">${escapeHtml(k)}</span><b>${v}</b></div>`).join("") : `<div class="small muted">暂无错误。</div>`}</div>
      </div>
    </div>`;
  $("#mon-refresh").addEventListener("click", renderMonitor);
  $("#mon-auto").addEventListener("change", (ev) => {
    if (monitorTimer) clearInterval(monitorTimer);
    if (ev.target.checked) monitorTimer = setInterval(() => { if (state.tab === "monitor") renderMonitor(); }, 10000);
  });
}

/* ------------------------------------------------------------------ */
/* 链路追踪（11-3）：Langfuse trace 的只读镜像                          */
/* 数据源是 Langfuse 公共 API，后端代理转发；未配置/未启动时给空状态而不是报错 */
/* ------------------------------------------------------------------ */
const traceState = {
  hours: 24, items: [], enabled: true, reachable: true, reason: "",
  selId: null, selNode: null, detail: null,
  branch: "", q: "", slow: false,
};

const langfuseUiUrl = () => `${location.protocol}//${location.hostname}:13000`;

function fmtDur(s) {
  if (s === null || s === undefined || Number.isNaN(s)) return "—";
  return s < 1 ? `${Math.round(s * 1000)}ms` : `${s.toFixed(2)}s`;
}
const latKind = (v) => (v < 5 ? "fast" : v < 10 ? "mid" : "slow");

function traceEmpty() {
  if (!traceState.enabled) {
    return `<div class="tr-empty"><div class="big">◎</div><h3>追踪服务未配置</h3>
      <p>${escapeHtml(traceState.reason || "未配置 Langfuse 密钥")}</p>
      <p class="small faint">在 backend/.env 填好 LANGFUSE_PUBLIC_KEY / SECRET_KEY 并重启 api 后，这里会自动出数。</p></div>`;
  }
  if (!traceState.reachable) {
    return `<div class="tr-empty"><div class="big">◌</div><h3>追踪服务未启动</h3>
      <p>${escapeHtml(traceState.reason || "连不上 Langfuse")}</p>
      <p class="small faint">启动命令：<code>docker compose --profile obs up -d</code><br/>
      历史数据仍在数据库里，启动后自动恢复，不会丢。</p></div>`;
  }
  return `<div class="tr-empty"><div class="big">∅</div><h3>这段时间没有调用</h3>
    <p>换个时间范围，或先去应用里问一个问题。</p></div>`;
}

async function renderTrace() {
  panel().innerHTML = `
    <div class="row" style="justify-content:space-between;margin-bottom:12px;flex-wrap:wrap;gap:8px">
      <div class="row" style="flex-wrap:wrap">
        <select id="tr-hours">
          <option value="1">最近 1 小时</option>
          <option value="6">最近 6 小时</option>
          <option value="24" selected>最近 24 小时</option>
          <option value="168">最近 7 天</option>
        </select>
        <select id="tr-branch">
          <option value="">全部分支</option>
          <option value="kb">知识库问答（kb）</option>
          <option value="sql">SQL 通道（sql）</option>
          <option value="blocked">拒答（blocked）</option>
        </select>
        <input class="tr-search" id="tr-q" placeholder="搜索问题 / trace id" />
        <label class="toggle"><input type="checkbox" id="tr-slow" /> 仅 &gt; 10s</label>
      </div>
      <div class="row">
        <span class="small faint" id="tr-count"></span>
        <button class="btn sm" id="tr-refresh">刷新</button>
        <a class="btn sm" href="${langfuseUiUrl()}" target="_blank" rel="noopener">在 Langfuse 打开</a>
      </div>
    </div>
    <div class="tr-split">
      <div class="card">
        <div class="card-head"><h3>最近调用</h3><span class="chip" id="tr-total">—</span></div>
        <div class="tr-list" id="tr-list">${loading("加载中…")}</div>
      </div>
      <div class="card" id="tr-detail">${loading("选择左侧任意一条调用")}</div>
    </div>`;

  $("#tr-hours").addEventListener("change", (e) => { traceState.hours = Number(e.target.value); loadTraceList(); });
  $("#tr-branch").addEventListener("change", (e) => { traceState.branch = e.target.value; renderTraceList(); });
  $("#tr-slow").addEventListener("change", (e) => { traceState.slow = e.target.checked; renderTraceList(); });
  $("#tr-q").addEventListener("input", (e) => { traceState.q = e.target.value; renderTraceList(); });
  $("#tr-refresh").addEventListener("click", () => loadTraceList());

  await loadTraceList();
}

async function loadTraceList() {
  $("#tr-list").innerHTML = loading("加载中…");
  try {
    const data = await api(`/api/v1/admin/traces?hours=${traceState.hours}&limit=50`);
    traceState.items = data.items || [];
    traceState.enabled = data.enabled !== false;
    traceState.reachable = data.reachable !== false;
    traceState.reason = data.reason || "";
  } catch (err) {
    traceState.items = [];
    traceState.enabled = true;
    traceState.reachable = false;
    traceState.reason = err.message;
  }
  renderTraceList();

  const list = filteredTraces();
  if (!list.length) {
    $("#tr-total").textContent = "0 条";
    $("#tr-detail").innerHTML = traceEmpty();
    traceState.detail = null;
    return;
  }
  const pick = list.find((x) => x.id === traceState.selId) || list[0];
  await selectTrace(pick.id);
}

function filteredTraces() {
  const q = traceState.q.trim().toLowerCase();
  return traceState.items.filter((x) => {
    if (traceState.branch && (x.branch || "") !== traceState.branch) return false;
    if (traceState.slow && (x.latency || 0) < 10) return false;
    if (q) {
      const hay = `${x.question || ""} ${x.id || ""}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

function renderTraceList() {
  const items = filteredTraces();
  $("#tr-total").textContent = `${items.length} 条`;
  $("#tr-count").textContent = traceState.items.length ? `最近 ${traceState.items.length} 条中筛选` : "";

  if (!traceState.items.length) {
    $("#tr-list").innerHTML = traceEmpty();
    return;
  }
  if (!items.length) {
    $("#tr-list").innerHTML = `<div class="card-body small muted">没有符合条件的调用。</div>`;
    return;
  }

  $("#tr-list").innerHTML = items.map((x) => {
    const t = x.timestamp ? new Date(x.timestamp) : null;
    const hhmmss = t ? t.toLocaleTimeString("zh-CN", { hour12: false }) : "—";
    return `<div class="tr-item ${x.id === traceState.selId ? "on" : ""}" data-id="${escapeHtml(x.id)}">
      <div class="l1">
        <span class="tm">${hhmmss}</span>
        ${x.level && x.level !== "DEFAULT" ? `<span class="chip ${x.level === "ERROR" ? "danger" : "warn"}">${escapeHtml(x.level)}</span>` : ""}
        <span class="lat ${latKind(x.latency || 0)}">${(x.latency || 0).toFixed(1)}s</span>
      </div>
      <div class="q">${escapeHtml(x.question || "(无问题文本)")}</div>
      <div class="l3">
        ${x.branch ? `<span class="chip ${x.branch === "kb" ? "brand" : x.branch === "sql" ? "ok" : "warn"}">${escapeHtml(x.branch)}</span>` : ""}
        ${x.refs ? `<span class="chip">${x.refs} 引用</span>` : ""}
        <span class="chip">${x.nodes || 0} 节点</span>
      </div>
    </div>`;
  }).join("");

  $("#tr-list").querySelectorAll(".tr-item").forEach((n) => {
    n.addEventListener("click", () => selectTrace(n.dataset.id));
  });
}

async function selectTrace(id) {
  if (!id) return;
  traceState.selId = id;
  traceState.selNode = null;
  document.querySelectorAll(".tr-item").forEach((n) => n.classList.toggle("on", n.dataset.id === id));
  const box = $("#tr-detail");
  box.innerHTML = loading("读取链路详情…");
  try {
    const d = await api(`/api/v1/admin/traces/${encodeURIComponent(id)}`);
    traceState.detail = d;
    traceState.reachable = d.reachable !== false;
    traceState.reason = d.reason || traceState.reason;
    traceState.enabled = d.enabled !== false;
    renderTraceDetail();
  } catch (err) {
    box.innerHTML = `<div class="card-body"><div class="warnbox">${escapeHtml(err.message)}</div></div>`;
  }
}

/* 把扁平的 observation 列表按 parentId 组织成 DFS 顺序，并算好层级深度 */
function buildTraceTree(obs) {
  const byId = new Map(obs.map((o) => [o.id, o]));
  const sorted = [...obs].sort((a, b) => String(a.startTime || "").localeCompare(String(b.startTime || "")));
  const buckets = new Map();
  for (const o of sorted) {
    const key = o.parentId && byId.has(o.parentId) ? o.parentId : "__root__";
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key).push(o);
  }
  const out = [];
  const walk = (key, depth) => {
    for (const o of buckets.get(key) || []) {
      out.push({ ...o, depth });
      walk(o.id, depth + 1);
    }
  };
  walk("__root__", 0);
  return out;
}

function traceKind(o, depth) {
  if (o.type === "GENERATION") return "c-gen";
  if (String(o.name || "").toLowerCase().startsWith("compliance")) return "c-comp";
  return depth > 0 ? "c-child" : "c-span";
}

function tickStep(total) {
  if (total <= 2) return 0.5;
  if (total <= 6) return 1;
  if (total <= 16) return 2;
  if (total <= 40) return 5;
  if (total <= 90) return 10;
  if (total <= 300) return 30;
  return 60;
}

const fmtTick = (v) => (v === 0 ? "0s" : v < 1 ? `${v.toFixed(1)}s` : `${Math.round(v)}s`);

/* input/output 可能是 JSON 字符串，也可能是对象，统一美化 */
function prettyValue(v) {
  if (v === null || v === undefined) return "—";
  if (typeof v === "string") {
    const s = v.trim();
    if (s.startsWith("{") || s.startsWith("[")) {
      try { return JSON.stringify(JSON.parse(s), null, 2); } catch { return v; }
    }
    return v;
  }
  try { return JSON.stringify(v, null, 2); } catch { return String(v); }
}

function renderTraceDetail() {
  const box = $("#tr-detail");
  const d = traceState.detail;
  if (!d || !d.trace) { box.innerHTML = traceEmpty(); return; }
  if (d.reachable === false) { box.innerHTML = traceEmpty(); return; }

  const t = d.trace;
  const nodes = buildTraceTree(d.observations || []);
  const t0 = Date.parse(t.timestamp || "") || 0;

  const offOf = (o) => Math.max(0, (Date.parse(o.startTime || "") - t0) / 1000 || 0);
  const durOf = (o) => {
    if (o.latency !== null && o.latency !== undefined) return Math.max(0, o.latency);
    const s = Date.parse(o.startTime || "");
    const e = Date.parse(o.endTime || "");
    return Number.isNaN(s) || Number.isNaN(e) ? 0 : Math.max(0, (e - s) / 1000);
  };

  const total = Math.max(
    t.latency || 0,
    ...nodes.map((o) => offOf(o) + durOf(o)),
    0.001,
  );

  const step = tickStep(total);
  const ticks = [];
  for (let v = 0; v <= total + 0.0001; v += step) ticks.push(v);

  const grid = ticks.map((v) => `<div class="gl" style="left:${(v / total) * 100}%"></div>`).join("");
  const axis = ticks.map((v) => `<div class="tk" style="left:${(v / total) * 100}%">${fmtTick(v)}</div>`).join("");

  const gen = nodes.find((o) => o.type === "GENERATION");
  const ttftLine = gen
    ? `<div class="ttft" style="left:${(offOf(gen) / total) * 100}%"><span>首字 ${fmtDur(offOf(gen))}</span></div>`
    : "";

  const usage = gen && gen.usage ? gen.usage : null;
  const tokens = usage ? (usage.total || ((usage.input || 0) + (usage.output || 0))) : null;

  const rows = nodes.map((o) => {
    const off = offOf(o);
    const dur = durOf(o);
    const left = (off / total) * 100;
    const width = Math.max(0.15, (dur / total) * 100);
    const kind = traceKind(o, o.depth);
    const inside = width > 13 || left + width > 86;
    const label = inside ? `<span>${fmtDur(dur)}</span>` : "";
    const outside = inside ? "" : `<span class="out">${fmtDur(dur)}</span>`;
    const sel = traceState.selNode === o.id;
    return `
      <div class="tw-row ${sel ? "sel" : ""}" data-nid="${escapeHtml(o.id)}">
        <div class="tw-label" style="padding-left:${12 + o.depth * 16}px">
          <span class="br">${o.depth ? "└" : ""}</span>
          <span class="dot ${kind}"></span>
          <span class="nm ${o.depth === 0 ? "root" : ""}">${escapeHtml(o.name || "?")}</span>
          ${o.type === "GENERATION" ? '<span class="gen-tag">GEN</span>' : ""}
        </div>
        <div class="tw-track">
          <div class="tw-bar ${kind}" style="left:${left}%;width:${width}%">${label}${outside}</div>
        </div>
      </div>
      ${sel ? renderNodeDetail(o) : ""}`;
  }).join("");

  const top = [...nodes].sort((a, b) => durOf(b) - durOf(a)).slice(0, 5);
  const maxDur = top.length ? durOf(top[0]) || 1 : 1;
  const rank = top.map((o) => `
    <div class="r">
      <span class="n">${escapeHtml(o.name || "?")}</span>
      <div class="t"><i class="${o.type === "GENERATION" ? "hot" : ""}" style="width:${(durOf(o) / maxDur) * 100}%"></i></div>
      <b>${fmtDur(durOf(o))}</b>
    </div>`).join("");

  const answer = typeof t.output === "string" ? t.output : prettyValue(t.output);
  const listed = traceState.items.find((x) => x.id === t.id);
  const questionText = (listed && listed.question)
    || (typeof t.input === "string" ? t.input : "")
    || "（无问题文本）";

  box.innerHTML = `
    <div class="tr-dhead">
      <div class="q">${escapeHtml(questionText)}</div>
      <div class="meta">
        <span class="it mono">${escapeHtml(t.id || "")}</span>
        <span class="it">${t.timestamp ? new Date(t.timestamp).toLocaleString("zh-CN", { hour12: false }) : "—"}</span>
        <span class="it">总耗时 <b>${fmtDur(t.latency)}</b></span>
        ${tokens ? `<span class="it">${tokens} token</span>` : ""}
        ${t.refs ? `<span class="it">${t.refs} 引用</span>` : ""}
        ${t.branch ? `<span class="it">${escapeHtml(t.branch)}</span>` : ""}
        <span class="it">${nodes.length} 节点</span>
      </div>
    </div>
    <div class="tw-legend">
      <span><i style="background:#818cf8"></i>span</span>
      <span><i style="background:#c7cbf7"></i>子阶段</span>
      <span><i style="background:#0d9488"></i>generation</span>
      <span><i style="background:#b8bfd4"></i>合规</span>
      <span class="sp">点节点看输入 / 输出</span>
    </div>
    <div class="tw-axis">
      <div class="in">调用链</div>
      <div class="ticks">${axis}</div>
    </div>
    <div class="tw-body">
      <div class="tw-grid">${grid}${ttftLine}</div>
      ${rows}
    </div>
    <div class="tr-foot">
      <div class="card" style="box-shadow:none">
        <div class="card-head"><h3>答案</h3><span class="chip">${(answer || "").length} 字</span></div>
        <div class="tr-ans">${escapeHtml(answer || "（无输出）")}</div>
      </div>
      <div class="card" style="box-shadow:none">
        <div class="card-head"><h3>阶段耗时排行</h3><span class="chip">Top 5</span></div>
        <div class="tr-rank">${rank || '<div class="small muted">无节点</div>'}</div>
      </div>
    </div>`;

  box.querySelectorAll(".tw-row").forEach((n) => {
    n.addEventListener("click", () => {
      traceState.selNode = traceState.selNode === n.dataset.nid ? null : n.dataset.nid;
      renderTraceDetail();
    });
  });
}

function renderNodeDetail(o) {
  const head = `
    <div class="hd">
      <b>${escapeHtml(o.name || "?")}</b>
      <span class="chip">${escapeHtml(o.type || "")}</span>
      <span class="chip">${fmtDur(o.latency)}</span>
      ${o.model ? `<span class="chip brand">${escapeHtml(o.model)}</span>` : ""}
      ${o.usage && o.usage.total ? `<span class="chip">${o.usage.total} tok</span>` : ""}
      ${o.level && o.level !== "DEFAULT" ? `<span class="chip danger">${escapeHtml(o.level)}</span>` : ""}
    </div>`;
  return `<div class="tw-detail">${head}
    <div class="cols">
      <div class="col"><h4>Input</h4><pre>${escapeHtml(prettyValue(o.input))}</pre></div>
      <div class="col"><h4>Output</h4><pre>${escapeHtml(prettyValue(o.output))}</pre></div>
    </div>
  </div>`;
}

/* ------------------------------------------------------------------ */
/* 路由                                                                */
/* ------------------------------------------------------------------ */
const TABS = [
  { id: "overview", name: "概览与能力", render: renderOverview, perms: ["audit:read", "kb:manage", "debug:retrieval"] },
  { id: "debug", name: "召回测试台", render: renderDebug, perms: ["debug:retrieval"] },
  { id: "eval", name: "批量评测", render: renderEval, perms: ["debug:retrieval"] },
  { id: "badcase", name: "Badcase", render: renderBadcase, perms: ["audit:read"] },
  { id: "compliance", name: "合规", render: renderCompliance, perms: ["compliance:manage", "audit:read"] },
  { id: "sql", name: "SQL 通道", render: renderSql, perms: ["debug:retrieval", "sql:manage"] },
  { id: "monitor", name: "监控看板", render: renderMonitor, perms: ["audit:read"] },
  { id: "trace", name: "链路追踪", render: renderTrace, perms: ["audit:read"] },
  { id: "llm", name: "模型配置", render: renderLlm, perms: ["admin:users"] },
];

const visibleTabs = () => TABS.filter((t) => t.perms.some((p) => can(p)));

function renderTabs() {
  const tabs = visibleTabs();
  $("#tabs").innerHTML = tabs.map((t) => `<button data-tab="${t.id}" class="${state.tab === t.id ? "active" : ""}">${t.name}</button>`).join("");
  $("#tabs").querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));
}

async function switchTab(id) {
  state.tab = id;
  if (monitorTimer) { clearInterval(monitorTimer); monitorTimer = null; }
  renderTabs();
  const tab = TABS.find((t) => t.id === id);
  if (!tab) return;
  panel().innerHTML = loading();
  try {
    await tab.render();
  } catch (err) {
    panel().innerHTML = errorBox(err.status === 403 ? "当前账号没有该功能的权限。" : err.message);
  }
}

async function main() {
  const u = await boot("admin");
  if (!u) return;
  const tabs = visibleTabs();
  if (!tabs.length) {
    panel().innerHTML = errorBox("当前账号没有任何管理权限。");
    return;
  }
  state.tab = tabs[0].id;
  renderTabs();
  await switchTab(state.tab);
}

main();

/* ------------------------------------------------------------------ */
/* 模型配置：多提供商管理 + 故障切换                                     */
/* ------------------------------------------------------------------ */

const LLM_PROVIDERS = {
  minimax: { label: "MiniMax", base_url: "https://api.minimaxi.com/v1", model: "MiniMax-M3.1-Flash" },
  deepseek: { label: "DeepSeek", base_url: "https://api.deepseek.com", model: "deepseek-chat" },
  qwen: { label: "阿里云 Qwen", base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "qwen-plus" },
  openai: { label: "OpenAI", base_url: "https://api.openai.com/v1", model: "gpt-4o-mini" },
  moonshot: { label: "月之暗面 Kimi", base_url: "https://api.moonshot.cn/v1", model: "moonshot-v1-8k" },
  zhipu: { label: "智谱 GLM", base_url: "https://open.bigmodel.cn/api/paas/v4", model: "glm-4-air" },
  ollama: { label: "Ollama（本机）", base_url: "http://127.0.0.1:11434/v1", model: "qwen2.5:7b" },
  custom: { label: "自定义（OpenAI 兼容）", base_url: "", model: "" },
};

async function renderLlm() {
  let providers = [];
  try {
    providers = await api("/api/v1/admin/llm-providers");
  } catch (e) {
    panel().innerHTML = errorBox(e.message);
    return;
  }

  const rows = providers.map((p) => [
    `<b>${escapeHtml(p.name)}</b>${p.is_primary ? ` <span class="chip ok">主用</span>` : ""}`,
    `<span class="chip brand">${escapeHtml(p.provider)}</span>`,
    escapeHtml(p.model),
    `<span class="mono small">${escapeHtml(p.base_url)}</span>`,
    `<span class="mono">${escapeHtml(p.key_hint)}</span>`,
    p.enabled ? `<span class="chip ok">启用</span>` : `<span class="chip">停用</span>`,
    `<span class="row" style="gap:6px;flex-wrap:wrap">
      <button class="btn sm" data-llm-act="test" data-id="${p.id}" title="真实发一次最小请求，验证 Key / 地址 / 模型名">测试</button>
      ${p.is_primary ? "" : `<button class="btn sm" data-llm-act="activate" data-id="${p.id}" title="排到调用链最前">设为主用</button>`}
      <button class="btn sm" data-llm-act="toggle" data-id="${p.id}">${p.enabled ? "停用" : "启用"}</button>
      <button class="btn sm" data-llm-act="edit" data-id="${p.id}">编辑</button>
      <button class="btn sm danger" data-llm-act="delete" data-id="${p.id}">删除</button>
    </span>`,
  ]);

  panel().innerHTML = `
    <div class="card">
      <div class="card-head">
        <div>
          <h3>大模型提供商</h3>
          <div class="small muted">问答与翻译按此列表的顺序调用：<b>主用</b>排最前，其余启用项依次作为故障备选——一个厂商故障自动切下一个，全部失败才提示不可用。</div>
        </div>
        <button class="btn primary sm" id="llm-add">＋ 新增提供商</button>
      </div>
      <div style="padding:0">
        ${table(["名称", "厂商", "模型", "Base URL", "API Key", "状态", "操作"], rows, "还没有配置——系统当前使用 .env 里的内置厂商。")}
      </div>
      <div class="card-body small muted" style="border-top:1px solid var(--border)">
        API Key 加密入库、界面只显示末 4 位；编辑时留空 Key 表示保持原值。
        所有修改<b>立即生效</b>（无需重启后端）。
      </div>
    </div>`;

  panel().querySelector("#llm-add").addEventListener("click", () => openLlmModal());
  panel().querySelectorAll("[data-llm-act]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = Number(btn.dataset.id);
      const act = btn.dataset.llmAct;
      if (act === "edit") {
        openLlmModal(providers.find((p) => p.id === id));
        return;
      }
      if (act === "test") {
        await runLlmTest(btn, id);
        return;
      }
      if (act === "delete") {
        if (!confirm("确定删除这条模型配置？删除后调用链自动落到其余启用项。")) return;
        await actLlm(`/${id}`, "DELETE", "已删除");
      } else if (act === "activate") {
        await actLlm(`/${id}/activate`, "POST", "已设为主用");
      } else if (act === "toggle") {
        await actLlm(`/${id}/toggle`, "POST", "状态已更新");
      }
    });
  });
}

/** 真发一次最小请求，验证 Key / Base URL / 模型名三者是否真能用。
 *  配错的典型症状是"保存成功了、问答全挂"，所以保存前/后都能测一遍。 */
async function runLlmTest(btn, id) {
  const cell = btn.closest("td");
  const label = btn.textContent;
  cell.querySelectorAll(".llm-test-badge").forEach((el) => el.remove());
  btn.disabled = true;
  btn.textContent = "测试中…";
  try {
    const r = await api(`/api/v1/admin/llm-providers/${id}/test`, { method: "POST" });
    const badge = document.createElement("span");
    badge.classList.add("llm-test-badge");
    if (r.ok) {
      badge.className = "chip ok llm-test-badge";
      badge.textContent = `连通 ${r.latency_ms}ms`;
      badge.title = `真实调用成功，模型回包：${r.reply || "(空)"}`;
      toast(`${r.name || r.model} 连接正常，往返 ${r.latency_ms}ms`, "ok");
    } else {
      badge.className = "chip danger llm-test-badge";
      badge.textContent = "不通";
      badge.title = r.message;
      badge.style.cursor = "pointer";
      badge.addEventListener("click", () => toast(r.message, "err"));
      toast(r.message, "err");
    }
    cell.appendChild(badge);
  } catch (e) {
    toast(e.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
}

async function actLlm(path, method, okMsg) {
  try {
    await api(`/api/v1/admin/llm-providers${path}`, { method });
    toast(okMsg, "ok");
    renderLlm();
  } catch (e) {
    toast(e.message, "err");
  }
}

function openLlmModal(existing) {
  const isEdit = Boolean(existing);
  const root = $("#modal-root");
  const options = Object.entries(LLM_PROVIDERS)
    .map(([k, v]) => `<option value="${k}" ${isEdit && existing.provider === k ? "selected" : ""}>${v.label}</option>`)
    .join("");

  root.innerHTML = `
    <div class="modal-mask">
      <div class="modal" style="width:min(620px,94vw)">
        <div class="spread" style="margin-bottom:14px">
          <h3>${isEdit ? "编辑模型提供商" : "新增模型提供商"}</h3>
          <button class="btn sm" data-close>关闭</button>
        </div>
        <div class="stack" style="gap:12px">
          <label class="field">显示名称
            <input type="text" id="llm-name" value="${isEdit ? escapeHtml(existing.name) : ""}" placeholder="例如：MiniMax 主力 / DeepSeek 备用" />
          </label>
          <label class="field">厂商
            <select id="llm-provider">${options}</select>
          </label>
          <label class="field">Base URL（OpenAI 兼容接口地址）
            <input type="text" id="llm-base-url" value="${isEdit ? escapeHtml(existing.base_url) : ""}" />
          </label>
          <label class="field">模型名
            <input type="text" id="llm-model" value="${isEdit ? escapeHtml(existing.model) : ""}" placeholder="例如 deepseek-chat / gpt-4o-mini" />
          </label>
          <label class="field">API Key ${isEdit ? `<span class="faint">（当前 ${escapeHtml(existing.key_hint)}）</span>` : ""}
            <input type="password" id="llm-key" autocomplete="new-password"
              placeholder="${isEdit ? "留空表示保持原 Key 不变" : "sk-……"}" />
          </label>
          <label class="field">备注（可选）
            <input type="text" id="llm-remark" value="${isEdit ? escapeHtml(existing.remark || "") : ""}" placeholder="例如：生产主力，余额不足时切备用" />
          </label>
          <div class="infobox small">保存后立即生效；若链上还没有主用，第一条会自动成为主用。建议至少配置两个厂商做故障互备。</div>
          <div class="infobox small" id="llm-test-result" style="display:none"></div>
          <div class="row" style="justify-content:flex-end">
            <button class="btn" id="llm-test">测试连接</button>
            <button class="btn" data-close>取消</button>
            <button class="btn primary" id="llm-save">${isEdit ? "保存修改" : "创建"}</button>
          </div>
        </div>
      </div>
    </div>`;

  const close = () => (root.innerHTML = "");
  root.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", close));

  // 选厂商 → 联动 base_url / model 默认值（用户自己填过的会被覆盖，这是预期：换厂商就该换地址）
  root.querySelector("#llm-provider").addEventListener("change", (ev) => {
    const preset = LLM_PROVIDERS[ev.target.value];
    if (!preset) return;
    root.querySelector("#llm-base-url").value = preset.base_url;
    root.querySelector("#llm-model").value = preset.model;
  });

  // 测试连接：用"当前表单值"试，不必先保存。编辑时 Key 留空会自动回落到库里已存的 Key。
  root.querySelector("#llm-test").addEventListener("click", async (ev) => {
    const btn = ev.target;
    const hint = root.querySelector("#llm-test-result");
    const body = {
      base_url: root.querySelector("#llm-base-url").value.trim(),
      model: root.querySelector("#llm-model").value.trim(),
      api_key: root.querySelector("#llm-key").value.trim(),
      provider_id: isEdit ? existing.id : null,
    };
    if (!body.base_url || !body.model) {
      toast("请先填 Base URL 和模型名", "err");
      return;
    }
    btn.disabled = true;
    btn.textContent = "测试中…";
    hint.style.display = "block";
    hint.className = "infobox small";
    hint.textContent = "正在真实发起一次最小请求（最多等 20 秒）…";
    try {
      const r = await api("/api/v1/admin/llm-providers/test", { method: "POST", body });
      if (r.ok) {
        hint.className = "infobox small ok-box";
        hint.innerHTML = `连接正常 · 往返 <b>${r.latency_ms}ms</b> · 模型回包「${escapeHtml(r.reply || "(空)")}」`;
      } else {
        hint.className = "infobox small err-box";
        hint.innerHTML = `<b>${escapeHtml(r.error_code || "失败")}</b> ${escapeHtml(r.message || "")}`;
      }
    } catch (e) {
      hint.className = "infobox small err-box";
      hint.textContent = e.message;
    } finally {
      btn.disabled = false;
      btn.textContent = "测试连接";
    }
  });

  root.querySelector("#llm-save").addEventListener("click", async (ev) => {    const btn = ev.target;
    const body = {
      name: root.querySelector("#llm-name").value.trim(),
      provider: root.querySelector("#llm-provider").value,
      base_url: root.querySelector("#llm-base-url").value.trim(),
      model: root.querySelector("#llm-model").value.trim(),
      api_key: root.querySelector("#llm-key").value.trim(),
      remark: root.querySelector("#llm-remark").value.trim(),
    };
    const missing = [];
    if (!body.name) missing.push("名称");
    if (!body.base_url) missing.push("Base URL");
    if (!body.model) missing.push("模型名");
    if (!isEdit && !body.api_key) missing.push("API Key");
    if (missing.length) {
      toast(`请填写：${missing.join("、")}`, "err");
      return;
    }
    btn.disabled = true;
    btn.textContent = "保存中…";
    try {
      await api(
        isEdit ? `/api/v1/admin/llm-providers/${existing.id}` : "/api/v1/admin/llm-providers",
        { method: isEdit ? "PUT" : "POST", body },
      );
      toast(isEdit ? "已保存，立即生效" : "已创建，立即生效", "ok");
      close();
      renderLlm();
    } catch (e) {
      toast(e.message, "err");
      btn.disabled = false;
      btn.textContent = isEdit ? "保存修改" : "创建";
    }
  });
}
