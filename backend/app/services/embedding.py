"""嵌入器（Embedder）：把文本变成向量（对应 PRD 3.2 嵌入选型 bge-m3）。

给新手的说明：
- "嵌入"= 把一段文字变成一串数字（向量），语义相近的文字向量距离也近——这是向量检索的基础。
- 本模块是可插拔设计（PRD 17.4 反锁定规则 1：禁止任何单模型硬编码）：
  - SentenceTransformerEmbedder：真实语义模型（默认 bge-m3，1024 维，多语言）。
    通过 sentence-transformers 加载，模型从 hf-mirror 镜像下载（官方站被网络策略阻断）。
  - HashEmbedder：开发/测试用确定性嵌入（字符 3-gram 哈希到 1024 维再 L2 归一化）。
    零依赖、可复现，但**没有语义能力**，仅在没有模型时保证链路可测。
- 切换入口：config.embed_provider
  · auto      优先真实模型，加载失败自动降级 hash 并告警（默认）
  · bge-m3 / st  强制真实模型（失败直接抛错，避免"以为在用模型其实在用兜底"）
  · hash      强制兜底
- 注意：不同模型维度不同（bge-m3=1024，MiniLM=384）。Collection 名带维度后缀
  （chunks_bge-m3_1024），换模型时新建 Collection，绝不混用旧向量（PRD 17.4 规则 2）。
"""
import hashlib
import logging
import math
import os
import threading
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger(__name__)


def resolve_model_ref(ref: str) -> str:
    """把"模型名"解析成"本地目录优先"的引用。

    优先顺序：① 明确给出的本地绝对/相对路径 → ② models/<模型名末段> 目录 → ③ 原样（走网络下载）。
    这样离线环境只要把模型放到 models/ 下就能直接生效，不必改配置。
    """
    path = Path(ref)
    if path.exists():
        return str(path)
    local = Path(get_settings().model_cache_dir) / ref.split("/")[-1]
    if local.exists():
        return str(local)
    return ref

# 与 PRD 6.2 Collection 命名 chunks_bge-m3_1024 对齐
EMBED_DIM = 1024


class BaseEmbedder:
    name: str = "base"
    dim: int = EMBED_DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError

    def info(self) -> dict:
        return {"provider": self.name, "dim": self.dim, "is_fallback": self.name != "st"}


class SentenceTransformerEmbedder(BaseEmbedder):
    """sentence-transformers 真实模型（默认 BAAI/bge-m3）。"""

    def __init__(self, model_name: str | None = None) -> None:
        settings = get_settings()
        # HF_ENDPOINT 必须在 import huggingface_hub 之前设置，否则镜像不生效
        if settings.hf_endpoint:
            os.environ.setdefault("HF_ENDPOINT", settings.hf_endpoint)
        from sentence_transformers import SentenceTransformer  # 延迟导入：未装依赖时报错清晰

        self.model_name = resolve_model_ref(model_name or settings.embed_model_name)
        self._batch_size = settings.embed_batch_size
        self._max_length = settings.embed_max_length
        self._model = SentenceTransformer(self.model_name, cache_folder=settings.model_cache_dir)
        self._model.max_seq_length = self._max_length
        self.dim = int(self._model.get_sentence_embedding_dimension())
        self.name = f"st:{self.model_name}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.encode(
            texts,
            batch_size=self._batch_size,
            normalize_embeddings=True,  # 归一化后内积=余弦相似度，Milvus 用 IP 度量
            show_progress_bar=False,
        )
        return [list(map(float, v)) for v in vectors]

    def info(self) -> dict:
        return {"provider": self.name, "dim": self.dim, "is_fallback": False}


class BgeM3Embedder(BaseEmbedder):
    """bge-m3 本地模型（FlagEmbedding 路径）。保留为可选实现。"""

    name = "bge-m3"

    def __init__(self) -> None:
        from FlagEmbedding import BGEM3FlagModel  # 延迟导入：未装依赖时给清晰报错

        settings = get_settings()
        self._model = BGEM3FlagModel(settings.bge_m3_model_path, use_fp16=False)

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = self._model.encode(texts, batch_size=16, max_length=1024)["dense_vecs"]
        return [list(map(float, v)) for v in out]


class HashEmbedder(BaseEmbedder):
    """确定性哈希嵌入（仅开发/测试）：字符 3-gram → 哈希桶 → TF 权重 → L2 归一化。

    不需要下载模型、对中/英/西/阿一视同仁（按字符切），保证全链路可测。
    """

    name = "hash-fallback"

    def _vectorize(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        t = text.strip().lower()
        grams = [t[i : i + 3] for i in range(max(len(t) - 2, 1))] or [t]
        for g in grams:
            h = int.from_bytes(hashlib.sha256(g.encode("utf-8")).digest()[:8], "big")
            idx = h % self.dim
            vec[idx] += 1.0
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vectorize(t) for t in texts]


_embedder: BaseEmbedder | None = None
_lock = threading.Lock()


def _build_embedder() -> BaseEmbedder:
    provider = (get_settings().embed_provider or "auto").lower()
    if provider == "hash":
        logger.info("嵌入器：hash-fallback（开发/测试用，语义能力有限）")
        return HashEmbedder()
    if provider == "bge-m3":
        return BgeM3Embedder()
    if provider in ("auto", "st", "sentence-transformers"):
        try:
            emb = SentenceTransformerEmbedder()
            logger.info("嵌入器就绪：%s（%s 维）", emb.name, emb.dim)
            return emb
        except Exception as exc:
            if provider == "auto":
                logger.warning("真实嵌入模型加载失败(%s)，降级 hash 嵌入器（检索效果会明显下降）", exc)
                return HashEmbedder()
            raise
    logger.warning("未知 embed_provider=%s，回退 hash", provider)
    return HashEmbedder()


def get_embedder() -> BaseEmbedder:
    """按配置返回嵌入器单例（线程安全，首次加载模型较慢）。"""
    global _embedder
    if _embedder is not None:
        return _embedder
    with _lock:
        if _embedder is None:
            _embedder = _build_embedder()
        return _embedder


def reset_embedder() -> None:
    """测试/切换配置时清空单例。"""
    global _embedder
    _embedder = None


def embedder_state() -> dict:
    """当前嵌入器状态（不触发模型加载，供 /system/capabilities 展示）。"""
    settings = get_settings()
    if _embedder is None:
        return {
            "provider": settings.embed_provider,
            "model": settings.embed_model_name,
            "loaded": False,
            "dim": None,
        }
    return {
        "provider": settings.embed_provider,
        "model": getattr(_embedder, "model_name", _embedder.name),
        "loaded": True,
        "active": _embedder.name,
        "dim": _embedder.dim,
        # 注意：真实模型 name 形如 "st:models/bge-m3"，必须按前缀判断而不是等值比较
        "is_fallback": not _embedder.name.startswith("st:"),
    }
