# -*- coding: utf-8 -*-
"""扩展模块集成测试：浏览器/脚本 -> Flask 接口 -> SQLite 的端到端链路。

与 tests/test_extensions.py（模块内不变量）互补：这里只关心
「接口接对了没有、权限有没有真的拦住、数据有没有真的落库」。

运行： pip install -r requirements-dev.txt && pytest tests/test_integration.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                            # noqa: E402
import pytest                                            # noqa: E402

from core import database as db                          # noqa: E402

ADMIN = config.DEFAULT_ADMIN_USER
ADMIN_PWD = config.DEFAULT_ADMIN_PASSWORD


@pytest.fixture(scope="module")
def webapp(tmp_path_factory):
    """用独立临时库加载一次 Flask 应用（避免污染真实 data/campus_market.db）。

    模块级 fixture 不能用 function 级的 monkeypatch，这里手动改并还原 config.DB_PATH。
    """
    old_path = config.DB_PATH
    config.DB_PATH = tmp_path_factory.mktemp("it") / "it.db"
    db.close_conn()
    try:
        import app as web                                # noqa: E402  导入即建表 + 建内置管理员
        web.app.config["TESTING"] = True
        # app 可能已被其它用例导入过（模块缓存），这里显式建一次表，保证临时库 schema 就绪
        db.init_db()
        from core import rbac
        rbac.ensure_default_admin()
        yield web
    finally:
        config.DB_PATH = old_path
        db.close_conn()


@pytest.fixture()
def client(webapp):
    return webapp.app.test_client()


def login(client, username=ADMIN, password=ADMIN_PWD):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.get_json()
    return r.get_json()


# ------------------------------------------------------------------ #
# 页面与基础链路
# ------------------------------------------------------------------ #
def test_pages_still_render(client):
    for path in ("/", "/admin", "/seller"):
        assert client.get(path).status_code == 200, path


def test_login_me_logout(client):
    j = login(client)
    assert j["user"]["role"] == "admin" and j["token"]
    me = client.get("/api/auth/me").get_json()
    assert me["user"]["username"] == ADMIN
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").get_json()["user"] is None


def test_login_failure_is_logged(client):
    r = client.post("/api/auth/login", json={"username": ADMIN, "password": "wrong-pwd"})
    assert r.status_code == 401
    j = client.get("/api/oplog?action=login").get_json()
    assert any(l["result"] == "denied" for l in j["logs"])


def test_token_can_be_used_without_cookie(client):
    token = login(client)["token"]
    anon = client.__class__(client.application)           # 独立 cookie 的新客户端
    r = anon.get("/api/auth/me", headers={"X-Auth-Token": token})
    assert r.get_json()["user"]["username"] == ADMIN
    assert anon.get("/api/auth/me").get_json()["user"] is None


# ------------------------------------------------------------------ #
# 权限
# ------------------------------------------------------------------ #
def test_seller_cannot_manage_users(client, webapp):
    login(client)
    r = client.post("/api/users", json={"username": "seller01", "password": "secret123",
                                        "role": "seller", "seller_id": "20230101"})
    assert r.get_json()["user"]["role"] == "seller"

    seller = webapp.app.test_client()
    assert login(seller, "seller01", "secret123")["user"]["role"] == "seller"
    denied = seller.get("/api/users")
    assert denied.status_code == 403
    assert seller.post("/api/items/1/review", json={"status": "pass"}).status_code == 403


def test_rbac_enforce_blocks_anonymous(client, webapp, monkeypatch):
    monkeypatch.setattr(config, "RBAC_ENFORCE", True)
    anon = webapp.app.test_client()
    assert anon.get("/api/users").status_code == 401


def test_role_change_and_last_admin_guard(client):
    login(client)
    users = client.get("/api/users").get_json()["users"]
    admin = next(u for u in users if u["role"] == "admin")
    r = client.post(f"/api/users/{admin['id']}/role", json={"role": "seller"})
    assert r.status_code == 400                            # 唯一管理员不可降级
    r = client.post("/api/users", json={"username": "auditor01", "password": "secret123",
                                        "role": "auditor"})
    uid = r.get_json()["user"]["id"]
    assert client.post(f"/api/users/{uid}/role", json={"role": "seller"}).get_json()["ok"]
    assert client.delete(f"/api/users/{uid}").get_json()["ok"]


def test_permissions_endpoint(client):
    j = client.get("/api/permissions").get_json()
    assert "admin" in j["matrix"] and "item.review" in j["permissions"][0]["key"] \
        or any(p["key"] == "item.review" for p in j["permissions"])


# ------------------------------------------------------------------ #
# 导入导出
# ------------------------------------------------------------------ #
def test_export_csv_endpoint(client):
    login(client)
    r = client.get("/api/export?fmt=csv&limit=10")
    assert r.status_code == 200 and r.headers["Content-Type"].startswith("text/csv")
    assert r.get_data(as_text=True).startswith("\ufeff")


def test_export_json_endpoint(client):
    login(client)
    r = client.get("/api/export?fmt=json&limit=5")
    assert r.get_json()["count"] >= 0 and "items" in r.get_json()


def test_import_dry_run_then_real(client):
    login(client)
    rows = [{"title": "高等数学同济第七版", "description": "教材八成新，无缺页", "price": "15"},
            {"title": "出一把开刃折叠刀", "description": "防身用，加微信，先款后货"}]
    before = db.count_items()

    dry = client.post("/api/import", json={"rows": rows, "mode": "submit",
                                           "dry_run": True}).get_json()["report"]
    assert dry["dry_run"] is True and db.count_items() == before

    real = client.post("/api/import", json={"rows": rows, "mode": "submit"}).get_json()["report"]
    assert real["ok"] == 2 and db.count_items() == before + 2
    assert real["counts"]["reject"] >= 1                   # 违禁品必须被拦下


def test_import_csv_text_and_bad_content(client):
    login(client)
    csv_text = "标题,描述,价格\n线性代数,几乎全新,20\n"
    j = client.post("/api/import", json={"content": csv_text, "mode": "audit"}).get_json()
    assert j["report"]["ok"] == 1
    bad = client.post("/api/import", json={"content": ""})
    assert bad.status_code == 400


# ------------------------------------------------------------------ #
# 批量任务
# ------------------------------------------------------------------ #
def test_batch_job_lifecycle(client):
    login(client)
    j = client.post("/api/batch/jobs", json={
        "op": "submit",
        "payload": {"items": [{"title": "线性代数教材", "description": "九成新", "price": 18}]}
    }).get_json()
    job = j["job"]
    assert job["status"] == "done" and job["success"] == 1

    got = client.get(f"/api/batch/jobs/{job['id']}").get_json()
    assert got["job"]["id"] == job["id"]
    listed = client.get("/api/batch/jobs?limit=5").get_json()
    assert listed["ok"] and listed["stats"]["total"] >= 1
    assert client.get("/api/batch/ops").get_json()["ops"]


def test_batch_review_through_api(client):
    login(client)
    item_id = client.post("/api/items", json={
        "title": "大学英语教材", "description": "八成新，无笔记", "price": 12,
        "seller_id": "20230102"}).get_json()["item"]["id"]
    j = client.post("/api/batch/jobs", json={
        "op": "review", "payload": {"ids": [item_id], "status": "reject", "note": "批量复核"},
    }).get_json()
    assert j["job"]["failed"] == 0
    assert db.get_item(item_id)["status"] == "reject"


# ------------------------------------------------------------------ #
# 通知与操作日志
# ------------------------------------------------------------------ #
def test_broadcast_and_inbox(client):
    login(client)
    sent = client.post("/api/notifications", json={
        "title": "系统维护", "content": "今晚 22:00 停机升级"}).get_json()
    assert sent["sent"] >= 1

    inbox = client.get("/api/notifications").get_json()["notifications"]
    assert any(n["title"] == "系统维护" for n in inbox)
    summary = client.get("/api/notifications/summary").get_json()["summary"]
    assert summary["unread"] >= 1

    first = inbox[0]
    assert client.post(f"/api/notifications/{first['id']}/read").get_json()["ok"]
    assert client.post("/api/notifications/read-all").get_json()["ok"]
    assert client.get("/api/notifications/summary").get_json()["summary"]["unread"] == 0


def test_notifications_require_login(client, webapp):
    anon = webapp.app.test_client()
    assert anon.get("/api/notifications").status_code == 401


def test_review_writes_oplog_and_notifies(client):
    login(client)
    item_id = client.post("/api/items", json={
        "title": "概率论教材", "description": "全新未拆封", "price": 25,
        "seller_id": "20230103"}).get_json()["item"]["id"]
    assert client.post(f"/api/items/{item_id}/review", json={
        "status": "reject", "note": "疑似盗版"}).get_json()["ok"]

    logs = client.get("/api/oplog?action=item.review").get_json()["logs"]
    assert any(str(l["target_id"]) == str(item_id) for l in logs)

    # 卖家收到人工复核结果通知（seller_id 未被账号绑定时按学号投递）
    assert client.get("/api/notifications?user_id=20230103").get_json()["notifications"]


def test_oplog_stats_endpoint(client):
    login(client)
    j = client.get("/api/oplog/stats").get_json()
    assert j["ok"] and j["stats"]["total"] >= 1 and j["retention_days"] >= 0
