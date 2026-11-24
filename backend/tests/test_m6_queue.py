"""M6 步骤10-2：异步摄入流水线测试。

测试环境走 INGEST_MODE=inline（conftest 设置），所以这里验证的是
"任务台账 + 执行器 + 接口"这三层，不需要 Redis/Celery 也能跑。
真实的 Celery 链路在 scripts/queue_smoke.py 里做端到端冒烟（起 Redis + worker）。

覆盖点：
1. 上传即建任务，同步模式下完成后台账是 indexed / progress=100；
2. 幂等：同一版本重复入队不新增台账行；
3. 失败可归因：解析不出文本 → failed，且不会无限重试；
4. 权限与业务组隔离：普通用户 403，b 组管理员看不到 a 组任务；
5. 重试 / 取消接口的状态机；
6. 进度回调真的被调用（不是"进度条永远 0%"）。
"""
import io

import pytest

from app.db.session import SessionLocal


def _upload(client, headers, kb_id, filename, content: bytes, **extra):
    data = {"kb_id": str(kb_id), "doc_type": "other", **{k: str(v) for k, v in extra.items()}}
    return client.post(
        "/api/v1/documents/upload",
        headers=headers,
        data=data,
        files={"file": (filename, io.BytesIO(content), "application/octet-stream")},
    )


TXT = ("报关单证要求：商业发票、装箱单、提单、原产地证。\n" * 30).encode("utf-8")


class TestIngestJob:
    def test_upload_creates_indexed_job(self, client, knowledge_headers, kb_a_id):
        resp = _upload(client, knowledge_headers, kb_a_id, "async-正常.txt", TXT)
        assert resp.status_code == 200, resp.text  # inline 模式：同步完成，返回 200
        body = resp.json()
        assert body["job_id"], "上传必须返回任务 id"
        assert body["queued"] is False
        assert body["status"] == "parsed"

        job = client.get(f"/api/v1/jobs/{body['job_id']}", headers=knowledge_headers).json()
        assert job["stage"] == "indexed"
        assert job["progress"] == 100
        assert job["attempts"] == 1
        assert job["doc_name"] == "async-正常.txt"
        assert job["result"]["chunks"] > 0
        assert job["duration_ms"] is not None

    def test_upload_returns_202_when_async(self, client, knowledge_headers, kb_a_id, monkeypatch):
        """thread/celery 模式下接口必须立刻返回 202，而不是等流水线跑完。"""
        from app.services import ingest as ingest_mod

        monkeypatch.setattr(ingest_mod, "resolve_mode", lambda: "thread")
        # 让线程不真的执行，只验证接口行为（避免测试里起后台线程抢库）
        monkeypatch.setattr(ingest_mod, "_start_thread", lambda job_id, delay=0: None)

        resp = _upload(client, knowledge_headers, kb_a_id, "async-排队.txt", TXT)
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["queued"] is True
        assert body["job_stage"] == "queued"
        assert body["queue_mode"] == "thread"

        job = client.get(f"/api/v1/jobs/{body['job_id']}", headers=knowledge_headers).json()
        assert job["stage"] == "queued"
        assert job["progress"] == 0
        # 收尾：让任务跑完，避免影响后续用例（inline 执行器）
        ingest_mod.run_ingest_job(job["id"])

    def test_enqueue_is_idempotent(self, client, knowledge_headers, kb_a_id):
        """同一文档版本重复入队：复用台账，不新增行（防止连点上传索引五遍）。"""
        resp = _upload(client, knowledge_headers, kb_a_id, "async-幂等.txt", TXT)
        job_id = resp.json()["job_id"]

        from app.models.document import DocVersion, Document
        from app.models.jobs import IngestJob
        from app.services.ingest import enqueue_ingest

        db = SessionLocal()
        try:
            doc = db.query(Document).filter(Document.doc_name == "async-幂等.txt").first()
            version = db.get(DocVersion, doc.current_version_id)
            before = db.query(IngestJob).count()
            job, _ = enqueue_ingest(db, doc, version, created_by="tester")
            after = db.query(IngestJob).count()
            assert job.id == job_id, "应复用同一条台账"
            assert after == before, "不应新增任务记录"
        finally:
            db.close()

    def test_parse_failure_marks_failed_without_retry(self, client, knowledge_headers, kb_a_id):
        """解析不出文本属于不可重试错误：直接 failed，attempts 停在 1。"""
        resp = _upload(client, knowledge_headers, kb_a_id, "async-空白.txt", b"   \n\n   ")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "failed"
        job = client.get(f"/api/v1/jobs/{body['job_id']}", headers=knowledge_headers).json()
        assert job["stage"] == "failed"
        assert job["attempts"] == 1
        assert "解析失败" in (job["error"] or "")

    def test_retry_resets_and_reruns(self, client, knowledge_headers, kb_a_id):
        resp = _upload(client, knowledge_headers, kb_a_id, "async-重试.txt", TXT)
        job_id = resp.json()["job_id"]
        retried = client.post(f"/api/v1/jobs/{job_id}/retry", headers=knowledge_headers)
        assert retried.status_code == 200, retried.text
        job = retried.json()["job"]
        assert job["id"] == job_id
        assert job["stage"] == "indexed"      # inline 模式：重试请求内即执行完
        assert job["attempts"] == 1           # 重试后计数从 0 重新开始

    def test_cancel_queued_job(self, client, knowledge_headers, kb_a_id, monkeypatch):
        from app.services import ingest as ingest_mod

        monkeypatch.setattr(ingest_mod, "resolve_mode", lambda: "thread")
        monkeypatch.setattr(ingest_mod, "_start_thread", lambda job_id, delay=0: None)

        resp = _upload(client, knowledge_headers, kb_a_id, "async-取消.txt", TXT)
        job_id = resp.json()["job_id"]
        out = client.post(f"/api/v1/jobs/{job_id}/cancel", headers=knowledge_headers)
        assert out.status_code == 200, out.text
        assert out.json()["canceled"] is True
        job = client.get(f"/api/v1/jobs/{job_id}", headers=knowledge_headers).json()
        assert job["stage"] == "canceled"

        # 已结束的任务不能重复取消
        again = client.post(f"/api/v1/jobs/{job_id}/retry", headers=knowledge_headers)
        assert again.status_code == 200  # retry 允许（把 canceled 重新排队）

    def test_stats_endpoint(self, client, admin_headers):
        stats = client.get("/api/v1/jobs/stats", headers=admin_headers).json()
        assert stats["mode"] in ("inline", "thread", "celery")
        assert stats["jobs"]["indexed"] >= 1
        assert "broker" in stats and "recent" in stats

    def test_list_active_filter(self, client, admin_headers):
        out = client.get("/api/v1/jobs?active=true&limit=10", headers=admin_headers).json()
        assert set(out.keys()) == {"total", "items"}
        for item in out["items"]:
            assert item["stage"] in ("queued", "parsing", "chunking", "embedding")


class TestJobPermissions:
    def test_plain_user_forbidden(self, client, sales_headers):
        assert client.get("/api/v1/jobs", headers=sales_headers).status_code == 403
        assert client.get("/api/v1/jobs/stats", headers=sales_headers).status_code == 403

    def test_biz_group_isolation(self, client, knowledge_headers, knowledge_headers_b, kb_a_id):
        """b 组管理员看不到 a 组文档的任务（越权与不存在统一 404）。"""
        resp = _upload(client, knowledge_headers, kb_a_id, "async-隔离.txt", TXT)
        job_id = resp.json()["job_id"]
        assert client.get(f"/api/v1/jobs/{job_id}", headers=knowledge_headers_b).status_code == 404

        listing = client.get("/api/v1/jobs?limit=200", headers=knowledge_headers_b).json()
        assert all(item["id"] != job_id for item in listing["items"])


class TestProgressReporting:
    def test_pipeline_reports_progress(self, client, knowledge_headers, kb_a_id):
        """run_indexing 必须真的回调进度（否则前端进度条永远停在 0）。"""
        from app.models.document import DocVersion, Document
        from app.services.pipeline import run_indexing

        _upload(client, knowledge_headers, kb_a_id, "async-进度.txt", TXT)
        db = SessionLocal()
        try:
            doc = db.query(Document).filter(Document.doc_name == "async-进度.txt").first()
            assert doc and doc.current_version_id
            version = db.get(DocVersion, doc.current_version_id)
            assert version.raw_text
            seen: list[tuple[int, str]] = []
            run_indexing(doc.id, db, progress=lambda pct, msg: seen.append((pct, msg)))
            assert seen, "应至少上报一次进度"
            assert seen[-1][0] == 100
            assert any("向量化" in msg for _, msg in seen)
            assert [p for p, _ in seen] == sorted(p for p, _ in seen), "进度必须单调不减"
        finally:
            db.close()

    def test_cancel_stops_indexing(self, client, knowledge_headers, kb_a_id):
        """协作式取消：should_cancel 返回 True 时抛 JobCanceled，不留半截 chunk。"""
        from app.models.chunk import Chunk
        from app.models.document import DocVersion, Document
        from app.services.pipeline import JobCanceled, run_indexing

        _upload(client, knowledge_headers, kb_a_id, "async-中断.txt", TXT)
        db = SessionLocal()
        try:
            doc = db.query(Document).filter(Document.doc_name == "async-中断.txt").first()
            version = db.get(DocVersion, doc.current_version_id)
            before = db.query(Chunk).filter(Chunk.doc_version_id == version.id).count()
            assert before > 0
            with pytest.raises(JobCanceled):
                run_indexing(doc.id, db, should_cancel=lambda: True)
            db.rollback()  # 模拟调用方回滚未提交的"删旧 + 写新"
            # 取消不应破坏已有索引：回滚后旧 chunk 完整保留，后续可继续服务
            left = db.query(Chunk).filter(Chunk.doc_version_id == version.id).count()
            assert left == before
        finally:
            db.close()


class TestQueueStatus:
    def test_queue_status_shape(self, client):
        from app.services.ingest import queue_status

        db = SessionLocal()
        try:
            out = queue_status(db)
        finally:
            db.close()
        assert "mode" in out and "jobs" in out
        for key in ("queued", "parsing", "chunking", "embedding", "indexed", "failed", "canceled"):
            assert key in out["jobs"]
        assert out["broker"]["queue"] == "ingest"

    def test_mask_url(self):
        from app.services.ingest import _mask_url

        assert _mask_url("redis://127.0.0.1:6379/0") == "redis://127.0.0.1:6379/0"
        assert "***" in _mask_url("redis://:secret@127.0.0.1:6379/0")
        assert "secret" not in _mask_url("redis://:secret@127.0.0.1:6379/0")
