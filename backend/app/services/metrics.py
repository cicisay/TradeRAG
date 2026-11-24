"""进程内指标（对应 PRD FR-801 完成率漏斗、FR-802 错误码观测、M5/11-2 可观测）。

11-2 改造：底层换成官方 `prometheus_client`，但**对外接口保持不变**
（enter/succeed/inc/observe/record_*/snapshot/render_prometheus/reset），
所以 chat、retrieval、monitor 的调用点一行都不用改。

为什么换：
- 旧实现把"进程内最近 5000 个样本算出的 p95"当 gauge 暴露——PromQL 算不了
  滑动窗口 P95，多副本也没法聚合（每个副本各算各的，Grafana 只能看到其中一台）。
- 官方 Histogram 暴露的是桶计数，`histogram_quantile(0.95, rate(..._bucket[5m]))`
  才是真正的窗口分位数，`sum by (le)` 才能跨副本聚合。
- 旧指标名（rag_stage_total / rag_stage_duration_ms / rag_completion_rate …）
  继续输出一个版本，避免已有看板/脚本立刻失效，后续再删。

口径（严格对齐 PRD 5.8.1）：
- 漏斗：accepted → intent → compliance → retrieval → rerank → first_token → completed
- 完成率 = completed 成功数 ÷（accepted − 主动中断）
- 空答率 = 空内容回答 ÷ 总回答
- 降级率 = 走了降级（翻译不可用 / SQL 降级 / 模型兜底）的请求 ÷ 总请求
- 合规拦截率 = block ÷ 全部预检

标签红线：query、user_id、session_id、doc_name **绝不做标签**（基数爆炸），
要追具体请求，用 request_id 去 Langfuse 查 trace。
"""
import threading
import time
from collections import Counter, defaultdict, deque

from prometheus_client import (
    CollectorRegistry,
    Counter as PCounter,
    GCCollector,
    Gauge,
    Histogram,
    PlatformCollector,
    ProcessCollector,
    generate_latest,
)

from app.config import get_settings

_LOCK = threading.RLock()
_STARTED_AT = time.time()

FUNNEL_STAGES = ("accepted", "intent", "compliance", "retrieval", "rerank", "first_token", "completed")

# ---- 旧口径（人读快照 + 兼容文本输出）----
_funnel: dict[str, dict[str, int]] = {s: {"entered": 0, "succeeded": 0} for s in FUNNEL_STAGES}
_counters: Counter[str] = Counter()
_latencies: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=5000))
_compliance = {"pass": 0, "annotate": 0, "block": 0}
_sql = {"ok": 0, "denied": 0, "error": 0}
_answers = {"total": 0, "empty": 0, "degraded": 0, "llm_failed": 0, "aborted": 0}
_errors: Counter[str] = Counter()


def _new_metrics() -> tuple[CollectorRegistry, dict]:
    """建一套全新的指标对象（reset 时整体替换，避免测试间串数据）。

    用独立 registry 而不是全局默认 registry：一是 reset 干净，二是测试里多次
    重置不会报 "Duplicated timeseries collected"。

    指标分区（对齐六大类观测对象）：
    A. HTTP & 并发         —— 运维 P0 看板
    B. 问答阶段 & TTFT     —— 运维 P0 看板
    C. 检索运行时          —— 检索调优
    D. 反馈 / badcase      —— 业务运营看板
    E. 答案质量评测（离线） —— 答案质量看板
    F. 安全合规拦截        —— 安全合规看板
    G. 错误 / 降级细分     —— 稳定性告警
    H. 知识库存量（Gauge） —— 知识管理员看板（由桥接层定时刷）
    I. 活跃度（Gauge）     —— 业务运营看板（由桥接层定时刷）
    J. 语义缓存            —— 技术调优
    """
    reg = CollectorRegistry()
    m: dict[str, object] = {}

    # ===== A. HTTP & 并发 =====
    m["http_requests"] = PCounter(
        "rag_http_requests_total", "HTTP 请求数（按方法/路由模板/状态码）",
        ["method", "route", "status"], registry=reg,
    )
    m["http_duration"] = Histogram(
        "rag_http_request_duration_seconds", "HTTP 端到端时延（秒）",
        ["method", "route"],
        buckets=(0.05, 0.1, 0.3, 0.5, 1, 2, 3, 5, 8, 10, 15, 20, 30, 60),
        registry=reg,
    )
    m["inflight"] = Gauge(
        "rag_http_inflight", "当前处理中的 HTTP 请求数（并发近似值）", registry=reg,
    )

    # ===== B. 问答阶段 & TTFT =====
    m["stage_duration"] = Histogram(
        "rag_chat_stage_duration_seconds", "问答各阶段耗时（秒）",
        ["stage"],
        buckets=(0.05, 0.1, 0.3, 0.5, 1, 2, 2.5, 3, 4, 5, 8, 10, 15, 20, 30, 60),
        registry=reg,
    )
    m["ttft"] = Histogram(
        "rag_ttft_seconds", "首 token 延迟（秒，仅文本通道）",
        buckets=(0.2, 0.5, 1, 1.5, 2, 2.5, 3, 4, 5, 8, 10, 15, 20, 30),
        registry=reg,
    )

    # ===== B2. LLM 用量（token，成本核算的唯一来源）=====
    # 注意：provider/model 是动态标签，不能预置 0 值；首次真实调用后才会出现序列。
    m["llm_tokens"] = PCounter(
        "rag_llm_tokens_total", "LLM token 用量（kind: prompt/completion/total）",
        ["kind", "provider", "model"], registry=reg,
    )
    m["llm_calls"] = PCounter(
        "rag_llm_calls_total", "LLM 调用次数（mode: stream/once；outcome: ok/error）",
        ["mode", "provider", "model", "outcome"], registry=reg,
    )
    m["llm_usage_missing"] = PCounter(
        "rag_llm_usage_missing_total", "厂商未返回 usage 的调用数（接口兼容性哨兵）",
        ["provider", "mode"], registry=reg,
    )

    # ===== C. 检索运行时 =====
    m["retrieval_hits"] = Histogram(
        "rag_retrieval_hits_count", "单通道命中条数",
        ["channel"], buckets=(1, 2, 3, 5, 8, 10, 15, 20), registry=reg,
    )
    m["retrieval_total"] = PCounter(
        "rag_retrieval_total", "检索次数（分通道）", ["channel"], registry=reg,
    )
    m["retrieval_empty"] = PCounter(
        "rag_retrieval_empty_total", "该通道 0 命中的次数", ["channel"], registry=reg,
    )
    # 检索上下文精确率：送入 LLM 的上下文中"有效证据占比"（observe_hits 额外写）
    m["context_noise"] = Histogram(
        "rag_context_noise_ratio", "LLM 上下文噪音占比（1 - 有效证据占比）",
        buckets=(0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9),
        registry=reg,
    )
    # 检索通道耗时分布（每个通道独立计时）
    m["channel_duration"] = Histogram(
        "rag_retrieval_channel_duration_seconds", "单检索通道耗时（秒）",
        ["channel"],
        buckets=(0.01, 0.05, 0.1, 0.2, 0.5, 1, 2, 3, 5),
        registry=reg,
    )

    # ===== D. 反馈 / badcase =====
    m["feedback"] = PCounter(
        "rag_feedback_total", "回答反馈（点赞/点踩）", ["rating"], registry=reg,
    )
    m["feedback_reason"] = PCounter(
        "rag_feedback_reason_total", "点踩原因分布", ["reason"], registry=reg,
    )
    m["badcase_total"] = Gauge(
        "rag_badcase_total", "badcase 总数（按状态）", ["status"], registry=reg,
    )
    m["badcase_open"] = Gauge(
        "rag_badcase_open_total", "待处理 badcase 数（pending/located/fixing）", registry=reg,
    )
    m["badcase_attribution"] = Gauge(
        "rag_badcase_attribution_total", "badcase 归因分布", ["attribution"], registry=reg,
    )
    m["badcase_close_days"] = Histogram(
        "rag_badcase_close_days", "badcase 闭环耗时（天）",
        buckets=(0.25, 0.5, 1, 2, 3, 7, 14, 30),
        registry=reg,
    )

    # ===== E. 答案质量评测（离线跑评测脚本时 push 到 Gauge）=====
    # 口径说明：这些都是"最近一次评测"的分数，不是进程内累计。
    # PromQL 里 avg_over_time 可以看趋势。
    m["eval_faithfulness"] = Gauge(
        "rag_eval_faithfulness", "RAGAS faithfulness（答案忠于证据的程度，0~1）",
        ["dataset"], registry=reg,
    )
    m["eval_answer_relevance"] = Gauge(
        "rag_eval_answer_relevance", "答案相关性（是否切题，0~1）",
        ["dataset"], registry=reg,
    )
    m["eval_retrieval_recall"] = Gauge(
        "rag_eval_retrieval_recall", "检索 Recall@K（0~1）",
        ["dataset"], registry=reg,
    )
    m["eval_mrr"] = Gauge("rag_eval_mrr", "Mean Reciprocal Rank", ["dataset"], registry=reg)
    m["eval_ndcg"] = Gauge("rag_eval_ndcg", "nDCG@K", ["dataset"], registry=reg)
    m["eval_compliance_accuracy"] = Gauge(
        "rag_eval_compliance_accuracy", "合规集准确率（0~1）",
        ["dataset"], registry=reg,
    )
    m["eval_pass_rate"] = Gauge(
        "rag_eval_pass_rate", "回归通过率（本次 vs 基线）",
        ["dataset"], registry=reg,
    )

    # ===== F. 安全合规拦截 =====
    # 拦截来源：rbac=权限越权（Milvus RBAC 过滤强制生效）
    #           prompt_injection=提示词注入（系统提示词层检测）
    #           pii=PII 脱敏触发（输出前遮罩）
    #           lexicon=合规词库命中（block 级）
    #           rule=合规规则命中（block 级）
    #           llm=LLM 输出兜底（block 级）
    m["security_block"] = PCounter(
        "rag_security_blocked_total", "安全合规拦截次数（按拦截类型）",
        ["type"], registry=reg,
    )
    m["compliance_block_layer"] = PCounter(
        "rag_compliance_block_layer_total", "合规三层拦截分布（lexicon/rule/llm）",
        ["layer"], registry=reg,
    )
    m["pii_masked"] = PCounter(
        "rag_pii_masked_total", "PII 脱敏事件次数", ["field_type"], registry=reg,
    )

    # ===== G. 错误 / 降级细分 =====
    m["error_category"] = PCounter(
        "rag_error_category_total", "错误按五大类打标",
        ["category"], registry=reg,
    )
    # category 取值: api / llm / retrieval / sql / compliance
    m["degraded"] = PCounter(
        "rag_degraded_total", "降级路由次数（按降级原因）",
        ["reason"], registry=reg,
    )
    # reason 取值: translation_unavailable / llm_fallback / sql_fallback / rerank_skipped

    # ===== H. 知识库存量（Gauge，桥接层定时刷）=====
    m["kb_documents"] = Gauge("rag_kb_documents_total", "文档总数", registry=reg)
    m["kb_chunks"] = Gauge("rag_kb_chunks_total", "切分块总数", registry=reg)
    m["kb_documents_expired"] = Gauge(
        "rag_kb_documents_expired_total", "过期（不再参与检索）的文档数", registry=reg,
    )
    m["kb_versions_inconsistent"] = Gauge(
        "rag_kb_versions_inconsistent_total", "新旧版本并存可能导致错误回答的文档数",
        registry=reg,
    )

    # ===== I. 活跃度（Gauge，桥接层定时刷）=====
    m["au_daily"] = Gauge("rag_active_users_24h", "过去 24h 活跃用户数", registry=reg)
    m["au_weekly"] = Gauge("rag_active_users_7d", "过去 7d 活跃用户数", registry=reg)
    m["questions_per_user_24h"] = Gauge(
        "rag_questions_per_user_24h", "过去 24h 人均提问次数", registry=reg,
    )
    m["repeat_question_ratio_24h"] = Gauge(
        "rag_repeat_question_ratio_24h", "过去 24h 问题重复率（相同 query hash 占比）",
        registry=reg,
    )

    # ===== J. 语义缓存 =====
    m["semantic_cache_hit"] = PCounter(
        "rag_semantic_cache_hit_total", "语义缓存命中次数", registry=reg,
    )
    m["semantic_cache_miss"] = PCounter(
        "rag_semantic_cache_miss_total", "语义缓存未命中次数", registry=reg,
    )

    # 进程/GC/平台指标（官方自带）：排查资源水位用
    ProcessCollector(registry=reg)
    PlatformCollector(registry=reg)
    GCCollector(registry=reg)

    # 预先把"已知通道/反馈标签/错误类别"的序列建出来（值为 0），
    # 否则告警表达式会落到 no-data 而不是 0%。
    for channel in ("vector", "bm25", "variant", "hybrid", "sql"):
        m["retrieval_total"].labels(channel=channel)
        m["retrieval_empty"].labels(channel=channel)
        m["channel_duration"].labels(channel=channel)
    for rating in ("up", "down"):
        m["feedback"].labels(rating=rating)
    for reason in ("wrong_answer", "incomplete", "off_topic", "bad_citation", "too_slow", "other"):
        m["feedback_reason"].labels(reason=reason)
    for category in ("api", "llm", "retrieval", "sql", "compliance"):
        m["error_category"].labels(category=category)
    for s in ("pending", "located", "fixing", "regressed", "closed", "archived"):
        m["badcase_total"].labels(status=s)
    # 安全/合规拦截的已知类型（否则首次触发前序列不存在，看板显示 no-data 而不是 0）
    for block_type in ("rbac", "prompt_injection", "pii", "lexicon", "rule", "llm"):
        m["security_block"].labels(type=block_type)
    for layer in ("lexicon", "rule", "llm"):
        m["compliance_block_layer"].labels(layer=layer)
    for field_type in ("phone", "email", "id", "bank", "generic"):
        m["pii_masked"].labels(field_type=field_type)
    for reason in ("translation_unavailable", "llm_fallback", "sql_fallback", "rerank_skipped"):
        m["degraded"].labels(reason=reason)
    for attribution in (
        "retrieval_miss", "chunking", "missing_knowledge", "hallucination",
        "translation", "intent", "permission",
    ):
        m["badcase_attribution"].labels(attribution=attribution)

    return reg, m


_REG, _M = _new_metrics()


def _enabled() -> bool:
    return get_settings().metrics_enabled


# ---------------- 旧接口（保持签名不变）----------------

def enter(stage: str, n: int = 1) -> None:
    """进入某阶段（分母）。"""
    if not _enabled():
        return
    with _LOCK:
        _funnel.setdefault(stage, {"entered": 0, "succeeded": 0})["entered"] += n


def succeed(stage: str, n: int = 1) -> None:
    """该阶段成功（分子）。"""
    if not _enabled():
        return
    with _LOCK:
        _funnel.setdefault(stage, {"entered": 0, "succeeded": 0})["succeeded"] += n


def inc(key: str, n: int = 1) -> None:
    if not _enabled():
        return
    with _LOCK:
        _counters[key] += n


def observe(stage: str, ms: float) -> None:
    """记录某阶段耗时（毫秒）。

    双写：旧 deque（给 snapshot 的人读分位数）+ 新 Histogram（给 PromQL 算窗口 P95）。
    first_token 额外写一份 rag_ttft_seconds，方便单独告警首字。
    """
    if not _enabled():
        return
    with _LOCK:
        _latencies[stage].append(float(ms))
    seconds = max(float(ms), 0.0) / 1000.0
    try:
        _M["stage_duration"].labels(stage=stage).observe(seconds)
        if stage == "first_token":
            _M["ttft"].observe(seconds)
    except Exception:  # noqa: BLE001 - 埋点不能影响业务
        pass


def record_compliance(action: str) -> None:
    if not _enabled():
        return
    with _LOCK:
        _compliance[action] = _compliance.get(action, 0) + 1


def record_sql(status: str) -> None:
    if not _enabled():
        return
    with _LOCK:
        _sql[status] = _sql.get(status, 0) + 1


def record_answer(*, empty: bool = False, degraded: bool = False, llm_failed: bool = False,
                  aborted: bool = False) -> None:
    if not _enabled():
        return
    with _LOCK:
        _answers["total"] += 1
        _answers["empty"] += int(empty)
        _answers["degraded"] += int(degraded)
        _answers["llm_failed"] += int(llm_failed)
        _answers["aborted"] += int(aborted)


def _as_int(v) -> int:
    try:
        return max(int(v), 0)
    except (TypeError, ValueError):
        return 0


def record_llm_usage(usage: dict, *, provider: str, model: str, mode: str) -> None:
    """记录一次 LLM 调用的 token 用量。

    兼容两种字段命名：OpenAI 风格 prompt_tokens/completion_tokens，
    以及部分厂商的 input_tokens/output_tokens。厂商没回 usage 时只记
    llm_usage_missing（用来判断该家是否支持 stream_options），不污染 token 计数。
    """
    if not _enabled():
        return
    usage = usage or {}
    prompt = _as_int(usage.get("prompt_tokens") or usage.get("input_tokens"))
    completion = _as_int(usage.get("completion_tokens") or usage.get("output_tokens"))
    total = _as_int(usage.get("total_tokens")) or (prompt + completion)
    # 推理模型的思考 token 单独记：MiniMax M3 的 completion 里有 80%+ 是 reasoning，
    # 既解释首字慢，也是成本大头（通常按输出价计费）。
    reasoning = _as_int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens"))
    try:
        if not (prompt or completion or total):
            _M["llm_usage_missing"].labels(provider=provider, mode=mode).inc()
            return
        for kind, val in (("prompt", prompt), ("completion", completion),
                          ("reasoning", reasoning), ("total", total)):
            if val:
                _M["llm_tokens"].labels(kind=kind, provider=provider, model=model).inc(val)
        _M["llm_calls"].labels(mode=mode, provider=provider, model=model, outcome="ok").inc()
    except Exception:  # noqa: BLE001 - 埋点不能影响业务
        pass


def record_llm_error(*, provider: str, model: str, mode: str) -> None:
    """记录一次 LLM 调用失败（含被降级切换掉的厂商），用于厂商可用性面板。"""
    if not _enabled():
        return
    try:
        _M["llm_calls"].labels(
            mode=mode, provider=provider, model=model, outcome="error",
        ).inc()
    except Exception:  # noqa: BLE001
        pass


def record_error(code: str) -> None:
    if not _enabled():
        return
    with _LOCK:
        _errors[code] += 1


# ---------------- 新增接口（11-2）----------------

def observe_http(method: str, route: str, status: int, seconds: float) -> None:
    """HTTP 中间件调用：一次请求结束记一条。route 必须是路由模板，不能是原始 URL。"""
    if not _enabled():
        return
    try:
        _M["http_requests"].labels(method=method, route=route, status=str(status)).inc()
        _M["http_duration"].labels(method=method, route=route).observe(max(seconds, 0.0))
    except Exception:  # noqa: BLE001
        pass


def inflight(delta: int) -> None:
    """在途请求数增减（单进程内足够；多副本各自计数由 PromQL 求和）。"""
    if not _enabled():
        return
    try:
        if delta >= 0:
            _M["inflight"].inc(delta)
        else:
            _M["inflight"].dec(-delta)
    except Exception:  # noqa: BLE001
        pass


def observe_hits(channel: str, count: int) -> None:
    """检索通道命中条数：同时记调用总数、0 命中数、命中条数分布。"""
    if not _enabled():
        return
    try:
        _M["retrieval_total"].labels(channel=channel).inc()
        if count <= 0:
            _M["retrieval_empty"].labels(channel=channel).inc()
        _M["retrieval_hits"].labels(channel=channel).observe(max(int(count), 0))
    except Exception:  # noqa: BLE001
        pass


def observe_channel_duration(channel: str, seconds: float) -> None:
    """单检索通道独立耗时（向量/BM25/排序/SQL 各自计时）。"""
    if not _enabled():
        return
    try:
        _M["channel_duration"].labels(channel=channel).observe(max(seconds, 0.0))
    except Exception:  # noqa: BLE001
        pass


def observe_context_noise(ratio: float) -> None:
    """送入 LLM 的上下文噪音占比（0~1）。

    口径：ratio = 1 - 有效证据条数 / Top-K。值越高说明检索混入了越多无关片段，
    可能导致幻觉或稀释关键证据。
    """
    if not _enabled():
        return
    try:
        _M["context_noise"].observe(min(max(ratio, 0.0), 1.0))
    except Exception:  # noqa: BLE001
        pass


# ---------------- 11-3 新增接口：反馈 / badcase ----------------

def record_feedback(rating: str, reason: str | None = None) -> None:
    """反馈（点赞/点踩）埋点。点踩必须带 reason，点赞忽略 reason。"""
    if not _enabled():
        return
    try:
        _M["feedback"].labels(rating=rating).inc()
        if rating == "down" and reason:
            _M["feedback_reason"].labels(reason=reason).inc()
    except Exception:  # noqa: BLE001
        pass


# ---------------- 11-3 新增接口：安全合规拦截 ----------------

def record_security_block(block_type: str, layer: str | None = None) -> None:
    """安全合规拦截埋点。

    block_type 取值：rbac（权限越权）/ prompt_injection（提示词注入）
                      / pii（PII 脱敏遮罩）/ lexicon / rule / llm（合规三层）
    layer 取值：lexicon / rule / llm（仅合规三层需要填）
    """
    if not _enabled():
        return
    try:
        _M["security_block"].labels(type=block_type).inc()
        if layer in ("lexicon", "rule", "llm"):
            _M["compliance_block_layer"].labels(layer=layer).inc()
    except Exception:  # noqa: BLE001
        pass


def record_pii_masked(field_type: str = "generic") -> None:
    """PII 脱敏触发（field_type: phone / email / id / bank / generic）。"""
    if not _enabled():
        return
    try:
        _M["pii_masked"].labels(field_type=field_type).inc()
    except Exception:  # noqa: BLE001
        pass


# ---------------- 11-3 新增接口：错误 / 降级细分 ----------------

def record_error_category(category: str) -> None:
    """把错误归入五大类（api / llm / retrieval / sql / compliance），比纯 error_code 好做趋势。"""
    if not _enabled():
        return
    try:
        _M["error_category"].labels(category=category).inc()
    except Exception:  # noqa: BLE001
        pass


def record_degraded(reason: str) -> None:
    """降级路由（translation_unavailable / llm_fallback / sql_fallback / rerank_skipped）。"""
    if not _enabled():
        return
    try:
        _M["degraded"].labels(reason=reason).inc()
    except Exception:  # noqa: BLE001
        pass


# ---------------- 11-3 新增接口：语义缓存 ----------------

def record_semantic_cache(hit: bool) -> None:
    """语义缓存命中 / 未命中。"""
    if not _enabled():
        return
    try:
        if hit:
            _M["semantic_cache_hit"].inc()
        else:
            _M["semantic_cache_miss"].inc()
    except Exception:  # noqa: BLE001
        pass


# ---------------- 11-3 新增接口：离线评测 push（评测脚本调）----------------

def push_eval_result(
    dataset: str,
    *,
    faithfulness: float | None = None,
    answer_relevance: float | None = None,
    recall: float | None = None,
    mrr: float | None = None,
    ndcg: float | None = None,
    compliance_accuracy: float | None = None,
    pass_rate: float | None = None,
) -> None:
    """离线评测脚本（run_eval.py）跑完后调这个函数，把分数 push 到 Gauge。

    口径：这些 Gauge 存的是"最近一次"的数值，PromQL 用 max_over_time 或 avg_over_time
    看历史趋势。dataset 必须与 EvalRun.dataset 对齐（retrieval_zh / retrieval_crosslingual
    / compliance ...）。
    """
    if not _enabled():
        return
    try:
        if faithfulness is not None:
            _M["eval_faithfulness"].labels(dataset=dataset).set(faithfulness)
        if answer_relevance is not None:
            _M["eval_answer_relevance"].labels(dataset=dataset).set(answer_relevance)
        if recall is not None:
            _M["eval_retrieval_recall"].labels(dataset=dataset).set(recall)
        if mrr is not None:
            _M["eval_mrr"].labels(dataset=dataset).set(mrr)
        if ndcg is not None:
            _M["eval_ndcg"].labels(dataset=dataset).set(ndcg)
        if compliance_accuracy is not None:
            _M["eval_compliance_accuracy"].labels(dataset=dataset).set(compliance_accuracy)
        if pass_rate is not None:
            _M["eval_pass_rate"].labels(dataset=dataset).set(pass_rate)
    except Exception:  # noqa: BLE001
        pass


# ---------------- 11-3 新增接口：桥接层 Gauge 刷新（monitor 定时调）----------------

def set_kb_inventory(*, documents: int | None = None, chunks: int | None = None,
                     expired: int | None = None, versions_inconsistent: int | None = None) -> None:
    """知识库存量 Gauge（DB 桥接层定时刷）。"""
    if not _enabled():
        return
    try:
        if documents is not None:
            _M["kb_documents"].set(documents)
        if chunks is not None:
            _M["kb_chunks"].set(chunks)
        if expired is not None:
            _M["kb_documents_expired"].set(expired)
        if versions_inconsistent is not None:
            _M["kb_versions_inconsistent"].set(versions_inconsistent)
    except Exception:  # noqa: BLE001
        pass


def set_active_users(*, daily: int | None = None, weekly: int | None = None,
                     questions_per_user_24h: float | None = None,
                     repeat_ratio_24h: float | None = None) -> None:
    """活跃度 Gauge（DB 桥接层定时刷）。"""
    if not _enabled():
        return
    try:
        if daily is not None:
            _M["au_daily"].set(daily)
        if weekly is not None:
            _M["au_weekly"].set(weekly)
        if questions_per_user_24h is not None:
            _M["questions_per_user_24h"].set(questions_per_user_24h)
        if repeat_ratio_24h is not None:
            _M["repeat_question_ratio_24h"].set(repeat_ratio_24h)
    except Exception:  # noqa: BLE001
        pass


def set_badcase_gauges(*, open_count: int | None = None,
                       by_status: dict[str, int] | None = None,
                       by_attribution: dict[str, int] | None = None) -> None:
    """badcase Gauge（DB 桥接层定时刷）。by_status/by_attribution 键与模型定义对齐。"""
    if not _enabled():
        return
    try:
        if open_count is not None:
            _M["badcase_open"].set(open_count)
        if by_status:
            for status, n in by_status.items():
                _M["badcase_total"].labels(status=status).set(n)
        if by_attribution:
            for attr, n in by_attribution.items():
                _M["badcase_attribution"].labels(attribution=attr).set(n)
    except Exception:  # noqa: BLE001
        pass


def observe_badcase_close_days(days: float) -> None:
    """badcase 闭环耗时（由 badcase API 在状态 closed/archived 时调）。"""
    if not _enabled():
        return
    try:
        _M["badcase_close_days"].observe(max(days, 0.0))
    except Exception:  # noqa: BLE001
        pass


# ---------------- 快照与输出 ----------------

def _percentiles(samples: deque[float]) -> dict[str, float]:
    if not samples:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "avg": 0.0}
    data = sorted(samples)
    n = len(data)

    def pick(q: float) -> float:
        idx = min(n - 1, max(0, int(round(q * (n - 1)))))
        return round(data[idx], 1)

    return {
        "p50": pick(0.50),
        "p95": pick(0.95),
        "p99": pick(0.99),
        "avg": round(sum(data) / n, 1),
    }


def snapshot() -> dict:
    """质量看板用的 JSON 快照（人读友好，含比率）。"""
    with _LOCK:
        stages = {k: dict(v) for k, v in _funnel.items()}
        counters = dict(_counters)
        compliance = dict(_compliance)
        sql = dict(_sql)
        answers = dict(_answers)
        errors = dict(_errors)
        latencies = {k: _percentiles(v) for k, v in _latencies.items()}

    for name, s in stages.items():
        s["rate"] = round(s["succeeded"] / s["entered"], 4) if s["entered"] else None
    accepted = stages["accepted"]["entered"]
    completed = stages["completed"]["succeeded"]
    completion_denominator = max(accepted - answers["aborted"], 1)
    total_answers = max(answers["total"], 1)
    checked = max(sum(compliance.values()), 1)
    return {
        "uptime_seconds": int(time.time() - _STARTED_AT),
        "requests_accepted": accepted,
        "funnel": stages,
        "rates": {
            "completion": round(completed / completion_denominator, 4) if accepted else None,
            "empty_answer": round(answers["empty"] / total_answers, 4) if answers["total"] else None,
            "degraded": round(answers["degraded"] / total_answers, 4) if answers["total"] else None,
            "llm_failed": round(answers["llm_failed"] / total_answers, 4) if answers["total"] else None,
            "compliance_block": round(compliance.get("block", 0) / checked, 4) if sum(compliance.values()) else None,
        },
        "latency_ms": latencies,
        "compliance": compliance,
        "sql": sql,
        "answers": answers,
        "errors": errors,
        "counters": counters,
    }


def render_prometheus() -> str:
    """输出 Prometheus 文本格式：旧指标（兼容一版）+ 新指标（官方 client）。"""
    snap = snapshot()
    lines: list[str] = []
    lines.append("# HELP rag_requests_accepted_total 进入问答链路的请求数")
    lines.append("# TYPE rag_requests_accepted_total counter")
    lines.append(f"rag_requests_accepted_total {snap['requests_accepted']}")

    lines.append("# HELP rag_stage_total 漏斗各阶段进入/成功数（旧口径，保留兼容）")
    lines.append("# TYPE rag_stage_total counter")
    for stage, s in snap["funnel"].items():
        lines.append(f'rag_stage_total{{stage="{stage}",result="entered"}} {s["entered"]}')
        lines.append(f'rag_stage_total{{stage="{stage}",result="succeeded"}} {s["succeeded"]}')

    # 旧的 percentile gauge：保留一个版本给已有脚本，**新看板请用
    # histogram_quantile(..., rate(rag_chat_stage_duration_seconds_bucket[5m]))**
    lines.append("# HELP rag_stage_duration_ms 各阶段耗时分位数（旧口径，进程内样本，勿用于告警）")
    lines.append("# TYPE rag_stage_duration_ms gauge")
    for stage, p in snap["latency_ms"].items():
        for q, v in p.items():
            if q == "avg":
                continue
            lines.append(f'rag_stage_duration_ms{{stage="{stage}",quantile="{q[1:]}"}} {v}')

    lines.append("# HELP rag_compliance_total 合规判定分布")
    lines.append("# TYPE rag_compliance_total counter")
    for action, v in snap["compliance"].items():
        lines.append(f'rag_compliance_total{{action="{action}"}} {v}')

    lines.append("# HELP rag_sql_total SQL 通道执行结果")
    lines.append("# TYPE rag_sql_total counter")
    for status, v in snap["sql"].items():
        lines.append(f'rag_sql_total{{status="{status}"}} {v}')

    lines.append("# HELP rag_answers_total 回答质量计数")
    lines.append("# TYPE rag_answers_total counter")
    for kind, v in snap["answers"].items():
        lines.append(f'rag_answers_total{{kind="{kind}"}} {v}')

    lines.append("# HELP rag_errors_total 错误码计数")
    lines.append("# TYPE rag_errors_total counter")
    for code, v in snap["errors"].items():
        lines.append(f'rag_errors_total{{code="{code}"}} {v}')

    lines.append("# HELP rag_completion_rate 端到端完成率")
    lines.append("# TYPE rag_completion_rate gauge")
    rate = snap["rates"]["completion"]
    lines.append(f"rag_completion_rate {rate if rate is not None else 0}")

    # 新指标（官方 client 生成）：HTTP QPS/时延、阶段直方图、TTFT、检索命中
    lines.append(generate_latest(_REG).decode("utf-8"))
    return "\n".join(lines)


def reset() -> None:
    """测试专用：清空所有指标（含官方 client 的 registry，整体换新）。"""
    global _STARTED_AT, _REG, _M
    with _LOCK:
        for s in _funnel:
            _funnel[s] = {"entered": 0, "succeeded": 0}
        _counters.clear()
        _latencies.clear()
        _compliance.update({"pass": 0, "annotate": 0, "block": 0})
        _sql.update({"ok": 0, "denied": 0, "error": 0})
        _answers.update({"total": 0, "empty": 0, "degraded": 0, "llm_failed": 0, "aborted": 0})
        _errors.clear()
        _STARTED_AT = time.time()
        _REG, _M = _new_metrics()
