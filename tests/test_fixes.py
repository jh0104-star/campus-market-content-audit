# -*- coding: utf-8 -*-
"""真实样本回归用例。

每条都对应一次实测漏判/误判：这些样本没有被写进 dev/holdout 数据集，
是人工探测出来的，所以单独成文件，防止"评估集 100%"掩盖真实缺口。

运行： pytest -q tests/test_fixes.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest                                            # noqa: E402

from core.pipeline import audit                          # noqa: E402


# ------------------------------------------------------------------ #
# 缺口一：只写品牌不写品类，关键词表完全失明
# ------------------------------------------------------------------ #
def test_brand_combo_blocks_tobacco():
    """「软中华一条」里没有任何烟草品类词，规则层必须靠品牌+规格组合召回。"""
    res = audit({
        "title": "软中华一条 450",
        "description": "朋友送的两条，抽不完出一条，保真可验",
        "price": 450,
    })
    assert res.decision == "reject"
    assert res.rule.blocked
    assert any("品牌+规格" in h.rule for h in res.rule.hits)


def test_brand_combo_blocks_alcohol():
    res = audit({
        "title": "飞天茅台两瓶",
        "description": "单位发的，喝不惯酱香，两瓶一起出",
        "price": 2600,
    })
    assert res.decision == "reject"
    assert "alcohol" in res.rule.categories_hit


@pytest.mark.parametrize("title,desc", [
    ("中华铅笔 12 支装", "全新未拆，考试涂卡用"),
    ("中华田园犬领养", "两个月大，已驱虫，找靠谱人家"),
    ("茅台镇旅游纪念册", "去年出差买的，九成新"),
])
def test_brand_combo_no_false_positive(title, desc):
    """豁免词与量词约束的意义：品牌出现在正常商品里不能判违禁。"""
    res = audit({"title": title, "description": desc, "price": 20})
    assert not res.rule.blocked, f"误伤：{res.reasons}"


def test_electric_baton_uses_correct_rule():
    """「电击棒」原先词表里只有「电击器/电棍」，靠语义层兜底导致理由错位。"""
    res = audit({
        "title": "电击棒",
        "description": "女生防身用，带电击，九成新",
        "price": 80,
    })
    assert res.rule.blocked
    assert any(h.rule == "违禁品-管制器具" for h in res.rule.hits)


# ------------------------------------------------------------------ #
# 缺口二：标题短 + 描述短被合并成"垃圾信息"，正常商品被误杀
# ------------------------------------------------------------------ #
def test_short_desc_is_review_not_reject():
    """商品本身完全正常，只是写得简略——应转人工，不该直接拒绝。"""
    res = audit({"title": "猫粮", "description": "皇家2kg装", "price": 60})
    assert res.decision != "reject", res.reasons
    assert not res.rule.blocked
    assert any("信息量不足" in h.rule for h in res.rule.hits)


def test_placeholder_still_rejected():
    """降档只针对"短"，占位/灌水这类强信号仍然必须拒绝，不能连带放水。"""
    res = audit({"title": "测试测试测试", "description": "测试测试测试测试测试测试",
                 "price": 1})
    assert res.decision == "reject"


# ------------------------------------------------------------------ #
# 缺口三：出售/代开发票没有任何类目覆盖
# ------------------------------------------------------------------ #
@pytest.mark.parametrize("title,desc", [
    ("代购发票 需要的联系", "可开各类发票，金额可议"),
    ("出售发票", "餐饮住宿加油均可，点数低"),
])
def test_invoice_selling_rejected(title, desc):
    res = audit({"title": title, "description": desc, "price": 100})
    assert res.decision == "reject"
    assert "invoice" in res.rule.categories_hit


def test_invoice_as_proof_not_blocked():
    """「有发票」是二手电子产品的正常卖点（保修凭证），不能被当成倒卖发票。"""
    res = audit({"title": "iPad 2021 有发票", "description": "九成新，带发票和充电器，保修期内",
                 "price": 1800})
    assert not res.rule.blocked, res.reasons


# ------------------------------------------------------------------ #
# 缺口四：拒绝结论的理由顺序错位（"结论对、理由错"）
# ------------------------------------------------------------------ #
def test_reject_reason_starts_with_hard_rule():
    """命中了硬红线，第一条理由就必须是那条红线，而不是"描述过短"。"""
    res = audit({"title": "电击棒", "description": "女生防身用，带电击，九成新", "price": 80})
    assert res.decision == "reject"
    assert res.reasons[0].startswith("[违禁品-"), res.reasons[:3]


def test_reject_without_banned_hit_has_summary_reason():
    """没有硬红线、靠多项信号叠加到拒绝时，必须给出"[综合判定]"和主要依据。"""
    res = audit({"title": "转让闲置", "description": "想要的私聊，价格私谈，先款后货",
                 "price": 0})
    assert res.decision == "reject"
    assert not any(h.type == "banned" for h in res.rule.hits)
    assert res.reasons[0].startswith("[综合判定]"), res.reasons[:3]
