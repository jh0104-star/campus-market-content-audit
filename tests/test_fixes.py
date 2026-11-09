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
