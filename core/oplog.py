# -*- coding: utf-8 -*-
"""操作日志记录。

与 `audit_logs`（审核流水：机器审核 / 申诉 / 举报等业务事件）区分开，
这里记录的是**人的管理动作**：登录、改判、删数据、加黑名单、导入导出、批量任务等，
字段固定包含 操作者 / 角色 / 动作 / 对象 / 结果 / IP / 附加信息，便于事后审计与导出。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import config
from . import database as db
from .logutils import get_logger

logger = get_logger("oplog")

# 动作清单：前端展示用，也作为「未知动作」的校验依据
ACTIONS = {
    "login": "登录",
    "logout": "退出登录",
    "item.review": "人工复核改判",
    "item.delete": "删除信息",
    "appeal.handle": "处理申诉",
    "blacklist.add": "加入黑名单",
    "blacklist.remove": "移出黑名单",
    "engine.switch": "切换审核引擎",
    "rule.replay": "规则重放",
    "data.export": "导出数据",
    "data.import": "导入数据",
    "batch.run": "执行批量任务",
    "user.create": "创建用户",
    "user.role": "调整角色",
    "user.status": "启停用户",
    "user.password": "重置口令",
    "user.delete": "删除用户",
    "notify.broadcast": "发布系统通知",
    "oplog.purge": "清理操作日志",
}

RESULTS = {"ok": "成功", "denied": "被拒绝", "failed": "失败"}


def action_name(action: str) -> str:
    return ACTIONS.get(action, action or "未命名动作")


def record(actor: str = "", action: str = "", target_type: str = "",
           target_id: Any = "", result: str = "ok", detail: str = "",
           ip: Optional[str] = "", role: str = "", extra: Any = None) -> int:
    """写一条操作日志；写失败只记录 warning，绝不因为日志影响主流程。"""
    try:
        return db.add_op_log(actor=str(actor or "anonymous"), action=str(action or ""),
                             target_type=str(target_type or ""),
                             target_id=str(target_id or "")[:64],
                             result=result if result in RESULTS else "ok",
                             detail=str(detail or "")[:500], ip=str(ip or ""),
                             role=str(role or ""), extra=extra)
    except Exception as exc:
        logger.warning("操作日志写入失败（action=%s）：%s: %s", action, type(exc).__name__, exc)
        return 0


def query(actor: str = "", action: str = "", result: str = "", keyword: str = "",
          limit: int = 100, offset: int = 0) -> Dict[str, Any]:
    """按条件检索操作日志，返回总数与当页数据（便于前端分页）。"""
    limit = max(1, min(int(limit or 100), 500))
    offset = max(0, int(offset or 0))
    logs = db.list_op_logs(actor=actor, action=action, result=result,
                           keyword=keyword, limit=limit, offset=offset)
    for lg in logs:
        lg["action_name"] = action_name(lg.get("action", ""))
        lg["result_name"] = RESULTS.get(lg.get("result", ""), lg.get("result", ""))
    return {"total": db.count_op_logs(actor=actor, action=action, result=result,
                                      keyword=keyword),
            "limit": limit, "offset": offset, "logs": logs}


def stats(days: int = 7) -> Dict[str, Any]:
    return db.op_log_stats(days=max(1, min(int(days or 7), 90)))


def purge(retention_days: Optional[int] = None) -> int:
    """按保留期清理过期日志。retention_days 缺省取配置值，<=0 表示不清理。"""
    days = config.OPLOG_RETENTION_DAYS if retention_days is None else int(retention_days)
    removed = db.purge_op_logs(days)
    if removed:
        record("system", "oplog.purge", result="ok",
               detail=f"清理 {days} 天前操作日志，共 {removed} 条")
    return removed


def target_history(target_type: str, target_id: Any, limit: int = 50) -> List[Dict[str, Any]]:
    """某对象的操作轨迹（例如「这条信息被谁改判过」）。"""
    logs = db.list_op_logs(keyword=str(target_id or ""), limit=max(1, min(int(limit), 200)))
    return [lg for lg in logs
            if (not target_type or lg.get("target_type") == target_type)
            and str(lg.get("target_id")) == str(target_id)]
