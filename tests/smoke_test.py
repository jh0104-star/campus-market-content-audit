# -*- coding: utf-8 -*-
"""冒烟测试：页面渲染 + REST 接口 + 审核链路。

运行： python tests/smoke_test.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as web
from core import database as db

ok, fail = 0, 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {name}")
    else:
        fail += 1
        print(f"  [FAIL] {name} {extra}")


def main():
    db.init_db()
    c = web.app.test_client()

    print("\n--- 页面渲染 ---")
    for path in ("/", "/admin", "/admin?status=reject", "/admin?category=textbook"):
        r = c.get(path)
        check(f"GET {path}", r.status_code == 200, r.status_code)

    print("\n--- REST 接口 ---")
    r = c.get("/api/categories")
    check("GET /api/categories", r.status_code == 200 and r.get_json()["ok"])

    r = c.get("/api/stats")
    check("GET /api/stats", r.status_code == 200 and "stats" in r.get_json())

    r = c.get("/api/system")
    check("GET /api/system", r.get_json()["ok"])

    r = c.get("/api/items?limit=5")
    check("GET /api/items", r.get_json()["ok"])

    print("\n--- 审核链路 ---")
    r = c.post("/api/audit", json={
        "title": "出一把防身用的折叠刀，开过刃",
        "description": "不多说了，懂的来，加微信 xxxx，先款后货。", "price": 88})
    j = r.get_json()
    check("违禁品 -> 拒绝", j["result"]["decision"] == "reject", j["result"]["decision"])

    r = c.post("/api/audit", json={
        "title": "高等数学同济第七版上册",
        "description": "大一教材，写了几页笔记，八成新，毕业出。", "price": 15})
    j = r.get_json()
    res = j["result"]
    check("正常教材 -> 通过", res["decision"] == "pass", res["decision"])
    check("正常教材 -> 分类为教材教辅", res["category"] == "textbook", res["category"])

    r = c.post("/api/audit", json={"title": "", "description": ""})
    check("空标题 -> 400", r.status_code == 400)

    print("\n--- 提交与人工复核 ---")
    r = c.post("/api/items", json={
        "title": "九成新机械键盘", "description": "先款后货，不走平台，不退不换。",
        "price": 200, "seller_id": "20990001"})
    j = r.get_json()
    check("提交成功", j["ok"])
    item_id = j["item"]["id"]

    r = c.post(f"/api/items/{item_id}/review", json={"status": "pass", "note": "人工复核通过"})
    check("人工复核接口", r.get_json()["ok"])
    check("状态已更新", db.get_item(item_id)["status"] == "pass")

    print("\n--- 黑名单 ---")
    c.post("/api/blacklist", json={"user_id": "20990001", "reason": "测试"})
    r = c.post("/api/audit", json={"title": "高数教材", "description": "八成新",
                                   "price": 15, "seller_id": "20990001"})
    check("黑名单用户被拦截", r.get_json()["result"]["decision"] == "reject")
    c.delete("/api/blacklist?user_id=20990001")
    check("黑名单移除", "20990001" not in db.blacklist_all())

    print("\n--- 卖家中心 / 申诉闭环 ---")
    r = c.get("/api/seller/items")
    check("未登录访问我的发布 -> 401", r.status_code == 401)

    r = c.post("/api/session", json={"seller_id": "20880001", "seller_name": "测试卖家"})
    check("卖家登录", r.get_json()["ok"])

    r = c.post("/api/items", json={
        "title": "四六级代考包过", "description": "稳过不用复习，加vx详谈。",
        "price": 800, "seller_id": "20880001"})
    j = r.get_json()
    bad_id = j["item"]["id"]
    check("违规信息被拦截", j["audit"]["decision"] == "reject", j["audit"]["decision"])

    r = c.post("/api/items", json={
        "title": "高等数学同济第七版上册", "description": "大一教材，八成新，毕业出。",
        "price": 15, "seller_id": "20880001"})
    good_id = r.get_json()["item"]["id"]

    r = c.get("/api/seller/items")
    ids = [i["id"] for i in r.get_json()["items"]]
    check("我的发布仅含本人信息", bad_id in ids and good_id in ids)

    r = c.post("/api/appeals", json={"item_id": good_id, "reason": "我不认可这个结论"})
    check("通过的信息不可申诉 -> 400", r.status_code == 400)

    r = c.post("/api/appeals", json={"item_id": bad_id, "reason": "这是教材资料不是代考"})
    check("提交申诉", r.get_json().get("ok"), r.get_json())
    appeal_id = r.get_json()["id"]

    r = c.post("/api/appeals", json={"item_id": bad_id, "reason": "再次提交一次申诉"})
    check("重复申诉 -> 409", r.status_code == 409)

    r = c.get("/api/appeals?scope=all&status=pending")
    check("管理员可见待处理申诉", any(a["id"] == appeal_id for a in r.get_json()["appeals"]))

    r = c.post(f"/api/appeals/{appeal_id}/handle",
               json={"status": "accepted", "reply": "核实为误判，恢复发布"})
    check("申诉成立 -> 恢复发布", r.get_json()["item_status"] == "pass")
    check("商品状态已更新", db.get_item(bad_id)["status"] == "pass")

    r = c.post(f"/api/appeals/{appeal_id}/handle", json={"status": "rejected", "reply": "再处理"})
    check("已处理申诉重复处理 -> 409", r.status_code == 409)

    c.delete("/api/session")
    r = c.post("/api/appeals", json={"item_id": bad_id, "reason": "未登录提交申诉"})
    check("未登录申诉 -> 401", r.status_code == 401)
    check("GET /seller（未登录）", c.get("/seller").status_code == 200)

    c.post("/api/session", json={"seller_id": "20880001"})
    check("GET /seller（已登录）", c.get("/seller").status_code == 200)
    c.delete(f"/api/items/{bad_id}")
    c.delete(f"/api/items/{good_id}")
    c.delete("/api/session")

    print("\n--- 反规避（对抗样本） ---")
    variants = [
        ("折-叠-刀", "出一把防身用的折-叠-刀，已开刃", 88),
        ("摺疊刀", "闲置摺疊刀一把，动手能力强者可改", 88),
        ("daixie", "论文 daixie，专业团队，稳过", 800),
        ("烟蛋", "闲置烟蛋 5 个装，口味任选，抽了上头", 100),
        ("先-款-后-货", "二手相机，先-款-后-货，不-走-平-台", 1500),
    ]
    for note, title, price in variants:
        r = c.post("/api/audit", json={"title": title, "description": title, "price": price})
        decision = r.get_json()["result"]["decision"]
        check(f"识别变体「{note}」-> 非自动通过", decision in ("review", "reject"), decision)

    r = c.get("/api/system")
    check("变体识别后命中项含 terms", any(
        h.get("terms") for h in
        (c.post("/api/audit", json={"title": "论文 daixie", "description": "稳过", "price": 800})
         .get_json()["result"]["rule"]["hits"] or [])))

    print("\n--- 管理接口鉴权 ---")
    import config
    orig_token = config.ADMIN_TOKEN
    try:
        # 配置口令后：不带口令一律 401，带正确口令才放行
        config.ADMIN_TOKEN = "smoke-admin-token"
        r = c.delete("/api/items/999999")
        check("未带口令删除商品 -> 401", r.status_code == 401, r.status_code)
        r = c.post("/api/items/999999/review", json={"status": "pass"})
        check("未带口令人工复核 -> 401", r.status_code == 401, r.status_code)
        r = c.get("/api/appeals?scope=all")
        check("未带口令拉取全部申诉 -> 401", r.status_code == 401, r.status_code)
        r = c.delete("/api/items/999999", headers={"X-Admin-Token": "smoke-admin-token"})
        check("带正确口令 -> 放行", r.status_code == 200, r.status_code)
    finally:
        config.ADMIN_TOKEN = orig_token

    # 未配置口令时：仅本机可访问，非回环地址一律 403
    from app import admin_allowed
    with web.app.test_request_context("/", environ_base={"REMOTE_ADDR": "127.0.0.1"}):
        check("本机访问管理接口放行", admin_allowed())
    with web.app.test_request_context("/", environ_base={"REMOTE_ADDR": "192.168.1.50"}):
        check("非本机访问管理接口 -> 拒绝", not admin_allowed())

    print("\n--- 隐私脱敏 ---")
    r = c.post("/api/items", json={
        "title": "高等数学同济第七版上册", "description": "教材八成新，无缺页。",
        "price": 15, "contact": "手机号 13912345678，邮箱 abc@test.com"})
    masked = db.get_item(r.get_json()["item"]["id"])["contact"]
    check("手机号已脱敏", "13912345678" not in masked and "****" in masked, masked)
    check("邮箱已脱敏", "abc@test.com" not in masked, masked)
    c.delete(f"/api/items/{r.get_json()['item']['id']}")

    print("\n--- 分级调用（成本优化） ---")
    from core.image import has_pillow, image_hash
    from core.lexicon import LEXICON_VERSION
    from core.pipeline import SKIP_REMOTE_RULE_SCORE

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    m = c.get("/api/metrics").get_json()["metrics"]
    check("GET /api/metrics", m["total"] >= 1)
    check("分级调用已生效（存在被跳过的远程调用）", m["llm_skipped"] >= 1, m)
    check("词表版本号可读", isinstance(LEXICON_VERSION, str) and LEXICON_VERSION.startswith("v"))
    check("跳过阈值已定义", SKIP_REMOTE_RULE_SCORE >= 70)

    # 规则引擎钉死结论时不得再调远程模型
    r = c.post("/api/audit", json={"title": "出一把防身用的折叠刀，已开刃",
                                   "description": "防身用，已开刃，先款后货", "price": 200})
    body = r.get_json()["result"]
    check("硬红线命中 -> 拒绝发布", body["decision"] == "reject", body["decision"])

    print("\n--- 图片侧审核 ---")
    if has_pillow():
        from PIL import Image
        import io
        def _png(color=(30, 90, 200)):
            buf = io.BytesIO()
            Image.new("RGB", (240, 240), color).save(buf, format="PNG")
            return buf.getvalue()
        h1, h2 = image_hash(_png()), image_hash(_png((30, 90, 205)))
        from core.image import hamming_distance
        check("相同图片指纹一致", h1 == image_hash(_png()))
        check("近似图片汉明距离很小", hamming_distance(h1, h2) <= 6, hamming_distance(h1, h2))

        # 同一张图发两次：第二条应命中盗图查重
        img = _png()
        rec = c.post("/api/items", data={
            "title": "小米移动电源 10000mAh", "description": "九成新，实物拍摄，配件齐全。",
            "price": "60", "image": (io.BytesIO(img), "a.png")},
            content_type="multipart/form-data").get_json()
        first = rec["item"]["id"]
        check("上传图片已计算哈希", bool(db.get_item(first).get("image_hash")))
        dup = c.post("/api/items", data={
            "title": "另一个充电宝", "description": "九成新，实物拍摄，配件齐全。",
            "price": "60", "image": (io.BytesIO(img), "b.png")},
            content_type="multipart/form-data").get_json()
        hits = dup["audit"]["rule"]["hits"]
        check("重复图片命中盗图查重", any("图片重复" in h["rule"] for h in hits),
              [h["rule"] for h in hits])
        c.delete(f"/api/items/{first}")
        c.delete(f"/api/items/{dup['item']['id']}")
    else:
        print("  [SKIP] 未安装 Pillow，图片能力退化为字节摘要")

    print("\n--- 文本查重（刷屏 / 盗帖）---")
    from core.dedup import hamming_distance as text_hd, text_fingerprint
    fp_a = text_fingerprint("出九成新 iPad Air5 64G", "国行WiFi版，无磕碰，配件齐全")
    fp_b = text_fingerprint("出九成新 iPad Air5 64G", "国行WiFi版，无磕碰，配件齐全，急出")
    fp_c = text_fingerprint("高等数学同济第七版上册", "教材，八成新，写了几页笔记")
    fp_d = text_fingerprint("出九成新 小米移动电源 10000mAh", "无磕碰，配件齐全")  # 不同商品同套话
    check("相似文案指纹距离很小", text_hd(fp_a, fp_b) <= 6, text_hd(fp_a, fp_b))
    check("无关文案指纹距离很大", text_hd(fp_a, fp_c) >= 20, text_hd(fp_a, fp_c))
    check("不同商品仅描述雷同不算重复", text_hd(fp_a, fp_d) > 6, text_hd(fp_a, fp_d))

    base = {"title": "出九成新 Kindle Paperwhite5", "description": "无划痕，原装壳，配件齐全",
            "price": 700, "seller_id": "smoke_dup"}
    first = c.post("/api/items", json=base).get_json()
    again = c.post("/api/items", json=base).get_json()
    hits2 = [h["rule"] for h in again["audit"]["rule"]["hits"]]
    check("同一卖家重复文案命中查重",
          any(("重复发帖" in h) or ("盗帖" in h) for h in hits2), hits2)

    rid = first["item"]["id"]
    rep = c.post("/api/report", json={"item_id": rid, "reason": "实物与描述不符，疑似虚假信息"})
    check("举报接口可用", rep.get_json().get("ok"))
    check("举报理由过短 -> 400",
          c.post("/api/report", json={"item_id": rid, "reason": "差"}).status_code == 400)
    c.delete(f"/api/items/{rid}")
    c.delete(f"/api/items/{again['item']['id']}")

    print("\n--- 批量审核 / 明细导出 ---")
    j = c.post("/api/batch", json={"items": [
        {"title": "高等数学同济第七版", "description": "九成新，无笔记", "price": 25},
        {"title": "代写论文包过", "description": "加微信详谈，先款后货", "price": 500}]}).get_json()
    check("批量审核返回汇总", j.get("ok") and j["summary"]["checked"] == 2, j.get("summary"))
    check("批量审核结论正确",
          j["rows"][0]["decision"] == "pass" and j["rows"][1]["decision"] == "reject",
          [r.get("decision") for r in j["rows"]])
    check("批量条数超上限 -> 400",
          c.post("/api/batch", json={"items": [{"title": "x"}] * 51}).status_code == 400)
    check("空数组 -> 400", c.post("/api/batch", json={"items": []}).status_code == 400)
    r = c.get("/api/export.csv")
    check("CSV 导出可用", r.status_code == 200 and "ID" in r.get_data(as_text=True))

    print("\n--- 规则治理与闭环学习 ---")
    j = c.get("/api/governance").get_json()
    check("治理报表可用", j.get("ok") and "rules" in j["report"])
    check("规则命中已入库统计", j["report"]["summary"]["total_hits"] > 0,
          j["report"]["summary"])
    j = c.post("/api/replay", json={"limit": 50}).get_json()
    check("重放回归可用", j.get("ok") and j["report"]["total"] >= 0, j.get("report", {}).get("total"))

    print("\n--- 引擎热切换 ---")
    import core.llm as llm_mod
    from core.llm import engine_status
    origin = llm_mod.current_provider()
    st0 = engine_status()
    r = c.get("/api/engine")
    check("GET /api/engine", r.status_code == 200 and r.get_json()["status"]["provider"] == origin)
    check("mock 引擎始终可选", "mock" in {p["key"] for p in st0["available"]})
    check("非法引擎 -> 400", c.post("/api/engine", json={"provider": "__nope__"}).status_code == 400)

    probe = {"title": "iPad 9代 64G WiFi版", "description": "有电子发票，电池健康92%。",
             "price": 1600, "has_image": True}
    c.post("/api/engine", json={"provider": "mock"})
    eng = c.post("/api/audit", json=probe).get_json()["result"]["llm"]["engine"]
    check("切到本地分析器后 engine=mock", eng == "mock", eng)

    if st0["has_key"]:
        # 优先使用 .env 中配置的那个 provider（它的密钥才有效）
        enabled = [p["key"] for p in st0["available"] if p["key"] != "mock" and p["enabled"]]
        remote = origin if origin in enabled else (enabled[0] if enabled else None)
        if not remote:
            enabled = remote = None
        c.post("/api/engine", json={"provider": remote})
        body = c.post("/api/audit", json=probe).get_json()["result"]
        check(f"切到远程引擎({remote})后 engine=api", body["llm"]["engine"] == "api",
              f'{body["llm"]["engine"]} fallback={body["llm"]["fallback"]}')
        check(f"远程调用未降级", not body["llm"]["fallback"], body["llm"]["reason"][:80])
        # 切回后重新审核，确认没有读到远程引擎的缓存结果
        c.post("/api/engine", json={"provider": "mock"})
        body = c.post("/api/audit", json=probe).get_json()["result"]
        check("再切回本地不使用远程缓存", body["llm"]["engine"] == "mock", body["llm"]["engine"])
    c.post("/api/engine", json={"provider": origin})

    print("\n--- 留出集评估 ---")
    from data.seed import evaluate
    res = evaluate(False)
    dev, hold = res["dev"], res["holdout"]
    check(f"dev 集严格准确率 {dev['strict']:.1%} >= 95%", dev["strict"] >= 0.95)
    check(f"留出集严格准确率 {hold['strict']:.1%} >= 90%", hold["strict"] >= 0.90)
    check(f"留出集拦截召回 {hold['recall']:.1%} >= 95%", hold["recall"] >= 0.95)
    check("留出集独立于开发集", hold["total"] >= 20 and dev["total"] >= 40)

    print("\n--- 扩展模块：用户权限 ---")
    import time as _time
    import config as _cfg
    j = c.post("/api/auth/login", json={"username": _cfg.DEFAULT_ADMIN_USER,
                                        "password": _cfg.DEFAULT_ADMIN_PASSWORD}).get_json()
    check("默认管理员可登录", j.get("ok") and j["user"]["role"] == "admin", j.get("error"))
    check("登录返回令牌", bool(j.get("token")))
    check("/api/auth/me 回显身份",
          c.get("/api/auth/me").get_json()["user"]["username"] == _cfg.DEFAULT_ADMIN_USER)
    j = c.get("/api/permissions").get_json()
    check("权限矩阵可用", j.get("ok") and "admin" in j["matrix"])

    seller_name = f"seller{int(_time.time()) % 100000}"
    j = c.post("/api/users", json={"username": seller_name, "password": "secret123",
                                   "role": "seller", "seller_id": "20239999"}).get_json()
    check("新建卖家账号", j.get("ok") and j["user"]["role"] == "seller", j.get("error"))
    c2 = web.app.test_client()                       # 独立会话，验证低权限账号被拦住
    c2.post("/api/auth/login", json={"username": seller_name, "password": "secret123"})
    check("卖家无权管理用户 -> 403", c2.get("/api/users").status_code == 403)
    check("卖家无权人工复核 -> 403",
          c2.post(f"/api/items/{item_id}/review", json={"status": "pass"}).status_code == 403)
    check("卖家无权访问他人通知 -> 401/403",
          c2.get("/api/notifications?user_id=other").status_code in (401, 403)
          or c2.get("/api/notifications").status_code == 200)

    print("\n--- 扩展模块：导入导出 ---")
    r = c.get("/api/export?fmt=csv&limit=5")
    check("CSV 导出（新接口）", r.status_code == 200
          and r.get_data(as_text=True).startswith("\ufeff"))
    j = c.get("/api/export?fmt=json&limit=5").get_json()
    check("JSON 导出（新接口）", j.get("count") is not None)
    check("非法导出格式 -> 400", c.get("/api/export?fmt=xlsx").status_code == 400)
    check("导入模板可下载", c.get("/api/export/template").status_code == 200)

    rows = [{"title": "大学物理教材", "description": "八成新，无缺页", "price": "18"},
            {"title": "代写论文包过", "description": "加微信详谈，先款后货", "price": 500}]
    before = db.count_items()
    rep = c.post("/api/import", json={"rows": rows, "mode": "submit",
                                      "dry_run": True}).get_json()["report"]
    check("导入空跑不写库", rep["dry_run"] and db.count_items() == before, rep.get("counts"))
    rep = c.post("/api/import", json={"rows": rows, "mode": "submit"}).get_json()["report"]
    check("导入入库且违禁被拦",
          rep["ok"] == 2 and rep["counts"]["reject"] >= 1 and db.count_items() == before + 2)
    rep = c.post("/api/import", json={"content": "标题,描述,价格\n线性代数,全新,20\n",
                                      "mode": "audit"}).get_json()["report"]
    check("CSV 文本导入可用（中文表头）", rep["ok"] == 1, rep.get("errors"))

    print("\n--- 扩展模块：批量任务 ---")
    j = c.post("/api/batch/jobs", json={
        "op": "submit",
        "payload": {"items": [{"title": "概率论教材", "description": "九成新", "price": 22}]}
    }).get_json()
    check("批量任务执行完成", j.get("ok") and j["job"]["status"] == "done", j.get("error"))
    job_id = j["job"]["id"]
    check("任务可查询", c.get(f"/api/batch/jobs/{job_id}").get_json()["job"]["id"] == job_id)
    check("任务列表可用", c.get("/api/batch/jobs?limit=5").get_json()["ok"])
    check("操作清单可用", len(c.get("/api/batch/ops").get_json()["ops"]) >= 4)
    check("非法批量操作 -> 400",
          c.post("/api/batch/jobs", json={"op": "fly", "payload": {}}).status_code == 400)

    print("\n--- 扩展模块：消息通知与操作日志 ---")
    j = c.post("/api/notifications", json={"title": "冒烟通知", "content": "这是一条测试广播"})
    check("管理员广播", j.get_json().get("sent", 0) >= 1)
    inbox = c.get("/api/notifications").get_json()["notifications"]
    check("收件箱收到广播", any(n["title"] == "冒烟通知" for n in inbox))
    check("标记全部已读", c.post("/api/notifications/read-all").get_json().get("ok"))
    check("已读后未读归零",
          c.get("/api/notifications/summary").get_json()["summary"]["unread"] == 0)

    j = c.get("/api/oplog?limit=50").get_json()
    check("操作日志可查询", j.get("ok") and j["total"] > 0)
    check("登录被记录", any(l["action"] == "login" for l in j["logs"]))
    check("批量任务被记录", any(l["action"] == "batch.run" for l in j["logs"]))
    check("越权被记录（denied）", any(l["result"] == "denied" for l in j["logs"]))
    check("日志统计可用", c.get("/api/oplog/stats").get_json()["stats"]["total"] > 0)

    print("\n--- 删除 ---")
    c.delete(f"/api/items/{item_id}")
    check("删除商品", db.get_item(item_id) is None)

    print(f"\n结果：通过 {ok} 项，失败 {fail} 项")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
