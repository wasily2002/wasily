"""跨端会话同步总线：对话内容与执行状态的唯一事实来源。

为什么需要它：
- Agent 对话页（/chat，含 /agent/ 内嵌）与 IDE 面板（/panel_ui）是两个独立文档，
  各自维护消息列表与 localStorage，天然会出现「这边问、那边看不到」；
- 只同步命令状态（command_bus）不够，用户要求对话、上下文、执行状态一起对齐。

这里把所有「用户提问 / 助手增量 / 助手最终消息 / 会话清空 / IDE 执行回报」
统一成带全局递增序号的事件：
- 两端订阅同一条 SSE，天然看到同一份顺序；
- 事件带 id，断线重连按 after 续传，缓冲淘汰时下发 reset 要求全量快照；
- 消息用稳定的 msg_id 幂等：重试、重新生成、重复上报都不会产生重复气泡；
- 每个会话维护一份可校验快照（rev + sha256），供两端做最终一致性核对。
"""
from __future__ import annotations

import collections
import hashlib
import json
import queue
import threading
import time
import uuid
from typing import Any

MAX_EVENTS = 20000            # 全局事件环形缓冲；超出后由 reset 事件要求全量对齐
MAX_SESSIONS = 200            # 保留的会话快照数
SUB_QUEUE_SIZE = 4000         # 订阅队列；慢了就靠重放补齐，不在内存里无限堆积

_LOCK = threading.RLock()
_EVENTS: "collections.deque[dict]" = collections.deque(maxlen=MAX_EVENTS)
_SUBS: dict[str, "queue.Queue[dict]"] = {}
_SESS: "collections.OrderedDict[str, dict]" = collections.OrderedDict()
_SEQ = [0]
SUB_COUNT = [0]
# 会话历史提供者：服务重启后快照是空的，由主服务把已落盘的会话喂回来，
# 两端打开页面才能看到同一份历史（否则只有重启后的新消息可见）。
_PROVIDER: list = [None]


def set_provider(fn) -> None:
    _PROVIDER[0] = fn


def _next_id() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _canon(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _msg_hash(msgs: list[dict]) -> str:
    """只把对用户可见的字段纳入哈希，避免 steps 里易变的耗时让两端误判不一致。"""
    view = [{"mid": m.get("mid"), "role": m.get("role"),
             "content": m.get("content", "")} for m in msgs]
    return hashlib.sha256(_canon(view).encode("utf-8")).hexdigest()[:16]


def _new_sess() -> dict:
    return {"messages": [], "rev": 0, "seq": 0, "updated": time.time(), "group": "default"}


def _sess_get(sid: str) -> dict:
    rec = _SESS.get(sid)
    if rec is None:
        rec = _new_sess()
        _SESS[sid] = rec
    return rec


def _evict() -> None:
    while len(_SESS) > MAX_SESSIONS:
        _SESS.popitem(last=False)


def publish(ev_type: str, session_id: str = "", source: str = "",
            group: str = "default", **fields: Any) -> dict:
    """分配全局序号、更新会话快照并扇出。

    快照与事件在同一把锁内写入，保证「事件顺序」与「快照内容」不会互相错位。
    """
    sid = str(session_id or "")
    grp = str(group or "default")
    with _LOCK:
        ev = {"id": _next_id(), "type": str(ev_type), "ts": time.time(),
              "session_id": sid, "source": str(source or ""), "group": grp}
        ev.update(fields)
        _EVENTS.append(ev)
        if sid:
            rec = _sess_get(sid)
            rec["seq"] = ev["id"]
            rec["updated"] = ev["ts"]
            rec["group"] = grp
            if ev_type == "user":
                _apply_user(rec, ev)
            elif ev_type == "assistant":
                _apply_assistant(rec, ev)
            elif ev_type == "session_clear":
                rec["messages"] = []
                rec["rev"] += 1
                _evict()
        subs = list(_SUBS.values())
    for q in subs:
        try:
            q.put_nowait(ev)
        except queue.Full:
            # 慢客户端直接丢；它重连时会用 after 重放，状态不会因此不一致
            pass
    return ev


def _apply_user(rec: dict, ev: dict) -> None:
    """用户消息幂等：重试/重生成传入同一 mid 时只更新，不追加第二条。"""
    mid = str(ev.get("msg_id") or "")
    if not mid:
        return
    msgs = rec["messages"]
    idx = next((i for i, m in enumerate(msgs) if m.get("mid") == mid), -1)
    item = {"mid": mid, "role": "user", "content": ev.get("content", ""),
            "source": ev.get("source", ""), "ts": ev.get("ts")}
    if idx >= 0:
        if msgs[idx].get("content") == item["content"]:
            return                     # 内容未变：不产生新版本
        msgs[idx] = dict(msgs[idx], **item)
    else:
        msgs.append(item)
    rec["rev"] += 1
    _evict()


def _apply_assistant(rec: dict, ev: dict) -> None:
    mid = str(ev.get("msg_id") or "")
    if not mid:
        return
    msgs = rec["messages"]
    idx = next((i for i, m in enumerate(msgs) if m.get("mid") == mid), -1)
    item = {"mid": mid, "role": "assistant", "content": ev.get("content", ""),
            "source": ev.get("source", ""), "ts": ev.get("ts")}
    for key in ("steps", "elapsed", "agent_run", "interrupted", "feedback"):
        if key in ev:
            item[key] = ev[key]
    if idx >= 0:
        msgs[idx] = dict(msgs[idx], **item)
    else:
        msgs.append(item)
    rec["rev"] += 1
    _evict()


def user_mid(session_id: str, content: str) -> str:
    """按「会话 + 内容 + 上一条助手消息」生成稳定 id，重试不会插两条相同提问。"""
    with _LOCK:
        rec = _sess_get(str(session_id or ""))
        prev = next((m.get("mid") for m in reversed(rec["messages"])
                     if m.get("role") == "assistant"), "")
    raw = "%s\n%s\n%s" % (session_id, content, prev)
    return "u:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def assistant_mid() -> str:
    return "a:" + uuid.uuid4().hex[:16]


def seed(session_id: str, messages: list[dict], group: str = "default") -> None:
    """用服务端已有会话初始化快照（重启后第一次请求即可恢复两端历史）。"""
    sid = str(session_id or "")
    if not sid or not messages:
        return
    with _LOCK:
        rec = _sess_get(sid)
        if rec["messages"]:
            return                      # 已有运行期快照：不覆盖更新的版本
        for i, m in enumerate(messages):
            role = str(m.get("role") or "")
            if role not in ("user", "assistant"):
                continue
            mid = str(m.get("mid") or "s:%s:%d" % (sid, i))
            item = {"mid": mid, "role": role, "content": str(m.get("content") or ""),
                    "source": "saved", "ts": float(m.get("ts") or 0)}
            for key in ("steps", "elapsed", "agent_run", "interrupted", "feedback"):
                if key in m:
                    item[key] = m[key]
            rec["messages"].append(item)
        rec["rev"] += 1
        rec["group"] = group
        rec["updated"] = time.time()
        _evict()


def snapshot(session_id: str) -> dict:
    sid = str(session_id or "")
    with _LOCK:
        rec = _SESS.get(sid)
        msgs = [dict(m) for m in rec["messages"]] if rec else []
    if not msgs and _PROVIDER[0] and sid:
        # 重启后快照为空：用已落盘的历史初始化一次，保证两端看到同一份上下文
        try:
            seeded = _PROVIDER[0](sid) or []
        except Exception:  # noqa: BLE001
            seeded = []
        if seeded:
            seed(sid, seeded)
            with _LOCK:
                rec = _SESS.get(sid)
                msgs = [dict(m) for m in rec["messages"]] if rec else []
    with _LOCK:
        if rec is None:
            return {"ok": True, "session_id": sid, "messages": [], "rev": 0,
                    "seq": _SEQ[0], "hash": _msg_hash([]), "updated": 0,
                    "exists": False}
        return {"ok": True, "session_id": sid, "messages": msgs, "rev": rec["rev"],
                "seq": rec["seq"] or _SEQ[0], "hash": _msg_hash(msgs),
                "updated": rec["updated"], "exists": True}


def verify(session_id: str, rev: int = -1, hash_value: str = "",
           count: int = -1, seq: int = -1) -> dict:
    """最终一致性核对：客户端报自己的摘要，服务端返回是否一致与差异原因。"""
    snap = snapshot(session_id)
    reasons = []
    if rev >= 0 and int(rev) != int(snap["rev"]):
        reasons.append("rev")
    if count >= 0 and int(count) != len(snap["messages"]):
        reasons.append("count")
    if hash_value and str(hash_value) != snap["hash"]:
        reasons.append("hash")
    if seq >= 0 and int(seq) < int(snap["seq"]):
        reasons.append("seq_behind")
    return {"ok": not reasons, "session_id": session_id, "reasons": reasons,
            "server": {"rev": snap["rev"], "hash": snap["hash"],
                       "count": len(snap["messages"]), "seq": snap["seq"]},
            "client": {"rev": rev, "hash": hash_value, "count": count, "seq": seq}}


def subscribe() -> "tuple[str, queue.Queue[dict]]":
    token = "syncsub_%s" % uuid.uuid4().hex[:12]
    q: "queue.Queue[dict]" = queue.Queue(maxsize=SUB_QUEUE_SIZE)
    with _LOCK:
        _SUBS[token] = q
        SUB_COUNT[0] = len(_SUBS)
    return token, q


def unsubscribe(token: str) -> None:
    with _LOCK:
        _SUBS.pop(token, None)
        SUB_COUNT[0] = len(_SUBS)


def events_after(seq: int) -> list[dict]:
    with _LOCK:
        return [e for e in _EVENTS if int(e["id"]) > int(seq or 0)]


def oldest_id() -> int:
    with _LOCK:
        return int(_EVENTS[0]["id"]) if _EVENTS else 0


def stats() -> dict:
    with _LOCK:
        return {"seq": _SEQ[0], "events": len(_EVENTS), "oldest": oldest_id(),
                "sessions": len(_SESS), "subscribers": len(_SUBS)}
