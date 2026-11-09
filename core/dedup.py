# -*- coding: utf-8 -*-
"""文本侧查重：SimHash 指纹 + 汉明距离相似度。

与 `core/image.py` 的感知哈希形成「图文双通道查重」：
* 图片通道（dHash）识别"同一张图改尺寸/加水印再发"的盗图行为；
* 文本通道（SimHash）识别"同一段文案换个标题再发"的刷屏与盗帖行为。

为什么用 SimHash 而不是 md5：
    md5 只能发现"一模一样"的文本，改写一两个字就绕过去了；
    SimHash 把文本映射成 64 位指纹，语义相近的文本指纹也相近，
    用汉明距离即可度量相似度，对轻微改写、语序调整、增删修饰词都鲁棒。
"""
from __future__ import annotations

import hashlib
import re
from typing import List, Tuple

# 指纹位数（64 bit -> 16 位十六进制）
HASH_BITS = 64
# 汉明距离阈值：<= 该值视为"同一段文案改写"
DUP_DISTANCE = 6
# 距离大于该值即认为不相关（用于快速短路，省掉后续比较）
UNRELATED_DISTANCE = 24

# 中文/英文混排：去掉标点与空白，只保留文字与数字，避免标点差异影响指纹
_STRIP_RE = re.compile(r"[\s\W_]+", re.UNICODE)


def _tokens(text: str) -> List[str]:
    """切词：中文按 2-gram 滑动，英文/数字按连续串切分。

    不依赖任何分词库（离线可跑），2-gram 对中文已经足够刻画"文案骨架"。
    """
    t = _STRIP_RE.sub("", str(text or "").lower())
    if not t:
        return []
    toks: List[str] = []
    buf = ""
    for ch in t:
        if "\u4e00" <= ch <= "\u9fff":        # 中文字符
            if buf:
                toks.append(buf)
                buf = ""
            toks.append(ch)
        else:
            buf += ch
    if buf:
        toks.append(buf)
    # 中文 2-gram
    grams = []
    for i in range(len(toks) - 1):
        if "\u4e00" <= toks[i] <= "\u9fff" and "\u4e00" <= toks[i + 1] <= "\u9fff":
            grams.append(toks[i] + toks[i + 1])
    return toks + grams


def text_fingerprint(title: str, desc: str, title_weight: int = 2) -> str:
    """计算文本 SimHash 指纹（16 位十六进制）。

    标题权重高于正文：二手描述高度模板化（"九成新 / 无磕碰 / 配件齐全"），
    若与正文等权，两件不同商品会因描述雷同被判为重复。给标题加权后，
    "同一件商品换个说法重发"仍然很近，而"不同商品、套话相同"会被拉开。

    实现：每个 token 取 md5 的前 64 bit 作为稳定哈希，按权重累加到 64 个 bit 上，
    最终正负定号得到指纹。
    """
    t_toks, d_toks = _tokens(title or ""), _tokens(desc or "")
    if not (t_toks or d_toks):
        return ""
    vec = [0] * HASH_BITS

    def _accumulate(tokens: List[str], weight: int) -> None:
        for tok in tokens:
            h = int(hashlib.md5(tok.encode("utf-8")).hexdigest()[:16], 16)
            for i in range(HASH_BITS):
                vec[i] += weight if (h >> i) & 1 else -weight

    _accumulate(t_toks, title_weight)
    _accumulate(d_toks, 1)
    bits = 0
    for i in range(HASH_BITS):
        bits |= (1 << i) if vec[i] > 0 else 0
    return f"{bits:016x}"


def hamming_distance(a: str, b: str) -> int:
    """两个指纹的汉明距离；缺失或非法时返回 HASH_BITS（视为完全不同）。"""
    if not a or not b or len(a) != len(b):
        return HASH_BITS
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except ValueError:
        return HASH_BITS


def similarity(a: str, b: str) -> float:
    """相似度 0~1 = 1 - 汉明距离/位数。"""
    if not a or not b:
        return 0.0
    return 1.0 - hamming_distance(a, b) / HASH_BITS


def find_text_duplicates(records: List[Tuple[int, str, str]], target: str,
                         threshold: int = DUP_DISTANCE) -> List[Tuple[int, int, str]]:
    """在 [(item_id, text_fp, seller_id), ...] 中找出与目标文本相似的条目。

    返回 [(item_id, 距离, seller_id), ...]，按距离升序。
    """
    if not target:
        return []
    found: List[Tuple[int, int, str]] = []
    for item_id, other, seller in records:
        dist = hamming_distance(target, other or "")
        if dist <= threshold:
            found.append((item_id, dist, seller or ""))
    return sorted(found, key=lambda x: x[1])
