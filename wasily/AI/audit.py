"""
AI 行为审计：记录 AI 每次工具调用 / 决策路径 / 耗时与资源，加密落盘 audit.enc
  audit_event(kind, detail) → 追加一条
  audit_query(limit, kind)  → 查询
  audit_clear()             → 清空
"""
import time

from secure_store import AUDIT_ENC
import psutil


def audit_event(kind: str, detail: str = "", session_id: str = "", meta: dict = None):
    """kind: tool_call / generate / train / install_tool / error ..."""
    rec = {
        "id": AUDIT_ENC.next_id(),
        "ts": time.time(),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "kind": kind,
        "session_id": session_id,
        "detail": (detail or "")[:500],
        "meta": meta or {},
    }
    try:
        proc = psutil.Process()
        rec["meta"]["cpu"] = psutil.cpu_percent(interval=None)
        rec["meta"]["mem_mb"] = round(proc.memory_info().rss / 1048576, 1)
    except Exception:
        pass
    AUDIT_ENC.append(rec)
    return rec["id"]


def audit_query(limit=200, kind=None, offset=0):
    recs = AUDIT_ENC.load_all()
    if kind:
        recs = [r for r in recs if r.get("kind") == kind]
    recs = recs[::-1]                       # 最新在前
    return recs[offset:offset + limit], len(recs)


def audit_clear():
    return AUDIT_ENC.delete_ids(None)
