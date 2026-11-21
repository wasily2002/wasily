# -*- coding: utf-8 -*-
"""agent 统一日志：优先走 log_center，回退标准输出。

不直接使用 logging 模块，是为了和现有 log_center 的目录/轮转策略保持一致。
"""
from __future__ import annotations

import time
import traceback

_PREFIX = "[agent]"


def _sink(line):
    try:
        import log_center
        log_center.write_line(line)
        return
    except Exception:
        pass
    try:
        print(line, flush=True)
    except Exception:
        pass


def _fmt(level, event, fields):
    parts = [_PREFIX, level.upper(), event]
    parts = [p for p in parts if p]
    if fields:
        extra = " ".join("%s=%s" % (k, v) for k, v in fields.items()
                         if v is not None)
        if extra:
            parts.append(extra)
    return " ".join(parts)


def info(event, **fields):
    _sink(_fmt("info", event, fields))


def warn(event, **fields):
    _sink(_fmt("warn", event, fields))


def error(event, exc=None, **fields):
    if exc is not None:
        fields.setdefault("err", "%s: %s" % (type(exc).__name__, exc))
    _sink(_fmt("error", event, fields))
    if exc is not None:
        try:
            _sink(traceback.format_exc().rstrip())
        except Exception:
            pass


def timed(event, started_at, **fields):
    """记录耗时；started_at 为 time.time() 的返回值。"""
    fields.setdefault("ms", int((time.time() - started_at) * 1000))
    info(event, **fields)
