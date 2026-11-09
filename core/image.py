# -*- coding: utf-8 -*-
"""图片侧审核：感知哈希查重 + 低信息量图片识别。

此前 `has_image` 只是布尔量，"图"完全没参与审核。本模块补上两块能力：

1. 感知哈希（dHash）：对缩放、压缩、水印鲁棒，可识别"同一张图改尺寸/加水印再发"
   的盗图与一图多帖行为；纯字节 md5 只能查完全相同的文件，很容易被绕过。
2. 低信息量图片：纯色/接近纯色的占位图、网图截图，与"实物拍摄"矛盾。

设计约束：
* 只可选依赖 Pillow。未安装时退化为字节 md5（查重能力下降但不报错），
  保证离线演示环境不会因为缺依赖而崩掉审核链路。
"""
from __future__ import annotations

import hashlib
import re
from typing import Dict, List, Optional, Tuple

from .logutils import get_logger, log_swallow

_logger = get_logger("image")

try:                                    # 可选依赖：没有也能跑，只是查重能力退化
    from PIL import Image, ImageStat
    import io as _io
    _HAS_PIL = True
except Exception as exc:                # pragma: no cover
    Image = None                        # type: ignore
    ImageStat = None                    # type: ignore
    _io = None                          # type: ignore
    _HAS_PIL = False
    log_swallow(_logger, "加载 Pillow（图片查重将退化为字节摘要）", exc)

# dHash 先把图缩到 9x8 再比较相邻像素，得到 64 bit 指纹
_HASH_WIDTH, _HASH_HEIGHT = 9, 8
# 汉明距离阈值：<= 该值视为"同一张图"（允许压缩/缩放带来的少量位翻转）
DUP_DISTANCE = 6
# 低信息量判定：灰度标准差低于该值说明画面几乎无细节（纯色/占位图）
LOW_INFO_STDDEV = 12.0

HEX_RE = re.compile(r"^[0-9a-f]{16}$")


def has_pillow() -> bool:
    return _HAS_PIL


def image_hash(data: bytes) -> str:
    """计算图片指纹。

    Pillow 可用 -> 64 位 dHash（16 位十六进制）；不可用 -> 字节 md5 前 16 位。
    """
    if not data:
        return ""
    if not _HAS_PIL:
        return hashlib.md5(data).hexdigest()[:16]
    try:
        img = Image.open(_io.BytesIO(data))
        img = img.convert("L").resize((_HASH_WIDTH, _HASH_HEIGHT))
        pixels = list(img.getdata())
        bits = 0
        for row in range(_HASH_HEIGHT):
            base = row * _HASH_WIDTH
            for col in range(_HASH_WIDTH - 1):
                bits = (bits << 1) | (1 if pixels[base + col] > pixels[base + col + 1] else 0)
        return f"{bits:016x}"
    except Exception as exc:            # 非图片/损坏文件：退回字节摘要，至少能查完全重复
        log_swallow(_logger, "感知哈希计算（退化为字节摘要）", exc, level=20)
        return hashlib.md5(data).hexdigest()[:16]


def hamming_distance(a: str, b: str) -> int:
    """两个 16 位十六进制指纹的汉明距离；长度/格式不同时视为完全不同。"""
    if not a or not b or len(a) != len(b) or not (HEX_RE.match(a) and HEX_RE.match(b)):
        return 64
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except ValueError:
        return 64


def is_low_information(data: bytes) -> bool:
    """是否为几乎无细节的占位图（纯色背景 / 空白截图）。"""
    if not data or not _HAS_PIL:
        return False
    try:
        img = Image.open(_io.BytesIO(data)).convert("L")
        return float(ImageStat.Stat(img).stddev[0]) < LOW_INFO_STDDEV
    except Exception as exc:
        log_swallow(_logger, "占位图识别", exc, level=20)
        return False


def image_meta(data: bytes) -> Dict[str, int]:
    """图片基本信息，仅用于审核日志展示。"""
    if not data or not _HAS_PIL:
        return {}
    try:
        img = Image.open(_io.BytesIO(data))
        return {"width": int(img.width), "height": int(img.height)}
    except Exception:
        return {}


def find_duplicates(records: List[Tuple[int, str]], target: str,
                    threshold: int = DUP_DISTANCE) -> List[Tuple[int, int]]:
    """在 (item_id, image_hash) 列表中找出与目标图相似的条目。

    返回 [(item_id, 距离), ...]，按距离升序。
    """
    if not target:
        return []
    found = []
    for item_id, other in records:
        dist = hamming_distance(target, other or "")
        if dist <= threshold:
            found.append((item_id, dist))
    return sorted(found, key=lambda x: x[1])
