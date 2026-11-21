# -*- coding: utf-8 -*-
"""agent 状态同步：统一快照 + 事件流。

上层（网页、IDE 面板、第三方）通过同一份快照读取系统状态，
通过订阅拿到增量事件，避免各自轮询不同接口导致状态不一致。
"""
from __future__ import annotations

import collections
import threading
import time

MAX_EVENTS = 200


class StateStore:
    def __init__(self, max_events=MAX_EVENTS):
        self._lock = threading.RLock()
        self._snapshot = {}
        self._version = 0
        self._events = collections.deque(maxlen=max_events)
        self._subs = []          # [(token, queue-like)]

    # ---------- 快照 ----------
    def update(self, patch):
        """合并写入快照，返回新版本号。"""
        with self._lock:
            if isinstance(patch, dict):
                self._snapshot.update(patch)
            self._version += 1
            snap = dict(self._snapshot)
        self.publish("state.updated", {"version": self._version,
                                       "keys": sorted(patch.keys()) if isinstance(patch, dict) else []})
        return snap

    def get(self, key=None, default=None):
        with self._lock:
            if key is None:
                return dict(self._snapshot)
            return self._snapshot.get(key, default)

    @property
    def version(self):
        return self._version

    # ---------- 事件 ----------
    def publish(self, event, data=None):
        ev = {"event": event, "data": data or {}, "ts": time.time(),
              "version": self._version}
        with self._lock:
            self._events.append(ev)
            subs = list(self._subs)
        for _, q in subs:
            try:
                q.put_nowait(ev)
            except Exception:
                pass
        return ev

    def since(self, after_ts=0, limit=50):
        with self._lock:
            out = [e for e in self._events if e["ts"] > float(after_ts or 0)]
        return out[-limit:]

    def subscribe(self):
        """返回 (token, queue)；queue 支持 get_nowait()。"""
        import queue
        q = queue.Queue(maxsize=100)
        token = "sub_%d" % int(time.time() * 1000)
        with self._lock:
            self._subs.append((token, q))
        return token, q

    def unsubscribe(self, token):
        with self._lock:
            self._subs = [(t, q) for t, q in self._subs if t != token]

    def stats(self):
        with self._lock:
            return {"version": self._version, "keys": sorted(self._snapshot.keys()),
                    "events": len(self._events), "subscribers": len(self._subs)}


STORE = StateStore()
