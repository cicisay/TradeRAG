"""向量库适配层（对应 PRD 6.2 Milvus Collection Schema / 17.4 反锁定）。

给新手的说明：
- 向量库存的是"每块文字的向量 + 标量过滤字段"。检索时不是全库搜，
  而是先按标量条件过滤（业务组/密级/状态），再在过滤后的子集里找最近向量——
  这就是 FR-603 权限隔离的落地点：**过滤条件在库端强制注入，不是应用层事后筛**。
- 本模块两个实现、同一接口：
  - MilvusVectorStore：生产目标（pymilvus，HNSW+IP，PRD 6.2/6.3）。需 Docker 里的 Milvus。
  - InMemoryVectorStore：开发/测试兜底（本机无 Docker 时用），逻辑一致，重启即清空。
- 切换入口：config.vector_store（"memory" / "milvus"）。

Milvus 强制过滤 expr 模板（PRD 6.2）：
  kb_id in [...] and biz_group in [...] and sensitivity <= 用户密级 and doc_status == "active"
"""
import logging

logger = logging.getLogger(__name__)

COLLECTION_FIELDS = (
    "id", "vector", "chunk_id", "chunk_type", "language",
    "doc_id", "doc_version_id", "kb_id",
    "biz_group", "sensitivity", "doc_status", "authority_level", "content_hash",
)


def sanitize_collection_name(name: str) -> str:
    """Milvus collection 名只允许字母/数字/下划线（10-3 真机发现：bge-m3 里的 '-' 非法）。

    统一在此归一化：bge-m3 → bge_m3。PRD 17.4 的"模型_维度后缀"语义不变，
    只是非法字符替换。所有取 collection 名的地方都必须经过这里，禁止手拼。
    """
    import re

    return re.sub(r"[^0-9A-Za-z_]", "_", name)


class VectorRecord(dict):
    """一条向量记录：字段对齐 PRD 6.2。"""

    def __init__(self, vector: list[float], **scalar_fields) -> None:
        missing = {"chunk_id", "kb_id", "biz_group"} - set(scalar_fields)
        if missing:
            raise ValueError(f"向量记录缺少必需标量字段：{missing}")
        super().__init__(
            vector=vector,
            chunk_type=scalar_fields.get("chunk_type", "content"),
            language=scalar_fields.get("language", "zh"),
            doc_id=scalar_fields.get("doc_id"),
            doc_version_id=scalar_fields.get("doc_version_id"),
            kb_id=scalar_fields["kb_id"],
            chunk_id=scalar_fields["chunk_id"],
            biz_group=scalar_fields["biz_group"],
            sensitivity=scalar_fields.get("sensitivity", 0),
            doc_status=scalar_fields.get("doc_status", "active"),
            authority_level=scalar_fields.get("authority_level", 2),
            content_hash=scalar_fields.get("content_hash", ""),
        )


def build_access_expr(kb_ids: list[int], biz_groups: list[str], max_sensitivity: int) -> str:
    """构造 Milvus 访问控制过滤表达式（PRD 6.2 / FR-603 / FR-202e）。

    任何检索（含翻译副本、QA 快通道）都必须经由此函数构造 expr，
    不允许业务代码手写过滤条件（防止遗漏权限字段）。
    """
    kb_list = "[" + ",".join(str(k) for k in kb_ids) + "]"
    # biz_group 虽来自库内数据，仍按不可信输入处理：转义反斜杠与双引号，
    # 防止恶意组名构造出 biz_group in [""] or 1==1 or [""] 之类的 expr 注入（10-3 自查修复）
    safe_groups = [g.replace("\\", "\\\\").replace('"', '\\"') for g in biz_groups]
    bg_list = "[" + ",".join(f'"{g}"' for g in safe_groups) + "]"
    return (
        f"kb_id in {kb_list} and biz_group in {bg_list} "
        f"and sensitivity <= {max_sensitivity} and doc_status == \"active\""
    )


class InMemoryVectorStore:
    """内存实现：进程内字典 + 暴力余量检索。语义与 Milvus 一致，仅性能不同。"""

    name = "memory"

    def __init__(self) -> None:
        self._collections: dict[str, list[VectorRecord]] = {}
        self._dims: dict[str, int] = {}

    def _col(self, collection: str) -> list[VectorRecord]:
        return self._collections.setdefault(collection, [])

    def ensure_collection(self, collection: str, dim: int) -> None:
        """登记集合维度；维度冲突立即报错（换嵌入模型必须换 Collection 名）。

        为什么必须拦：余弦相似度要求两个向量长度一致，长度不一致时 zip 会静默截断，
        返回看似正常的分数——这种"静默错误"比报错更危险（PRD 17.4 反锁定规则）。
        """
        existing = self._dims.get(collection)
        if existing is not None and existing != dim:
            raise ValueError(
                f"向量集合 {collection} 已是 {existing} 维，当前嵌入器输出 {dim} 维；"
                f"换模型请同时修改 MILVUS_COLLECTION（如 chunks_bge-m3_1024）"
            )
        self._dims[collection] = dim
        self._col(collection)

    def insert(self, collection: str, records: list[VectorRecord]) -> int:
        self._check_dim(collection, records[0]["vector"] if records else None)
        self._col(collection).extend(records)
        return len(records)

    def _check_dim(self, collection: str, vector: list[float] | None) -> None:
        expected = self._dims.get(collection)
        if vector is not None and expected is not None and len(vector) != expected:
            raise ValueError(f"向量维度 {len(vector)} 与集合 {collection} 的 {expected} 维不一致")

    def search(
        self,
        collection: str,
        query_vector: list[float],
        expr: str | None = None,
        top_k: int = 10,
    ) -> list[dict]:
        """余量相似检索。expr 为简化版过滤（当前支持 x in [...] / x == y / x <= n 的 and 组合）。"""
        self._check_dim(collection, query_vector)
        candidates = [r for r in self._col(collection) if _match_expr(r, expr)]
        scored = []
        for r in candidates:
            score = _dot(query_vector, r["vector"])
            scored.append((score, r))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            {"score": s, **{k: v for k, v in r.items() if k != "vector"}}
            for s, r in scored[:top_k]
        ]

    def delete_by_doc_version(self, collection: str, doc_version_id: int) -> int:
        """按版本删除（文档更新：删旧写新，PRD 6.3）。"""
        col = self._col(collection)
        before = len(col)
        self._collections[collection] = [r for r in col if r["doc_version_id"] != doc_version_id]
        return before - len(self._collections[collection])

    def count(self, collection: str) -> int:
        return len(self._col(collection))


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _match_expr(record: dict, expr: str | None) -> bool:
    """极简 expr 求值：and 连接的 `field in [..]` / `field == "x"` / `field <= n`。

    注意：仅用于内存实现的测试语义；生产 Milvus 端由引擎原生求值。
    """
    if not expr:
        return True
    import re
    for cond in expr.split(" and "):
        cond = cond.strip()
        m = re.fullmatch(r"(\w+) in \[([^\]]*)\]", cond)
        if m:
            field, raw = m.group(1), m.group(2)
            values = [v.strip().strip('"') for v in raw.split(",") if v.strip()]
            if str(record.get(field)) not in [str(v) for v in values]:
                return False
            continue
        m = re.fullmatch(r"(\w+) == \"([^\"]+)\"", cond)
        if m:
            if str(record.get(m.group(1))) != m.group(2):
                return False
            continue
        m = re.fullmatch(r"(\w+) <= (\d+)", cond)
        if m:
            if float(record.get(m.group(1), 0)) > float(m.group(2)):
                return False
            continue
        # 未知条件：安全兜底为不匹配
        return False
    return True


class MilvusVectorStore:
    """Milvus 实现（pymilvus）。本机无 Docker 时不可实例化，接口与内存版完全一致。"""

    name = "milvus"

    def __init__(self, uri: str) -> None:
        from pymilvus import MilvusClient  # 延迟导入
        from app.config import get_settings

        settings = get_settings()
        # 10-4：服务端启用 RBAC 后匿名连接会被拒，必须带凭据
        self._client = MilvusClient(
            uri=uri, user=settings.milvus_user, password=settings.milvus_password,
        )

    def ensure_collection(self, collection: str, dim: int) -> None:
        from pymilvus import DataType

        if self._client.has_collection(collection):
            return
        schema = self._client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", DataType.INT64, is_primary=True)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=dim)
        for f in ("chunk_id", "doc_id", "doc_version_id", "kb_id", "authority_level", "sensitivity"):
            schema.add_field(f, DataType.INT64)
        for f in ("chunk_type", "language", "biz_group", "doc_status", "content_hash"):
            schema.add_field(f, DataType.VARCHAR, max_length=512)
        index_params = self._client.prepare_index_params()
        # PRD 6.2：HNSW（M=16, efConstruction=200），度量 IP
        index_params.add_index("vector", index_type="HNSW", metric_type="IP",
                               params={"M": 16, "efConstruction": 200})
        self._client.create_collection(collection, schema=schema, index_params=index_params)

    def insert(self, collection: str, records: list[VectorRecord]) -> int:
        import hashlib

        rows = []
        for r in records:
            row = dict(r)
            # 主键：content_hash（"doc-version-chunk" 形如 "5-1-23"）的 md5 前 14 位十六进制 → 56bit 整数。
            # 旧写法 int(content_hash.replace("-","")[:16]) 有两类问题：
            #   1) "5-12-3" 与 "51-2-3" 拼接后相同 → 主键碰撞静默覆盖；
            #   2) content_hash 一旦出现非数字字符 int() 直接抛异常。
            row["id"] = int(hashlib.md5(row["content_hash"].encode("utf-8")).hexdigest()[:14], 16)
            rows.append(row)
        self._client.insert(collection, rows)
        # 立刻 flush：默认一致性是 Bounded，不 flush 的话紧随其后的检索可能看不到刚写入的数据
        self._client.flush(collection)
        return len(rows)

    def search(self, collection: str, query_vector: list[float], expr: str | None = None, top_k: int = 10) -> list[dict]:
        res = self._client.search(
            collection, data=[query_vector], limit=top_k,
            filter=expr or "", output_fields=[f for f in COLLECTION_FIELDS if f not in ("vector", "id")],
        )
        hits = res[0] if res else []
        return [{"score": h["distance"], **h["entity"]} for h in hits]

    def delete_by_doc_version(self, collection: str, doc_version_id: int) -> int:
        self._client.delete(collection, filter=f"doc_version_id == {doc_version_id}")
        return 0  # Milvus 不返回删除计数

    def count(self, collection: str) -> int:
        return self._client.get_collection_stats(collection)["row_count"]


_store = None
degraded_reason: str | None = None  # 非空 = 期望 Milvus 但降级到了内存库（供 /system/status 暴露）


def get_vector_store():
    """按配置返回向量库单例；milvus 不可用时降级内存版并**显式告警**。

    10-4 教训：认证失败时只打 warning 会让人以为"检索正常"，其实向量路零召回。
    这里升级为 ERROR 级日志 + 记录 degraded_reason，由健康检查接口暴露出去。
    """
    global _store, degraded_reason
    if _store is not None:
        return _store
    from app.config import get_settings

    settings = get_settings()
    if settings.vector_store == "milvus":
        try:
            _store = MilvusVectorStore(settings.milvus_uri)
            logger.info("向量库：Milvus(%s)", settings.milvus_uri)
            return _store
        except Exception as exc:
            degraded_reason = f"Milvus 连接失败：{str(exc)[:200]}"
            logger.error(
                "向量库降级到内存（服务可启动但语义检索将零召回，检查 MILVUS_URI/USER/PASSWORD）：%s",
                exc,
            )
    _store = InMemoryVectorStore()
    logger.info("向量库：InMemory（开发/测试用）")
    return _store
    return _store


def reset_vector_store() -> None:
    """测试辅助：清空单例。"""
    global _store
    _store = None
