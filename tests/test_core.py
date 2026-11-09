# -*- coding: utf-8 -*-
"""core 层单元测试（pytest）。

此前 core/ 下 rules / antievasion / dedup / privacy / image / lexicon 没有任何独立用例，
规则一旦改动定位不到是哪个模块退化。这里补齐核心不变量。

运行： pip install -r requirements-dev.txt && pytest -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                            # noqa: E402
import pytest                                            # noqa: E402

from core import database as db                          # noqa: E402
from core.antievasion import restore_pinyin, strip_noise  # noqa: E402
from core.dedup import text_fingerprint                  # noqa: E402
from core.pipeline import (FLOOR_BLOCKED, NON_DISPLAY_TYPES,  # noqa: E402
                           PIPELINE, RULE_STRONG_FLOOR, SERVICE_TYPES,
                           SKIP_REMOTE_RULE_SCORE, audit)
from core.privacy import mask_text                       # noqa: E402
from core.rules import ENGINE                            # noqa: E402


# ------------------------------------------------------------------ #
# 常量自洽性：改一个常量而忘改另一个，会让分级调用/决策档位静默失效
# ------------------------------------------------------------------ #
def test_thresholds_are_ordered():
    assert 0 <= config.REVIEW_SCORE < config.REJECT_SCORE <= 100, \
        "复核阈值必须低于拒绝阈值，否则「转人工复核」永远不会出现"


def test_weights_sum_to_one():
    assert abs(config.RULE_WEIGHT + config.LLM_WEIGHT - 1.0) < 1e-6


def test_skip_remote_matches_floor():
    """分级调用的前提：rule.score >= SKIP_REMOTE 时，保底分已达到拒绝档。"""
    floor = RULE_STRONG_FLOOR * SKIP_REMOTE_RULE_SCORE
    assert floor >= config.REJECT_SCORE, \
        f"rule={SKIP_REMOTE_RULE_SCORE} 时下限 {floor} 应不低于拒绝阈值 {config.REJECT_SCORE}"
    assert SKIP_REMOTE_RULE_SCORE <= FLOOR_BLOCKED


def test_type_sets_are_known():
    """语义层产出的违禁类型必须被归档规则覆盖，否则会归档进可陈列类目。"""
    from core.llm import SEMANTIC_RISK
    produced = {r["type"] for r in SEMANTIC_RISK}
    covered = SERVICE_TYPES | NON_DISPLAY_TYPES | {"piracy", "fraud"}
    assert produced <= covered, f"未覆盖的语义类型：{produced - covered}"


# ------------------------------------------------------------------ #
# 反规避
# ------------------------------------------------------------------ #
@pytest.mark.parametrize("raw,word", [
    ("折-叠-刀", "折叠刀"),
    ("tikao", "替考"),
    ("daxie", "代写"),
    ("pijiu", "啤酒"),
])
def test_evasion_restore(raw, word):
    assert word in restore_pinyin(strip_noise(raw)) or word in restore_pinyin(raw)


def test_noise_strip():
    assert strip_noise("先-款-后-货") == "先款后货"


# ------------------------------------------------------------------ #
# 规则引擎计分
# ------------------------------------------------------------------ #
def test_fraud_variant_not_double_counted():
    """变体命中与原文命中是同一事实，只能记一次分。"""
    item = {"title": "二手相机，先-款-后-货", "description": "不-走-平-台，先-款-后-货",
            "price": 1500}
    res = ENGINE.run(item)
    fraud_hits = [h for h in res.hits if h.rule.startswith("疑似诈骗表述")]
    assert len(fraud_hits) == 1, [h.rule for h in fraud_hits]
    assert sum(h.score for h in fraud_hits) <= 35


def test_drain_with_noise_is_detected():
    item = {"title": "转让闲置", "description": "想要的加微-信，价格私谈", "price": 100}
    res = ENGINE.run(item)
    assert any(h.rule.startswith("站外引流") or "引流" in h.rule for h in res.hits)


def test_price_signals_are_structural():
    res = ENGINE.run({"title": "iPhone 15", "description": "全新未拆封", "price": 1})
    assert res.signals.get("price", 0) > 0 or any(h.type == "price" for h in res.hits)


# ------------------------------------------------------------------ #
# 融合与归档
# ------------------------------------------------------------------ #
def test_weapon_semantics_not_archived_as_goods():
    """语义层判定的管制器具不得归档进可陈列类目。"""
    res = audit({"title": "自用防身小刀", "description": "开过刃，防身用，随身带着方便",
                 "price": 120})
    assert res.category == "other", res.category


def test_duplicate_signal_not_double_counted():
    """描述过短在规则层与语义层都会被识别，融合时只应按一次计分。"""
    item = {"title": "高等数学同济第七版上册", "description": "便宜出", "price": 15}
    res = audit(item)
    overlap = set(res.rule.signals) & set(res.llm.signals)
    assert "short_desc" in overlap, (res.rule.signals, res.llm.signals)
    dup = sum(min(res.rule.signals[k], res.llm.signals[k]) for k in overlap)
    naive = config.RULE_WEIGHT * res.rule.score + config.LLM_WEIGHT * res.llm.risk_score
    assert res.risk_score < naive - 0.5 * config.LLM_WEIGHT * dup + 1e-6, \
        (res.risk_score, naive, dup)


def test_audit_result_has_signals():
    res = audit({"title": "高等数学同济第七版", "description": "大一教材八成新", "price": 15})
    assert res.rule is not None and isinstance(res.rule.signals, dict)


# ------------------------------------------------------------------ #
# 隐私与查重
# ------------------------------------------------------------------ #
def test_mask_phone_and_email():
    masked = mask_text("联系我 13912345678 或 abc@test.com")
    assert "13912345678" not in masked and "abc@test.com" not in masked


def test_text_fingerprint_distance():
    from core.image import hamming_distance
    a = text_fingerprint("出九成新 iPad Air5 64G", "无磕碰无划痕，配件齐全，当面交易")
    b = text_fingerprint("出九成新 iPad Air5 64G", "无磕碰无划痕，配件齐全，当面交易")
    c = text_fingerprint("出小米移动电源 10000mAh", "无磕碰无划痕，配件齐全，当面交易")
    assert hamming_distance(a, b) == 0
    assert hamming_distance(a, c) > 6


# ------------------------------------------------------------------ #
# 数据层：单事务写入不留半写状态
# ------------------------------------------------------------------ #
def test_insert_with_audit_is_atomic(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    db.close_conn()
    db.init_db()
    item_id = db.insert_item_with_audit(
        {"title": "测试", "description": "内容", "price": 10, "status": "pass"},
        [{"rule": "测试规则", "type": "fake", "severity": "low", "score": 5}],
        log_detail="测试")
    with db.get_conn() as conn:
        hits = conn.execute("SELECT COUNT(*) FROM rule_hits WHERE item_id=?",
                            (item_id,)).fetchone()[0]
        logs = conn.execute("SELECT COUNT(*) FROM audit_logs WHERE item_id=?",
                            (item_id,)).fetchone()[0]
    assert hits == 1 and logs == 1
    db.close_conn()


def test_pipeline_stats_thread_safe():
    before = PIPELINE.stats["total"]
    audit({"title": "高等数学", "description": "教材八成新，无缺页", "price": 20})
    assert PIPELINE.stats["total"] == before + 1
