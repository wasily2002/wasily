# -*- coding: utf-8 -*-
"""agent 生命周期管理：注册服务 → 按依赖启动 → 健康检查 → 逆向停止。

服务由 name + start/stop/health 三个可调用对象描述，depends 用于排序。
未提供 start 的服务视为「外部托管」，只做健康探测。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from . import errors, logs


@dataclass
class ServiceSpec:
    name: str
    start: object = None        # callable() -> None
    stop: object = None         # callable() -> None
    health: object = None       # callable() -> dict|bool
    depends: list = field(default_factory=list)
    auto_start: bool = True
    timeout: float = 30.0


class LifecycleManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._specs = {}
        self._status = {}       # name -> {state, error, started_at, checks, ms}

    def register(self, spec: ServiceSpec):
        with self._lock:
            self._specs[spec.name] = spec
            self._status.setdefault(spec.name, {
                "state": "registered", "error": "", "started_at": 0,
                "checks": 0, "ms": 0})
        return spec.name

    def _order(self, names):
        """拓扑排序；检测循环依赖。"""
        result, seen, stack = [], set(), set()

        def visit(n):
            if n in seen:
                return
            if n in stack:
                raise errors.DependencyMissing("循环依赖: %s" % n)
            stack.add(n)
            for d in self._specs[n].depends:
                if d not in self._specs:
                    raise errors.DependencyMissing(
                        "%s 依赖的服务未注册: %s" % (n, d))
                visit(d)
            stack.discard(n)
            seen.add(n)
            result.append(n)

        for n in names:
            visit(n)
        return result

    def start_all(self, only=None):
        names = [n for n, s in self._specs.items()
                 if s.auto_start and (only is None or n in only)]
        ordered = self._order(names)
        started = []
        for n in ordered:
            try:
                self.start(n)
                started.append(n)
            except Exception as e:
                logs.error("服务启动失败", e, service=n)
        return started

    def start(self, name):
        spec = self._specs.get(name)
        if spec is None:
            raise errors.NotFound("未注册的服务: %s" % name)
        st = self._status[name]
        if st["state"] == "running":
            return st
        t0 = time.time()
        try:
            if callable(spec.start):
                spec.start()
            st.update(state="running", error="", started_at=time.time())
            logs.timed("service.start", t0, service=name)
        except Exception as e:
            st.update(state="failed", error="%s: %s" % (type(e).__name__, e))
            logs.error("服务启动异常", e, service=name)
            raise
        return st

    def stop(self, name):
        spec = self._specs.get(name)
        if spec is None:
            raise errors.NotFound("未注册的服务: %s" % name)
        t0 = time.time()
        try:
            if callable(spec.stop):
                spec.stop()
        except Exception as e:
            logs.warn("服务停止异常", service=name,
                      err="%s: %s" % (type(e).__name__, e))
        self._status[name].update(state="stopped", ms=int((time.time() - t0) * 1000))
        logs.timed("service.stop", t0, service=name)
        return self._status[name]

    def stop_all(self):
        stopped = []
        for n in reversed(self._order(list(self._specs.keys()))):
            if self._status.get(n, {}).get("state") == "running":
                try:
                    self.stop(n)
                    stopped.append(n)
                except Exception:
                    pass
        return stopped

    def restart(self, name):
        self.stop(name)
        return self.start(name)

    def health(self, name=None):
        names = [name] if name else list(self._specs.keys())
        out = {}
        for n in names:
            spec = self._specs.get(n)
            if spec is None:
                out[n] = {"state": "missing"}
                continue
            st = dict(self._status[n])
            result = None
            if callable(spec.health):
                try:
                    result = spec.health()
                except Exception as e:
                    result = {"ok": False,
                              "error": "%s: %s" % (type(e).__name__, e)}
            st["checks"] += 1
            st["health"] = result if result is not None else (
                "unknown" if not callable(spec.health) else True)
            out[n] = st
        return out

    def status(self):
        return {n: dict(s) for n, s in self._status.items()}
