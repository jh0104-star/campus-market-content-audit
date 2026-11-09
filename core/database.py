# -*- coding: utf-8 -*-
"""SQLite 数据访问层。

表：items(商品与审核结论) / audit_logs(审核流水) / blacklist(卖家黑名单)
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

import config

# 黑名单读取缓存有效期（秒）：审核链路每次都要查，没必要每次打库
BLACKLIST_TTL = 30.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT NOT NULL,
    description   TEXT DEFAULT '',
    price         REAL,
    condition     TEXT DEFAULT '',
    category      TEXT DEFAULT 'other',
    sub_category  TEXT DEFAULT '',
    seller_id     TEXT DEFAULT '',
    seller_name   TEXT DEFAULT '',
    contact       TEXT DEFAULT '',
    has_image     INTEGER DEFAULT 0,
    status        TEXT DEFAULT 'pending',
    decision      TEXT DEFAULT '',
    risk_score    INTEGER DEFAULT 0,
    risk_level    TEXT DEFAULT 'low',
    rule_score    INTEGER DEFAULT 0,
    llm_score     INTEGER DEFAULT 0,
    reason        TEXT DEFAULT '',
    suggestion    TEXT DEFAULT '',
    detail_json   TEXT DEFAULT '',
    source        TEXT DEFAULT 'web',
    created_at    TEXT DEFAULT '',
    reviewed_at   TEXT DEFAULT '',
    reviewer      TEXT DEFAULT '',
    review_note   TEXT DEFAULT '',
    image_hash    TEXT DEFAULT '',     -- 图片感知哈希，用于盗图/一图多帖查重
    text_fp       TEXT DEFAULT '',     -- 文本 SimHash 指纹，用于刷屏/盗帖查重
    image_path    TEXT DEFAULT ''      -- 上传图片落盘文件名（便于复核与回收）
);

-- 规则命中明细：每条审核命中了哪些规则，供规则治理与闭环学习统计
CREATE TABLE IF NOT EXISTS rule_hits (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id    INTEGER,
    rule       TEXT,
    type       TEXT,
    severity   TEXT,
    score      INTEGER DEFAULT 0,
    created_at TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id    INTEGER,
    action     TEXT,
    detail     TEXT DEFAULT '',
    operator   TEXT DEFAULT 'system',
    created_at TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS blacklist (
    user_id    TEXT PRIMARY KEY,
    reason     TEXT DEFAULT '',
    created_at TEXT DEFAULT ''
);

-- 卖家申诉：被拒绝/转人工的信息可发起申诉，管理员处理后闭环
CREATE TABLE IF NOT EXISTS appeals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     INTEGER NOT NULL,
    seller_id   TEXT NOT NULL,
    seller_name TEXT DEFAULT '',
    reason      TEXT DEFAULT '',
    status      TEXT DEFAULT 'pending',   -- pending / accepted / rejected
    reply       TEXT DEFAULT '',
    handler     TEXT DEFAULT '',
    created_at  TEXT DEFAULT '',
    handled_at  TEXT DEFAULT ''
);

-- 用户与权限（RBAC）：口令用 pbkdf2 + 随机盐存储，不存明文
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    display_name  TEXT DEFAULT '',
    role          TEXT DEFAULT 'seller',   -- admin / auditor / seller / guest
    pwd_hash      TEXT DEFAULT '',
    pwd_salt      TEXT DEFAULT '',
    seller_id     TEXT DEFAULT '',         -- 卖家角色绑定的学号（用于通知与归属校验）
    status        TEXT DEFAULT 'active',   -- active / disabled
    created_at    TEXT DEFAULT '',
    last_login_at TEXT DEFAULT ''
);

-- 操作日志：管理类动作的留痕（谁在什么时候对什么做了什么、结果如何）
CREATE TABLE IF NOT EXISTS op_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    actor       TEXT DEFAULT '',
    role        TEXT DEFAULT '',
    action      TEXT DEFAULT '',
    target_type TEXT DEFAULT '',
    target_id   TEXT DEFAULT '',
    result      TEXT DEFAULT 'ok',         -- ok / denied / failed
    detail      TEXT DEFAULT '',
    ip          TEXT DEFAULT '',
    extra_json  TEXT DEFAULT '',
    created_at  TEXT DEFAULT ''
);

-- 站内通知：审核结论、申诉结果、批量任务完成等系统消息
CREATE TABLE IF NOT EXISTS notifications (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id   TEXT DEFAULT '',             -- 接收者（用户名或学号；空串表示广播副本）
    channel   TEXT DEFAULT 'inbox',
    level     TEXT DEFAULT 'info',         -- info / success / warn / error
    title     TEXT DEFAULT '',
    content   TEXT DEFAULT '',
    ref_type  TEXT DEFAULT '',             -- item / appeal / batch_job ...
    ref_id    TEXT DEFAULT '',
    is_read   INTEGER DEFAULT 0,
    created_at TEXT DEFAULT '',
    read_at   TEXT DEFAULT ''
);

-- 批量任务：批量审核 / 改判 / 删除的执行记录与进度
CREATE TABLE IF NOT EXISTS batch_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    op          TEXT DEFAULT '',           -- audit / submit / review / delete / export
    operator    TEXT DEFAULT '',
    total       INTEGER DEFAULT 0,
    processed   INTEGER DEFAULT 0,
    success     INTEGER DEFAULT 0,
    failed      INTEGER DEFAULT 0,
    status      TEXT DEFAULT 'running',    -- running / done / failed
    params_json TEXT DEFAULT '',
    result_json TEXT DEFAULT '',
    started_at  TEXT DEFAULT '',
    finished_at TEXT DEFAULT '',
    duration_ms INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_items_status  ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_cat     ON items(category);
CREATE INDEX IF NOT EXISTS idx_items_created ON items(created_at);
CREATE INDEX IF NOT EXISTS idx_items_seller  ON items(seller_id);
CREATE INDEX IF NOT EXISTS idx_logs_item     ON audit_logs(item_id);
CREATE INDEX IF NOT EXISTS idx_appeals_item   ON appeals(item_id);
CREATE INDEX IF NOT EXISTS idx_appeals_seller ON appeals(seller_id);
CREATE INDEX IF NOT EXISTS idx_appeals_status ON appeals(status);
CREATE INDEX IF NOT EXISTS idx_rule_hits_item ON rule_hits(item_id);
CREATE INDEX IF NOT EXISTS idx_rule_hits_rule ON rule_hits(rule);
CREATE INDEX IF NOT EXISTS idx_users_role    ON users(role);
CREATE INDEX IF NOT EXISTS idx_oplog_actor   ON op_logs(actor);
CREATE INDEX IF NOT EXISTS idx_oplog_action  ON op_logs(action);
CREATE INDEX IF NOT EXISTS idx_oplog_created ON op_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_notify_user   ON notifications(user_id, is_read);
CREATE INDEX IF NOT EXISTS idx_jobs_created  ON batch_jobs(id);
"""

from .logutils import get_logger, log_swallow                      # noqa: E402

logger = get_logger("db")

# 写多读少场景下用 WAL，避免读阻塞写、写阻塞读。
# busy_timeout：Flask 与 Streamlit 两进程并发写同一库时，等锁而不是立刻抛
# "database is locked"（此前只设了 connect timeout，锁冲突会直接 500）。
# 注意：journal_mode 切换开销较大（毫秒级），只在建连接时执行一次，不能每次查询都跑。
PRAGMAS = ("PRAGMA journal_mode=WAL",
           "PRAGMA synchronous=NORMAL",
           "PRAGMA foreign_keys=ON",
           "PRAGMA busy_timeout=5000")


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    for p in PRAGMAS:
        try:
            conn.execute(p)
        except sqlite3.DatabaseError as exc:   # 只读 / 异常环境下不阻塞主流程
            log_swallow(logger, f"执行 {p}", exc)


def _new_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(config.DB_PATH), timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn)
    return conn


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# 黑名单缓存（ts 为上次加载时间戳，0 表示失效；set 与 data 同步更新，
# 避免审核链路每次调用都重建一次 set）
_BLACKLIST_CACHE: Dict[str, Any] = {"ts": 0.0, "data": [], "set": set()}


_thread_local = threading.local()


@contextmanager
def get_conn():
    """按线程复用连接。

    SQLite 连接对象非线程安全，因此用 thread-local 隔离；
    相比每次查询新建连接，省掉连接建立 + PRAGMA 设置开销（实测约 1ms/次）。
    """
    conn = getattr(_thread_local, "conn", None)
    if conn is None:
        conn = _new_conn()
        _thread_local.conn = conn
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()          # 出错时显式回滚，避免残留未提交事务
        raise


def close_conn() -> None:
    """关闭当前线程连接（测试 / 重建数据库时使用）。"""
    conn = getattr(_thread_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        finally:
            _thread_local.conn = None


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _ensure_column(conn, "items", "image_hash", "TEXT DEFAULT ''")
        _ensure_column(conn, "items", "text_fp", "TEXT DEFAULT ''")
        _ensure_column(conn, "items", "image_path", "TEXT DEFAULT ''")


def _ensure_column(conn, table: str, column: str, decl: str) -> None:
    """轻量迁移：老库缺列时补上，避免升级后插入报 no such column。"""
    try:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    except sqlite3.DatabaseError as exc:
        log_swallow(logger, f"迁移补列 {table}.{column}", exc)


# ------------------------------------------------------------------ #
# items
# ------------------------------------------------------------------ #
def insert_item(item: Dict[str, Any]) -> int:
    cols = ["title", "description", "price", "condition", "category", "sub_category",
            "seller_id", "seller_name", "contact", "has_image", "status", "decision",
            "risk_score", "risk_level", "rule_score", "llm_score", "reason",
            "suggestion", "detail_json", "source", "created_at", "image_hash", "text_fp",
            "image_path"]
    values = [item.get(c) for c in cols]
    values[cols.index("created_at")] = item.get("created_at") or now()
    sql = f"INSERT INTO items ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
    with get_conn() as conn:
        cur = conn.execute(sql, values)
        return int(cur.lastrowid)


def insert_item_with_audit(item: Dict[str, Any], hits: Optional[List[Dict[str, Any]]] = None,
                           log_detail: str = "", operator: str = "system") -> int:
    """一次审核入库 = 商品 + 规则命中明细 + 审核日志，放在同一事务里。

    此前这三次写是三个独立事务，中途失败会留下「有商品无命中明细 / 无日志」的半写状态，
    直接导致规则治理报表少数据。
    """
    cols = ["title", "description", "price", "condition", "category", "sub_category",
            "seller_id", "seller_name", "contact", "has_image", "status", "decision",
            "risk_score", "risk_level", "rule_score", "llm_score", "reason",
            "suggestion", "detail_json", "source", "created_at", "image_hash", "text_fp",
            "image_path"]
    values = [item.get(c) for c in cols]
    values[cols.index("created_at")] = item.get("created_at") or now()
    sql = f"INSERT INTO items ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"

    ts = now()
    hit_rows = [(str(h.get("rule") or ""), str(h.get("type") or ""),
                 str(h.get("severity") or ""), int(h.get("score") or 0), ts)
                for h in (hits or []) if h.get("rule")]
    with get_conn() as conn:
        item_id = int(conn.execute(sql, values).lastrowid)
        if hit_rows:
            conn.executemany(
                "INSERT INTO rule_hits (item_id, rule, type, severity, score, created_at)"
                " VALUES (?,?,?,?,?,?)",
                [(item_id,) + r for r in hit_rows])
        conn.execute(
            "INSERT INTO audit_logs (item_id, action, detail, operator, created_at)"
            " VALUES (?,?,?,?,?)",
            (item_id, "auto_audit", log_detail, operator, ts))
    return item_id


def handle_appeal_tx(appeal_id: int, status: str, handler: str, reply: str,
                     final_status: str, note: str) -> None:
    """申诉处理 = 更新申诉 + 改商品状态 + 写日志，同一事务。

    避免出现「申诉已处理但商品状态没改」的不一致。
    """
    ts = now()
    with get_conn() as conn:
        conn.execute(
            "UPDATE appeals SET status=?, reply=?, handler=?, handled_at=? WHERE id=?",
            (status, reply, handler, ts, appeal_id))
        row = conn.execute("SELECT item_id FROM appeals WHERE id=?", (appeal_id,)).fetchone()
        item_id = int(row["item_id"]) if row else 0
        if item_id:
            conn.execute(
                "UPDATE items SET status=?, reviewer=?, review_note=?, reviewed_at=? WHERE id=?",
                (final_status, handler, note, ts, item_id))
        conn.execute(
            "INSERT INTO audit_logs (item_id, action, detail, operator, created_at)"
            " VALUES (?,?,?,?,?)",
            (item_id, f"appeal_{status}",
             f"申诉处理：{'申诉成立' if status == 'accepted' else '申诉驳回'}｜{reply or '无'}",
             handler, ts))


def get_item(item_id: int) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    return dict(row) if row else None


def list_items(status: str = "", category: str = "", keyword: str = "",
               seller_id: str = "", limit: int = 200, offset: int = 0) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM items WHERE 1=1"
    args: List[Any] = []
    if status:
        sql += " AND status=?"
        args.append(status)
    if category:
        sql += " AND category=?"
        args.append(category)
    if seller_id:
        sql += " AND seller_id=?"
        args.append(seller_id)
    if keyword:
        sql += " AND (title LIKE ? OR description LIKE ? OR seller_id LIKE ?)"
        args += [f"%{keyword}%"] * 3
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    args += [limit, offset]
    with get_conn() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


# 查重指纹缓存：每次审核都全表取 500 行再逐条算汉明距离，批量审核时开销被放大 50 倍。
# 用 (总数, 最大 id) 作为版本号：插入会改变二者，删除也会改变总数，因此缓存不会读到脏数据。
_PAIRS_CACHE: Dict[str, Any] = {"version": None, "image": [], "text": []}


def _items_version() -> tuple:
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) c, COALESCE(MAX(id), 0) m FROM items").fetchone()
    return (int(row["c"]), int(row["m"]))


def image_hash_pairs(limit: int = 500) -> List[tuple]:
    """取 (id, image_hash) 列表，供感知哈希查重比对。"""
    version = _items_version()
    if _PAIRS_CACHE["version"] == version and _PAIRS_CACHE["image"]:
        return list(_PAIRS_CACHE["image"])
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, image_hash FROM items "
            "WHERE image_hash IS NOT NULL AND image_hash != '' "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    data = [(int(r["id"]), r["image_hash"]) for r in rows]
    _PAIRS_CACHE["version"] = version
    _PAIRS_CACHE["image"] = data
    return list(data)


def text_fp_pairs(limit: int = 500, exclude_id: int = 0) -> List[tuple]:
    """取 (id, text_fp, seller_id) 列表，供文案查重比对。"""
    data = _text_pairs_cached(limit)
    return [p for p in data if p[0] != exclude_id] if exclude_id else list(data)


def _text_pairs_cached(limit: int = 500) -> List[tuple]:
    """带版本号校验的指纹列表缓存（exclude 过滤放在取用侧，避免换 id 就失效）。"""
    version = _items_version()
    if _PAIRS_CACHE["version"] == version and _PAIRS_CACHE["text"]:
        return _PAIRS_CACHE["text"]
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, text_fp, seller_id FROM items "
            "WHERE text_fp IS NOT NULL AND text_fp != '' ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
    data = [(int(r["id"]), r["text_fp"] or "", r["seller_id"] or "") for r in rows]
    _PAIRS_CACHE["version"] = version
    _PAIRS_CACHE["text"] = data
    return data


def add_rule_hits(item_id: Optional[int], hits: List[Dict[str, Any]]) -> None:
    """批量写入规则命中明细（规则治理的数据源）。"""
    if not item_id or not hits:
        return
    ts = now()
    rows = [(item_id, str(h.get("rule") or ""), str(h.get("type") or ""),
             str(h.get("severity") or ""), int(h.get("score") or 0), ts)
            for h in hits if h.get("rule")]
    with get_conn() as conn:
        conn.executemany(
            "INSERT INTO rule_hits (item_id, rule, type, severity, score, created_at)"
            " VALUES (?,?,?,?,?,?)", rows)


def rule_hit_stats() -> List[Dict[str, Any]]:
    """按规则聚合命中次数与「被推翻」次数。

    被推翻的三种来源（闭环学习的反馈信号）：
    * 申诉成立（appeals.status='accepted'） -> 规则误杀
    * 人工复核改判通过（audit_logs.action='manual_pass'） -> 规则误杀
    * 用户举报（audit_logs.action='report'） -> 规则漏放
    """
    sql = """
    SELECT rh.rule AS rule,
           COUNT(*) AS hits,
           SUM(CASE WHEN rh.severity='high' THEN 1 ELSE 0 END) AS high_hits,
           SUM(CASE WHEN EXISTS (SELECT 1 FROM appeals a
                                 WHERE a.item_id = rh.item_id AND a.status='accepted')
                    THEN 1 ELSE 0 END) AS appeal_accepted,
           SUM(CASE WHEN EXISTS (SELECT 1 FROM audit_logs l
                                 WHERE l.item_id = rh.item_id AND l.action='manual_pass')
                    THEN 1 ELSE 0 END) AS manual_pass,
           SUM(CASE WHEN EXISTS (SELECT 1 FROM audit_logs l
                                 WHERE l.item_id = rh.item_id AND l.action='report')
                    THEN 1 ELSE 0 END) AS reported
    FROM rule_hits rh
    GROUP BY rh.rule
    ORDER BY hits DESC
    """
    with get_conn() as conn:
        rows = conn.execute(sql).fetchall()
    out = []
    for r in rows:
        hits = int(r["hits"] or 0)
        accepted = int(r["appeal_accepted"] or 0)
        manual = int(r["manual_pass"] or 0)
        reported = int(r["reported"] or 0)
        out.append({
            "rule": r["rule"], "hits": hits,
            "high_hits": int(r["high_hits"] or 0),
            "appeal_accepted": accepted, "manual_pass": manual, "reported": reported,
            "overturned": accepted + manual,
            "false_positive_rate": round((accepted + manual) / hits, 3) if hits else 0.0,
            "miss_rate": round(reported / hits, 3) if hits else 0.0,
        })
    return out


def update_review(item_id: int, status: str, reviewer: str = "admin",
                  note: str = "") -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE items SET status=?, reviewer=?, review_note=?, reviewed_at=? WHERE id=?",
            (status, reviewer, note, now(), item_id))


def delete_item(item_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM items WHERE id=?", (item_id,))


def count_items() -> int:
    with get_conn() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM items").fetchone()[0])


def clear_items() -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM items")
        conn.execute("DELETE FROM audit_logs")


def recent_post_count(seller_id: str, hours: int = 24) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM items WHERE seller_id=? AND created_at >= datetime('now','localtime',?)",
            (seller_id, f"-{hours} hours")).fetchone()
    return int(row[0]) if row else 0


# ------------------------------------------------------------------ #
# 统计
# ------------------------------------------------------------------ #
def stats() -> Dict[str, Any]:
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        by_status = {r["status"]: r["c"] for r in conn.execute(
            "SELECT status, COUNT(*) c FROM items GROUP BY status")}
        by_category = {r["category"]: r["c"] for r in conn.execute(
            "SELECT category, COUNT(*) c FROM items GROUP BY category")}
        avg_risk = conn.execute("SELECT AVG(risk_score) FROM items").fetchone()[0]
        by_level = {r["risk_level"]: r["c"] for r in conn.execute(
            "SELECT risk_level, COUNT(*) c FROM items GROUP BY risk_level")}
        today = conn.execute(
            "SELECT COUNT(*) FROM items WHERE date(created_at)=date('now','localtime')").fetchone()[0]
        top_rules = conn.execute(
            "SELECT reason FROM items WHERE risk_score>=? ORDER BY id DESC LIMIT 100",
            (config.REJECT_SCORE,)).fetchall()
    return {
        "total": total or 0,
        "today": today or 0,
        "by_status": by_status,
        "by_category": by_category,
        "by_level": by_level,
        "avg_risk": round(float(avg_risk or 0), 1),
        "high_risk_samples": [r["reason"] for r in top_rules],
    }


def daily_trend(days: int = 14) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT date(created_at) d, COUNT(*) total,
                      SUM(CASE WHEN status='reject' THEN 1 ELSE 0 END) rejected,
                      SUM(CASE WHEN status='review' THEN 1 ELSE 0 END) reviewed,
                      SUM(CASE WHEN status='pass' THEN 1 ELSE 0 END) passed
               FROM items GROUP BY d ORDER BY d DESC LIMIT ?""", (days,)).fetchall()
    return [dict(r) for r in rows][::-1]


# ------------------------------------------------------------------ #
# 日志 & 黑名单
# ------------------------------------------------------------------ #
def add_log(item_id: Optional[int], action: str, detail: str = "",
            operator: str = "system") -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO audit_logs (item_id, action, detail, operator, created_at) VALUES (?,?,?,?,?)",
            (item_id, action, detail, operator, now()))


def list_logs(limit: int = 50) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def _invalidate_blacklist_cache() -> None:
    _BLACKLIST_CACHE["ts"] = 0.0


def blacklist_add(user_id: str, reason: str = "") -> None:
    with get_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO blacklist (user_id, reason, created_at) VALUES (?,?,?)",
                     (user_id, reason, now()))
    _invalidate_blacklist_cache()


def blacklist_all() -> List[str]:
    """带 TTL 的名单读取：审核链路高频调用，30 秒内复用缓存。"""
    if time.time() - _BLACKLIST_CACHE["ts"] < BLACKLIST_TTL:
        return list(_BLACKLIST_CACHE["data"])
    with get_conn() as conn:
        data = [r["user_id"] for r in conn.execute("SELECT user_id FROM blacklist")]
    _BLACKLIST_CACHE.update(ts=time.time(), data=data, set=set(data))
    return list(data)


def blacklist_contains(user_id: str) -> bool:
    """审核链路专用：直接基于缓存集合做 O(1) 判断，避免每次建列表。"""
    return bool(user_id) and user_id in blacklist_set()


def blacklist_set() -> Set[str]:
    blacklist_all()                       # 刷新（命中 TTL 时直接返回缓存）
    return _BLACKLIST_CACHE["set"]


def blacklist_remove(user_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM blacklist WHERE user_id=?", (user_id,))
    _invalidate_blacklist_cache()


# ------------------------------------------------------------------ #
# 申诉
# ------------------------------------------------------------------ #
def insert_appeal(item_id: int, seller_id: str, seller_name: str = "",
                  reason: str = "") -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO appeals (item_id, seller_id, seller_name, reason, status, created_at)"
            " VALUES (?,?,?,?,'pending',?)",
            (item_id, seller_id, seller_name, reason, now()))
        return int(cur.lastrowid)


def get_appeal(appeal_id: int) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM appeals WHERE id=?", (appeal_id,)).fetchone()
    return dict(row) if row else None


def list_appeals(status: str = "", seller_id: str = "",
                 limit: int = 100) -> List[Dict[str, Any]]:
    """申诉列表。status / seller_id 为空表示不过滤。"""
    sql = "SELECT * FROM appeals WHERE 1=1"
    args: List[Any] = []
    if status:
        sql += " AND status=?"
        args.append(status)
    if seller_id:
        sql += " AND seller_id=?"
        args.append(seller_id)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def pending_appeal_of(item_id: int) -> Optional[Dict[str, Any]]:
    """某条信息是否已有未处理的申诉（防止重复申诉）。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM appeals WHERE item_id=? AND status='pending' ORDER BY id DESC LIMIT 1",
            (item_id,)).fetchone()
    return dict(row) if row else None


def update_appeal(appeal_id: int, status: str, handler: str = "", reply: str = "") -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE appeals SET status=?, handler=?, reply=?, handled_at=? WHERE id=?",
            (status, handler, reply, now(), appeal_id))


def count_appeals(status: str = "") -> int:
    sql = "SELECT COUNT(*) FROM appeals" + (" WHERE status=?" if status else "")
    with get_conn() as conn:
        row = conn.execute(sql, (status,) if status else ()).fetchone()
    return int(row[0] or 0)


def seller_stats(seller_id: str) -> Dict[str, Any]:
    """卖家视角统计：发布量、各状态数量、通过率、待处理申诉数。"""
    with get_conn() as conn:
        by_status = {r["status"]: r["c"] for r in conn.execute(
            "SELECT status, COUNT(*) c FROM items WHERE seller_id=? GROUP BY status",
            (seller_id,))}
        total = sum(by_status.values())
        appeals = conn.execute(
            "SELECT COUNT(*) FROM appeals WHERE seller_id=?", (seller_id,)).fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM appeals WHERE seller_id=? AND status='pending'",
            (seller_id,)).fetchone()[0]
    passed = by_status.get("pass", 0)
    return {
        "total": total,
        "by_status": by_status,
        "pass_rate": round(passed / total, 3) if total else 0.0,
        "appeals": int(appeals or 0),
        "pending_appeals": int(pending or 0),
    }


# ------------------------------------------------------------------ #
# 用户与权限（RBAC）
# ------------------------------------------------------------------ #
USER_COLS = ("username", "display_name", "role", "pwd_hash", "pwd_salt",
             "seller_id", "status", "created_at", "last_login_at")


def insert_user(user: Dict[str, Any]) -> int:
    cols = list(USER_COLS)
    values = [user.get(c, "") if c != "created_at" else (user.get("created_at") or now())
              for c in cols]
    sql = f"INSERT INTO users ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
    with get_conn() as conn:
        return int(conn.execute(sql, values).lastrowid)


def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    return dict(row) if row else None


def list_users(role: str = "", keyword: str = "", limit: int = 200) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM users WHERE 1=1"
    args: List[Any] = []
    if role:
        sql += " AND role=?"
        args.append(role)
    if keyword:
        sql += " AND (username LIKE ? OR display_name LIKE ? OR seller_id LIKE ?)"
        args += [f"%{keyword}%"] * 3
    sql += " ORDER BY id LIMIT ?"
    args.append(limit)
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def count_users(role: str = "") -> int:
    sql = "SELECT COUNT(*) FROM users" + (" WHERE role=?" if role else "")
    with get_conn() as conn:
        row = conn.execute(sql, (role,) if role else ()).fetchone()
    return int(row[0] or 0)


def touch_login(user_id: int) -> None:
    """更新最近登录时间（登录成功后调用）。"""
    with get_conn() as conn:
        conn.execute("UPDATE users SET last_login_at=? WHERE id=?", (now(), user_id))


def update_user_fields(user_id: int, **fields: Any) -> None:
    """白名单式更新，避免调用方拼出任意列名。"""
    allowed = {"display_name", "role", "pwd_hash", "pwd_salt", "seller_id", "status"}
    pairs = [(k, v) for k, v in fields.items() if k in allowed]
    if not pairs:
        return
    sql = "UPDATE users SET " + ",".join(f"{k}=?" for k, _ in pairs) + " WHERE id=?"
    with get_conn() as conn:
        conn.execute(sql, [v for _, v in pairs] + [user_id])


def delete_user(user_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))


# ------------------------------------------------------------------ #
# 操作日志
# ------------------------------------------------------------------ #
def add_op_log(actor: str = "", action: str = "", target_type: str = "",
               target_id: str = "", result: str = "ok", detail: str = "",
               ip: str = "", role: str = "", extra: Any = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO op_logs (actor, role, action, target_type, target_id, result,"
            " detail, ip, extra_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (actor, role, action, target_type, target_id, result, detail, ip,
             json.dumps(extra, ensure_ascii=False) if extra is not None else "", now()))
        return int(cur.lastrowid)


def list_op_logs(actor: str = "", action: str = "", result: str = "",
                 keyword: str = "", limit: int = 100,
                 offset: int = 0) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM op_logs WHERE 1=1"
    args: List[Any] = []
    if actor:
        sql += " AND actor=?"
        args.append(actor)
    if action:
        sql += " AND action=?"
        args.append(action)
    if result:
        sql += " AND result=?"
        args.append(result)
    if keyword:
        sql += " AND (action LIKE ? OR detail LIKE ? OR target_id LIKE ?)"
        args += [f"%{keyword}%"] * 3
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    args += [limit, offset]
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def count_op_logs(actor: str = "", action: str = "", result: str = "",
                  keyword: str = "") -> int:
    sql = "SELECT COUNT(*) FROM op_logs WHERE 1=1"
    args: List[Any] = []
    if actor:
        sql += " AND actor=?"
        args.append(actor)
    if action:
        sql += " AND action=?"
        args.append(action)
    if result:
        sql += " AND result=?"
        args.append(result)
    if keyword:
        sql += " AND (action LIKE ? OR detail LIKE ? OR target_id LIKE ?)"
        args += [f"%{keyword}%"] * 3
    with get_conn() as conn:
        return int(conn.execute(sql, args).fetchone()[0] or 0)


def op_log_stats(days: int = 7) -> Dict[str, Any]:
    """操作日志概览：按动作 / 操作者 / 结果聚合，便于审计与异常排查。"""
    with get_conn() as conn:
        total = int(conn.execute("SELECT COUNT(*) FROM op_logs").fetchone()[0] or 0)
        by_action = {r["action"]: r["c"] for r in conn.execute(
            "SELECT action, COUNT(*) c FROM op_logs GROUP BY action ORDER BY c DESC LIMIT 20")}
        by_actor = {r["actor"]: r["c"] for r in conn.execute(
            "SELECT actor, COUNT(*) c FROM op_logs GROUP BY actor ORDER BY c DESC LIMIT 20")}
        by_result = {r["result"]: r["c"] for r in conn.execute(
            "SELECT result, COUNT(*) c FROM op_logs GROUP BY result")}
        daily = [dict(r) for r in conn.execute(
            """SELECT date(created_at) d,
                      COUNT(*) total,
                      SUM(CASE WHEN result='ok' THEN 1 ELSE 0 END) ok,
                      SUM(CASE WHEN result='denied' THEN 1 ELSE 0 END) denied,
                      SUM(CASE WHEN result='failed' THEN 1 ELSE 0 END) failed
               FROM op_logs GROUP BY d ORDER BY d DESC LIMIT ?""", (days,))]
    return {"total": total, "by_action": by_action, "by_actor": by_actor,
            "by_result": by_result, "daily": daily[::-1]}


def purge_op_logs(retention_days: int) -> int:
    """按保留期清理过期操作日志，返回删除条数（retention_days<=0 表示不清理）。"""
    if retention_days <= 0:
        return 0
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM op_logs WHERE created_at < datetime('now','localtime',?)",
            (f"-{retention_days} days",))
    return int(cur.rowcount or 0)


# ------------------------------------------------------------------ #
# 站内通知
# ------------------------------------------------------------------ #
def insert_notification(user_id: str = "", title: str = "", content: str = "",
                        level: str = "info", channel: str = "inbox",
                        ref_type: str = "", ref_id: str = "") -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO notifications (user_id, channel, level, title, content,"
            " ref_type, ref_id, is_read, created_at) VALUES (?,?,?,?,?,?,?,0,?)",
            (user_id, channel, level, title, content, ref_type, ref_id, now()))
        return int(cur.lastrowid)


def list_notifications(user_id: str = "", unread_only: bool = False,
                       level: str = "", limit: int = 50) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM notifications WHERE 1=1"
    args: List[Any] = []
    if user_id:
        sql += " AND user_id=?"
        args.append(user_id)
    if unread_only:
        sql += " AND is_read=0"
    if level:
        sql += " AND level=?"
        args.append(level)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def count_unread(user_id: str) -> int:
    sql = "SELECT COUNT(*) FROM notifications WHERE is_read=0"
    args: List[Any] = []
    if user_id:
        sql += " AND user_id=?"
        args.append(user_id)
    with get_conn() as conn:
        return int(conn.execute(sql, args).fetchone()[0] or 0)


def mark_notification_read(notify_id: int, user_id: str = "") -> bool:
    """标记已读；传 user_id 时校验归属，避免越权标记他人消息。"""
    sql = "UPDATE notifications SET is_read=1, read_at=? WHERE id=? AND is_read=0"
    args: List[Any] = [now(), notify_id]
    if user_id:
        sql += " AND user_id=?"
        args.append(user_id)
    with get_conn() as conn:
        return int(conn.execute(sql, args).rowcount or 0) > 0


def mark_all_read(user_id: str) -> int:
    if not user_id:
        return 0
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE notifications SET is_read=1, read_at=? WHERE user_id=? AND is_read=0",
            (now(), user_id))
    return int(cur.rowcount or 0)


def purge_notifications(retention_days: int) -> int:
    """清理已读过且超过保留期的通知（未读消息永不自动清理）。"""
    if retention_days <= 0:
        return 0
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM notifications WHERE is_read=1 AND created_at < datetime('now','localtime',?)",
            (f"-{retention_days} days",))
    return int(cur.rowcount or 0)


# ------------------------------------------------------------------ #
# 批量任务
# ------------------------------------------------------------------ #
_JOB_UPDATABLE = {"processed", "success", "failed", "status", "result_json",
                  "finished_at", "duration_ms"}


def insert_job(op: str, operator: str = "", total: int = 0,
               params: Any = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO batch_jobs (op, operator, total, processed, success, failed,"
            " status, params_json, started_at) VALUES (?,?,?,0,0,0,'running',?,?)",
            (op, operator, total,
             json.dumps(params, ensure_ascii=False) if params is not None else "", now()))
        return int(cur.lastrowid)


def update_job(job_id: int, **fields: Any) -> None:
    pairs = [(k, v) for k, v in fields.items() if k in _JOB_UPDATABLE]
    if not pairs:
        return
    sql = "UPDATE batch_jobs SET " + ",".join(f"{k}=?" for k, _ in pairs) + " WHERE id=?"
    with get_conn() as conn:
        conn.execute(sql, [v for _, v in pairs] + [job_id])


def get_job(job_id: int) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM batch_jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def list_jobs(op: str = "", limit: int = 20) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM batch_jobs"
    args: List[Any] = []
    if op:
        sql += " WHERE op=?"
        args.append(op)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def job_stats() -> Dict[str, Any]:
    with get_conn() as conn:
        total = int(conn.execute("SELECT COUNT(*) FROM batch_jobs").fetchone()[0] or 0)
        by_op = {r["op"]: r["c"] for r in conn.execute(
            "SELECT op, COUNT(*) c FROM batch_jobs GROUP BY op")}
        by_status = {r["status"]: r["c"] for r in conn.execute(
            "SELECT status, COUNT(*) c FROM batch_jobs GROUP BY status")}
        processed = conn.execute("SELECT COALESCE(SUM(processed),0) FROM batch_jobs").fetchone()[0]
        failed = conn.execute("SELECT COALESCE(SUM(failed),0) FROM batch_jobs").fetchone()[0]
    return {"total": total, "by_op": by_op, "by_status": by_status,
            "processed": int(processed or 0), "failed": int(failed or 0)}


def to_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)
