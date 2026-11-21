"""任务级并行调度（本机 llama.cpp 只有一个推理槽，所以并行是「任务级」而非「推理级」）。

设计要点：
- 同时最多 N 个任务处于 running（默认 2，WASILY_MAX_TASKS 可调）。工具执行、文件读写、
  IDE 桥调用这些阶段能真正重叠；真正调模型的阶段由 llama-server 内部排队，
  不会互相打断。
- 超出的任务进入 FIFO 队列，面板可通过 /v1/tasks 看到「运行中 / 排队中 / 第 N 位」。
- 每个任务独立 session_id，取消仍走 /v1/agent/cancel（按会话取消，不受并行影响）。
"""
from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from typing import Any

MAX_ACTIVE = max(1, int(os.environ.get("WASILY_MAX_TASKS", "2") or 2))
ACQUIRE_TIMEOUT = float(os.environ.get("WASILY_TASK_TIMEOUT", "900") or 900)

_cond = threading.Condition()
_tasks: "OrderedDict[str, dict]" = OrderedDict()
_active = 0
_seq = 0
_history: list[dict] = []


def _prune() -> None:
    """只保留最近 60 条已结束任务，避免长跑进程里无限增长。"""
    finished = [k for k, v in _tasks.items() if v["state"] in ("done", "error", "cancelled")]
    for k in finished[:-60]:
        _tasks.pop(k, None)


def acquire(sid: str = "", kind: str = "chat", title: str = "") -> tuple[str, int]:
    """申请一个执行槽。返回 (task_id, 排队位次)；位次 0 表示立即获得槽位。"""
    global _active, _seq
    with _cond:
        _seq += 1
        tid = "t%04d-%s" % (_seq, (sid or "anon").replace(":", "_")[:24])
        _tasks[tid] = {
            "tid": tid, "sid": sid, "kind": kind, "title": title or kind,
            "state": "queued", "queue_pos": 1,
            "created_at": time.time(), "started_at": 0, "ended_at": 0,
        }
        deadline = time.time() + ACQUIRE_TIMEOUT
        while True:
            queued = [v for v in _tasks.values() if v["state"] == "queued"]
            me = _tasks[tid]
            if _active < MAX_ACTIVE and queued and queued[0]["tid"] == tid:
                _active += 1
                me["state"] = "running"
                me["queue_pos"] = 0
                me["started_at"] = time.time()
                _cond.notify_all()
                return tid, 0
            me["queue_pos"] = 1 + next((i for i, v in enumerate(queued) if v["tid"] == tid), 0)
            remain = deadline - time.time()
            if remain <= 0:
                me["state"] = "error"
                me["ended_at"] = time.time()
                _cond.notify_all()
                raise TimeoutError("排队超时（%ds），任务未启动" % int(ACQUIRE_TIMEOUT))
            _cond.wait(min(remain, 1.0))


def release(tid: str, state: str = "done") -> None:
    global _active
    with _cond:
        t = _tasks.get(tid)
        if t and t["state"] == "running":
            _active = max(0, _active - 1)
        if t:
            t["state"] = state
            t["ended_at"] = time.time()
            t["queue_pos"] = 0
            if state != "done" or True:
                _history.append({"tid": tid, "sid": t.get("sid"), "state": state,
                                 "dur": round(t["ended_at"] - (t["started_at"] or t["created_at"]), 1)})
                del _history[:-60]
        _prune()
        _cond.notify_all()


def cancel(tid: str) -> dict[str, Any]:
    with _cond:
        t = _tasks.get(tid)
        if not t:
            return {"ok": False, "error": "任务不存在"}
        if t["state"] in ("queued", "running"):
            t["state"] = "cancelled"
            t["ended_at"] = time.time()
            _cond.notify_all()
        return {"ok": True, "tid": tid, "state": t["state"], "sid": t.get("sid")}


def list_tasks() -> dict[str, Any]:
    with _cond:
        running = [dict(v) for v in _tasks.values() if v["state"] == "running"]
        queued = [dict(v) for v in _tasks.values() if v["state"] == "queued"]
        return {
            "ok": True, "max_active": MAX_ACTIVE, "active": _active,
            "running": running, "queued": queued,
            "running_count": len(running), "queued_count": len(queued),
            "recent": _history[-10:],
        }
