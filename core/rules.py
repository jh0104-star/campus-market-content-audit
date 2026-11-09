# -*- coding: utf-8 -*-
"""规则引擎：违禁品识别 / 价格异常 / 虚假描述 / 账号风险。

特点：
* 毫秒级响应，可解释（每条命中都给出 evidence + advice）
* 与大模型结果做加权融合，规则负责"硬红线"，模型负责"软语义"
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, asdict
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

from .antievasion import evasion_hits, strip_noise, trad_to_simp
from .lexicon import (
    ABNORMAL_PRICE_NUMBERS, BANNED_RULES, CATEGORIES, CATEGORY_MAP,
    CONDITION_NEW_WORDS, CONTACT_PATTERNS, DRAIN_WORDS, FRAUD_WORDS,
    HYPE_WORDS, PLACEHOLDER_WORDS, PRICE_HINTS,
)
from .logutils import get_logger, log_swallow

_logger = get_logger("rules")


# ------------------------------------------------------------------ #
# 文本工具
# ------------------------------------------------------------------ #
@lru_cache(maxsize=4096)
def normalize(text: str) -> str:
    """全角转半角 + 小写 + 去除空白，便于关键词匹配。"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", str(text))
    text = text.lower()
    return re.sub(r"\s+", "", text)


@lru_cache(maxsize=4096)
def raw_lower(text: str) -> str:
    return unicodedata.normalize("NFKC", str(text or "")).lower()


def content_text(title: str, desc: str) -> str:
    """标题 + 正文的规范拼接。

    统一由本函数拼接，保证规则引擎与大模型拿到同一份缓存键，
    避免同一条信息被重复做关键词分类（normalize 会去掉空白，拼接方式不影响结果）。
    """
    return f"{title or ''} {desc or ''}"


def match_keywords(text_norm: str, keywords: Dict[str, int]) -> Tuple[int, Dict[str, int]]:
    """返回 (总权重, {命中词: 权重})。"""
    score, hit = 0, {}
    for word, weight in keywords.items():
        if word in text_norm:
            score += weight
            hit[word] = weight
    return score, hit


@lru_cache(maxsize=2048)
def _classify_cached(text: str) -> Tuple[str, str, float, Tuple[Tuple[str, float], ...]]:
    """分类核心逻辑（带缓存）。scores 以元组返回，避免调用方改动污染缓存。"""
    t = normalize(text)
    scores: Dict[str, float] = {}
    for cat in CATEGORIES:
        s, _ = match_keywords(t, cat["keywords"])
        scores[cat["key"]] = float(s)

    best = max(scores, key=lambda k: scores[k])
    if scores[best] <= 0:
        return "other", "", 0.0, tuple(scores.items())

    total = sum(scores.values()) or 1.0
    confidence = round(scores[best] / total, 3)

    # 子类
    sub_name, sub_best = "", 0.0
    cat = CATEGORY_MAP[best]
    for name, kws in cat["sub"].items():
        s, _ = match_keywords(t, kws)
        if s > sub_best:
            sub_name, sub_best = name, s
    return best, sub_name, confidence, tuple(scores.items())


def keyword_classify(text: str) -> Tuple[str, str, float, Dict[str, float]]:
    """基于关键词权重的快速分类，返回 (主类key, 子类名, 置信度, 各类得分)。"""
    best, sub_name, confidence, scores = _classify_cached(text)
    return best, sub_name, confidence, dict(scores)


def resolve_price_reference(text_norm: str, category_key: str) -> Tuple[Optional[Dict[str, float]], str]:
    """优先用具体商品的参考价，其次用类目参考价。

    返回 (参考价字典, 参考名)。若既没有命中具体商品、"其他杂项"又没有专属参考价，
    则返回 (None, "") —— 没有可靠参考价时不应做相对比价，避免把日常用品误判为异常。
    """
    for word, ref in PRICE_HINTS.items():
        if word in text_norm:
            return ref, word
    if category_key == "other":
        return None, ""
    cat = CATEGORY_MAP.get(category_key) or CATEGORY_MAP["other"]
    return {"new": cat["new_price"], "min": cat["price_min"], "max": cat["price_max"]}, cat["name"]


PRICE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:元|块|rmb|钱)")
REPEAT_RE = re.compile(r"(.)\1{4,}")     # 连续重复字符（灌水特征）


def extract_price_mentions(text: str) -> List[float]:
    """从文案中提取提到的金额，用于与标价做一致性校验。"""
    t = raw_lower(text)
    return [float(x) for x in PRICE_RE.findall(t)]


def extract_contacts(text: str) -> Dict[str, List[str]]:
    t = raw_lower(text)
    found: Dict[str, List[str]] = {}
    for name, pattern in CONTACT_PATTERNS:
        vals = re.findall(pattern, t)
        if vals:
            found[name] = [v if isinstance(v, str) else str(v) for v in vals][:3]
    return found


# ------------------------------------------------------------------ #
# 结果结构
# ------------------------------------------------------------------ #
@dataclass
class RuleHit:
    rule: str            # 规则名称
    type: str            # banned / price / fake / contact / account
    severity: str        # high / medium / low
    evidence: str        # 命中证据
    advice: str          # 整改建议
    score: int = 0
    terms: List[str] = field(default_factory=list)   # 命中的关键词，供前端原文高亮
    evasion: str = ""                                # 非空表示命中了规避变体（记录还原策略）

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RuleResult:
    hits: List[RuleHit] = field(default_factory=list)
    score: int = 0
    blocked: bool = False                 # 命中高危硬性红线
    category_guess: str = "other"
    sub_guess: str = ""
    category_confidence: float = 0.0
    categories_hit: set = field(default_factory=set)   # 命中的违禁类型 key
    # 结构化信号（信号名 -> 分值）：用于与语义层做同源信号去重，
    # 避免"规则算一次 + 语义再算一次"把同一个事实计入两遍。
    signals: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "hits": [h.to_dict() for h in self.hits],
            "score": self.score,
            "blocked": self.blocked,
            "category_guess": self.category_guess,
            "sub_guess": self.sub_guess,
            "category_confidence": self.category_confidence,
            "categories_hit": sorted(self.categories_hit),
            "signals": dict(self.signals),
        }


# 与语义层语义等价的信号（key 需与 core/llm.py 中保持一致）
SIGNAL_BY_RULE = {
    "描述过短": "short_desc",
    "标题过短": "short_title",
    "描述重复灌水": "repeat",
}


def _collect_signals(hits: List[RuleHit]) -> Dict[str, int]:
    """把命中项归纳成结构化信号，供融合阶段与语义层做同源去重。"""
    signals: Dict[str, int] = {}
    for h in hits:
        if h.type == "price":
            key = "price"
        elif h.type == "dup" or h.rule.startswith("图片重复"):
            key = "dup"
        else:
            key = SIGNAL_BY_RULE.get(h.rule, "")
        if not key:
            continue
        signals[key] = max(signals.get(key, 0), h.score)
    return signals


# ------------------------------------------------------------------ #
# 规则引擎
# ------------------------------------------------------------------ #
class RuleEngine:
    """可插拔规则引擎，score 上限 100。"""

    MAX = 100

    def run(self, item: dict) -> RuleResult:
        title = str(item.get("title") or "")
        desc = str(item.get("description") or "")
        price = self._to_float(item.get("price"))
        contact = str(item.get("contact") or "")
        # 违禁词匹配包含联系方式（联系方式里也可能藏违规信息）
        full_norm = normalize(title + desc + contact)
        # 文本质量检测只看标题与正文，避免手机号等字符串造成误伤
        content_raw = raw_lower(title + " " + desc)
        full_raw = raw_lower(title + " " + desc + " " + contact)

        result = RuleResult()
        content = content_text(title, desc)          # 与大模型共用同一份拼接，命中分类缓存
        cat_key, sub_name, conf, _ = keyword_classify(content)
        result.category_guess, result.sub_guess, result.category_confidence = cat_key, sub_name, conf

        self._check_account(item, result)
        self._check_banned(full_norm, result)
        self._check_price(content, price, cat_key, result)
        self._check_fake(title, desc, price, item, content_raw, result)
        self._check_contact(contact, full_raw, result)
        self._check_image(item, result)
        self._check_duplicate(item, result)

        result.score = min(self.MAX, sum(h.score for h in result.hits))
        result.blocked = any(h.severity == "high" and h.type == "banned" for h in result.hits)
        result.signals = _collect_signals(result.hits)
        return result

    # ---------------- 账号维度 ----------------
    def _check_account(self, item: dict, result: RuleResult) -> None:
        if item.get("is_blacklisted"):
            result.hits.append(RuleHit(
                "账号黑名单", "account", "high",
                f"卖家 {item.get('seller_id') or '-'} 在黑名单中",
                "该账号已被限制发布，请联系管理员申诉。", 100))
        recent = int(item.get("recent_post_count") or 0)
        if recent > 10:
            result.hits.append(RuleHit(
                "高频发布", "account", "low",
                f"近24小时已发布 {recent} 条信息",
                "短时间内大量发布，疑似广告刷屏，建议限流。", 12))

    # ---------------- 违禁品 ----------------
    def _check_banned(self, text_norm: str, result: RuleResult) -> None:
        for rule in BANNED_RULES:
            exclude = rule.get("exclude", [])
            hit_words = [w for w in rule["words"] if w in text_norm]
            if hit_words and not any(x in text_norm for x in exclude):
                severity = rule["severity"]
                score = rule.get("score") or (95 if severity == "high" else 45)
                if severity == "high":
                    result.categories_hit.add(rule["key"])
                result.hits.append(RuleHit(
                    f"违禁品-{rule['name']}", "banned", severity,
                    "、".join(hit_words[:5]), rule["advice"], score,
                    terms=hit_words[:5]))
                continue

            # 原文未命中 -> 尝试还原后的变体（干扰符 / 繁体 / 谐音 / 拼音）
            if not hit_words and text_norm:
                self._check_banned_evasion(text_norm, rule, exclude, result)

    def _check_banned_evasion(self, text_norm: str, rule: dict,
                              exclude: List[str], result: RuleResult) -> None:
        """在反规避还原文本上二次匹配，命中即按原规则定级。"""
        hits = evasion_hits(text_norm, tuple(rule["words"]))
        if not hits:
            return
        # 豁免词在还原文本上同样生效，避免过度拦截
        for word, _cand, strategy in hits:
            if any(x in text_norm for x in exclude):
                continue
            severity = rule["severity"]
            score = rule.get("score") or (95 if severity == "high" else 45)
            if severity == "high":
                result.categories_hit.add(rule["key"])
            result.hits.append(RuleHit(
                f"违禁品-{rule['name']}（变体）", "banned", severity,
                f"检测到「{word}」的规避写法（{strategy}）", rule["advice"], score,
                terms=[word], evasion=strategy))
            break       # 同一条规则只记一次，避免重复计分

    # ---------------- 价格异常 ----------------
    def _check_price(self, text: str, price: Optional[float], cat_key: str, result: RuleResult) -> None:
        if price is None:
            result.hits.append(RuleHit("价格缺失", "price", "medium", "未填写价格",
                                       "请补充明确的售价，便于买家判断。", 15))
            return
        if price <= 0:
            result.hits.append(RuleHit("价格异常", "price", "medium", f"价格为 {price}",
                                       "价格需大于 0 元，请修正。", 20))
            return

        ref, ref_name = resolve_price_reference(normalize(text), cat_key)

        if ref is None:
            # 没有可信参考价，只做绝对金额校验，不做相对比价
            return

        new_price = ref["new"]

        if int(price) in ABNORMAL_PRICE_NUMBERS:
            result.hits.append(RuleHit("占位价格", "price", "high", f"标价 {int(price)} 元",
                                       "疑似占位/玩笑价格，请填写真实售价。", 30))
        if price > 200000:
            result.hits.append(RuleHit("金额超范围", "price", "medium", f"标价 {price} 元",
                                       "校园二手交易金额异常，请核实。", 20))

        ratio = price / new_price

        # 极端低价：远低于市场价且低于二手合理下限的一半
        if ratio < 0.05 and price < ref["min"] * 0.5:
            result.hits.append(RuleHit(
                "价格远低于市场价", "price", "high",
                f"标价 {price:g} 元，仅为「{ref_name}」参考价({new_price}元)的 {ratio*100:.2f}%",
                "极低价常见于虚假引流或诈骗，需人工核验实物与交易方式。", 40))
        elif price < ref["min"] * 0.4:
            result.hits.append(RuleHit(
                "价格低于合理区间", "price", "low",
                f"标价 {price:g} 元，低于「{ref_name}」二手合理下限 {ref['min']} 元",
                "请说明低价原因（破损/急出），或调整价格。", 15))

        if ratio > 5:
            result.hits.append(RuleHit(
                "价格严重高于市场价", "price", "high",
                f"标价 {price:g} 元，为「{ref_name}」参考价({new_price}元)的 {ratio:.1f} 倍",
                "售价数倍于市场价，疑似占位、恶搞或诈骗信息。", 55))
        elif ratio > 1.5:
            result.hits.append(RuleHit(
                "价格高于全新价", "price", "medium",
                f"标价 {price:g} 元，高于「{ref_name}」全新参考价 {new_price} 元",
                "二手商品价格不应高于新品，疑似虚标或诈骗。", 25))
        elif price > ref["max"] * 1.3:
            result.hits.append(RuleHit(
                "价格高于合理区间", "price", "low",
                f"标价 {price:g} 元，高于「{ref_name}」二手合理上限 {ref['max']} 元",
                "价格偏高，建议补充成色说明或降价。", 12))

        # 文案金额与标价不一致
        for mentioned in extract_price_mentions(text):
            if mentioned <= 0:
                continue
            diff = abs(mentioned - price) / max(mentioned, price)
            if diff > 0.25:
                result.hits.append(RuleHit(
                    "标价与描述不一致", "price", "medium",
                    f"标价 {price:g} 元，描述中出现 {mentioned:g} 元",
                    "请保持标价与描述一致，避免误导买家。", 15))
                break

        # 成色与价格矛盾
        t_norm = normalize(text)
        # 成色与价格矛盾：仅对贵重商品生效（低价日用品"全新+便宜"是正常的）
        if new_price >= 200 and any(w in t_norm for w in CONDITION_NEW_WORDS) and ratio < 0.25:
            result.hits.append(RuleHit(
                "成色与价格矛盾", "price", "high",
                f"宣称「全新/未拆」但售价仅为参考价的 {ratio*100:.1f}%",
                "全新商品与极低价矛盾，疑似虚假描述。", 30))

    # ---------------- 图片维度 ----------------
    def _check_image(self, item: dict, result: RuleResult) -> None:
        """图片侧审核：盗图查重 + 占位图识别。

        此前 has_image 只是布尔量，"图"完全没参与审核；这里补上两类硬信号。
        库里没有同类图时不会误伤（查重只在有历史图片时才可能命中）。
        """
        from . import database as db
        from .image import DUP_DISTANCE, find_duplicates

        img_hash = str(item.get("image_hash") or "").strip()
        if not img_hash:
            return

        try:
            pairs = db.image_hash_pairs()
        except Exception as exc:
            # 查重是增强项，失败不应阻断审核；但要留痕，否则能力失效了没人知道
            log_swallow(_logger, "图片查重取历史指纹", exc)
            return
        dups = find_duplicates(pairs, img_hash, DUP_DISTANCE)
        if dups:
            nearest_id, dist = dups[0]
            result.hits.append(RuleHit(
                "图片重复（疑似盗图/一图多帖）", "image", "medium",
                f"与历史图片 #{nearest_id} 相似度极高（汉明距离 {dist}）",
                "请上传本人实拍图，重复使用他人图片属盗图，平台不予展示。",
                20 + min(10, 3 - dist // 2)))

        if item.get("image_low_info"):
            result.hits.append(RuleHit(
                "图片疑似占位/低信息量", "image", "low", "图片几乎无细节（接近纯色）",
                "请上传能看清商品外观的实物图。", 10))

    # ---------------- 文本查重 ----------------
    def _check_duplicate(self, item: dict, result: RuleResult) -> None:
        """文案查重：与图片通道互补，识别"换标题重发"的刷屏与盗帖。

        只有调用方显式传入 text_fp 才启用（提交链路会算好指纹），
        因此离线评估 / 接口预览不会被历史库里的数据干扰。
        """
        from . import database as db
        from .dedup import DUP_DISTANCE, find_text_duplicates

        fp = str(item.get("text_fp") or "").strip()
        if not fp:
            return
        try:
            pairs = db.text_fp_pairs(exclude_id=int(item.get("id") or 0))
        except Exception as exc:
            log_swallow(_logger, "文案查重取历史指纹", exc)
            return
        dups = find_text_duplicates(pairs, fp, DUP_DISTANCE)
        if not dups:
            return

        seller = str(item.get("seller_id") or "")
        same_seller = [d for d in dups if seller and d[2] == seller]
        if same_seller:
            item_id, dist, _ = same_seller[0]
            result.hits.append(RuleHit(
                "重复发帖（同一卖家）", "dup", "medium",
                f"与本人历史信息 #{item_id} 文案高度相似（汉明距离 {dist}）",
                "请勿重复发布同一件商品，重复信息会被折叠或下架。", 22))
            return
        item_id, dist, other = dups[0]
        result.hits.append(RuleHit(
            "疑似盗帖/文案雷同", "dup", "low",
            f"与 #{item_id}（卖家 {other or '未知'}）文案高度相似（汉明距离 {dist}）",
            "若非本人发布，请修改文案并上传实拍图，避免被判定为搬运信息。", 10))

    # ---------------- 虚假描述 ----------------
    def _check_fake(self, title: str, desc: str, price: Optional[float],
                    item: dict, full_raw: str, result: RuleResult) -> None:
        d = desc.strip()
        spam_signals = 0

        if len(d) < 10:
            spam_signals += 1
            result.hits.append(RuleHit("描述过短", "fake", "low", f"描述仅 {len(d)} 字",
                                       "请补充成色、购入时间、转手原因等信息。", 10))
        if REPEAT_RE.search(full_raw):
            spam_signals += 1
            result.hits.append(RuleHit("描述重复灌水", "fake", "medium", "正文存在连续重复字符",
                                       "请填写真实有效的商品描述。", 18))
        elif len(d) >= 8 and len(set(d)) / len(d) < 0.35:
            spam_signals += 1
            result.hits.append(RuleHit("描述重复灌水", "fake", "low", "描述存在大量重复字符",
                                       "请填写真实有效的商品描述。", 10))
        if len(title.strip()) < 4:
            spam_signals += 1
            result.hits.append(RuleHit("标题过短", "fake", "low", f"标题「{title.strip()}」",
                                       "标题应包含物品名称与关键规格。", 10))

        # 引流/夸大/占位此前只做原文包含匹配，"加微-信""私-聊"这类插干扰符写法可直接绕过。
        # 这里在"去干扰符 + 繁转简"的文本上再匹配一次（不动用谐音/拼音还原，避免误杀）。
        # 惰性计算：大多数正常文本原文就能命中，不必多跑一次正则。
        _lite_cache: List[str] = [""]

        def lite() -> str:
            if not _lite_cache[0] and full_raw:
                _lite_cache[0] = trad_to_simp(strip_noise(full_raw))
            return _lite_cache[0]

        drain = [w for w in DRAIN_WORDS if w in full_raw]
        if not drain:
            drain = [w for w in DRAIN_WORDS if w in lite()]
        if drain:
            result.hits.append(RuleHit(
                "站外引流", "fake", "medium", "、".join(drain[:4]),
                "请勿引导站外交易，平台无法保障售后。", 14 + min(6, len(drain)),
                terms=drain[:4]))

        hype = [w for w in HYPE_WORDS if w in full_raw]
        if not hype:
            hype = [w for w in HYPE_WORDS if w in lite()]
        if hype:
            result.hits.append(RuleHit(
                "夸大宣传", "fake", "low", "、".join(hype[:4]),
                "避免使用绝对化用语，客观描述商品即可。", 8, terms=hype[:4]))

        fraud = [w for w in FRAUD_WORDS if w in full_raw]
        variants = []
        if not fraud and full_raw:
            # 原文未命中 -> 识别被拆解/变体改写的高危话术
            variants = list(evasion_hits(full_raw, tuple(FRAUD_WORDS)))
        if variants:
            # 变体命中与原文命中是同一事实，走互斥分支：此前会先记 35 再记 35，
            # 单条信号直接 70 分，再叠加"高危交易话术"可达 110 分，计分粒度失控。
            fraud = [w for w, _c, _s in variants]
            word, _cand, strategy = variants[0]
            result.hits.append(RuleHit(
                "疑似诈骗表述（变体）", "fake", "high",
                f"检测到「{word}」的规避写法（{strategy}）",
                "要求先款后货/私下转账属高危诈骗特征，建议拒绝。", 35,
                terms=[word], evasion=strategy))
        elif fraud:
            result.hits.append(RuleHit(
                "疑似诈骗表述", "fake", "high", "、".join(fraud[:4]),
                "要求先款后货/私下转账属高危诈骗特征，建议拒绝。", 35, terms=fraud[:4]))
        if len(fraud) >= 2:
            result.hits.append(RuleHit(
                "高危交易话术", "fake", "high", "、".join(fraud[:4]),
                "多条高危话术叠加，存在明显诈骗嫌疑，建议直接拒绝。", 40, terms=fraud[:4]))

        # 站外引流 + 无实质商品信息：典型引流/诈骗信息（如"转让闲置 / 想要的私聊，价格私谈"）
        # 单独看引流只是中危，但叠加"说不出卖的是什么"就是红线，不应只落到人工复核
        if drain and (len(d) < 20 or any(w in full_raw for w in ("私聊", "私谈", "价格私", "面议"))):
            result.hits.append(RuleHit(
                "无效引流信息", "fake", "high", "、".join(drain[:3]) + " 且无实质商品信息",
                "未说明具体商品却引导站外私聊，属典型引流/诈骗信息，禁止发布。", 55, terms=drain[:3]))

        placeholder = [w for w in PLACEHOLDER_WORDS if w in full_raw]
        if not placeholder and lite():
            placeholder = [w for w in PLACEHOLDER_WORDS if w in lite()]
        if placeholder:
            spam_signals += 1
            result.hits.append(RuleHit(
                "占位/测试内容", "fake", "medium", "、".join(placeholder[:3]),
                "请填写真实商品信息，不要发布测试内容。", 18, terms=placeholder[:3]))

        if not item.get("has_image") and (price or 0) >= 500:
            result.hits.append(RuleHit(
                "高价无实物图", "fake", "low", "价格 ≥ 500 元且未上传实物图",
                "高价商品请上传实物照片，提升可信度。", 6))

        # 占位内容 + 站外引流 / 无有效品类词 = 明显非真实商品
        if placeholder and (drain or result.category_guess == "other"):
            spam_signals += 1

        # 综合判定：多条低质信号叠加 = 垃圾/无效信息
        if spam_signals >= 2:
            result.hits.append(RuleHit(
                "垃圾/无效信息", "fake", "high",
                f"命中 {spam_signals} 项低质特征（信息量不足/占位/重复/标题过短）",
                "信息量严重不足，无法判断商品真实性，拒绝发布。", 70))

    # ---------------- 联系方式 ----------------
    def _check_contact(self, contact: str, full_raw: str, result: RuleResult) -> None:
        found = extract_contacts(full_raw)
        platforms = [k for k in found if k != "网址/短链"]
        if len(platforms) >= 3:
            result.hits.append(RuleHit(
                "多平台联系方式", "contact", "low", "、".join(platforms),
                "联系方式过多，疑似营销号。", 8))
        if "网址/短链" in found:
            result.hits.append(RuleHit(
                "外部链接", "contact", "medium", found["网址/短链"][0],
                "禁止在描述中放置外部链接，防范钓鱼风险。", 10))

    # ---------------- 工具 ----------------
    @staticmethod
    def _to_float(v) -> Optional[float]:
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None


ENGINE = RuleEngine()
