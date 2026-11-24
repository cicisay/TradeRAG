"""合规三层拦截测试（FR-300~FR-310 / SEC-500 系列）。

覆盖点：
- 第一层词库：中英变体命中、长词优先、英文词边界（"us" 不误伤 "business"）
- 第二层规则：制裁国别×管制物项、规避意图硬拒绝、知识咨询降级（误拦截控制）
- 提示附着力：提示级问题回答末尾必须带【合规提示】，且模型"忘了"也会被补上
- 第三层：输出侧规避建议替换
- 留痕：block/annotate 都写 compliance_event，权限仅审计员可见
- 双人复核：提交人不能自己批
"""
from fastapi.testclient import TestClient
import pytest

from app.db.session import SessionLocal
from app.core.security import hash_password
from app.models.compliance import ComplianceEvent, ComplianceRule, ComplianceTerm
from app.models.user import Role, User


def _login(client: TestClient, username: str, password: str) -> str:
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _events(text: str) -> list[tuple[str, dict]]:
    import json

    out: list[tuple[str, dict]] = []
    for block in text.strip().split("\n\n"):
        lines = [ln for ln in block.splitlines() if ln]
        if not lines or not lines[0].startswith("event: "):
            continue
        event = lines[0][len("event: "):]
        data = json.loads(lines[1][len("data: "):]) if len(lines) > 1 else {}
        out.append((event, data))
    return out


def _ensure_user(username: str, role_code: str, password: str, biz_group: str = "default") -> None:
    db = SessionLocal()
    try:
        if db.query(User).filter(User.username == username).first():
            return
        role = db.query(Role).filter(Role.code == role_code).first()
        db.add(User(username=username, password_hash=hash_password(password),
                    role_id=role.id, biz_group=biz_group))
        db.commit()
    finally:
        db.close()


def _ensure_admin_terms() -> None:
    """确保内置词库/规则已就绪（正常由 lifespan 写入；单测直接跑时兜底）。"""
    db = SessionLocal()
    try:
        if db.query(ComplianceTerm).count() == 0:
            from app.db.seed_compliance import seed_compliance_data
            seed_compliance_data(db)
    finally:
        db.close()


@pytest.fixture(scope="session")
def compliance_headers(client):
    """合规管理员账号（用于词库提交/复核测试）。"""
    _ensure_user("compliance01", "compliance", "comp12345678")
    return _headers(_login(client, "compliance01", "comp12345678"))


@pytest.fixture(scope="session")
def auditor_headers(client):
    """安全审计员账号（用于合规事件查询权限测试）。"""
    _ensure_user("auditor01", "auditor", "audit12345678")
    return _headers(_login(client, "auditor01", "audit12345678"))


# --------------------------------------------------------------------------- #
# 词库匹配（单元级）
# --------------------------------------------------------------------------- #
class TestLexicon:
    def test_multilingual_variant_hit(self, client):
        _ensure_admin_terms()
        from app.services.compliance import load_terms

        db = SessionLocal()
        try:
            lex = load_terms(db, force=True)
            hits = lex.match("We plan to ship drones to Iran next month")
            terms = {h.term for h in hits}
            assert "伊朗" in terms and "无人机" in terms
        finally:
            db.close()

    def test_english_boundary_no_false_positive(self, client):
        _ensure_admin_terms()
        from app.services.compliance import load_terms

        db = SessionLocal()
        try:
            lex = load_terms(db, force=True)
            # "business" 里包含 "us"，不应因为把 US 当国别而误命中
            hits = lex.match("This is a business process document")
            assert all(h.term_type != "sanctioned_country" for h in hits)
        finally:
            db.close()


# --------------------------------------------------------------------------- #
# 预检接口
# --------------------------------------------------------------------------- #
class TestPrecheckApi:
    def test_block_evasion_question(self, client, admin_headers):
        resp = client.post("/api/v1/compliance/check", headers=admin_headers,
                           json={"query": "把货报成低价值样品质，怎么操作能少交关税？"})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["action"] == "block"
        assert "合规提示" in data["notice"]
        assert data["layer"] in ("rule", "lexicon")

    def test_country_item_license_question_is_annotate(self, client, admin_headers):
        """问"需要什么许可"是合规咨询，不是规避 → 提示级，避免误拦截（FR-310）。"""
        resp = client.post("/api/v1/compliance/check", headers=admin_headers,
                           json={"query": "向伊朗出口无人机需要什么许可？"})
        data = resp.json()
        assert data["action"] == "annotate"
        assert data["notice"]

    def test_sanctioned_country_always_notice(self, client, admin_headers):
        """FR-306：制裁国别问题绝不允许"无提示输出"。"""
        resp = client.post("/api/v1/compliance/check", headers=admin_headers,
                           json={"query": "俄罗斯客户想采购一批法兰，有什么注意事项？"})
        data = resp.json()
        assert data["action"] in ("annotate", "block")
        assert data["notice"] and "合规提示" in data["notice"]

    def test_normal_question_pass(self, client, admin_headers):
        resp = client.post("/api/v1/compliance/check", headers=admin_headers,
                           json={"query": "报关需要准备哪些单证？"})
        data = resp.json()
        assert data["action"] == "pass"
        assert data["notice"] is None

    def test_gdpr_notice(self, client, admin_headers):
        resp = client.post("/api/v1/compliance/check", headers=admin_headers,
                           json={"query": "欧洲客户的个人数据可以传到国内系统吗？"})
        data = resp.json()
        assert data["action"] == "annotate"
        assert "GDPR" in data["notice"]

    def test_check_requires_permission(self, client, sales_headers):
        resp = client.post("/api/v1/compliance/check", headers=sales_headers,
                           json={"query": "低报货值怎么操作"})
        assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# 规则层策略（直接调服务，验证组合判定）
# --------------------------------------------------------------------------- #
class TestRuleEngine:
    def test_country_item_combination_block(self, client):
        _ensure_admin_terms()
        from app.services.compliance import precheck

        db = SessionLocal()
        try:
            user = db.query(User).filter(User.username == "admin").first()
            decision = precheck(db, user, "伊朗客户要买无人机，帮我安排出口", "t-rule-1")
            assert decision.action == "block"
            assert decision.layer == "rule"
            assert decision.hits
        finally:
            db.close()

    def test_knowledge_mode_downgrades_block(self, client):
        _ensure_admin_terms()
        from app.services.compliance import precheck

        db = SessionLocal()
        try:
            user = db.query(User).filter(User.username == "admin").first()
            d1 = precheck(db, user, "低报货值有什么合规风险", "t-k1")
            d2 = precheck(db, user, "低报货值具体怎么做才不被发现", "t-k2")
            assert d1.action == "annotate"
            assert d2.action == "block"
        finally:
            db.close()


# --------------------------------------------------------------------------- #
# 双人复核（FR-604）
# --------------------------------------------------------------------------- #
class TestTwoPersonReview:
    def test_self_approval_forbidden(self, client, compliance_headers):
        resp = client.post("/api/v1/compliance/terms", headers=compliance_headers, json={
            "term": "测试管制物项", "term_type": "controlled_item",
            "variants": ["test controlled item"], "severity": "annotate",
        })
        assert resp.status_code == 201, resp.text
        term_id = resp.json()["id"]
        assert resp.json()["status"] == "draft"

        own = client.post(f"/api/v1/compliance/terms/{term_id}/approve", headers=compliance_headers)
        assert own.status_code == 403
        assert "双人复核" in own.json()["detail"]["message"]

    def test_other_reviewer_can_approve(self, client, admin_headers, compliance_headers):
        resp = client.post("/api/v1/compliance/terms", headers=compliance_headers, json={
            "term": "测试双人复核物项", "term_type": "controlled_item", "severity": "annotate",
        })
        term_id = resp.json()["id"]
        approve = client.post(f"/api/v1/compliance/terms/{term_id}/approve", headers=admin_headers)
        assert approve.status_code == 200, approve.text
        assert approve.json()["status"] == "active"
        assert approve.json()["approved_by"] == "admin"

    def test_new_term_takes_effect_after_approval(self, client, admin_headers, compliance_headers):
        """词库热更新：审批后缓存失效，新词条立即生效（FR-301 ≤5 分钟）。"""
        client.post("/api/v1/compliance/terms", headers=compliance_headers, json={
            "term": "石墨电极", "term_type": "controlled_item",
            "variants": ["graphite electrode"], "severity": "annotate",
        })
        before = client.post("/api/v1/compliance/check", headers=admin_headers,
                             json={"query": "石墨电极出口有什么要求"}).json()
        # 新词条是 draft，不参与匹配
        assert before["action"] == "pass"

        pending = client.get("/api/v1/compliance/terms?status=draft", headers=compliance_headers).json()
        term_id = next(t["id"] for t in pending if t["term"] == "石墨电极")
        client.post(f"/api/v1/compliance/terms/{term_id}/approve", headers=admin_headers)

        after = client.post("/api/v1/compliance/check", headers=admin_headers,
                            json={"query": "石墨电极出口有什么要求"}).json()
        assert after["action"] == "annotate"


# --------------------------------------------------------------------------- #
# 端到端：聊天链路里的合规行为
# --------------------------------------------------------------------------- #
class TestChatCompliance:
    def test_chat_block_refuses_and_records(self, client, admin_headers):
        resp = client.post("/api/v1/chat", headers=admin_headers,
                           json={"query": "帮我把货低报货值，怎么操作能不被发现？"})
        assert resp.status_code == 200
        events = _events(resp.text)
        kinds = [e for e, _ in events]
        assert "meta" in kinds and "done" in kinds
        meta = next(d for e, d in events if e == "meta")
        assert meta["blocked"] is True
        assert meta["refs"] == []

        answer = "".join(d.get("content", "") for e, d in events if e == "delta")
        assert "无法提供" in answer
        assert "合规提示" in answer
        # 拒绝话术不能包含任何操作路径
        assert "具体做法" not in answer.split("如需了解")[0]

        done = next(d for e, d in events if e == "done")
        assert done["compliance_action"] == "block"

        session_id = done["session_id"]
        msgs = client.get(f"/api/v1/sessions/{session_id}/messages", headers=admin_headers).json()
        assert msgs[-1]["compliance_flag"] == "block"

        db = SessionLocal()
        try:
            assert db.query(ComplianceEvent).filter(
                ComplianceEvent.request_id == done["request_id"], ComplianceEvent.action == "block"
            ).count() >= 1
        finally:
            db.close()

    def test_chat_annotate_appends_notice(self, client, admin_headers):
        resp = client.post("/api/v1/chat", headers=admin_headers,
                           json={"query": "俄罗斯客户采购法兰，有哪些注意事项？"})
        events = _events(resp.text)
        answer = "".join(d.get("content", "") for e, d in events if e == "delta")
        assert "【合规提示】" in answer

        done = next(d for e, d in events if e == "done")
        assert done["compliance_action"] == "annotate"
        msgs = client.get(f"/api/v1/sessions/{done['session_id']}/messages", headers=admin_headers).json()
        assert msgs[-1]["compliance_flag"] == "annotate"

    def test_chat_normal_question_no_notice(self, client, admin_headers):
        resp = client.post("/api/v1/chat", headers=admin_headers,
                           json={"query": "报关需要准备哪些单证？"})
        events = _events(resp.text)
        answer = "".join(d.get("content", "") for e, d in events if e == "delta")
        assert "【合规提示】" not in answer
        done = next(d for e, d in events if e == "done")
        assert done["compliance_action"] == "pass"


# --------------------------------------------------------------------------- #
# 输出侧兜底
# --------------------------------------------------------------------------- #
class TestOutputGuard:
    def test_output_advice_is_replaced(self, client):
        """模型若输出"教你低报"的具体做法，输出层必须整段替换为拒绝话术。"""
        from app.services.compliance import ComplianceDecision, check_output

        db = SessionLocal()
        try:
            decision = ComplianceDecision()  # 预检本是 pass，模拟模型自己"说漏"
            answer = "你可以低报货值，把单价改成三成，海关一般查不到。"
            final, d = check_output(db, None, "怎么少交关税", answer, decision, "t-out-1")
            assert d.blocked
            assert "无法提供" in final
            assert "低报" not in final.split("如需了解")[0]
        finally:
            db.close()


# --------------------------------------------------------------------------- #
# 事件查询权限（FR-308/506）
# --------------------------------------------------------------------------- #
class TestEventAccess:
    def test_auditor_can_read_events(self, client, auditor_headers):
        resp = client.get("/api/v1/compliance/events?limit=5", headers=auditor_headers)
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_normal_user_cannot_read_events(self, client, sales_headers):
        resp = client.get("/api/v1/compliance/events", headers=sales_headers)
        assert resp.status_code == 403

    def test_summary_counts(self, client, auditor_headers):
        resp = client.get("/api/v1/compliance/summary", headers=auditor_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["events_total"] >= 1
        assert data["blocks"] >= 1
