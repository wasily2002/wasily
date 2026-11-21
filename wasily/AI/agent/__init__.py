# -*- coding: utf-8 -*-
"""agent 统一管理层门面。

其他模块只需要：
    import agent
    agent.call("tool:file_read", {"path": "..."})
    agent.snapshot()
    agent.health()
不需要再分别 import tools / mcp_hub / kb_* / browser_*。
"""
from __future__ import annotations

import os
import tempfile
import threading

from .errors import AgentError, InvalidArgument, NotFound, PermissionDenied
from .lifecycle import LifecycleManager, ServiceSpec
from .permissions import PermissionPolicy
from .registry import Resource, ResourceRegistry, sync_project
from .router import Router
from .state import STORE

REGISTRY = ResourceRegistry()
LIFECYCLE = LifecycleManager()
POLICY = PermissionPolicy()
ROUTER = Router(REGISTRY, LIFECYCLE, STORE, POLICY)

# 收紧后的默认范围：只允许在项目目录内读写；TEMP 供截图/导出等临时产物使用
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
DEFAULT_ROOTS = [PROJECT_ROOT, tempfile.gettempdir()]

_boot_lock = threading.Lock()
_booted = False


# ---------- 内置服务探测 ----------
def _health_tools():
    import tools
    return {"ok": True, "tools": len(tools.TOOLS)}


def _health_mcp():
    import mcp_hub
    servers = mcp_hub.status() or []
    return {"ok": any(s.get("connected") for s in servers),
            "servers": len(servers),
            "connected": sum(1 for s in servers if s.get("connected"))}


def _health_kb():
    import kb_search
    st = kb_search.stats()
    return {"ok": True, "files": st.get("files"), "chunks": st.get("chunks")}


def _health_blender():
    import socket
    s = socket.socket()
    s.settimeout(0.6)
    try:
        s.connect(("127.0.0.1", 9876))
        return {"ok": True, "port": 9876}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        s.close()


def _health_ide():
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:18753/health",
                                    timeout=0.8) as r:
            return {"ok": r.status == 200}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _register_builtin_services():
    specs = [
        ServiceSpec("tools", health=_health_tools),
        ServiceSpec("mcp", health=_health_mcp),
        ServiceSpec("kb", health=_health_kb, depends=["tools"]),
        ServiceSpec("blender", health=_health_blender),
        ServiceSpec("ide", health=_health_ide),
    ]
    for sp in specs:
        if sp.name not in LIFECYCLE.status():
            LIFECYCLE.register(sp)


def _load_policy():
    """从 config.json 的 agent_policy 读取权限配置；缺失时用收紧后的默认值。"""
    global POLICY
    data = {}
    try:
        import config as cfg
        data = cfg.get("agent_policy", {}) or {}
    except Exception:
        data = {}
    role = str(data.get("role") or "operator")
    roots = data.get("roots")
    if not isinstance(roots, list) or not roots:
        roots = list(DEFAULT_ROOTS)
    POLICY = PermissionPolicy(role=role, roots=roots,
                              allow_mcp_write=bool(data.get("allow_mcp_write", False)))
    ROUTER.policy = POLICY
    return POLICY


def bootstrap(force=False):
    """登记现有资源并注册内置服务；重复调用安全。"""
    global _booted
    with _boot_lock:
        if _booted and not force:
            return snapshot()
        if force:
            REGISTRY.clear()
        count = sync_project(REGISTRY)
        _register_builtin_services()
        _load_policy()
        _booted = True
        STORE.update({"booted_at": _now(), "registered": count})
        return snapshot()


def reload_registry():
    """重新扫描项目资源，不重建服务。"""
    REGISTRY.clear()
    count = sync_project(REGISTRY)
    STORE.update({"registered": count, "reloaded_at": _now()})
    return {"registered": count, "stats": REGISTRY.stats()}


def _now():
    import time
    return int(time.time())


# ---------- 对外接口 ----------
def call(capability, args=None, timeout=None):
    bootstrap()
    return ROUTER.call(capability, args, timeout)


def describe(capability):
    bootstrap()
    return ROUTER.describe(capability)


def search(keyword, kind=None, limit=50):
    bootstrap()
    return [r.to_dict() for r in REGISTRY.search(keyword, kind, limit)]


def resources(kind=None, group=None):
    bootstrap()
    return [r.to_dict() for r in REGISTRY.list(kind, group)]


def health(name=None):
    bootstrap()
    return LIFECYCLE.health(name)


def snapshot():
    if not _booted:
        try:
            bootstrap()
        except Exception:
            pass
    return _snapshot_raw()


def _snapshot_raw():
    return {
        "ok": True,
        "agent": {"version": 1, "booted": _booted},
        "registry": REGISTRY.stats(),
        "lifecycle": LIFECYCLE.status(),
        "state": STORE.stats(),
        "policy": POLICY.to_dict(),
        "tools_total": _safe_count(),
    }


def _safe_count():
    try:
        import tools
        return len(tools.TOOLS)
    except Exception:
        return 0


def set_policy(role=None, roots=None, allow_mcp_write=None, persist=True):
    """切换权限并写回 config.json（agent_policy），重启后依然生效。"""
    global POLICY
    POLICY = PermissionPolicy(
        role=role or POLICY.role,
        roots=roots if roots is not None else POLICY.roots,
        allow_mcp_write=(POLICY.allow_mcp_write if allow_mcp_write is None
                         else allow_mcp_write))
    ROUTER.policy = POLICY
    data = POLICY.to_dict()
    if persist:
        try:
            import config as cfg
            cfg.set_value("agent_policy", data)
        except Exception:
            pass
    STORE.publish("policy.changed", data)
    return data


def events(after_ts=0, limit=50):
    return STORE.since(after_ts, limit)


__all__ = ["AgentError", "InvalidArgument", "NotFound", "PermissionDenied",
           "REGISTRY", "LIFECYCLE", "POLICY", "ROUTER", "STORE",
           "bootstrap", "reload_registry", "call", "describe", "search",
           "resources", "health", "snapshot", "set_policy", "events"]
