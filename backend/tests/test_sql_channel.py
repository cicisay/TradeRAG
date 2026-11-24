"""SQL 通道测试（PRD 7.4 五铁律 + FR-204/FR-703）。

每个测试对应一条铁律，便于验收时逐条核对：
- 铁律1 只读：query_only + ro 连接（写操作在 AST 就被拒，DB 层再兜底）
- 铁律2 限流：令牌桶超限 → E3002 + 降级
- 铁律3/4 禁止 LLM 生成 SQL / 必须来自注册模板：只有 T-XX 白名单，参数严格校验
- 铁律5 敏感表拒绝：基表/系统表不在白名单，AST 守卫直接拒
"""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db.session import SessionLocal
from app.models.sql_channel import SqlExecAudit
from app.services import sql_channel
from app.services.sql_channel import ALLOWED_VIEWS, guard_sql


@pytest.fixture(autouse=True)
def _reset_limiter():
    """每个测试前清空限流桶，避免用例之间互相影响。"""
    sql_channel.LIMITER.reset()
    yield
    sql_channel.LIMITER.reset()


# --------------------------------------------------------------------------- #
# AST 守卫（铁律 3/4/5）
# --------------------------------------------------------------------------- #
class TestAstGuard:
    def test_accepts_registered_view_select(self):
        ok, reason = guard_sql(
            "SELECT base_currency, rate FROM v_fx_rate WHERE base_currency = ? LIMIT 1"
        )
        assert ok, reason

    def test_rejects_base_table(self):
        """铁律5：直接查基表（可能含敏感列）必须拒绝。"""
        ok, reason = guard_sql("SELECT * FROM quotation")
        assert not ok and "白名单" in reason

    def test_rejects_system_table(self):
        ok, reason = guard_sql("SELECT name FROM sqlite_master")
        assert not ok

    def test_rejects_union(self):
        ok, reason = guard_sql("SELECT product_code FROM v_quotation UNION SELECT hs_code FROM v_tariff_rate")
        assert not ok and "UNION" in reason

    def test_rejects_multiple_statements(self):
        ok, reason = guard_sql("SELECT 1; DROP TABLE fx_rate")
        assert not ok and "单条" in reason

    def test_rejects_write(self):
        ok, reason = guard_sql("DELETE FROM v_fx_rate")
        assert not ok

    def test_rejects_dangerous_function(self):
        ok, reason = guard_sql("SELECT load_extension('evil')")
        assert not ok and "函数" in reason

    def test_rejects_cross_schema(self):
        ok, reason = guard_sql("SELECT * FROM main.v_fx_rate")
        assert not ok

    def test_whitelist_is_exactly_four_views(self):
        assert ALLOWED_VIEWS == {"v_fx_rate", "v_quotation", "v_incoterms_cost", "v_tariff_rate"}


# --------------------------------------------------------------------------- #
# 模板查询（铁律1/3/4 + 行业校验）
# --------------------------------------------------------------------------- #
class TestTemplateQuery:
    def test_list_four_templates(self, client, admin_headers):
        resp = client.get("/api/v1/sql/templates", headers=admin_headers)
        assert resp.status_code == 200
        ids = {t["template_id"] for t in resp.json()}
        assert ids == {"T-FX", "T-QUO", "T-INC", "T-TAR"}

    def test_fx_query_ok_with_source_and_notice(self, client, admin_headers):
        resp = client.post("/api/v1/sql/query", headers=admin_headers, json={
            "template_id": "T-FX",
            "params": {"base_currency": "USD", "quote_currency": "CNY", "date": date.today().isoformat()},
        })
        data = resp.json()
        assert data["status"] == "ok", data
        row = data["rows"][0]
        assert row["rate"] == pytest.approx(7.085, rel=1e-4)
        assert row["source"]
        assert "仅供参考" in data["answer"]
        assert data["llm_sql_generated"] is False  # 铁律3：SQL 由服务端渲染

    def test_fx_stale_rate_refused(self, client, admin_headers):
        """汇率缺失/过期 > 7 天禁止硬算（PRD 7.4 行业特有校验）。"""
        future = (date.today() + timedelta(days=60)).isoformat()
        resp = client.post("/api/v1/sql/query", headers=admin_headers, json={
            "template_id": "T-FX",
            "params": {"base_currency": "USD", "quote_currency": "CNY", "date": future},
        })
        data = resp.json()
        assert data["status"] == "error"
        assert data["error_code"] == "E3003"
        assert data["degraded"] is True

    def test_unregistered_template_rejected(self, client, admin_headers):
        """铁律4：没有注册模板 = 没有查询入口。"""
        resp = client.post("/api/v1/sql/query", headers=admin_headers, json={
            "template_id": "T-RAW", "params": {"sql": "SELECT * FROM quotation"},
        })
        data = resp.json()
        assert data["status"] == "denied"
        assert data["error_code"] == "E3001"

    def test_extra_param_rejected(self, client, admin_headers):
        """加一个 biz_group 想查别组报价 → 参数严格模式直接拒（铁律5 行级过滤）。"""
        resp = client.post("/api/v1/sql/query", headers=admin_headers, json={
            "template_id": "T-QUO",
            "params": {"product_code": "SKU-1001", "date": date.today().isoformat(), "biz_group": "biz_a"},
        })
        data = resp.json()
        assert data["status"] == "denied"
        assert data["error_code"] == "E3004"

    def test_quotation_row_level_by_biz_group(self, client, admin_headers, sales_headers):
        """同一产品不同业务组价格不同：服务端用登录用户的业务组过滤。"""
        today = date.today().isoformat()
        admin = client.post("/api/v1/sql/query", headers=admin_headers, json={
            "template_id": "T-QUO", "params": {"product_code": "SKU-1001", "date": today},
        }).json()
        sales = client.post("/api/v1/sql/query", headers=sales_headers, json={
            "template_id": "T-QUO", "params": {"product_code": "SKU-1001", "date": today},
        }).json()
        assert admin["rows"][0]["unit_price"] == pytest.approx(12.80)
        assert sales["rows"][0]["unit_price"] == pytest.approx(12.50)
        assert sales["rows"][0]["biz_group"] == "biz_a"

    def test_audit_written(self, client, admin_headers):
        client.post("/api/v1/sql/query", headers=admin_headers, json={
            "template_id": "T-TAR", "params": {"dest_country": "US", "hs_code": "7307210000"},
        })
        db = SessionLocal()
        try:
            row = db.query(SqlExecAudit).order_by(SqlExecAudit.id.desc()).first()
            assert row is not None
            assert row.template_id == "T-TAR"
            assert row.status == "success"
            assert row.rendered_sql and "v_tariff_rate" in row.rendered_sql
        finally:
            db.close()


# --------------------------------------------------------------------------- #
# 自然语言路由 + 调试台（FR-703）
# --------------------------------------------------------------------------- #
class TestRouting:
    def test_route_tariff_question(self, client, admin_headers):
        resp = client.post("/api/v1/sql/route", headers=admin_headers, json={
            "query": "LED灯串出口沙特，HS 85395000 关税多少？",
        })
        data = resp.json()
        assert data["routed"] is True
        assert data["template_id"] == "T-TAR"
        assert data["status"] == "ok"
        assert data["rendered_sql"].startswith("SELECT")
        assert data["rows"][0]["tariff_rate"] == pytest.approx(5.0)

    def test_route_non_data_question(self, client, admin_headers):
        resp = client.post("/api/v1/sql/route", headers=admin_headers, json={"query": "报关需要哪些单证？"})
        data = resp.json()
        assert data["routed"] is False

    def test_route_requires_debug_permission(self, client, sales_headers):
        resp = client.post("/api/v1/sql/route", headers=sales_headers, json={"query": "美元汇率"})
        assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# 限流（铁律2）
# --------------------------------------------------------------------------- #
class TestRateLimit:
    def test_user_limit_degrades(self, client, sales_headers, monkeypatch):
        settings = get_settings()
        monkeypatch.setattr(settings, "sql_user_rate_per_min", 2, raising=False)
        sql_channel.LIMITER.reset()

        payload = {"template_id": "T-FX", "params": {
            "base_currency": "USD", "quote_currency": "CNY", "date": date.today().isoformat()}}
        statuses = [client.post("/api/v1/sql/query", headers=sales_headers, json=payload).json()["status"]
                    for _ in range(4)]
        assert statuses[:2] == ["ok", "ok"]
        assert "denied" in statuses[2:]
        denied = client.post("/api/v1/sql/query", headers=sales_headers, json=payload).json()
        assert denied["error_code"] == "E3002"
        assert denied["degraded"] is True


# --------------------------------------------------------------------------- #
# 端到端：聊天链路自动走 SQL 通道
# --------------------------------------------------------------------------- #
class TestChatSqlChannel:
    def test_chat_routes_to_sql_and_answers_with_numbers(self, client, admin_headers):
        resp = client.post("/api/v1/chat", headers=admin_headers,
                           json={"query": "美元兑人民币汇率是多少？"})
        assert resp.status_code == 200
        text = resp.text
        assert '"template_id": "T-FX"' in text
        assert "7.0850" in text
        assert "仅供参考" in text
        # 结构化回答不编造引用
        assert '"refs": []' in text

    def test_chat_falls_back_to_docs_when_sql_empty(self, client, admin_headers):
        """查一个不存在的产品：SQL 无结果 → 降级文档检索，不报错。"""
        resp = client.post("/api/v1/chat", headers=admin_headers,
                           json={"query": "SKU-9999 的报价是多少"})
        text = resp.text
        assert "sql_degraded" in text
        assert "event: done" in text
