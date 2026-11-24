"""文档相关 Pydantic 模型（出入参定义）。"""
from pydantic import BaseModel


class DocumentOut(BaseModel):
    id: int
    kb_id: int
    doc_name: str
    status: str
    current_version_id: int | None
    doc_type: str | None = None
    languages: list[str] | None = None
    authority_level: int | None = None
    biz_group: str | None = None
    page_count: int | None = None
    parse_status: str | None = None
    chunks: int | None = None  # 已向量化入库的分片数
    # ---- OCR 质量（FR-104）----
    ocr_used: bool = False
    ocr_confidence: float | None = None
    ocr_low_pages: list[int] = []  # 低置信度页码，需人工抽检
    ocr_error: str | None = None
    # ---- 异步摄入（M6 步骤10-2）----
    job_id: int | None = None          # 本次上传对应的任务 id，前端用它轮询进度
    job_stage: str | None = None       # queued / parsing / chunking / embedding / indexed / failed
    queue_mode: str | None = None      # celery / thread / inline
    queued: bool = False               # True 表示"已返回、后台还在处理"

    class Config:
        from_attributes = True
