# -*- coding: utf-8 -*-
"""规则治理与闭环学习。

解决的问题：规则词表一旦上线，"哪条规则在误杀、哪条在漏放"只能靠人拍脑袋。
本模块把三类真实反馈汇总到每条规则上，给出可执行的调参建议：

| 反馈来源 | 含义 | 统计字段 |
|---|---|---|
| 卖家申诉成立 (`appeals.status='accepted'`) | 规则误杀 | `appeal_accepted` |
| 人工复核改判通过 (`audit_logs.action='manual_pass'`) | 规则误杀 | `manual_pass` |
| 用户举报 (`audit_logs.action='report'`) | 规则漏放 | `reported` |

同时提供 `replay_history()`：词表升级后用当前规则重跑历史数据，
输出结论变化的条目与方向（变严 / 变松），让规则迭代的影响面可度量。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from . import database as db
from .lexicon import LEXICON_VERSION
from .pipeline import audit

# 误杀率超过该阈值即视为"疑似过严"，建议降权或补豁免词
STRICT_RATE = 0.3
# 至少命中这么多次才给建议，避免小样本噪音
MIN_HITS_FOR_ADVICE = 3


def rule_report() -> Dict[str, Any]:
    """规则治理报表：命中分布 + 误杀/漏放反馈 + 调参建议。"""
    rows = db.rule_hit_stats()
    for r in rows:
        r["advice"] = _advice(r)
        r["level"] = _level(r)

    total_hits = sum(r["hits"] for r in rows)
    overturned = sum(r["overturned"] for r in rows)
    reported = sum(r["reported"] for r in rows)
    appeals = db.count_appeals()
    accepted = db.count_appeals("accepted")

    return {
        "lexicon_version": LEXICON_VERSION,
        "rules": rows,
        "summary": {
            "rule_count": len(rows),
            "total_hits": total_hits,
            "overturned": overturned,
            "reported": reported,
            "false_positive_rate": round(overturned / total_hits, 3) if total_hits else 0.0,
            "appeals": appeals,
            "appeal_accepted": accepted,
            "appeal_accept_rate": round(accepted / appeals, 3) if appeals else 0.0,
        },
        "suggestions": [r["advice"] for r in rows if r["advice"]][:6],
    }


def _level(r: Dict[str, Any]) -> str:
    """规则健康度：ok / warn / danger。"""
    if r["hits"] < MIN_HITS_FOR_ADVICE:
        return "ok"
    if r["false_positive_rate"] >= STRICT_RATE:
        return "danger"
    if r["overturned"] or r["reported"] >= 2:
        return "warn"
    return "ok"


def _advice(r: Dict[str, Any]) -> str:
    """按反馈生成调参建议（人工可直接照做）。"""
    if r["hits"] < MIN_HITS_FOR_ADVICE:
        return ""
    name, rate = r["rule"], r["false_positive_rate"]
    if rate >= STRICT_RATE:
        return (f"「{name}」误杀率 {rate:.0%}（{r['overturned']}/{r['hits']} 条被推翻），"
                f"疑似过严：建议下调分值或补充豁免词")
    if r["overturned"]:
        return f"「{name}」已被推翻 {r['overturned']} 次，建议复核阈值"
    if r["reported"] >= 2:
        return f"「{name}」相关条目被举报 {r['reported']} 次，疑似漏放：建议上调分值或补充同义说法"
    return ""


def backfill_rule_hits(limit: int = 2000) -> int:
    """回填历史条目的规则命中明细（幂等）。

    规则治理是后加的能力，此前的审核记录只把结论存进了 detail_json。
    这里从 detail_json 反解出命中规则，让老数据也能参与治理统计。
    """
    items = db.list_items(limit=limit)
    if not items:
        return 0
    with db.get_conn() as conn:
        done = {int(r["item_id"]) for r in
                conn.execute("SELECT DISTINCT item_id FROM rule_hits").fetchall()}
    n = 0
    for it in items:
        if it.get("id") in done or not it.get("detail_json"):
            continue
        try:
            detail = json.loads(it["detail_json"])
        except (TypeError, ValueError):
            continue
        hits = (detail.get("rule") or {}).get("hits") or []
        if hits:
            db.add_rule_hits(it["id"], hits)
            n += len(hits)
    return n


def replay_history(limit: int = 200) -> Dict[str, Any]:
    """用当前词表重跑历史数据，量化"规则升级的影响面"。

    只重放不写库，避免污染真实审核结论。
    """
    items = db.list_items(limit=limit)
    changes: List[Dict[str, Any]] = []
    stricter = looser = 0

    for old in items:
        item = {
            "id": old.get("id"),
            "title": old.get("title", ""),
            "description": old.get("description", ""),
            "price": old.get("price"),
            "condition": old.get("condition", ""),
            "seller_id": old.get("seller_id", "") or "",
            "contact": "",                      # 库中联系方式已脱敏，不应再参与判定
            "has_image": old.get("has_image", 0),
            "image_hash": old.get("image_hash", "") or "",
            # 不传 text_fp：重放自身不应被自己的指纹命中
        }
        res = audit(item)
        old_status = old.get("status", "")
        delta = res.risk_score - int(old.get("risk_score") or 0)
        if res.decision == old_status and abs(delta) < 5:
            continue
        rank = {"pass": 0, "review": 1, "reject": 2}
        direction = ("变严" if rank.get(res.decision, 0) > rank.get(old_status, 0)
                     else "变松" if rank.get(res.decision, 0) < rank.get(old_status, 0)
                     else "分数变化")
        if direction == "变严":
            stricter += 1
        elif direction == "变松":
            looser += 1
        changes.append({
            "id": old.get("id"),
            "title": (old.get("title") or "")[:40],
            "old_status": old_status, "new_status": res.decision,
            "old_score": old.get("risk_score"), "new_score": res.risk_score,
            "delta": delta, "direction": direction,
        })

    return {
        "lexicon_version": LEXICON_VERSION,
        "total": len(items),
        "changed": len(changes),
        "unchanged": len(items) - len(changes),
        "stricter": stricter,
        "looser": looser,
        "consistency": round((len(items) - len(changes)) / len(items), 3) if items else 0.0,
        "changes": changes[:50],
    }
