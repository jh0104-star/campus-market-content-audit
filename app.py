# -*- coding: utf-8 -*-
"""Flask Web 端：发布审核演示 + REST API + 管理后台。

启动：  python app.py         -> http://127.0.0.1:5000
"""
from __future__ import annotations

import sys
import time
from functools import wraps
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Windows 控制台默认 GBK，打印中文/emoji 会抛 UnicodeEncodeError，这里兜底
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                  # 非交互/已包装的流，忽略
    pass

from flask import (Flask, Response, jsonify, render_template,  # noqa: E402
                   request, session)
from werkzeug.exceptions import HTTPException                       # noqa: E402

import config                                                       # noqa: E402
from core import database as db                                     # noqa: E402
from core.lexicon import CATEGORIES, CATEGORY_MAP                   # noqa: E402
from core.llm import (engine_name, engine_status, fallback_stats,    # noqa: E402
                      switch_engine)
from core.image import has_pillow, image_hash, is_low_information    # noqa: E402
from core.governance import (backfill_rule_hits, replay_history,      # noqa: E402
                             rule_report)
from core.lexicon import LEXICON_VERSION                             # noqa: E402
from core.logutils import get_logger, log_swallow                    # noqa: E402
from core.pipeline import PIPELINE, SKIP_REMOTE_RULE_SCORE, audit, submit  # noqa: E402
from core.samples import QUICK_SAMPLES                              # noqa: E402
# ---- 扩展模块：权限 / 操作日志 / 通知 / 导入导出 / 批量任务 ----
from core import batch as batchsvc                                   # noqa: E402
from core import dataio, notify, oplog, rbac                         # noqa: E402

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
# Flask >= 2.2 起 JSON_AS_ASCII 已移除，需用 app.json 配置，否则中文会被转成 \uXXXX
app.json.ensure_ascii = False
app.json.sort_keys = False
# 请求体上限，避免超大文本拖垮审核链路（图片上传按 8MB 计，见 MAX_IMAGE_BYTES）
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
# session cookie 加固：HttpOnly 防 XSS 窃取，SameSite=Lax 防 CSRF
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

logger = get_logger("web")

db.init_db()

# 规则治理是后加的能力：老数据只有 detail_json，启动时幂等回填一次命中明细
try:
    if db.count_items() and not db.rule_hit_stats():
        backfill_rule_hits()
except Exception as exc:                           # 回填失败不影响服务启动
    log_swallow(logger, "规则命中明细回填", exc)

# 首次启动自动建内置管理员（已存在管理员时跳过），否则新库没有任何可登录账号
try:
    if rbac.ensure_default_admin():
        logger.warning("已创建内置管理员 %s，请尽快在 .env 修改 DEFAULT_ADMIN_PASSWORD",
                       config.DEFAULT_ADMIN_USER)
except Exception as exc:
    log_swallow(logger, "内置管理员初始化", exc)

if config.SECRET_KEY_IS_EPHEMERAL:
    logger.warning("未设置 SECRET_KEY，已随机生成（进程重启后登录态失效；"
                   "多进程部署请显式配置 SECRET_KEY）")
if not config.ADMIN_TOKEN:
    logger.warning("未设置 ADMIN_TOKEN，管理类接口仅允许本机(127.0.0.1)访问；"
                   "对外开放前请在 .env 中配置 ADMIN_TOKEN")
if not config.SELLER_LOGIN_CODE:
    logger.warning("未设置 SELLER_LOGIN_CODE，卖家登录沿用免口令模式（仅演示用）")

# 输入长度上限（与前端保持一致，防止超长文本攻击/误填）
MAX_TITLE_LEN = 200
MAX_DESC_LEN = 2000
MAX_TEXT_FIELD_LEN = 100
MAX_PRICE = 10_000_000
# 批量审核单次条数上限（防止一次请求拖垮服务）
MAX_BATCH = 50

STATUS_META = {
    "pass": ("自动通过", "ok"),
    "review": ("转人工复核", "warn"),
    "reject": ("拒绝发布", "bad"),
    "pending": ("待审核", "info"),
}

CAT_NAME = {c["key"]: c["name"] for c in CATEGORIES}
CAT_COLOR = {c["key"]: c["color"] for c in CATEGORIES}


def _text(value: Any, max_len: int) -> str:
    """统一清洗文本字段：去空、限长。"""
    return str(value or "").strip()[:max_len]


def _int(value: Any, default: int, low: int, high: int) -> int:
    """整数参数归一化：非法值退回默认值，并夹在 [low, high] 区间内。"""
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _price(value: Any) -> Optional[float]:
    """价格归一化：非法或越界值统一为 None，交给规则引擎按"价格缺失"处理。"""
    try:
        p = float(value)
    except (TypeError, ValueError):
        return None
    if p != p or p < 0:                            # NaN / 负数按"价格缺失"处理
        return None
    # 超大值夹到上限而不是丢弃：否则会被规则当作"价格缺失"只扣 15 分，
    # 比"金额超范围"还轻，形成高价异常的检测空洞。
    return min(p, MAX_PRICE)


# ------------------------------------------------------------------ #
# 管理接口鉴权
# ------------------------------------------------------------------ #
LOOPBACK_ADDRS = {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}


def _client_is_loopback() -> bool:
    """是否来自本机（考虑到 X-Forwarded-For 可被伪造，仅取直连地址判断）。"""
    return (request.remote_addr or "") in LOOPBACK_ADDRS


def _supplied_admin_token() -> str:
    """从 Header / JSON / 表单 / query 任一处取管理口令，方便脚本与前端调用。"""
    token = request.headers.get("X-Admin-Token", "")
    if not token:
        data = request.get_json(silent=True) or request.form.to_dict() or {}
        token = str(data.get("admin_token") or "")
    if not token:
        token = request.args.get("admin_token", "") or ""
    return token.strip()


def admin_allowed() -> bool:
    """管理类接口放行判定。

    配置了 ADMIN_TOKEN  -> 必须携带且匹配；
    未配置（演示默认）  -> 仅放行本机回环访问，局域网/公网请求一律拒绝。
    """
    if config.ADMIN_TOKEN:
        return _supplied_admin_token() == config.ADMIN_TOKEN
    return _client_is_loopback()


def admin_denied():
    """统一的拒绝响应（区分「口令不对」与「来源不允许」两种原因）。"""
    if config.ADMIN_TOKEN:
        return jsonify({"error": "管理口令缺失或不正确"}), 401
    return jsonify({"error": "管理接口仅允许本机访问，"
                             "对外开放请在 .env 配置 ADMIN_TOKEN"}), 403


def require_admin(fn):
    """管理接口统一鉴权：删数据 / 改判 / 加黑名单 / 处理申诉 / 切换引擎 / 重放。"""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not admin_allowed():
            return admin_denied()
        return fn(*args, **kwargs)
    return wrapper


# ------------------------------------------------------------------ #
# 用户权限（RBAC）：登录态解析 + 权限装饰器
# ------------------------------------------------------------------ #
USER_SESSION_KEY = "user"


def current_user() -> Optional[Dict[str, Any]]:
    """当前登录用户：先取 session（浏览器），再取 X-Auth-Token（脚本 / curl）。"""
    user = session.get(USER_SESSION_KEY)
    if isinstance(user, dict) and user.get("username"):
        return user
    token = request.headers.get("X-Auth-Token", "") or request.args.get("token", "")
    return rbac.verify_token(token) if token else None


def current_actor() -> str:
    """操作者标识：未登录时记 anonymous，便于日志区分。"""
    user = current_user()
    return user.get("username", "") if user else "anonymous"


def has_perm(perm: str) -> bool:
    """权限判定。

    已登录   -> 按角色权限表判断；
    未登录   -> RBAC_ENFORCE=1 时一律拒绝；否则沿用旧行为
                （本机回环 / ADMIN_TOKEN 视为内置管理员），保证老脚本与演示不受影响。
    """
    user = current_user()
    if user:
        return rbac.can(user.get("role", ""), perm)
    if config.RBAC_ENFORCE:
        return False
    return admin_allowed() and rbac.can("admin", perm)


def require_perm(perm: str, action: str = ""):
    """按权限点鉴权，并在被拒绝时写一条 denied 操作日志。"""
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if has_perm(perm):
                return fn(*args, **kwargs)
            user = current_user()
            oplog.record(user.get("username", "") if user else "anonymous",
                         action or perm, result="denied",
                         detail=f"权限不足：需要 {perm}",
                         role=user.get("role", "") if user else "", ip=request.remote_addr)
            if not user:
                # 未登录：沿用旧鉴权的响应语义——口令不对/来源不允许走 admin_denied
                # （401 口令缺失或错误 / 403 非本机），强制登录模式则提示先登录
                if config.ADMIN_TOKEN or not _client_is_loopback():
                    return admin_denied()
                if config.RBAC_ENFORCE:
                    return jsonify({"error": "请先登录"}), 401
            return jsonify({"error": f"权限不足，需要 {perm} 权限"}), 403
        return wrapper
    return deco


# ---------------- 图片上传 ----------------
UPLOAD_DIR = config.DATA_DIR / "uploads"
ALLOWED_IMAGE_EXT = {"jpg", "jpeg", "png", "webp", "gif", "bmp"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def _is_real_image(data: bytes) -> bool:
    """用 Pillow 真正解码一次，避免「改扩展名的非图片文件」被当图片收下。

    Pillow 缺失时退化为 magic bytes 检查（能力下降但仍有基本校验）。
    """
    try:
        from PIL import Image
        import io as _io
        im = Image.open(_io.BytesIO(data))
        im.verify()
        return True
    except ImportError:
        return data[:3] in (b"\xff\xd8\xff", b"\x89PN") or data[:8] == b"\x89PNG\r\n\x1a\n" \
            or data[:4] == b"RIFF" or data[:3] == b"GIF" or data[:2] == b"BM"
    except Exception:
        return False


def _remove_upload(name: str) -> None:
    """删除该条目对应的上传文件（best-effort，失败只记日志）。"""
    if not name or "/" in name or "\\" in name:      # 防目录穿越，只接受文件名
        return
    try:
        (UPLOAD_DIR / name).unlink(missing_ok=True)
    except Exception as exc:
        log_swallow(logger, f"清理上传文件 {name}", exc, level=20)


def _handle_upload() -> Dict[str, Any]:
    """保存上传图片并计算感知哈希，供盗图查重使用。

    非法类型 / 超大文件直接忽略（不阻断审核），保证演示时乱传文件也不会 500。
    """
    f = request.files.get("image")
    if f is None or not f.filename:
        return {}
    ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
    if ext not in ALLOWED_IMAGE_EXT:
        return {}
    data = f.read(MAX_IMAGE_BYTES + 1)
    if not data or len(data) > MAX_IMAGE_BYTES:
        return {}
    if not _is_real_image(data):                     # 内容校验：防止任意文件伪装成图片
        return {}
    try:
        h = image_hash(data)
        low = is_low_information(data)
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        name = f"{(h or 'img')}_{int(time.time() * 1000)}.{ext}"
        (UPLOAD_DIR / name).write_bytes(data)
    except Exception as exc:
        log_swallow(logger, "图片保存/哈希计算", exc)
        return {}
    return {"has_image": 1, "image_hash": h, "image_low_info": 1 if low else 0,
            "image_path": name}


def payload() -> Dict[str, Any]:
    """读取并清洗请求参数（JSON 或表单，支持图片上传）。"""
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    data = {**data, **_handle_upload()}
    return {
        "title": _text(data.get("title"), MAX_TITLE_LEN),
        "description": _text(data.get("description"), MAX_DESC_LEN),
        "price": _price(data.get("price")),
        "condition": _text(data.get("condition"), MAX_TEXT_FIELD_LEN),
        "seller_id": _text(data.get("seller_id"), MAX_TEXT_FIELD_LEN),
        "seller_name": _text(data.get("seller_name"), MAX_TEXT_FIELD_LEN),
        "contact": _text(data.get("contact"), MAX_TEXT_FIELD_LEN),
        "has_image": 1 if data.get("has_image") else 0,
        # 图片侧审核字段（感知哈希 / 低信息量标记），无图时为空
        "image_hash": _text(data.get("image_hash"), 64),
        "image_low_info": 1 if data.get("image_low_info") else 0,
        # 上传文件落盘后的文件名（此前算出来就丢弃，导致孤儿文件无法回收与复核）
        "image_path": _text(data.get("image_path"), 200),
    }


# ------------------------------------------------------------------ #
# 页面
# ------------------------------------------------------------------ #
@app.route("/")
def index():
    stats = db.stats()
    return render_template(
        "index.html",
        categories=CATEGORIES,
        stats=stats,
        engine=engine_name(),
        provider=config.LLM_PROVIDER,
        items=db.list_items(limit=60),
        status_meta=STATUS_META,
        cat_name=CAT_NAME,
        cat_color=CAT_COLOR,
        samples=QUICK_SAMPLES,
    )


@app.route("/admin")
def admin():
    status = request.args.get("status", "")
    category = request.args.get("category", "")
    keyword = request.args.get("keyword", "")
    items = db.list_items(status=status, category=category, keyword=keyword, limit=300)
    me = current_user()
    # 运维中心数据按权限裁剪：看不到就传空列表，页面只隐藏面板，不报错
    return render_template(
        "admin.html",
        items=items,
        stats=db.stats(),
        trend=db.daily_trend(14),
        categories=CATEGORIES,
        blacklist=db.blacklist_all(),
        logs=db.list_logs(30),
        appeals=db.list_appeals(status="pending", limit=50),
        appeal_meta=APPEAL_STATUS_META,
        filters={"status": status, "category": category, "keyword": keyword},
        status_meta=STATUS_META,
        cat_name=CAT_NAME,
        engine=engine_name(),
        me=me,
        can={"user": has_perm("user.manage"), "oplog": has_perm("oplog.read"),
             "batch": has_perm("batch.read"), "import": has_perm("data.import"),
             "notify_broadcast": has_perm("notify.broadcast")},
        users=rbac.list_users(limit=50) if has_perm("user.manage") else [],
        role_names=rbac.ROLES,
        oplogs=oplog.query(limit=30)["logs"] if has_perm("oplog.read") else [],
        jobs=batchsvc.list_jobs(limit=10) if has_perm("batch.read") else [],
        batch_ops=batchsvc.OPS,
        notifications=notify.inbox(me.get("username", ""), limit=15) if me else [],
    )


# ------------------------------------------------------------------ #
# 卖家中心：我的发布 / 审核反馈 / 申诉
# ------------------------------------------------------------------ #
SELLER_SESSION_KEY = "seller_id"
APPEALABLE_STATUS = ("reject", "review")      # 仅这两类结论可申诉
APPEAL_STATUS_META = {
    "pending": ("申诉待处理", "warn"),
    "accepted": ("申诉成立，已恢复发布", "ok"),
    "rejected": ("申诉驳回，维持原判", "bad"),
}


def current_seller() -> Optional[Dict[str, str]]:
    sid = session.get(SELLER_SESSION_KEY)
    if not sid:
        return None
    return {"seller_id": sid, "seller_name": session.get("seller_name", "")}


@app.route("/seller")
def seller():
    me = current_seller()
    return render_template(
        "seller.html",
        seller=me,
        stats=db.seller_stats(me["seller_id"]) if me else None,
        items=db.list_items(seller_id=me["seller_id"], limit=100) if me else [],
        appeals=db.list_appeals(seller_id=me["seller_id"], limit=50) if me else [],
        status_meta=STATUS_META,
        cat_name=CAT_NAME,
        appeal_meta=APPEAL_STATUS_META,
        engine=engine_name(),
        filters={"status": request.args.get("status", "")},
    )


@app.route("/api/session", methods=["GET", "POST", "DELETE"])
def api_session():
    """卖家登录态。

    演示模式（未配置 SELLER_LOGIN_CODE）沿用学号直登，便于答辩演示；
    一旦在 .env 配置了统一口令，则必须校验通过才允许登录。
    """
    if request.method == "POST":
        data = request.get_json(silent=True) or request.form.to_dict() or {}
        sid = _text(data.get("seller_id"), MAX_TEXT_FIELD_LEN)
        if not sid:
            return jsonify({"error": "学号不能为空"}), 400
        if config.SELLER_LOGIN_CODE:
            code = _text(data.get("code"), MAX_TEXT_FIELD_LEN)
            if not code:
                return jsonify({"error": "本平台已启用登录口令"}), 401
            if code != config.SELLER_LOGIN_CODE:
                return jsonify({"error": "登录口令不正确"}), 401
        session[SELLER_SESSION_KEY] = sid
        session["seller_name"] = _text(data.get("seller_name"), MAX_TEXT_FIELD_LEN) or f"同学{sid[-4:]}"
        return jsonify({"ok": True, "seller": current_seller()})
    if request.method == "DELETE":
        session.pop(SELLER_SESSION_KEY, None)
        session.pop("seller_name", None)
        return jsonify({"ok": True})
    return jsonify({"ok": True, "seller": current_seller()})


@app.route("/api/seller/items")
def api_seller_items():
    me = current_seller()
    if not me:
        return jsonify({"error": "请先登录卖家账号"}), 401
    return jsonify({
        "ok": True,
        "stats": db.seller_stats(me["seller_id"]),
        "items": db.list_items(status=_text(request.args.get("status"), 20),
                               seller_id=me["seller_id"], limit=200),
    })


@app.route("/api/appeals", methods=["GET", "POST"])
def api_appeals():
    if request.method == "POST":
        me = current_seller()
        if not me:
            return jsonify({"error": "请先登录卖家账号"}), 401
        data = request.get_json(silent=True) or request.form.to_dict() or {}
        try:
            item_id = int(data.get("item_id"))
        except (TypeError, ValueError):
            return jsonify({"error": "item_id 非法"}), 400

        item = db.get_item(item_id)
        if not item:
            return jsonify({"error": "商品不存在"}), 404
        if (item.get("seller_id") or "") != me["seller_id"]:
            return jsonify({"error": "只能对本人发布的信息发起申诉"}), 403
        if item.get("status") not in APPEALABLE_STATUS:
            return jsonify({"error": "仅「拒绝发布」或「转人工复核」的信息可申诉"}), 400
        if db.pending_appeal_of(item_id):
            return jsonify({"error": "该信息已有申诉在处理中"}), 409

        reason = _text(data.get("reason"), 500)
        if len(reason) < 5:
            return jsonify({"error": "申诉理由请不少于 5 个字"}), 400
        appeal_id = db.insert_appeal(item_id, me["seller_id"],
                                     me.get("seller_name", ""), reason)
        db.add_log(item_id, "appeal", f"卖家申诉：{reason[:60]}", me["seller_id"])
        return jsonify({"ok": True, "id": appeal_id, "appeal": db.get_appeal(appeal_id)})

    status = _text(request.args.get("status"), 20)
    if request.args.get("scope") == "all":                     # 管理员视角
        if not admin_allowed():                   # 申诉含学号与理由，禁止匿名拉取
            return admin_denied()
        return jsonify({"ok": True,
                        "appeals": db.list_appeals(status=status, limit=200),
                        "pending": db.count_appeals("pending")})
    me = current_seller()
    if not me:
        return jsonify({"error": "请先登录卖家账号"}), 401
    return jsonify({"ok": True, "appeals": db.list_appeals(
        status=status, seller_id=me["seller_id"], limit=100)})


@app.route("/api/appeals/<int:appeal_id>/handle", methods=["POST"])
@require_perm("appeal.handle", "appeal.handle")
def api_appeal_handle(appeal_id: int):
    """管理员处理申诉：成立则恢复发布，驳回则维持原判，并通知卖家。"""
    appeal = db.get_appeal(appeal_id)
    if not appeal:
        return jsonify({"error": "申诉不存在"}), 404
    if appeal["status"] != "pending":
        return jsonify({"error": "该申诉已处理"}), 409

    data = request.get_json(silent=True) or request.form.to_dict() or {}
    status = _text(data.get("status"), 20)
    if status not in ("accepted", "rejected"):
        return jsonify({"error": "处理结果非法（accepted / rejected）"}), 400

    reply = _text(data.get("reply"), 500)
    handler = _text(data.get("handler"), MAX_TEXT_FIELD_LEN) or "admin"
    final_status = "pass" if status == "accepted" else "reject"

    # 申诉状态 + 商品状态 + 日志在同一事务内完成，避免"申诉已处理但商品未恢复"
    db.handle_appeal_tx(appeal_id, status, handler, reply, final_status,
                        f"申诉{APPEAL_STATUS_META[status][0]}｜{reply}")
    notify.notify_appeal_result(appeal_id, appeal.get("seller_id", ""), status, reply)
    oplog.record(handler, "appeal.handle", target_type="appeal", target_id=appeal_id,
                 result="ok", detail=f"{APPEAL_STATUS_META[status][0]}｜{reply or '无'}",
                 ip=request.remote_addr)
    return jsonify({"ok": True, "id": appeal_id, "status": status,
                    "item_status": final_status})


# ------------------------------------------------------------------ #
# REST API
# ------------------------------------------------------------------ #
@app.route("/api/audit", methods=["POST"])
def api_audit():
    """只审核不入库，用于前端实时预览。"""
    data = payload()
    if not (data["title"] or data["description"]):
        return jsonify({"error": "标题和描述不能为空"}), 400
    result = audit(data)
    return jsonify({"ok": True, "result": result.to_dict()})


@app.route("/api/batch", methods=["POST"])
def api_batch():
    """批量审核：一次提交多条（只审核不入库），用于批量预检 / 演示压测。"""
    data = request.get_json(silent=True) or {}
    raw = data.get("items")
    if not isinstance(raw, list) or not raw:
        return jsonify({"error": "items 必须是非空数组"}), 400
    if len(raw) > MAX_BATCH:
        return jsonify({"error": f"单次最多审核 {MAX_BATCH} 条"}), 400

    t0 = time.time()
    rows, counts = [], {"pass": 0, "review": 0, "reject": 0}
    for i, one in enumerate(raw):
        if not isinstance(one, dict):
            rows.append({"index": i, "error": "条目格式非法"})
            continue
        item = {
            "title": _text(one.get("title"), MAX_TITLE_LEN),
            "description": _text(one.get("description"), MAX_DESC_LEN),
            "price": _price(one.get("price")),
            "condition": _text(one.get("condition"), MAX_TEXT_FIELD_LEN),
            "seller_id": _text(one.get("seller_id"), MAX_TEXT_FIELD_LEN),
            "contact": _text(one.get("contact"), MAX_TEXT_FIELD_LEN),
            "has_image": 1 if one.get("has_image") else 0,
        }
        if not (item["title"] or item["description"]):
            rows.append({"index": i, "error": "标题和描述不能为空"})
            continue
        res = audit(item)
        counts[res.decision] += 1
        rows.append({"index": i, "title": item["title"][:40], "decision": res.decision,
                     "decision_label": res.decision_label, "risk_score": res.risk_score,
                     "category": res.category_name,
                     "top_rule": res.rule.hits[0].rule if res.rule and res.rule.hits else ""})

    total_ms = int((time.time() - t0) * 1000)
    checked = sum(counts.values())
    return jsonify({"ok": True, "rows": rows, "summary": {
        "total": len(raw), "checked": checked, "counts": counts,
        "total_ms": total_ms,
        "avg_ms": round(total_ms / checked, 2) if checked else 0.0,
        "reject_rate": round(counts["reject"] / checked, 3) if checked else 0.0,
    }})


@app.route("/api/export.csv")
def api_export_csv():
    """旧版导出入口（保持兼容）：内部统一走 core/dataio.export_items。"""
    return api_export()


@app.route("/api/report", methods=["POST"])
def api_report():
    """用户举报：已通过的信息被举报后自动转人工复核，并计入规则漏放反馈。"""
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    try:
        item_id = int(data.get("item_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "item_id 非法"}), 400
    item = db.get_item(item_id)
    if not item:
        return jsonify({"error": "商品不存在"}), 404
    reason = _text(data.get("reason"), 200)
    if len(reason) < 5:
        return jsonify({"error": "举报理由请不少于 5 个字"}), 400

    db.add_log(item_id, "report", f"用户举报：{reason[:80]}", "user")
    to_review = False
    if item.get("status") == "pass":           # 机器放过但被举报 -> 交回人工
        db.update_review(item_id, "review", reviewer="system",
                         note=f"用户举报，转人工复核｜{reason[:60]}")
        to_review = True
    return jsonify({"ok": True, "id": item_id, "to_review": to_review})


@app.route("/api/governance")
def api_governance():
    """规则治理报表：命中分布 + 申诉/人工改判/举报反馈 + 调参建议。"""
    return jsonify({"ok": True, "report": rule_report()})


@app.route("/api/replay", methods=["POST"])
@require_perm("rule.replay", "rule.replay")
def api_replay():
    """规则重放：用当前词表重跑历史数据，量化规则升级的影响面（只读，不写库）。"""
    rep = replay_history(limit=_int((request.get_json(silent=True) or {}).get("limit"),
                                    200, 1, 1000))
    db.add_log(None, "replay",
               f"规则重放（{rep['lexicon_version']}）：{rep['changed']}/{rep['total']} 条结论变化",
               "admin")
    return jsonify({"ok": True, "report": rep})


@app.route("/api/items", methods=["GET"])
def api_items():
    return jsonify({
        "ok": True,
        "items": db.list_items(
            status=request.args.get("status", ""),
            category=request.args.get("category", ""),
            keyword=request.args.get("keyword", ""),
            limit=_int(request.args.get("limit"), 200, 1, 500),
        ),
    })


@app.route("/api/items", methods=["POST"])
def api_items_post():
    data = payload()
    if not data["title"]:
        return jsonify({"error": "标题不能为空"}), 400
    record = submit(data, source="api")
    # 审核结论即时通知卖家：避免"发布后不知道结果"
    reasons = (record.get("audit", {}) or {}).get("reasons", [])
    notify.notify_audit_result(record.get("id"), data.get("seller_id", ""),
                               record.get("status", ""), record.get("risk_score", 0),
                               "；".join(reasons)[:120])
    return jsonify({"ok": True, "item": {k: v for k, v in record.items() if k != "audit"},
                    "audit": record["audit"]})


@app.route("/api/items/<int:item_id>", methods=["GET", "DELETE"])
def api_item(item_id: int):
    if request.method == "DELETE":
        if not admin_allowed():
            return admin_denied()
        item = db.get_item(item_id)               # 删除前取回，用于清理上传文件
        _remove_upload((item or {}).get("image_path", ""))
        db.delete_item(item_id)
        db.add_log(item_id, "delete", "管理员删除", "admin")
        oplog.record(current_actor(), "item.delete", target_type="item",
                     target_id=item_id, ip=request.remote_addr)
        return jsonify({"ok": True})
    item = db.get_item(item_id)
    if not item:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True, "item": item})


@app.route("/api/items/<int:item_id>/review", methods=["POST"])
@require_perm("item.review", "item.review")
def api_review(item_id: int):
    data = request.get_json(silent=True) or request.form.to_dict()
    status = data.get("status", "pass")
    if status not in STATUS_META:
        return jsonify({"error": "状态非法"}), 400
    note = _text(data.get("note"), 200)
    reviewer = _text(data.get("reviewer"), MAX_TEXT_FIELD_LEN) or current_actor()
    item = db.get_item(item_id)
    db.update_review(item_id, status, reviewer=reviewer, note=note)
    db.add_log(item_id, f"manual_{status}", f"人工判定为{STATUS_META[status][0]}｜{note}", reviewer)
    notify.notify_review_result(item_id, (item or {}).get("seller_id", ""), status, note)
    oplog.record(reviewer, "item.review", target_type="item", target_id=item_id,
                 result="ok", detail=f"判定为{STATUS_META[status][0]}｜{note or '无'}",
                 ip=request.remote_addr)
    return jsonify({"ok": True, "id": item_id, "status": status})


@app.route("/api/blacklist", methods=["GET", "POST", "DELETE"])
@require_perm("blacklist.write", "blacklist.add")
def api_blacklist():
    if request.method == "POST":
        data = request.get_json(silent=True) or request.form.to_dict() or {}
        user_id = _text(data.get("user_id"), MAX_TEXT_FIELD_LEN)
        if not user_id:
            return jsonify({"error": "user_id 必填"}), 400
        db.blacklist_add(user_id, _text(data.get("reason"), 200))
        oplog.record(current_actor(), "blacklist.add", target_type="seller",
                     target_id=user_id, detail=_text(data.get("reason"), 200),
                     ip=request.remote_addr)
        return jsonify({"ok": True, "blacklist": db.blacklist_all()})
    if request.method == "DELETE":
        user_id = _text(request.args.get("user_id"), MAX_TEXT_FIELD_LEN)
        if not user_id:
            return jsonify({"error": "user_id 必填"}), 400
        db.blacklist_remove(user_id)
        oplog.record(current_actor(), "blacklist.remove", target_type="seller",
                     target_id=user_id, ip=request.remote_addr)
        return jsonify({"ok": True, "blacklist": db.blacklist_all()})
    return jsonify({"ok": True, "blacklist": db.blacklist_all()})


@app.route("/api/metrics")
def api_metrics():
    """运行期指标：分级调用节省了多少次远程大模型调用。"""
    st: Dict[str, Any] = dict(PIPELINE.stats)
    st["skip_rate"] = (st["llm_skipped"] / st["total"]) if st["total"] else 0.0
    st["policy"] = {"skip_remote_rule_score": SKIP_REMOTE_RULE_SCORE}
    # 远程调用失败降级的次数：>0 说明部分结论其实来自本地兜底，不能当成真实模型结论
    st["llm"] = fallback_stats()
    return jsonify({"ok": True, "metrics": st})


@app.route("/api/stats")
def api_stats():
    return jsonify({"ok": True, "stats": db.stats(), "trend": db.daily_trend(14)})


@app.route("/api/categories")
def api_categories():
    return jsonify({"ok": True, "categories": [
        {"key": c["key"], "name": c["name"], "icon": c["icon"], "desc": c["desc"]}
        for c in CATEGORIES]})


@app.route("/api/engine", methods=["GET", "POST"])
@require_perm("engine.switch", "engine.switch")
def api_engine():
    """查看 / 切换语义层引擎（本地分析器 ↔ 远程大模型），无需重启进程。"""
    if request.method == "GET":
        return jsonify({"ok": True, "status": engine_status()})
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    provider = _text(data.get("provider"), 32)
    try:
        status = switch_engine(provider)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    db.add_log(None, "switch_engine", f"审核引擎切换为 {status['provider']}", "admin")
    return jsonify({"ok": True, "status": status})


@app.route("/api/system")
def api_system():
    return jsonify({
        "ok": True,
        "provider": config.LLM_PROVIDER,
        "engine": engine_name(),
        "model": config.LLM_MODEL,
        "thresholds": {"reject": config.REJECT_SCORE, "review": config.REVIEW_SCORE,
                       "rule_weight": config.RULE_WEIGHT, "llm_weight": config.LLM_WEIGHT},
        "lexicon_version": LEXICON_VERSION,          # 当前生效的规则词表版本（可追溯）
        "image_audit": {"enabled": has_pillow()},    # 图片查重是否可用（缺 Pillow 时退化）
        "text_audit": {"enabled": True},             # 文案查重（SimHash，无外部依赖）
        "batch_limit": MAX_BATCH,
        "categories": [c["key"] for c in CATEGORIES],
        # 只暴露文件名，不暴露服务器绝对路径（信息泄露面）
        "db_name": config.DB_PATH.name,
        "auth": {"admin_token_required": bool(config.ADMIN_TOKEN),
                 "seller_code_required": bool(config.SELLER_LOGIN_CODE),
                 "rbac_enforce": config.RBAC_ENFORCE,
                 "current_user": (current_user() or {}).get("username", "")},
        # 运维模块能力开关与限额，前端据此决定是否展示对应面板
        "limits": {"batch_max_items": config.BATCH_MAX_ITEMS,
                   "import_max_rows": config.IMPORT_MAX_ROWS,
                   "oplog_retention_days": config.OPLOG_RETENTION_DAYS},
        "modules": {"rbac": True, "dataio": True, "oplog": True,
                    "notify": True, "batch": True},
    })


# ================================================================== #
# 扩展模块一：用户权限管理（RBAC）
# ================================================================== #
@app.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    """账号口令登录：成功返回用户信息与令牌（浏览器写 session，脚本用 X-Auth-Token）。"""
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    username = _text(data.get("username"), 32)
    password = _text(data.get("password"), 128)
    if not username or not password:
        return jsonify({"error": "用户名与口令不能为空"}), 400
    user = rbac.authenticate(username, password)
    if not user:
        oplog.record(username or "anonymous", "login", result="denied",
                     detail="口令错误或账号已停用", ip=request.remote_addr)
        return jsonify({"error": "用户名或口令不正确，或账号已停用"}), 401
    session[USER_SESSION_KEY] = user
    # 卖家角色顺带打通原有的"学号登录态"，登录后可直接用卖家中心
    if user.get("seller_id"):
        session[SELLER_SESSION_KEY] = user["seller_id"]
        session["seller_name"] = user.get("display_name", "")
    oplog.record(user["username"], "login", result="ok", role=user["role"],
                 detail=f"{user['role_name']}登录", ip=request.remote_addr)
    return jsonify({"ok": True, "user": user, "token": rbac.issue_token(user),
                    "expires_in": config.AUTH_TOKEN_TTL})


@app.route("/api/auth/logout", methods=["POST"])
def api_auth_logout():
    user = current_user()
    if user:
        oplog.record(user["username"], "logout", role=user.get("role", ""),
                     ip=request.remote_addr)
    session.pop(USER_SESSION_KEY, None)
    return jsonify({"ok": True})


@app.route("/api/auth/me")
def api_auth_me():
    user = current_user()
    return jsonify({"ok": True, "user": user, "enforce": config.RBAC_ENFORCE})


@app.route("/api/permissions")
def api_permissions():
    """角色 / 权限矩阵（前端渲染用，无敏感信息）。"""
    return jsonify({"ok": True, **rbac.role_matrix()})


@app.route("/api/users", methods=["GET", "POST"])
@require_perm("user.manage", "user.create")
def api_users():
    if request.method == "POST":
        data = request.get_json(silent=True) or request.form.to_dict() or {}
        try:
            uid = rbac.create_user(_text(data.get("username"), 32),
                                   _text(data.get("password"), 128),
                                   _text(data.get("role"), 20) or "seller",
                                   _text(data.get("display_name"), 50),
                                   _text(data.get("seller_id"), MAX_TEXT_FIELD_LEN))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        oplog.record(current_actor(), "user.create", target_type="user", target_id=uid,
                     detail=f"创建用户 {_text(data.get('username'), 32)}"
                            f"（{_text(data.get('role'), 20) or 'seller'}）",
                     ip=request.remote_addr)
        return jsonify({"ok": True, "user": rbac.get_user(uid)})
    return jsonify({"ok": True,
                    "users": rbac.list_users(role=_text(request.args.get("role"), 20),
                                             keyword=_text(request.args.get("keyword"), 50),
                                             limit=_int(request.args.get("limit"), 200, 1, 500)),
                    "roles": rbac.ROLES})


@app.route("/api/users/<int:user_id>", methods=["GET", "DELETE"])
@require_perm("user.manage", "user.delete")
def api_user(user_id: int):
    if request.method == "DELETE":
        me = current_user()
        if me and int(me.get("id") or 0) == user_id:
            return jsonify({"error": "不能删除当前登录账号"}), 400
        try:
            rbac.delete_user(user_id)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        oplog.record(current_actor(), "user.delete", target_type="user",
                     target_id=user_id, ip=request.remote_addr)
        return jsonify({"ok": True, "id": user_id})
    user = rbac.get_user(user_id)
    if not user:
        return jsonify({"error": "用户不存在"}), 404
    return jsonify({"ok": True, "user": user})


@app.route("/api/users/<int:user_id>/role", methods=["POST"])
@require_perm("user.manage", "user.role")
def api_user_role(user_id: int):
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    try:
        user = rbac.set_role(user_id, _text(data.get("role"), 20))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    oplog.record(current_actor(), "user.role", target_type="user", target_id=user_id,
                 detail=f"角色调整为 {user['role_name']}", ip=request.remote_addr)
    return jsonify({"ok": True, "user": user})


@app.route("/api/users/<int:user_id>/status", methods=["POST"])
@require_perm("user.manage", "user.status")
def api_user_status(user_id: int):
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    try:
        user = rbac.set_status(user_id, _text(data.get("status"), 20))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    oplog.record(current_actor(), "user.status", target_type="user", target_id=user_id,
                 detail=f"账号状态改为 {user['status']}", ip=request.remote_addr)
    return jsonify({"ok": True, "user": user})


@app.route("/api/users/<int:user_id>/password", methods=["POST"])
@require_perm("user.manage", "user.password")
def api_user_password(user_id: int):
    """管理员重置指定用户口令（不需要旧口令）。"""
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    try:
        rbac.reset_password(user_id, _text(data.get("password"), 128))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    oplog.record(current_actor(), "user.password", target_type="user", target_id=user_id,
                 ip=request.remote_addr)
    return jsonify({"ok": True, "id": user_id})


# ================================================================== #
# 扩展模块二：数据导出与导入
# ================================================================== #
@app.route("/api/export")
def api_export():
    """统一导出入口：fmt=csv（默认）/ json，支持 status/category/keyword/limit/mask 过滤。"""
    try:
        exp = dataio.export_items(
            fmt=_text(request.args.get("fmt"), 8) or "csv",
            status=_text(request.args.get("status"), 20),
            category=_text(request.args.get("category"), 32),
            keyword=_text(request.args.get("keyword"), 50),
            limit=_int(request.args.get("limit"), 500, 1, config.IMPORT_MAX_ROWS),
            mask=request.args.get("mask", "1") not in ("0", "false", "False"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    oplog.record(current_actor(), "data.export", result="ok",
                 detail=f"{exp['fmt']}，{exp['count']} 条", ip=request.remote_addr)
    return Response(exp["content"], mimetype=exp["mime"],
                    headers={"Content-Disposition":
                             f"attachment; filename={exp['filename']}"})


@app.route("/api/export/template")
def api_export_template():
    """导入模板下载：只有表头与一行示例。"""
    return Response("\ufeff" + dataio.template_csv().lstrip("\ufeff"),
                    mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             "attachment; filename=import_template.csv"})


@app.route("/api/import", methods=["POST"])
@require_perm("data.import", "data.import")
def api_import():
    """批量导入：支持 JSON rows / 文本 content / 上传文件三种提交方式。

    参数：mode=audit|submit|restore，dry_run=1 时只返回预览不写库。
    """
    data = request.get_json(silent=True) or {}
    mode = _text(data.get("mode") or request.form.get("mode"), 16) or "submit"
    dry_run = str(data.get("dry_run") or request.form.get("dry_run") or "") in ("1", "true", "True")

    rows = data.get("rows") if isinstance(data.get("rows"), list) else \
        data.get("items") if isinstance(data.get("items"), list) else None
    parse_errors: list = []
    if rows is None:
        text = str(data.get("content") or request.form.get("content") or "")
        f = request.files.get("file")
        if not text and f is not None and f.filename:
            text = f.read(2 * 1024 * 1024).decode("utf-8-sig", errors="replace")
        if not text.strip():
            return jsonify({"error": "请提供 rows / content / file 任一导入内容"}), 400
        rows, parse_errors = dataio.parse_table(
            text, _text(data.get("fmt") or request.form.get("fmt"), 8) or "auto")

    try:
        report = dataio.import_items(rows, mode=mode, dry_run=dry_run,
                                     source=_text(data.get("source"), 20) or "import",
                                     operator=current_actor())
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    report["parse_errors"] = parse_errors
    oplog.record(current_actor(), "data.import", result="ok",
                 detail=f"mode={mode}｜{report['total']} 行，成功 {report['ok']}，"
                        f"失败 {report['failed']}"
                        f"{'（dry_run）' if dry_run else ''}",
                 ip=request.remote_addr)
    return jsonify({"ok": True, "report": report})


# ================================================================== #
# 扩展模块三：操作日志
# ================================================================== #
@app.route("/api/oplog")
@require_perm("oplog.read", "oplog.read")
def api_oplog():
    return jsonify({"ok": True, **oplog.query(
        actor=_text(request.args.get("actor"), 32),
        action=_text(request.args.get("action"), 32),
        result=_text(request.args.get("result"), 16),
        keyword=_text(request.args.get("keyword"), 50),
        limit=_int(request.args.get("limit"), 100, 1, 500),
        offset=_int(request.args.get("offset"), 0, 0, 100000))})


@app.route("/api/oplog/stats")
@require_perm("oplog.read", "oplog.read")
def api_oplog_stats():
    return jsonify({"ok": True, "stats": oplog.stats(_int(request.args.get("days"), 7, 1, 90)),
                    "retention_days": config.OPLOG_RETENTION_DAYS})


@app.route("/api/oplog/purge", methods=["POST"])
@require_perm("oplog.purge", "oplog.purge")
def api_oplog_purge():
    data = request.get_json(silent=True) or {}
    removed = oplog.purge(_int(data.get("days"), config.OPLOG_RETENTION_DAYS, 1, 3650))
    return jsonify({"ok": True, "removed": removed})


# ================================================================== #
# 扩展模块四：消息通知
# ================================================================== #
def _notify_user_id() -> Tuple[str, Any]:
    """解析通知归属：管理员可代查他人，普通用户只能看自己的。

    返回 (user_id, 错误响应)；错误响应非空时调用方直接返回它。
    """
    user = current_user()
    if not user:
        # 兼容"学号直登"的卖家：没有 RBAC 账号时按学号取收件箱
        sid = session.get(SELLER_SESSION_KEY)
        if not sid:
            return "", (jsonify({"error": "请先登录"}), 401)
        return str(sid), None
    target = _text(request.args.get("user_id"), 32)
    if target and rbac.can(user.get("role", ""), "notify.broadcast"):
        return target, None
    return str(user.get("username", "")), None


@app.route("/api/notifications", methods=["GET", "POST"])
def api_notifications():
    if request.method == "POST":                      # 管理员广播
        if not has_perm("notify.broadcast"):
            return jsonify({"error": "权限不足，需要 notify.broadcast 权限"}), 403
        data = request.get_json(silent=True) or request.form.to_dict() or {}
        title = _text(data.get("title"), 100)
        content = _text(data.get("content"), 500)
        if not title:
            return jsonify({"error": "通知标题不能为空"}), 400
        role = _text(data.get("role"), 20)
        sent = notify.broadcast(title, content,
                                level=_text(data.get("level"), 16) or "info",
                                role=role or None)
        oplog.record(current_actor(), "notify.broadcast", result="ok",
                     detail=f"发给 {sent} 人（role={role or '全部'}）：{title}",
                     ip=request.remote_addr)
        return jsonify({"ok": True, "sent": sent})

    user_id, err = _notify_user_id()
    if err:
        return err
    return jsonify({"ok": True, "notifications": notify.inbox(
        user_id, unread_only=request.args.get("unread") == "1",
        level=_text(request.args.get("level"), 16),
        limit=_int(request.args.get("limit"), 50, 1, 200))})


@app.route("/api/notifications/summary")
def api_notifications_summary():
    user_id, err = _notify_user_id()
    if err:
        return err
    return jsonify({"ok": True, "summary": notify.summary(user_id)})


@app.route("/api/notifications/read-all", methods=["POST"])
def api_notifications_read_all():
    user_id, err = _notify_user_id()
    if err:
        return err
    return jsonify({"ok": True, "updated": notify.mark_all_read(user_id)})


@app.route("/api/notifications/<int:notify_id>/read", methods=["POST"])
def api_notification_read(notify_id: int):
    user_id, err = _notify_user_id()
    if err:
        return err
    # 管理员可代标记（_notify_user_id 已做权限判断），普通用户只能标记自己的
    target = user_id if rbac.can((current_user() or {}).get("role", ""),
                                 "notify.broadcast") else user_id
    return jsonify({"ok": True, "updated": notify.mark_read(notify_id, target)})


# ================================================================== #
# 扩展模块五：批量处理
# ================================================================== #
@app.route("/api/batch/ops")
def api_batch_ops():
    """支持的批量操作清单（前端下拉框用）。"""
    return jsonify({"ok": True, "ops": [{"key": k, "name": v} for k, v in batchsvc.OPS.items()],
                    "max_items": config.BATCH_MAX_ITEMS})


@app.route("/api/batch/jobs", methods=["GET", "POST"])
def api_batch_jobs():
    if request.method == "POST":
        if not has_perm("batch.run"):
            oplog.record(current_actor(), "batch.run", result="denied",
                         detail="权限不足：需要 batch.run", ip=request.remote_addr)
            return jsonify({"error": "权限不足，需要 batch.run 权限"}), 403
        data = request.get_json(silent=True) or {}
        op = _text(data.get("op"), 20)
        payload = data.get("payload") if data.get("payload") is not None else data
        try:
            job = batchsvc.run_job(op, payload, operator=current_actor(),
                                   role=(current_user() or {}).get("role", ""))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "job": job})
    if not has_perm("batch.read"):
        return jsonify({"error": "权限不足，需要 batch.read 权限"}), 403
    return jsonify({"ok": True,
                    "jobs": batchsvc.list_jobs(op=_text(request.args.get("op"), 20),
                                               limit=_int(request.args.get("limit"), 20, 1, 100)),
                    "stats": batchsvc.stats()})


@app.route("/api/batch/jobs/<int:job_id>")
def api_batch_job(job_id: int):
    if not has_perm("batch.read"):
        return jsonify({"error": "权限不足，需要 batch.read 权限"}), 403
    job = batchsvc.get_job(job_id)
    if not job:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify({"ok": True, "job": job})


@app.errorhandler(HTTPException)
def handle_http(e: HTTPException):
    """所有 HTTP 异常统一返回 JSON，避免前端拿到 HTML 错误页。"""
    return jsonify({"error": e.description or e.name}), e.code or 500


@app.errorhandler(Exception)
def handle_exception(e: Exception):
    """未捕获异常兜底：记录日志并返回结构化错误，不暴露堆栈。"""
    if isinstance(e, HTTPException):
        return handle_http(e)
    app.logger.exception("未处理异常：%s", e)
    return jsonify({"error": f"服务内部错误：{type(e).__name__}"}), 500


if __name__ == "__main__":
    print("=" * 64)
    print("  校园二手交易信息智能审核与分类系统")
    print(f"  审核引擎 : {engine_name()}")
    print(f"  Web 地址 : http://{config.FLASK_HOST}:{config.FLASK_PORT}")
    print(f"  数据库   : {config.DB_PATH}")
    print("=" * 64)
    app.run(host=config.FLASK_HOST, port=config.FLASK_PORT, debug=False)
