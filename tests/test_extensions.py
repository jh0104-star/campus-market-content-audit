# -*- coding: utf-8 -*-
"""扩展模块单元测试（pytest）：权限 / 操作日志 / 通知 / 导入导出 / 批量任务。

与 tests/test_core.py 的分工：这里只测"扩展模块自身的不变量"，
端到端链路（浏览器 → 接口 → 库）放在 tests/integration_test.py。

运行： pip install -r requirements-dev.txt && pytest -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                            # noqa: E402
import pytest                                            # noqa: E402

from core import batch, dataio, database as db, notify, oplog, rbac  # noqa: E402


@pytest.fixture()
def tmpdb(tmp_path, monkeypatch):
    """每个用例用独立临时库，避免相互污染 / 污染真实 data/campus_market.db。"""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "ext.db")
    db.close_conn()
    db.init_db()
    yield
    db.close_conn()


# ------------------------------------------------------------------ #
# 权限管理
# ------------------------------------------------------------------ #
def test_password_is_hashed_not_plaintext(tmpdb):
    uid = rbac.create_user("stu001", "secret123", role="seller", seller_id="20230101")
    raw = db.get_user(uid)
    assert raw["pwd_hash"] and "secret123" not in raw["pwd_hash"]
    assert raw["pwd_salt"] and raw["pwd_salt"] not in raw["pwd_hash"]


def test_authenticate_success_and_failure(tmpdb):
    rbac.create_user("stu002", "secret123", role="seller")
    assert rbac.authenticate("stu002", "secret123")["username"] == "stu002"
    assert rbac.authenticate("stu002", "wrong") is None
    assert rbac.authenticate("nobody", "secret123") is None


def test_disabled_user_cannot_login(tmpdb):
    uid = rbac.create_user("stu003", "secret123", role="seller")
    rbac.set_status(uid, "disabled")
    assert rbac.authenticate("stu003", "secret123") is None


def test_public_user_hides_secrets(tmpdb):
    uid = rbac.create_user("stu004", "secret123", role="seller")
    pub = rbac.get_user(uid)
    assert "pwd_hash" not in pub and "pwd_salt" not in pub


def test_create_user_validates(tmpdb):
    with pytest.raises(ValueError):
        rbac.create_user("ab", "secret123")                  # 用户名过短
    with pytest.raises(ValueError):
        rbac.create_user("stu005", "123")                    # 口令过短
    with pytest.raises(ValueError):
        rbac.create_user("stu005", "secret123", role="boss")  # 角色不存在
    rbac.create_user("stu005", "secret123")
    with pytest.raises(ValueError):
        rbac.create_user("stu005", "secret123")              # 重名


def test_role_permission_matrix(tmpdb):
    assert rbac.can("admin", "item.delete")
    assert not rbac.can("auditor", "item.delete")
    assert rbac.can("auditor", "item.review")
    assert not rbac.can("seller", "item.review")
    assert rbac.can("seller", "item.submit")
    assert not rbac.can("viewer", "item.submit")
    assert rbac.can("admin", "user.manage") and not rbac.can("auditor", "user.manage")


def test_last_admin_cannot_be_downgraded_or_deleted(tmpdb):
    uid = rbac.create_user("admin1", "secret123", role="admin")
    with pytest.raises(ValueError):
        rbac.set_role(uid, "seller")
    with pytest.raises(ValueError):
        rbac.delete_user(uid)
    rbac.create_user("admin2", "secret123", role="admin")     # 有两个管理员后可以降级
    assert rbac.set_role(uid, "auditor")["role"] == "auditor"


def test_token_roundtrip_and_tamper(tmpdb):
    uid = rbac.create_user("stu006", "secret123", role="seller")
    user = rbac.get_user(uid)
    token = rbac.issue_token(user)
    assert rbac.verify_token(token)["username"] == "stu006"
    assert rbac.verify_token(token[:-1] + ("0" if token[-1] != "0" else "1")) is None
    assert rbac.verify_token("") is None


def test_expired_token_is_rejected(tmpdb):
    uid = rbac.create_user("stu007", "secret123", role="seller")
    token = rbac.issue_token(rbac.get_user(uid), ttl=-1)
    assert rbac.verify_token(token) is None


def test_default_admin_is_created_once(tmpdb):
    assert rbac.ensure_default_admin() is True
    assert rbac.ensure_default_admin() is False             # 幂等
    assert db.count_users("admin") == 1


# ------------------------------------------------------------------ #
# 操作日志
# ------------------------------------------------------------------ #
def test_oplog_record_and_query(tmpdb):
    oplog.record("admin", "item.review", target_type="item", target_id=7,
                 detail="判定为自动通过", role="admin")
    oplog.record("admin", "item.delete", target_type="item", target_id=8, result="failed")
    res = oplog.query(action="item.review")
    assert res["total"] == 1 and res["logs"][0]["target_id"] == "7"
    assert res["logs"][0]["action_name"] == "人工复核改判"
    assert oplog.query(result="failed")["total"] == 1
    assert oplog.query(keyword="自动通过")["total"] == 1


def test_oplog_stats_and_target_history(tmpdb):
    oplog.record("u1", "login", result="ok")
    oplog.record("u1", "login", result="denied")
    st = oplog.stats(days=7)
    assert st["total"] == 2 and st["by_result"].get("denied") == 1
    oplog.record("u1", "item.review", target_type="item", target_id=99)
    assert len(oplog.target_history("item", 99)) == 1


def test_oplog_purge_by_retention(tmpdb):
    oplog.record("u1", "login")
    assert oplog.purge(retention_days=0) == 0                # 0 表示不清理
    with db.get_conn() as conn:
        conn.execute("UPDATE op_logs SET created_at='2000-01-01 00:00:00'")
    assert oplog.purge(retention_days=30) == 1


# ------------------------------------------------------------------ #
# 消息通知
# ------------------------------------------------------------------ #
def test_notify_inbox_and_read(tmpdb):
    notify.notify("stu1", "标题", "内容", level="warn")
    notify.notify("stu1", "标题2", "内容2")
    notify.notify("stu2", "别人的消息", "不该出现在 stu1 收件箱")
    assert notify.summary("stu1")["unread"] == 2
    inbox = notify.inbox("stu1")
    assert len(inbox) == 2 and all(n["user_id"] == "stu1" for n in inbox)
    nid = inbox[0]["id"]
    assert notify.mark_read(nid, "stu1") is True
    assert notify.summary("stu1")["unread"] == 1
    assert notify.mark_all_read("stu1") == 1
    assert notify.summary("stu1")["unread"] == 0


def test_mark_read_respects_owner(tmpdb):
    nid = notify.notify("stu1", "标题", "内容")
    assert notify.mark_read(nid, "stu2") is False            # 不能标记他人的消息
    assert notify.mark_read(nid, "stu1") is True


def test_broadcast_by_role(tmpdb):
    rbac.create_user("adm01", "secret123", role="admin")
    rbac.create_user("sel01", "secret123", role="seller")
    rbac.create_user("sel02", "secret123", role="seller")
    assert notify.broadcast("全体通知", "内容") == 3
    assert notify.broadcast("仅卖家", "内容", role="seller") == 2
    assert notify.summary("sel01")["unread"] == 2


def test_notify_audit_result_template(tmpdb):
    notify.notify_audit_result(11, "20230101", "reject", 95, "命中违禁词")
    inbox = notify.inbox("20230101")
    assert inbox and "未通过审核" in inbox[0]["title"]
    assert inbox[0]["level"] == "error" and inbox[0]["ref_id"] == "11"


# ------------------------------------------------------------------ #
# 导入导出
# ------------------------------------------------------------------ #
def _seed_item(**kw):
    item = {"title": "高等数学同济第七版", "description": "大一教材八成新", "price": 15,
            "condition": "八成新", "category": "textbook", "seller_id": "20230101",
            "status": "pass", "risk_score": 8, "rule_score": 5, "llm_score": 3,
            "reason": "无异常", "suggestion": "可直接发布", "source": "seed",
            **kw}
    return db.insert_item(item)


def test_export_csv_has_bom_and_header(tmpdb):
    _seed_item()
    exp = dataio.export_items(fmt="csv")
    assert exp["content"].startswith("\ufeff")               # BOM：Excel 不乱码
    assert "标题" in exp["content"] and "高等数学" in exp["content"]
    assert exp["count"] == 1


def test_export_json_and_filters(tmpdb):
    _seed_item(status="pass")
    _seed_item(title="折叠刀", status="reject")
    exp = dataio.export_items(fmt="json", status="reject")
    assert exp["count"] == 1 and '"status"' in exp["content"]
    with pytest.raises(ValueError):
        dataio.export_items(fmt="xlsx")


def test_export_masks_sensitive_text(tmpdb):
    _seed_item(description="联系我 13912345678")
    exp = dataio.export_items(fmt="csv", mask=True)
    assert "13912345678" not in exp["content"]


def test_parse_table_auto_detects_csv_and_json(tmpdb):
    rows, errs = dataio.parse_table("标题,描述,价格\n书本,八成新,15\n")
    assert not errs and len(rows) == 1
    rows, errs = dataio.parse_table('[{"title":"书本","price":"15"}]')
    assert not errs and rows[0]["title"] == "书本"
    rows, errs = dataio.parse_table("")
    assert errs and rows == []


def test_normalize_row_maps_chinese_and_english(tmpdb):
    row = dataio.normalize_row({"标题": " 书本 ", "描述": "八成新", "价格": "¥1,200",
                                "卖家学号": "20230101", "未知列": "忽略"})
    assert row["title"] == "书本" and row["price"] == 1200.0
    assert row["seller_id"] == "20230101" and "未知列" not in row
    assert dataio.normalize_row({"price": "abc"})["price"] is None
    assert dataio.normalize_row({"price": "-5"})["price"] is None


def test_import_dry_run_does_not_write(tmpdb):
    rows = [{"title": "高等数学", "description": "教材八成新", "price": "15"}]
    rep = dataio.import_items(rows, mode="submit", dry_run=True)
    assert rep["ok"] == 1 and rep["dry_run"] is True
    assert db.count_items() == 0                             # 空跑不落库


def test_import_submit_writes_and_counts(tmpdb):
    rows = [{"title": "高等数学同济第七版", "description": "教材八成新，无缺页", "price": "15"},
            {"title": "出一把开刃折叠刀", "description": "防身用，加微信，先款后货"}]
    rep = dataio.import_items(rows, mode="submit")
    assert rep["total"] == 2 and rep["failed"] == 0
    assert db.count_items() == 2
    assert rep["counts"]["reject"] >= 1                      # 违禁品必须被拦下


def test_import_reports_bad_rows(tmpdb):
    rep = dataio.import_items([{"title": "", "description": ""},
                               {"title": "正常书本", "description": "八成新"}], mode="audit")
    assert rep["failed"] == 1 and rep["errors"][0]["error"]
    assert rep["ok"] == 1 and db.count_items() == 0          # audit 模式不入库


def test_import_rejects_bad_mode(tmpdb):
    with pytest.raises(ValueError):
        dataio.import_items([{"title": "x"}], mode="fly")


def test_template_csv_has_header(tmpdb):
    tpl = dataio.template_csv()
    assert tpl.startswith("\ufeff") and "标题" in tpl


# ------------------------------------------------------------------ #
# 批量任务
# ------------------------------------------------------------------ #
def test_batch_audit_job(tmpdb):
    job = batch.run_job("audit", {"items": [
        {"title": "高等数学", "description": "教材八成新", "price": 15},
        {"title": "折叠刀", "description": "开刃防身，加微信先款后货"},
    ]}, operator="admin")
    assert job["status"] == "done" and job["total"] == 2
    assert job["result"]["counts"]["reject"] == 1
    assert db.count_items() == 0                             # audit 不入库


def test_batch_submit_job_writes_items(tmpdb):
    job = batch.run_job("submit", {"items": [
        {"title": "高等数学", "description": "教材八成新", "price": 15}]}, operator="admin")
    assert job["success"] == 1 and db.count_items() == 1
    assert batch.get_job(job["id"])["op"] == "submit"


def test_batch_review_and_delete(tmpdb):
    ids = [_seed_item(status="review"), _seed_item(status="review")]
    job = batch.run_job("review", {"ids": ids, "status": "pass"}, operator="admin")
    assert job["failed"] == 0
    assert all(db.get_item(i)["status"] == "pass" for i in ids)

    job = batch.run_job("delete", {"ids": ids}, operator="admin")
    assert job["success"] == 2 and db.count_items() == 0


def test_batch_delete_tolerates_missing_id(tmpdb):
    job = batch.run_job("delete", {"ids": [9999]}, operator="admin")
    assert job["failed"] == 1 and job["result"]["errors"][0]["error"] == "商品不存在"


def test_batch_records_job_and_stats(tmpdb):
    batch.run_job("audit", {"items": [{"title": "书本", "description": "八成新"}]},
                  operator="admin")
    st = batch.stats()
    assert st["total"] == 1 and st["by_op"].get("audit") == 1
    assert batch.list_jobs(op="audit")[0]["duration_ms"] >= 0


def test_batch_rejects_unknown_op_and_empty(tmpdb):
    with pytest.raises(ValueError):
        batch.run_job("fly", {"items": []})
    with pytest.raises(ValueError):
        batch.run_job("audit", {"items": []})
    with pytest.raises(ValueError):
        batch.run_job("review", {"ids": [], "status": "pass"})


def test_batch_respects_max_items(tmpdb, monkeypatch):
    monkeypatch.setattr(config, "BATCH_MAX_ITEMS", 2)
    with pytest.raises(ValueError):
        batch.run_job("audit", {"items": [{"title": f"书{i}", "description": "新"}
                                          for i in range(3)]})


def test_batch_run_is_logged_and_notified(tmpdb):
    batch.run_job("audit", {"items": [{"title": "书本", "description": "八成新"}]},
                  operator="admin")
    logs = oplog.query(action="batch.run")["logs"]
    assert logs and logs[0]["actor"] == "admin"
    assert notify.summary("admin")["unread"] == 1            # 任务完成通知
