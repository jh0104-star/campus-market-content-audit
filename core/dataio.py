# -*- coding: utf-8 -*-
"""数据导出与导入。

* 导出：CSV（UTF-8 BOM，Excel 直开）/ JSON，支持按状态、品类、关键词过滤，
  默认对描述与理由二次脱敏（历史数据可能是脱敏上线前写入的）；
* 导入：CSV / JSON 自动识别，中英文表头都能映射；
  三种模式 `audit`（只审核不入库）/ `submit`（审核并入库）/ `restore`（原样恢复备份），
  支持 `dry_run` 先跑一遍看影响面，再真正落库。
"""
from __future__ import annotations

import csv
import io
import json
from typing import Any, Dict, List, Optional, Tuple

import config
from . import database as db
from .lexicon import CATEGORY_MAP
from .logutils import get_logger
from .pipeline import audit, submit
from .privacy import mask_text

logger = get_logger("dataio")

# 导出列：(字段, 表头)。导入时按同一批表头做反向映射
EXPORT_FIELDS: List[Tuple[str, str]] = [
    ("id", "ID"), ("title", "标题"), ("description", "描述"), ("price", "价格"),
    ("condition", "成色"), ("category", "分类"), ("sub_category", "子类"),
    ("seller_id", "卖家学号"), ("seller_name", "卖家"), ("status", "审核结论"),
    ("risk_score", "风险分"), ("rule_score", "规则分"), ("llm_score", "模型分"),
    ("reason", "判定理由"), ("suggestion", "整改建议"), ("source", "来源"),
    ("created_at", "发布时间"),
]

STATUS_LABEL = {"pass": "自动通过", "review": "转人工复核", "reject": "拒绝发布",
                "pending": "待审核"}

# 导入表头别名（中英文混排都能识别）
FIELD_ALIASES = {
    "id": "id", "ID": "id",
    "title": "title", "标题": "title", "商品名称": "title", "名称": "title", "name": "title",
    "description": "description", "描述": "description", "正文": "description",
    "详情": "description", "desc": "description",
    "price": "price", "价格": "price", "售价": "price", "金额": "price",
    "condition": "condition", "成色": "condition", "新旧": "condition",
    "category": "category", "分类": "category",
    "sub_category": "sub_category", "子类": "sub_category", "二级分类": "sub_category",
    "seller_id": "seller_id", "学号": "seller_id", "卖家学号": "seller_id",
    "seller_name": "seller_name", "卖家": "seller_name", "姓名": "seller_name",
    "contact": "contact", "联系方式": "contact", "电话": "contact",
    "status": "status", "审核结论": "status", "状态": "status",
    "risk_score": "risk_score", "风险分": "risk_score",
    "reason": "reason", "判定理由": "reason",
    "suggestion": "suggestion", "整改建议": "suggestion",
    "source": "source", "来源": "source",
    "created_at": "created_at", "发布时间": "created_at", "时间": "created_at",
}

IMPORT_MODES = ("audit", "submit", "restore")

MAX_TITLE_LEN = 200
MAX_DESC_LEN = 2000


def _cat_name(key: str) -> str:
    return (CATEGORY_MAP.get(key, {}) or {}).get("name", key or "")


def _status_label(status: str) -> str:
    return STATUS_LABEL.get(status, status or "")


# ------------------------------------------------------------------ #
# 导出
# ------------------------------------------------------------------ #
def _export_row(it: Dict[str, Any], mask: bool) -> Dict[str, Any]:
    desc = it.get("description", "") or ""
    reason = it.get("reason", "") or ""
    return {
        "id": it.get("id"),
        "title": it.get("title", ""),
        "description": mask_text(desc) if mask else desc,
        "price": it.get("price"),
        "condition": it.get("condition", ""),
        "category": _cat_name(it.get("category", "")),
        "sub_category": it.get("sub_category", ""),
        "seller_id": it.get("seller_id", ""),
        "seller_name": it.get("seller_name", ""),
        "status": _status_label(it.get("status", "")),
        "risk_score": it.get("risk_score", 0),
        "rule_score": it.get("rule_score", 0),
        "llm_score": it.get("llm_score", 0),
        "reason": mask_text(reason) if mask else reason,
        "suggestion": it.get("suggestion", ""),
        "source": it.get("source", ""),
        "created_at": it.get("created_at", ""),
    }


def export_items(fmt: str = "csv", status: str = "", category: str = "",
                 keyword: str = "", limit: int = 500, mask: bool = True,
                 fields: Optional[List[str]] = None) -> Dict[str, Any]:
    """导出审核明细，返回 {fmt, count, filename, mime, content}。

    fields 可指定导出列（字段名），缺省导出全部列。
    """
    fmt = (fmt or "csv").lower()
    if fmt not in ("csv", "json"):
        raise ValueError("导出格式只支持 csv / json")
    limit = max(1, min(int(limit or 500), config.IMPORT_MAX_ROWS))
    items = db.list_items(status=status or "", category=category or "",
                          keyword=keyword or "", limit=limit)
    cols = [c for c in EXPORT_FIELDS if not fields or c[0] in fields] or EXPORT_FIELDS
    rows = [_export_row(it, mask) for it in items]

    stamp = db.now().replace(" ", "_").replace(":", "")
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([h for _, h in cols])
        for r in rows:
            writer.writerow([r.get(k, "") for k, _ in cols])
        return {"fmt": "csv", "count": len(rows), "filename": f"audit_export_{stamp}.csv",
                "mime": "text/csv; charset=utf-8",
                "content": "\ufeff" + buf.getvalue()}      # BOM：Excel 直接打开不乱码
    body = json.dumps({"exported_at": db.now(), "count": len(rows),
                       "fields": [k for k, _ in cols], "items": rows},
                      ensure_ascii=False, indent=2)
    return {"fmt": "json", "count": len(rows), "filename": f"audit_export_{stamp}.json",
            "mime": "application/json; charset=utf-8", "content": body}


def template_csv() -> str:
    """导入模板：只含表头与一行示例，降低用户填错列的概率。"""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["标题", "描述", "价格", "成色", "卖家学号", "卖家"])
    writer.writerow(["高等数学同济第七版", "大一教材八成新，无缺页", "15", "八成新",
                     "20230101", "张三"])
    return "\ufeff" + buf.getvalue()


# ------------------------------------------------------------------ #
# 导入：解析
# ------------------------------------------------------------------ #
def normalize_row(raw: Dict[str, Any]) -> Dict[str, Any]:
    """把任意表头的行映射成内部字段；无法识别的列直接忽略。"""
    out: Dict[str, Any] = {}
    for key, value in (raw or {}).items():
        field = FIELD_ALIASES.get(str(key).strip()) or \
            FIELD_ALIASES.get(str(key).strip().lower())
        if not field or field == "id":        # id 不参与导入，避免覆盖既有记录
            continue
        if isinstance(value, str):
            value = value.strip()
        if field == "price":
            out[field] = _to_float(value)
        else:
            out[field] = value
    out["title"] = str(out.get("title", ""))[:MAX_TITLE_LEN]
    out["description"] = str(out.get("description", ""))[:MAX_DESC_LEN]
    return out


def _to_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        p = float(str(value).replace("¥", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    if p != p or p < 0:                       # NaN / 负数按价格缺失处理
        return None
    return min(p, 10_000_000)


def parse_table(text: str, fmt: str = "auto") -> Tuple[List[Dict[str, Any]], List[str]]:
    """解析 CSV / JSON 文本，返回 (行列表, 错误信息)。

    fmt=auto 时按首字符判断：`{` / `[` 视为 JSON，否则按 CSV 解析。
    """
    if not (text or "").strip():
        return [], ["内容为空"]
    fmt = (fmt or "auto").lower()
    if fmt == "auto":
        fmt = "json" if text.lstrip()[:1] in ("{", "[") else "csv"
    try:
        if fmt == "json":
            data = json.loads(text)
            rows = data.get("items", []) if isinstance(data, dict) else data
            if not isinstance(rows, list):
                return [], ["JSON 顶层必须是数组，或含 items 数组的对象"]
            return [r for r in rows if isinstance(r, dict)], []
        reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
        if not reader.fieldnames:
            return [], ["CSV 缺少表头"]
        return [dict(r) for r in reader], []
    except Exception as exc:                                   # 解析失败不影响服务
        logger.warning("导入内容解析失败（%s）：%s", fmt, exc)
        return [], [f"{fmt} 解析失败：{type(exc).__name__}: {exc}"]


def validate_row(row: Dict[str, Any]) -> Optional[str]:
    """行级校验：标题为空是唯一硬错误，其余字段缺失按缺省处理。"""
    if not str(row.get("title", "")).strip() and not str(row.get("description", "")).strip():
        return "标题与描述不能同时为空"
    return None


# ------------------------------------------------------------------ #
# 导入：执行
# ------------------------------------------------------------------ #
def import_items(rows: List[Dict[str, Any]], mode: str = "submit", auto_save: bool = True,
                 source: str = "import", operator: str = "",
                 dry_run: bool = False) -> Dict[str, Any]:
    """批量导入。

    mode      : audit 只审核 / submit 审核并入库 / restore 原样恢复（不重新审核）
    dry_run   : True 时只返回预览结果，不写库
    返回报告   : {total, ok, failed, counts, errors, results, dry_run, mode}
    """
    if mode not in IMPORT_MODES:
        raise ValueError(f"导入模式非法：{mode}（可选 {IMPORT_MODES}）")
    if len(rows) > config.IMPORT_MAX_ROWS:
        raise ValueError(f"单次导入最多 {config.IMPORT_MAX_ROWS} 行，当前 {len(rows)} 行")

    save = auto_save and not dry_run
    counts = {"pass": 0, "review": 0, "reject": 0}
    results: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    for idx, raw in enumerate(rows):
        row = normalize_row(raw)
        err = validate_row(row)
        if err:
            errors.append({"index": idx, "title": str(row.get("title", ""))[:40],
                           "error": err})
            continue
        try:
            if mode == "restore":                      # 备份恢复：保留原审核结论
                row.setdefault("status", row.get("status") or "pending")
                if save:
                    item_id = db.insert_item_with_audit(
                        row, log_detail=f"数据导入恢复（{source}）", operator=operator or "system")
                else:
                    item_id = None
                results.append({"index": idx, "id": item_id, "title": row["title"],
                                "decision": row.get("status", "pending"),
                                "risk_score": row.get("risk_score", 0),
                                "category": _cat_name(row.get("category", ""))})
                counts[row.get("status", "pending")] = counts.get(row.get("status", "pending"), 0) + 1
                continue

            res = audit(row)
            decision = res.decision
            counts[decision] = counts.get(decision, 0) + 1
            item_id = None
            if mode == "submit" and save:
                item_id = submit(row, source=source)["id"]
            results.append({"index": idx, "id": item_id, "title": row["title"][:40],
                            "decision": decision, "decision_label": res.decision_label,
                            "risk_score": res.risk_score,
                            "category": res.category_name,
                            "top_rule": res.rule.hits[0].rule if res.rule and res.rule.hits else ""})
        except Exception as exc:                       # 单行失败不中断整批
            logger.warning("导入第 %s 行失败：%s: %s", idx, type(exc).__name__, exc)
            errors.append({"index": idx, "title": str(row.get("title", ""))[:40],
                           "error": f"{type(exc).__name__}: {exc}"})

    return {
        "mode": mode, "dry_run": bool(dry_run), "total": len(rows),
        "ok": len(results), "failed": len(errors),
        "counts": counts, "errors": errors,
        "results": results[:200],                      # 预览只回传前 200 条，避免响应过大
        "operator": operator, "source": source,
    }
