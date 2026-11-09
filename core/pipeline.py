# -*- coding: utf-8 -*-
"""审核流水线：规则引擎 -> 大模型审核 -> 决策融合 -> 落库。

融合策略（答辩要点）：
* 规则引擎负责"硬红线"（违禁词、明确的价格异常），命中 high 直接拒绝；
* 大模型负责"软语义"（话外之音、标题与正文矛盾、隐性诈骗话术）；
* 两者加权融合，并记录分歧用于人工复核与后续迭代。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import config
from . import database as db
from .dedup import text_fingerprint
from .lexicon import CATEGORY_MAP, LEXICON_VERSION
from .llm import LLMResult, analyze
from .privacy import mask_text
from .rules import RuleResult, RuleHit, ENGINE

DECISION_LABEL = {
    "pass": "自动通过",
    "review": "转人工复核",
    "reject": "拒绝发布",
}

# 非实物交易（违规服务 / 虚拟物品），命中后分类强制归入"其他杂项"
SERVICE_TYPES = {"cheating", "gamble", "account", "spy", "drain"}
# 语义层单独判定出的违禁实物（管制器具 / 烟草酒精）：与硬红线一致，
# 同样不得归档进"服饰鞋包""生活用品"这类可陈列类目，否则结论自相矛盾。
NON_DISPLAY_TYPES = {"weapon", "tobacco"}

# --- 融合保底阈值（集中配置，便于调参与答辩讲解）---
# 规则引擎强信号保底系数：避免规则的硬信号被模型稀释
RULE_STRONG_FLOOR = 0.8
# 命中多条 / 单条高危(非违禁)规则时的风险分下限
FLOOR_HIGH_MULTI = 78
FLOOR_HIGH_SINGLE = 58
FLOOR_MID_MULTI = 46
FLOOR_MID_SINGLE = 38
# 命中硬性违禁红线的风险分下限
FLOOR_BLOCKED = 92
# 无实质商品信息 + 站外引流：直接拒绝，不落到人工复核
FLOOR_INVALID_DRAIN = 88
# 大模型强语义信号保底：(模型分阈值, 综合分下限)，从高到低匹配
LLM_FLOORS = ((85, 78), (70, 62), (50, 45))
# 规则置信度的先验优势（规则精准但召回偏低）
RULE_PRIOR_BONUS = 0.15

# ---- 分级调用（成本/延迟优化）----
# 规则引擎已经"钉死"结论时，不再调用远程大模型，改由本地分析器补位：
# 综合分下限为 max(0.8*rule.score, FLOOR_BLOCKED)，rule.score >= 该值时决策恒为 reject，
# 远程模型再高的语义分也无法改变结论，白白付出 ~1.2s 延迟与额度。
SKIP_REMOTE_RULE_SCORE = 88.0


@dataclass
class AuditResult:
    decision: str = "pass"
    decision_label: str = "自动通过"
    risk_score: int = 0
    risk_level: str = "low"
    category: str = "other"
    category_name: str = "其他杂项"
    sub_category: str = ""
    rule: Optional[RuleResult] = None
    llm: Optional[LLMResult] = None
    reasons: List[str] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)
    latency_ms: int = 0

    def to_dict(self) -> dict:
        # 显式构造而非 asdict：避免对 rule/llm 做一遍无谓的深拷贝
        return {
            "decision": self.decision,
            "decision_label": self.decision_label,
            "risk_score": self.risk_score,
            "risk_level": self.risk_level,
            "category": self.category,
            "category_name": self.category_name,
            "sub_category": self.sub_category,
            "rule": self.rule.to_dict() if self.rule else None,
            "llm": self.llm.to_dict() if self.llm else None,
            "reasons": list(self.reasons),
            "suggestions": list(self.suggestions),
            "conflicts": list(self.conflicts),
            "latency_ms": self.latency_ms,
        }


class AuditPipeline:
    def __init__(self):
        self.rule_engine = ENGINE
        # 分级调用统计（线程安全）：用于 /api/metrics 展示"省下了多少次远程调用"
        self._lock = threading.Lock()
        self.stats = {"total": 0, "llm_remote": 0, "llm_skipped": 0}

    # ---------------- 主流程 ----------------
    def run(self, item: Dict[str, Any]) -> AuditResult:
        t0 = time.time()

        # 卖家维度上下文
        seller_id = str(item.get("seller_id") or "")
        if seller_id:
            item["is_blacklisted"] = db.blacklist_contains(seller_id)
            item["recent_post_count"] = db.recent_post_count(seller_id)

        rule_result = self.rule_engine.run(item)

        # 分级调用：规则引擎已把结论钉死（硬性违禁 / 风险分已达拒绝档）时跳过远程大模型，
        # 改用本地分析器补位——结论不变，但省掉一次 ~1.2s 的远程往返。
        skip_remote = bool(rule_result.blocked) or rule_result.score >= SKIP_REMOTE_RULE_SCORE
        llm_result = analyze(item, force_local=skip_remote)

        with self._lock:
            self.stats["total"] += 1
            if skip_remote and llm_result.engine == "mock":
                self.stats["llm_skipped"] += 1
            elif llm_result.engine == "api":
                self.stats["llm_remote"] += 1

        merged = self._fuse(item, rule_result, llm_result)
        merged.latency_ms = int((time.time() - t0) * 1000)
        return merged

    # ---------------- 融合 ----------------
    def _fuse(self, item: Dict[str, Any], rule: RuleResult, llm: LLMResult) -> AuditResult:
        res = AuditResult(rule=rule, llm=llm)

        # 1) 分类融合
        res.category, res.sub_category, cat_conflicts = self._fuse_category(rule, llm)

        # 违规服务 / 虚拟物品不属于任何实物门类，强制归入"其他杂项"
        hit_types = set(rule.categories_hit) | set(llm.violation_types)
        if hit_types & SERVICE_TYPES:
            res.category = "other"
            res.sub_category = "违规服务/虚拟物品"
        # 硬性违禁红线（管制刀具/烟草/偷拍设备等）不应被归档进可正常陈列的类目，
        # 否则会出现"高仿AJ1 → 服饰鞋包"这类语义自相矛盾的归档结果
        if (rule.blocked or (hit_types & NON_DISPLAY_TYPES)) and res.category != "other":
            res.category = "other"
            res.sub_category = "违规/不予展示物品"
        # 占位灌水内容没有真实商品，归档无意义，同样归入"其他杂项"
        # 注意只认"占位/测试内容"这条规则，不能误伤"占位价格"（后者是价格造假，商品仍可归类）
        if any(h.rule == "占位/测试内容" for h in rule.hits):
            res.category = "other"
            res.sub_category = res.sub_category or "无效内容"
        res.category_name = CATEGORY_MAP.get(res.category, CATEGORY_MAP["other"])["name"]
        res.conflicts = cat_conflicts

        # 2) 风险分融合：加权平均 + 强信号保底
        # RULE_STRONG_FLOOR*rule 项保证规则引擎的强信号不被模型稀释
        weighted = config.RULE_WEIGHT * rule.score + config.LLM_WEIGHT * llm.risk_score
        # 同源信号去重：价格异常 / 描述过短 / 刷屏 / 查重这类信号规则层与语义层各算了一次，
        # 若直接加权等于把同一个事实计两遍（如"描述<10字" 规则 +10、语义 +20）。
        # 这里扣掉语义层重复计入的部分（取两侧较小值），保留规则层那一次。
        dup_penalty = sum(min(rule.signals.get(k, 0), llm.signals.get(k, 0))
                          for k in set(rule.signals) & set(llm.signals))
        weighted -= config.LLM_WEIGHT * dup_penalty
        score = max(weighted, RULE_STRONG_FLOOR * rule.score)

        high_hits = [h for h in rule.hits if h.severity == "high" and h.type != "banned"]
        mid_hits = [h for h in rule.hits if h.severity == "medium"]
        if len(high_hits) >= 2:
            score = max(score, FLOOR_HIGH_MULTI)      # 多条高危信号 -> 拒绝
        elif len(high_hits) == 1:
            score = max(score, FLOOR_HIGH_SINGLE)     # 单条高危 -> 至少人工复核
        elif len(mid_hits) >= 2:
            score = max(score, FLOOR_MID_MULTI)
        elif len(mid_hits) == 1:
            score = max(score, FLOOR_MID_SINGLE)

        # 大模型强语义信号保底
        for threshold, floor in LLM_FLOORS:
            if llm.risk_score >= threshold:
                score = max(score, floor)
                break

        # 规则命中硬红线直接拉满
        if rule.blocked:
            score = max(score, FLOOR_BLOCKED)
        if any(h.rule == "无效引流信息" for h in rule.hits):
            score = max(score, FLOOR_INVALID_DRAIN)
        res.risk_score = int(max(0, min(100, round(score))))
        res.risk_level = "high" if res.risk_score >= config.REJECT_SCORE else (
            "medium" if res.risk_score >= config.REVIEW_SCORE else "low")

        # 3) 决策
        if rule.blocked or res.risk_score >= config.REJECT_SCORE:
            res.decision = "reject"
        elif res.risk_score >= config.REVIEW_SCORE:
            res.decision = "review"
        else:
            res.decision = "pass"
        res.decision_label = DECISION_LABEL[res.decision]

        # 4) 理由与建议
        for h in rule.hits:
            res.reasons.append(f"[{h.rule}] {h.evidence}")
            if h.advice not in res.suggestions:
                res.suggestions.append(h.advice)
        res.reasons += llm.violations
        if llm.suggestion and llm.suggestion not in res.suggestions:
            res.suggestions.append(llm.suggestion)
        if not res.reasons:
            res.reasons.append("规则引擎与大模型均未发现异常，商品信息完整、定价合理。")
        if not res.suggestions:
            res.suggestions.append("内容合规，可直接发布。")
        return res

    @staticmethod
    def _fuse_category(rule: RuleResult, llm: LLMResult):
        """规则先验 + 模型判定，置信度高者优先；不一致时记录分歧。"""
        conflicts: List[str] = []
        r_cat, r_conf = rule.category_guess, rule.category_confidence
        l_cat, l_conf = llm.category, llm.confidence

        if r_cat == l_cat:
            return l_cat, (llm.sub_category or rule.sub_guess), conflicts

        conflicts.append(
            f"品类分歧：规则引擎判定「{CATEGORY_MAP.get(r_cat, {}).get('name', r_cat)}」"
            f"(置信度{r_conf:.0%})，大模型判定「{CATEGORY_MAP.get(l_cat, {}).get('name', l_cat)}」"
            f"(置信度{l_conf:.0%})")

        if r_cat == "other" and l_cat != "other":
            return l_cat, llm.sub_category, conflicts
        if l_cat == "other" and r_cat != "other":
            return r_cat, rule.sub_guess, conflicts
        # 规则置信度加 0.15 的先验优势（规则精准但召回低）
        if r_conf + 0.15 >= l_conf:
            return r_cat, rule.sub_guess, conflicts
        return l_cat, llm.sub_category, conflicts


PIPELINE = AuditPipeline()


def audit(item: Dict[str, Any]) -> AuditResult:
    """只审核，不落库（接口 / 预览用）。"""
    return PIPELINE.run(dict(item))


def submit(item: Dict[str, Any], source: str = "web", auto_save: bool = True) -> Dict[str, Any]:
    """审核并落库，返回商品记录 + 审核结论。"""
    data = dict(item)
    # 提交链路计算文本指纹并参与查重（预览/评估链路不传，因此不会被历史库干扰）
    if not data.get("text_fp"):
        data["text_fp"] = text_fingerprint(data.get("title", ""), data.get("description", ""))
    result = PIPELINE.run(data)
    record = {
        "title": item.get("title", ""),
        # 正文同样脱敏：卖家常在描述里写"电话 138xxxx"，此前只脱敏 contact 字段，
        # 等于手机号仍明文入库并被 CSV 原样导出
        "description": mask_text(item.get("description", "")),
        "price": _to_float(item.get("price")),
        "condition": item.get("condition", ""),
        "category": result.category,
        "sub_category": result.sub_category,
        "seller_id": item.get("seller_id", ""),
        "seller_name": item.get("seller_name", ""),
        # 联系方式脱敏后入库：审核用原文（保证检测能力），存储只留掩码
        "contact": mask_text(item.get("contact", "")),
        "has_image": 1 if item.get("has_image") else 0,
        "image_hash": item.get("image_hash", ""),
        "image_path": item.get("image_path", ""),
        "text_fp": data.get("text_fp", ""),
        "status": result.decision,
        "decision": result.decision_label,
        "risk_score": result.risk_score,
        "risk_level": result.risk_level,
        "rule_score": result.rule.score if result.rule else 0,
        "llm_score": result.llm.risk_score if result.llm else 0,
        "reason": mask_text("；".join(result.reasons))[:500],
        "suggestion": " ".join(result.suggestions)[:500],
        "detail_json": db.to_json(result.to_dict()),
        "source": source,
        "created_at": item.get("created_at") or db.now(),
    }
    item_id = None
    if auto_save:
        # 商品 + 规则命中明细 + 审核日志放在同一事务，避免半写状态
        item_id = db.insert_item_with_audit(
            record,
            [h.to_dict() for h in result.rule.hits] if result.rule else None,
            log_detail=(f"{result.decision_label}｜风险 {result.risk_score}"
                        f"｜分类 {result.category_name}｜规则版本 {LEXICON_VERSION}"),
        )
    record["id"] = item_id
    record["audit"] = result.to_dict()
    return record


def _to_float(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
