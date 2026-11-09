# -*- coding: utf-8 -*-
"""统一日志出口。

此前项目里所有异常都是 `except Exception: pass`，规则命中写失败、查重失败、
PRAGMA 失效等情况一律静默，导致「规则治理报表少数据」「盗图能力失效」这类问题
无法被发现。这里提供统一 logger，把静默失败改为可观测的 warning。
"""
from __future__ import annotations

import logging
import sys
from typing import Optional

_LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_CONFIGURED = False


def setup_logging(level: int = logging.INFO,
                  stream: Optional[object] = None) -> None:
    """配置根 logger（幂等，重复调用无副作用）。"""
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(stream or _utf8_stdout())
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, "%H:%M:%S"))
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)
    # Windows 控制台 GBK 时中文会抛编码错误，降级为 replace 而不是让日志打崩程序
    _CONFIGURED = True


def _utf8_stdout():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    return sys.stdout


def get_logger(name: str = "audit") -> logging.Logger:
    """取 logger；未显式 setup 时按需初始化，保证任何入口都有日志出口。"""
    if not _CONFIGURED:
        setup_logging()
    return logging.getLogger(name)


def log_swallow(logger: logging.Logger, action: str, exc: BaseException,
                level: int = logging.WARNING) -> None:
    """记录被兜底吞掉的异常：统一格式，便于检索「哪些能力在静默降级」。"""
    logger.log(level, "%s 失败（已降级，不影响主流程）：%s: %s",
               action, type(exc).__name__, exc)
