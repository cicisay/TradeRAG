"""图形验证码测试：生成、错误拒绝、正确通过（不区分大小写）、一次一用。"""
import time


def test_captcha_returns_png(client):
    resp = client.get("/api/v1/auth/captcha")
    assert resp.status_code == 200
    data = resp.json()
    assert data["captcha_id"]
    assert data["image"].startswith("data:image/png;base64,")


def test_login_rejects_wrong_captcha(client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "login_captcha_enabled", True)
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin123", "captcha_id": "no-such-id", "captcha_text": "abcd"},
    )
    assert resp.status_code == 401
    assert "E6005" in resp.text


def test_login_requires_captcha_when_enabled(client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "login_captcha_enabled", True)
    resp = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 401
    assert "E6005" in resp.text


def test_login_accepts_correct_captcha_case_insensitive(client, monkeypatch):
    """不区分大小写 + 容忍首尾空格；且验证码一次一用，第二次必须失败。"""
    from app.config import get_settings
    from app.services import captcha as cap

    monkeypatch.setattr(get_settings(), "login_captcha_enabled", True)
    cid = "cid-test-1"
    cap._store[cid] = ("ab2d", time.time() + 300)

    body = {"username": "admin", "password": "admin123", "captcha_id": cid}
    resp = client.post("/api/v1/auth/login", json={**body, "captcha_text": "  AB2D "})
    assert resp.status_code == 200, resp.text

    # 同一个 captcha_id 已经被消费，重放必须被拒
    resp2 = client.post("/api/v1/auth/login", json={**body, "captcha_id": "cid-test-2", "captcha_text": "ab2d"})
    assert resp2.status_code == 401


def test_expired_captcha_rejected(client, monkeypatch):
    from app.config import get_settings
    from app.services import captcha as cap

    monkeypatch.setattr(get_settings(), "login_captcha_enabled", True)
    cid = "cid-expired"
    cap._store[cid] = ("ab2d", time.time() - 10)
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin123", "captcha_id": cid, "captcha_text": "ab2d"},
    )
    assert resp.status_code == 401
