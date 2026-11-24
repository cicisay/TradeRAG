"""认证与 RBAC 接口测试（对应 FR-601/602/603 验收）。

覆盖：
1. 登录成功 / 密码错误 / 停用账号
2. 令牌鉴权：/me 返回身份与权限；无令牌 401；伪造令牌 401
3. RBAC：管理员可访问管理端点；普通用户 403（E6003）
"""
BAD_TOKEN_HEADERS = {"Authorization": "Bearer fake.invalid.token"}


class TestLogin:
    def test_login_success(self, client):
        resp = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["token_type"] == "bearer"
        assert len(data["access_token"]) > 50
        assert data["expires_in"] > 0

    def test_login_wrong_password(self, client):
        resp = client.post("/api/v1/auth/login", json={"username": "admin", "password": "wrong-password"})
        assert resp.status_code == 401
        assert resp.json()["detail"]["error_code"] == "E6001"

    def test_login_unknown_user_same_message(self, client):
        """用户不存在与密码错误必须同文案（防用户名枚举）。"""
        r1 = client.post("/api/v1/auth/login", json={"username": "nobody", "password": "whatever123"})
        r2 = client.post("/api/v1/auth/login", json={"username": "admin", "password": "wrong-password"})
        assert r1.status_code == r2.status_code == 401
        assert r1.json()["detail"]["message"] == r2.json()["detail"]["message"]


class TestLoginPortal:
    """登录入口隔离：后台账号不得从「用户登录」入口进入，反之亦然。

    此前前端 tab 只是装饰——admin 在用户入口照样能登进来（2026-11-09 修复）。
    """

    def test_admin_blocked_on_user_portal(self, client):
        """管理员账号在「用户登录」入口 → 403 E6006，并告知应去哪个入口。"""
        resp = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "admin123", "role": "user"},
        )
        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["error_code"] == "E6006"
        assert detail["portal"] == "admin"
        assert "管理员登录" in detail["message"]

    def test_admin_allowed_on_admin_portal(self, client):
        resp = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "admin123", "role": "admin"},
        )
        assert resp.status_code == 200
        assert resp.json()["access_token"]

    def test_user_blocked_on_admin_portal(self, client):
        """普通用户从「管理员登录」入口 → 403 E6006，portal=user。"""
        resp = client.post(
            "/api/v1/auth/login",
            json={"username": "sales01", "password": "sales123456", "role": "admin"},
        )
        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["error_code"] == "E6006"
        assert detail["portal"] == "user"

    def test_user_allowed_on_user_portal(self, client):
        resp = client.post(
            "/api/v1/auth/login",
            json={"username": "sales01", "password": "sales123456", "role": "user"},
        )
        assert resp.status_code == 200
        assert resp.json()["access_token"]

    def test_knowledge_admin_uses_admin_portal(self, client):
        """知识管理员有后台权限，归入管理员入口（knowledge_a 属知识管理员）。"""
        resp = client.post(
            "/api/v1/auth/login",
            json={"username": "knowledge_a", "password": "kn12345678", "role": "user"},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["portal"] == "admin"

    def test_wrong_password_does_not_reveal_portal(self, client):
        """密码错时仍只报 E6001，不能靠入口报错探测账号角色。"""
        resp = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "wrong-password", "role": "user"},
        )
        assert resp.status_code == 401
        assert resp.json()["detail"]["error_code"] == "E6001"

    def test_role_field_optional_for_api_clients(self, client):
        """不传 role 的直连调用保持可用（自动化脚本/集成方兼容）。"""
        resp = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
        assert resp.status_code == 200


class TestMe:
    def test_me_with_token(self, client, admin_headers):
        resp = client.get("/api/v1/auth/me", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["role"] == "admin"
        assert "admin:users" in data["permissions"]
        assert "password_hash" not in resp.text  # 任何情况下不泄漏哈希

    def test_me_without_token(self, client):
        resp = client.get("/api/v1/auth/me")
        assert resp.status_code == 401

    def test_me_with_forged_token(self, client):
        resp = client.get("/api/v1/auth/me", headers=BAD_TOKEN_HEADERS)
        assert resp.status_code == 401


class TestRBAC:
    def test_admin_allowed(self, client, admin_headers):
        resp = client.get("/api/v1/admin/ping", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["pong"] is True

    def test_normal_user_forbidden(self, client, sales_headers):
        """普通用户访问管理端点 → 403 E6003。"""
        resp = client.get("/api/v1/admin/ping", headers=sales_headers)
        assert resp.status_code == 403
        assert resp.json()["detail"]["error_code"] == "E6003"

    def test_normal_user_biz_group(self, client, sales_headers):
        """业务组隔离前提：普通用户身份里带自己的 biz_group（FR-603）。"""
        resp = client.get("/api/v1/auth/me", headers=sales_headers)
        assert resp.status_code == 200
        assert resp.json()["biz_group"] == "biz_a"
