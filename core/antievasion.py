# -*- coding: utf-8 -*-
"""反规避（Anti-Evasion）：识别被刻意改写过的违规表述。

真实平台的违规信息不会老实写"折叠刀""代写"，常见规避手法：
    1. 干扰符   ：折-叠-刀、代*写、v.xin
    2. 繁体字   ：摺疊刀、香煙、賭博
    3. 谐音字   ：戴考、贷写、烟蛋、潘多拉
    4. 全拼     ：daixie、dianziyan、baoguo
    5. 数字替字 ：4六级代考（4→四）

本模块不做直接判定，而是把原文"还原"成若干候选标准文本，
交给规则引擎去匹配。每条命中都会标注用了哪种还原策略，
保证结论依然可解释（答辩要点：可解释的对抗防御）。
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Dict, List, Tuple

# ------------------------------------------------------------------ #
# 一、干扰符：词中间插入符号以割裂关键词
# ------------------------------------------------------------------ #
# 保留中文、字母、数字，其余符号一律视为噪声剔除
NOISE_RE = re.compile(r"[^一-龥a-zA-Z0-9]")

# ------------------------------------------------------------------ #
# 二、繁体 / 异体字 -> 简体（仅列可能与违禁词相关的常用字）
# ------------------------------------------------------------------ #
TRAD_MAP = str.maketrans({
    "摺": "折", "疊": "叠", "藥": "药", "煙": "烟", "彈": "弹", "賭": "赌",
    "號": "号", "賣": "卖", "買": "买", "槍": "枪", "聽": "听", "視": "视",
    "雞": "鸡", "鴨": "鸭", "獵": "猎", "彈": "弹", "劍": "剑", "刀": "刀",
    "電": "电", "壓": "压", "氣": "气", "體": "体", "個": "个", "們": "们",
    "為": "为", "學": "学", "寫": "写", "紙": "纸", "證": "证", "單": "单",
    "發": "发", "轉": "转", "嗎": "吗", "這": "这", "來": "来", "後": "后",
    "網": "网", "開": "开", "關": "关", "門": "门", "間": "间", "時": "时",
})

# ------------------------------------------------------------------ #
# 三、谐音 / 形近字 -> 规范字（只挑歧义小、收益高的映射）
# ------------------------------------------------------------------ #
HOMOPHONE_MAP = str.maketrans({
    "戴": "代", "貸": "代", "帶": "代", "待": "代",
    "蟹": "写", "瀉": "写", "謝": "写",
    "烤": "考", "拷": "考",
    "煲": "包", "胞": "包",
    "淡": "弹", "蛋": "弹",
    "氿": "酒", "玖": "酒",
    "祥": "详", "局": "局",
})

# ------------------------------------------------------------------ #
# 四、全拼 -> 标准词（选无常见英文同形的词，降低误杀）
# ------------------------------------------------------------------ #
PINYIN_MAP: Dict[str, str] = {
    "daikao": "代考", "tikao": "替考", "daixie": "代写", "qiangshou": "枪手",
    "dabian": "代答辩", "daxie": "代写",
    "dianziyan": "电子烟", "yandan": "烟弹", "xiangyan": "香烟", "juanyan": "卷烟",
    "baijiu": "白酒", "pijiu": "啤酒",
    "xiankuanhuohuo": "先款后货", "xianfukuan": "先付款", "xiandaikuan": "先打款",
    "buzoupingtai": "不走平台", "sijiaojiaoyi": "私下交易",
    "zhediedao": "折叠刀", "guanzidaoju": "管制刀具",
    "zuobiqi": "作弊器", "toupai": "偷拍", "qietingqi": "窃听器",
}
# ------------------------------------------------------------------ #
# 五、数字替字（4→四、5→五 等）；仅处理固定组合，避免全局误伤
# ------------------------------------------------------------------ #
NUMBER_SLANG = {
    "4六级": "四六级", "4级": "四级", "6级": "六级",
    "5粮液": "五粮液", "2锅头": "二锅头",
}


def strip_noise(text: str) -> str:
    """剔除所有非汉字/字母/数字的字符（反干扰符）。"""
    return NOISE_RE.sub("", text or "")


def trad_to_simp(text: str) -> str:
    """繁体 / 异体字转简体。"""
    return (text or "").translate(TRAD_MAP)


def restore_homophone(text: str) -> str:
    """常见谐音字还原。"""
    return (text or "").translate(HOMOPHONE_MAP)


def restore_pinyin(text: str) -> str:
    """把文本中的全拼音片段替换为对应标准词。"""
    t = text or ""
    # 门控从 6 降到 5：原阈值让表里 tikao(5)/daxie(5)/pijiu(5) 三条映射永远无法生效。
    # 5 个字母的全拼在中文语境里误撞概率极低，短于此（如 4 字母）才需要谨慎。
    for py, word in PINYIN_MAP.items():
        if len(py) >= 5 and py in t:
            t = t.replace(py, word)
    return t


def restore_number_slang(text: str) -> str:
    """数字替字还原（如 4六级 -> 四六级）。"""
    t = text or ""
    for slang, word in NUMBER_SLANG.items():
        t = t.replace(slang, word)
    return t


# 还原管道：(策略说明, 处理函数)
RESTORERS: List[Tuple[str, callable]] = [
    ("去除干扰符", strip_noise),
    ("繁简转换", trad_to_simp),
    ("谐音还原", restore_homophone),
    ("拼音还原", restore_pinyin),
    ("数字符号还原", restore_number_slang),
]


@lru_cache(maxsize=2048)
def variant_forms(text: str) -> Tuple[Tuple[str, str], ...]:
    """生成原文的若干"还原候选"。

    返回 ((策略名, 还原后文本), ...)，已去重且与原文不同的才返回。
    注意：候选文本只用于**二次匹配**，不改变原文判定结果。
    """
    t = text or ""
    stripped = strip_noise(t)
    out: List[Tuple[str, str]] = []
    seen = {t, stripped}

    # 单一策略
    if stripped != t:
        out.append(("去除干扰符", stripped))
    for name, fn in RESTORERS[1:]:
        cand = fn(stripped)
        if cand and cand not in seen:
            seen.add(cand)
            out.append((name, cand))

    # 组合策略：去噪 + 繁简 + 谐音 + 拼音 + 数字，一次性全上
    combo = stripped
    for name, fn in RESTORERS[1:]:
        combo = fn(combo)
    if combo and combo not in seen:
        out.append(("组合还原", combo))

    return tuple(out)


def evasion_hits(text: str, keywords) -> List[Tuple[str, str, str]]:
    """在还原候选里找关键词。

    返回 [(命中标准词, 原文还原后的写法, 策略名), ...]
    """
    t = text or ""
    found: List[Tuple[str, str, str]] = []
    seen_words = set()
    # 注意：必须传原文（而非去噪后的文本），否则"去除干扰符"这一候选永远生成不出来
    for strategy, cand in variant_forms(t):
        for w in keywords:
            if len(w) < 2 or w in seen_words:
                continue
            # 原文已能直接命中就不算规避
            if w in t:
                continue
            if w in cand:
                seen_words.add(w)
                found.append((w, cand, strategy))
    return found
