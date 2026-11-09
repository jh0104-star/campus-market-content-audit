# -*- coding: utf-8 -*-
"""批量处理。

把「批量预检」升级为**有记录、有进度、可回溯**的批量任务：
每次执行都在 `batch_jobs` 表留一条任务记录（操作者 / 条目数 / 成功失败数 / 耗时 / 错误明细），
完成后给操作者推一条站内通知，并写操作日志。

任务同步执行（演示规模下足够，且避免引入线程与状态机复杂度），
但进度按块写库，任务中断也能看到已处理到哪一条。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

import config
from . import database as db
from . import dataio
from . import oplog
from .logutils import get_logger
from .pipeline import audit, submit

logger = get_logger("batch")

OPS = {
    "audit": "批量预检（只审核不入库）",
    "submit": "批量入库（审核并保存）",
    "review": "批量改判",
    "delete": "批量删除",
    "export": "批量导出",
}
# 每处理 PROGRESS_CHUNK 条回写一次进度，避免频繁写库
PROGRESS_CHUNK = 10
VALID_STATUS = ("pass", "review", "reject")


def _load(job_id: int) -> Optional[Dict[str, Any]]:
    job = db.get_job(job_id)
    if not job:
        return None
    try:
        job["result"] = json.loads(job.get("result_json") or "{}")
    except (TypeError, ValueError):
        job["result"] = {}
    return job


def get_job(job_id: int) -> Optional[Dict[str, Any]]:
    return _load(job_id)


def list_jobs(op: str = "", limit: int = 20) -> List[Dict[str, Any]]:
    jobs = db.list_jobs(op=op, limit=max(1, min(int(limit or 20), 200)))
    for j in jobs:
        try:
            j["result"] = json.loads(j.get("result_json") or "{}")
        except (TypeError, ValueError):
            j["result"] = {}
    return jobs


def stats() -> Dict[str, Any]:
    return db.job_stats()


def _items_of(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        items = payload.get("items")
        if isinstance(items, list):
            return [x for x in items if isinstance(x, dict)]
    return []


def _ids_of(payload: Any) -> List[int]:
    raw = payload.get("ids", []) if isinstance(payload, dict) else payload or []
    ids: List[int] = []
    for v in raw:
        try:
            ids.append(int(v))
        except (TypeError, ValueError):
            continue
    return ids


# ------------------------------------------------------------------ #
# 各操作实现
# ------------------------------------------------------------------ #
def _run_audit_or_submit(job_id: int, op: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    counts = {"pass": 0, "review": 0, "reject": 0}
    rows: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    for i, raw in enumerate(items):
        item = dataio.normalize_row(raw)
        if dataio.validate_row(item):
            errors.append({"index": i, "error": dataio.validate_row(item)})
            continue
        try:
            res = audit(item)
            counts[res.decision] = counts.get(res.decision, 0) + 1
            item_id = None
            if op == "submit":
                item_id = submit(item, source="batch")["id"]
            rows.append({"index": i, "id": item_id, "title": item["title"][:40],
                         "decision": res.decision, "risk_score": res.risk_score,
                         "category": res.category_name})
        except Exception as exc:                       # 单条失败不影响整批
            logger.warning("批量任务 #%s 第 %s 条失败：%s", job_id, i, exc)
            errors.append({"index": i, "error": f"{type(exc).__name__}: {exc}"})
        if (i + 1) % PROGRESS_CHUNK == 0:
            db.update_job(job_id, processed=i + 1, success=len(rows), failed=len(errors))
    return {"counts": counts, "errors": errors, "rows": rows[:200]}


def _run_review(job_id: int, ids: List[int], status: str, reviewer: str,
                note: str) -> Dict[str, Any]:
    ok, failed, errors = 0, 0, []
    for i, item_id in enumerate(ids):
        try:
            item = db.get_item(item_id)
            if not item:
                errors.append({"index": i, "id": item_id, "error": "商品不存在"})
                failed += 1
                continue
            db.update_review(item_id, status, reviewer=reviewer, note=note)
            db.add_log(item_id, f"manual_{status}",
                       f"批量改判为{status}｜{note}", reviewer or "system")
            ok += 1
        except Exception as exc:
            logger.warning("批量改判 #%s 第 %s 条失败：%s", job_id, item_id, exc)
            errors.append({"index": i, "id": item_id, "error": f"{type(exc).__name__}: {exc}"})
            failed += 1
        if (i + 1) % PROGRESS_CHUNK == 0:
            db.update_job(job_id, processed=i + 1, success=ok, failed=failed)
    return {"status": status, "errors": errors}


def _run_delete(job_id: int, ids: List[int], operator: str) -> Dict[str, Any]:
    ok, failed, errors = 0, 0, []
    for i, item_id in enumerate(ids):
        try:
            item = db.get_item(item_id)
            if not item:
                errors.append({"index": i, "id": item_id, "error": "商品不存在"})
                failed += 1
                continue
            db.delete_item(item_id)
            db.add_log(item_id, "delete", "批量删除", operator or "system")
            ok += 1
        except Exception as exc:
            logger.warning("批量删除 #%s 第 %s 条失败：%s", job_id, item_id, exc)
            errors.append({"index": i, "id": item_id, "error": f"{type(exc).__name__}: {exc}"})
            failed += 1
        if (i + 1) % PROGRESS_CHUNK == 0:
            db.update_job(job_id, processed=i + 1, success=ok, failed=failed)
    return {"errors": errors}


# ------------------------------------------------------------------ #
# 任务入口
# ------------------------------------------------------------------ #
def run_job(op: str, payload: Any = None, operator: str = "system",
            role: str = "") -> Dict[str, Any]:
    """执行一个批量任务并留痕。成功与否都会写任务记录、操作日志与通知。"""
    op = (op or "").lower()
    if op not in OPS:
        raise ValueError(f"不支持的批量操作：{op}（可选 {list(OPS)}）")

    payload = payload or {}
    t0 = time.time()
    result: Dict[str, Any] = {}
    total = 0

    try:
        if op in ("audit", "submit"):
            items = _items_of(payload)
            if not items:
                raise ValueError("items 不能为空")
            if len(items) > config.BATCH_MAX_ITEMS:
                raise ValueError(f"单个任务最多 {config.BATCH_MAX_ITEMS} 条，当前 {len(items)} 条")
            total = len(items)
        elif op == "review":
            status = str(payload.get("status") or "")
            if status not in VALID_STATUS:
                raise ValueError(f"改判状态非法：{status}（可选 {VALID_STATUS}）")
            total = len(_ids_of(payload))
            if not total:
                raise ValueError("ids 不能为空")
        elif op == "delete":
            total = len(_ids_of(payload))
            if not total:
                raise ValueError("ids 不能为空")
        else:                                          # export
            total = 1

        job_id = db.insert_job(op, operator=operator or "system", total=total, params=payload)
    except ValueError as exc:
        oplog.record(operator, "batch.run", target_type="batch_job", result="failed",
                     detail=f"{op} 参数校验失败：{exc}", role=role)
        raise

    status = "done"
    try:
        if op in ("audit", "submit"):
            result = _run_audit_or_submit(job_id, op, _items_of(payload))
            ok = sum(result["counts"].values())
            failed = len(result["errors"])
        elif op == "review":
            result = _run_review(job_id, _ids_of(payload), payload.get("status"),
                                 operator, str(payload.get("note") or "")[:200])
            ok = total - len(result["errors"])
            failed = len(result["errors"])
        elif op == "delete":
            result = _run_delete(job_id, _ids_of(payload), operator)
            ok = total - len(result["errors"])
            failed = len(result["errors"])
        else:
            export = dataio.export_items(
                fmt=str(payload.get("fmt") or "csv"),
                status=str(payload.get("status") or ""),
                category=str(payload.get("category") or ""),
                keyword=str(payload.get("keyword") or ""),
                limit=int(payload.get("limit") or 500),
                mask=bool(payload.get("mask", True)))
            result = {"filename": export["filename"], "count": export["count"],
                      "fmt": export["fmt"]}
            ok, failed = 1, 0
    except Exception as exc:                           # 任务级失败：记录为 failed 并继续留痕
        status = "failed"
        ok, failed = 0, total
        result = {"error": f"{type(exc).__name__}: {exc}"}
        logger.warning("批量任务 #%s 执行失败：%s", job_id, exc)

    duration_ms = int((time.time() - t0) * 1000)
    db.update_job(job_id, processed=total, success=ok, failed=failed, status=status,
                  result_json=db.to_json(result), finished_at=db.now(),
                  duration_ms=duration_ms)
    job = _load(job_id) or {}
    oplog.record(operator, "batch.run", target_type="batch_job", target_id=job_id,
                 result="ok" if status == "done" else "failed",
                 detail=f"{OPS[op]}：共 {total} 条，成功 {ok} 条，失败 {failed} 条，"
                        f"耗时 {duration_ms} ms", role=role)
    try:
        from . import notify
        notify.notify_batch_done(job)
    except Exception as exc:                           # 通知失败不影响任务结果
        logger.warning("批量任务通知失败：%s", exc)
    return job
