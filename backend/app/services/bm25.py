"""BM25 关键词检索（对应 PRD FR-203 混合检索的另一路；生产可迁 OpenSearch/BM25s）。

给新手的说明：
- 向量检索擅长"意思相近"，但对**型号、单号、HS 编码、缩写（L/C、B/L）**这类精确词不敏感；
  BM25 是经典关键词打分算法，正好补这个短板。两路结果用 RRF 融合（见 retrieval.py）。
- 中文没有空格分词：用「汉字二元组」切分（"不锈钢法兰"→ 不锈/锈钢/钢法/法兰），
  免去安装 jieba 等分词依赖，对短查询召回效果足够；英文/数字按单词切。
- 当前实现为进程内索引（每次检索时从 DB 构建），数据量大时换 Milvus 稀疏向量或 OpenSearch。
"""
import math
import re
from collections import Counter

_CJK = re.compile(r"[\u4e00-\u9fff]+")
_LATIN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """中英混排分词：汉字二元组 + 小写英文/数字单词。"""
    text = text.lower()
    tokens: list[str] = []
    for seg in _CJK.findall(text):
        if len(seg) == 1:
            tokens.append(seg)
        else:
            tokens.extend(seg[i : i + 2] for i in range(len(seg) - 1))
    tokens.extend(_LATIN.findall(text))
    return tokens


class BM25Index:
    """轻量 BM25（k1=1.5, b=0.75），数据来自 DB chunk 表。"""

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.doc_ids: list[int] = []
        self.doc_tokens: list[Counter] = []
        self.doc_len: list[int] = []
        self.avg_len: float = 0.0
        self.df: Counter = Counter()  # 文档频率

    def build(self, docs: list[tuple[int, str]]) -> None:
        """docs: [(chunk_id, content), ...]"""
        self.doc_ids = [d[0] for d in docs]
        self.doc_tokens = [Counter(tokenize(d[1])) for d in docs]
        self.doc_len = [sum(c.values()) for c in self.doc_tokens]
        self.avg_len = (sum(self.doc_len) / len(self.doc_len)) if self.doc_len else 0.0
        self.df = Counter()
        for counter in self.doc_tokens:
            self.df.update(counter.keys())

    def search(self, query: str, top_k: int = 10) -> list[tuple[int, float]]:
        """返回 [(chunk_id, score)]，按分数降序。"""
        if not self.doc_ids:
            return []
        q_tokens = tokenize(query)
        n = len(self.doc_ids)
        scores: list[tuple[int, float]] = []
        for i, counter in enumerate(self.doc_tokens):
            score = 0.0
            dl = self.doc_len[i] or 1
            for t in q_tokens:
                tf = counter.get(t, 0)
                if not tf:
                    continue
                idf = math.log(1 + (n - self.df[t] + 0.5) / (self.df[t] + 0.5))
                denom = tf + self.k1 * (1 - self.b + self.b * dl / (self.avg_len or 1))
                score += idf * tf * (self.k1 + 1) / denom
            if score > 0:
                scores.append((self.doc_ids[i], score))
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:top_k]
