"""多语言重排（Reranker，对应 PRD FR-205）。

给新手的说明：
- 检索（召回）追求"别漏"，所以一次取几十条候选；重排追求"准确"，把这几十条按
  "与问题的相关程度"重新打分，只留最相关的前几条喂给大模型。
- 两种实现：
  ① CrossEncoderReranker：把"问题+候选"成对送入交叉编码器（bge-reranker），
     精度最高，但每条候选都要跑一次模型，CPU 上较慢（数十条约 1~3 秒）。
  ② HeuristicReranker：无模型的确定性重排——按"问题关键词被候选覆盖的比例"
     加权，再叠加召回排名。快、零依赖，作为模型未就位时的降级方案。
- 选择：config.rerank_provider = auto（有模型用模型，否则启发式）/ cross / heuristic / off
"""
from __future__ import annotations

import logging
import re

from app.config import get_settings

logger = logging.getLogger(__name__)

# 中文按二元组切（无分词依赖），英文/数字按词切
_ZH_CHAR = re.compile(r"[\u4e00-\u9fff]")
_TOKEN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._\-/]{1,}")
_STOP = {
    "的", "了", "是", "在", "和", "与", "有", "吗", "呢", "什", "么", "怎", "样",
    "如", "何", "请", "问", "我", "你", "需", "要", "可", "以", "哪", "些", "多", "少",
    "the", "a", "an", "is", "are", "of", "to", "and", "or", "for", "what", "how",
}


def query_units(query: str) -> set[str]:
    """把问题拆成"匹配单元"：中文二元组 + 英文单词（去停用词）。"""
    units: set[str] = set()
    for token in _TOKEN.findall(query.lower()):
        if token not in _STOP and len(token) > 1:
            units.add(token)
    zh = "".join(_ZH_CHAR.findall(query))
    for i in range(len(zh) - 1):
        bigram = zh[i : i + 2]
        if bigram not in _STOP:
            units.add(bigram)
    if not units and query.strip():
        units.add(query.strip().lower())
    return units


class BaseReranker:
    name = "base"
    is_fallback = True

    def rerank(self, query: str, hits: list, top_k: int) -> list:
        raise NotImplementedError

    def info(self) -> dict:
        return {"provider": self.name, "is_fallback": self.is_fallback}


class HeuristicReranker(BaseReranker):
    """无模型重排：关键词覆盖率（0.7）+ 原召回排名（0.3）。"""

    name = "heuristic"
    is_fallback = True

    def rerank(self, query: str, hits: list, top_k: int) -> list:
        units = query_units(query)
        total = max(len(hits), 1)
        ranked: list[tuple[float, object]] = []
        for rank, hit in enumerate(hits):
            content = (getattr(hit, "content", "") or "").lower()
            matched = sum(1 for u in units if u in content)
            coverage = matched / len(units) if units else 0.0
            rank_score = 1.0 - rank / total  # 召回排名越靠前越高
            score = 0.7 * coverage + 0.3 * rank_score
            setattr(hit, "rerank_score", round(score, 6))
            ranked.append((score, hit))
        ranked.sort(key=lambda x: x[0], reverse=True)
        return [hit for _, hit in ranked[:top_k]]


class CrossEncoderReranker(BaseReranker):
    """bge-reranker 交叉编码器（真实语义重排）。"""

    name = "cross-encoder"
    is_fallback = False

    def __init__(self, model_name: str | None = None) -> None:
        import os

        settings = get_settings()
        if settings.hf_endpoint:
            os.environ.setdefault("HF_ENDPOINT", settings.hf_endpoint)
        from sentence_transformers import CrossEncoder

        from app.services.embedding import resolve_model_ref

        self.model_name = resolve_model_ref(model_name or settings.rerank_model_name)
        # 注意：CrossEncoder 的 cache_dir 参数已废弃，本地模型无需缓存目录
        self._model = CrossEncoder(self.model_name, max_length=512)
        self.name = f"cross:{self.model_name}"

    def rerank(self, query: str, hits: list, top_k: int) -> list:
        if not hits:
            return []
        pairs = [(query, getattr(h, "content", "") or "") for h in hits]
        scores = self._model.predict(pairs, batch_size=8, show_progress_bar=False)
        for hit, raw in zip(hits, scores):
            # bge-reranker 输出 logits，用 sigmoid 压到 0~1 便于展示
            value = 1.0 / (1.0 + pow(2.718281828, -float(raw)))
            setattr(hit, "rerank_score", round(value, 6))
        ordered = sorted(hits, key=lambda h: getattr(h, "rerank_score", 0.0), reverse=True)
        return ordered[:top_k]


_reranker: BaseReranker | None = None


def get_reranker() -> BaseReranker | None:
    """按配置返回重排器；off 时返回 None。"""
    global _reranker
    if _reranker is not None:
        return _reranker
    settings = get_settings()
    provider = (settings.rerank_provider or "auto").lower()
    if not settings.rerank_enabled or provider == "off":
        return None
    if provider == "heuristic":
        _reranker = HeuristicReranker()
        return _reranker
    if provider in ("auto", "cross"):
        try:
            _reranker = CrossEncoderReranker()
            logger.info("重排器就绪：%s", _reranker.name)
            return _reranker
        except Exception as exc:
            if provider == "cross":
                raise
            logger.warning("重排模型不可用(%s)，降级启发式重排", exc)
    _reranker = HeuristicReranker()
    return _reranker


def reset_reranker() -> None:
    global _reranker
    _reranker = None


def reranker_state() -> dict:
    settings = get_settings()
    return {
        "enabled": settings.rerank_enabled,
        "provider": settings.rerank_provider,
        "model": settings.rerank_model_name,
        "loaded": _reranker is not None,
        "active": _reranker.name if _reranker is not None else None,
        "is_fallback": _reranker.is_fallback if _reranker is not None else None,
    }
