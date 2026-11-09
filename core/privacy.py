# -*- coding: utf-8 -*-
"""隐私脱敏。

本项目本身是内容治理系统，若把卖家手机号原文明文入库、明文展示，
等于自己违反隐私合规。因此：
* 审核阶段使用原文（保证违规检测不被掩码削弱）；
* 入库与展示前统一做掩码处理。
"""
from __future__ import annotations

import re
from typing import List, Tuple

# (说明, 正则, 掩码函数)
MASKERS: List[Tuple[str, re.Pattern, callable]] = [
    ("手机号", re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)"),
     lambda m: m.group(1)[:3] + "****" + m.group(1)[-4:]),
    ("邮箱", re.compile(r"([\w.+-]+)@([\w-]+\.[\w.]+)"),
     lambda m: (m.group(1)[:2] or "*") + "***@" + m.group(2)),
    ("QQ号", re.compile(r"((?:qq|扣扣|q号)[\s:：]*)(\d{5,12})", re.I),
     lambda m: m.group(1) + m.group(2)[:2] + "****" + m.group(2)[-2:]),
    ("微信号", re.compile(r"((?:微信|weixin|vx|v信)[\s:：]*)([A-Za-z0-9_-]{5,20})", re.I),
     lambda m: m.group(1) + m.group(2)[:2] + "****"),
]


def mask_text(text: str) -> str:
    """对文本中的联系方式做掩码替换。"""
    t = str(text or "")
    for _name, pattern, fn in MASKERS:
        t = pattern.sub(fn, t)
    return t


def is_masked(text: str) -> bool:
    """判断一段文本是否已含掩码标记（用于避免重复处理）。"""
    return bool(text) and "****" in text
