# -*- coding: utf-8 -*-
"""消息通知系统。

站内信（inbox）通道：审核结论、申诉结果、人工改判、批量任务完成等事件
都会给相关用户推一条消息；管理员可按角色广播。

设计上刻意保持「通道无关」：写入统一落 `notifications` 表，
后续要接邮件 / 企业微信 / 短信时，只需在 `send()` 里追加一个 channel 分支，
业务侧调用不变。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import config
from . import database as db
from .logutils import get_logger

logger = get_logger("notify")

LEVELS = ("info", "success", "warn", "error")
LEVEL_META = {
    "info": ("通知", "info"),
    "success": ("通过", "ok"),
    "warn": ("提醒", "warn"),
    "error": ("拦截", "bad"),
}

DECISION_LABEL = {
    "pass": "已通过审核，正常展示",
    "review": "需人工复核，请耐心等待",
    "reject": "未通过审核，已被拦截",
}


def _resolve(user_id: str) -> str:
    """把学号映射成站内账号：便于卖家用学号登录时也能收到消息。"""
    if not user_id:
        return ""
    for u in db.list_users(keyword=user_id, limit=20):
        if u.get("seller_id") == user_id:
            return u.get("username") or user_id
    return user_id


def notify(user_id: str = "", title: str = "", content: str = "", level: str = "info",
           channel: str = "inbox", ref_type: str = "", ref_id: Any = "") -> int:
    """发一条站内通知；失败只告警，不阻断业务流程。"""
    if level not in LEVELS:
        level = "info"
    try:
        return db.insert_notification(user_id=str(user_id or ""), title=str(title)[:100],
                                      content=str(content)[:500], level=level,
                                      channel=channel, ref_type=ref_type,
                                      ref_id=str(ref_id or ""))
    except Exception as exc:
        logger.warning("通知写入失败（user=%s）：%s: %s", user_id, type(exc).__name__, exc)
        return 0


def notify_audit_result(item_id: Any, seller_id: str, decision: str,
                        risk_score: int = 0, reason: str = "") -> int:
    """发布审核结论通知（三档结论各有对应文案）。"""
    level = {"pass": "success", "review": "warn", "reject": "error"}.get(decision, "info")
    return notify(_resolve(seller_id),
                  f"发布审核结果：{DECISION_LABEL.get(decision, decision)}",
                  f"信息 #{item_id} 风险分 {risk_score}。{reason or '可在卖家中心查看完整依据。'}",
                  level=level, ref_type="item", ref_id=item_id)


def notify_review_result(item_id: Any, seller_id: str, status: str, note: str = "") -> int:
    level = {"pass": "success", "review": "warn", "reject": "error"}.get(status, "info")
    return notify(_resolve(seller_id),
                  f"人工复核结果：{DECISION_LABEL.get(status, status)}",
                  f"信息 #{item_id} 已由审核人员处理。{note or ''}",
                  level=level, ref_type="item", ref_id=item_id)


def notify_appeal_result(appeal_id: Any, seller_id: str, status: str, reply: str = "") -> int:
    accepted = status == "accepted"
    return notify(_resolve(seller_id),
                  "申诉结果：" + ("申诉成立，已恢复发布" if accepted else "申诉驳回，维持原判"),
                  f"申诉 #{appeal_id}：" + (reply or ("已恢复展示，感谢反馈。" if accepted
                                                   else "经复核维持原判定，可修改后重新发布。")),
                  level="success" if accepted else "warn",
                  ref_type="appeal", ref_id=appeal_id)


def notify_batch_done(job: Dict[str, Any]) -> int:
    op_name = {"audit": "批量预检", "submit": "批量入库", "review": "批量改判",
               "delete": "批量删除", "export": "批量导出"}.get(job.get("op", ""),
                                                        job.get("op", "批量任务"))
    ok = job.get("success", 0)
    failed = job.get("failed", 0)
    return notify(str(job.get("operator") or ""),
                  f"{op_name}已完成（#{job.get('id')}）",
                  f"共 {job.get('total', 0)} 条，成功 {ok} 条，失败 {failed} 条，"
                  f"耗时 {job.get('duration_ms', 0)} ms。",
                  level="error" if failed and not ok else "success",
                  ref_type="batch_job", ref_id=job.get("id"))


def broadcast(title: str, content: str, level: str = "info", role: Optional[str] = None,
              ref_type: str = "", ref_id: Any = "") -> int:
    """管理员广播：按角色发送（role 为空表示全体）。返回发送条数。"""
    users = db.list_users(role=role or "", limit=1000) if role else db.list_users(limit=1000)
    sent = 0
    for u in users:
        if u.get("status") != "active":
            continue
        if notify(u.get("username", ""), title, content, level=level,
                  ref_type=ref_type, ref_id=ref_id):
            sent += 1
    return sent


def inbox(user_id: str = "", unread_only: bool = False, level: str = "",
          limit: int = 50) -> List[Dict[str, Any]]:
    rows = db.list_notifications(user_id=str(user_id or ""), unread_only=unread_only,
                                 level=level, limit=max(1, min(int(limit or 50), 200)))
    for r in rows:
        r["level_name"], r["level_class"] = LEVEL_META.get(r.get("level", "info"),
                                                           ("通知", "info"))
    return rows


def summary(user_id: str = "") -> Dict[str, Any]:
    """消息概览：未读数 + 分级未读数，供页面角标使用。"""
    unread = db.list_notifications(user_id=str(user_id or ""), unread_only=True, limit=200)
    by_level: Dict[str, int] = {}
    for r in unread:
        by_level[r.get("level", "info")] = by_level.get(r.get("level", "info"), 0) + 1
    return {"user_id": str(user_id or ""), "unread": len(unread),
            "by_level": by_level,
            "total": len(db.list_notifications(user_id=str(user_id or ""), limit=500))}


def mark_read(notify_id: int, user_id: str = "") -> bool:
    """标记已读。user_id 非空时校验归属（防止标记他人消息）。"""
    return db.mark_notification_read(int(notify_id), str(user_id or ""))


def mark_all_read(user_id: str) -> int:
    return db.mark_all_read(str(user_id or ""))


def purge(retention_days: Optional[int] = None) -> int:
    """清理已读且超过保留期的通知（未读消息永不自动清理）。"""
    days = config.NOTIFY_RETENTION_DAYS if retention_days is None else int(retention_days)
    return db.purge_notifications(days)
