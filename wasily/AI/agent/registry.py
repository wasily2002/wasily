# -*- coding: utf-8 -*-
"""agent 资源注册表：把项目里的工具、MCP 服务、技能、配置项、模块统一登记。

设计要点：
- 只读登记，不改动既有实现；所有条目都带 source，能追溯回原文件/模块。
- 同名冲突不覆盖，标记 conflict，交给上层决策（避免 MCP 工具覆盖 user_tools）。
- 支持按 kind / group / 关键字检索，供路由和前端展示。
"""
from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field, asdict

from . import errors

AI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS_DIR = os.path.join(AI_DIR, "skills")

KINDS = ("tool", "service", "skill", "config", "module")


@dataclass
class Resource:
    id: str                      # 全局唯一：<kind>:<name>
    kind: str
    name: str
    description: str = ""
    group: str = ""
    readonly: bool = True
    source: str = ""
    enabled: bool = True
    conflict: bool = False
    meta: dict = field(default_factory=dict)

    def to_dict(self):
        d = asdict(self)
        d["meta"] = dict(self.meta)
        return d


class ResourceRegistry:
    """线程安全的资源登记表。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._items = {}          # id -> Resource
        self._by_name = {}        # name -> [id]
        self._version = 0

    # ---------- 写入 ----------
    def register(self, res: Resource, replace=False):
        if res.kind not in KINDS:
            raise errors.InvalidArgument("未知资源类型: %s" % res.kind)
        if not res.name:
            raise errors.InvalidArgument("资源名不能为空")
        res.id = "%s:%s" % (res.kind, res.name)
        with self._lock:
            old = self._items.get(res.id)
            if old is not None and not replace:
                if old.source != res.source:
                    old.conflict = True
                    return old
                return old
            self._items[res.id] = res
            self._by_name.setdefault(res.name, [])
            if res.id not in self._by_name[res.name]:
                self._by_name[res.name].append(res.id)
            self._version += 1
            return res

    def unregister(self, res_id):
        with self._lock:
            res = self._items.pop(res_id, None)
            if res is None:
                return False
            ids = self._by_name.get(res.name) or []
            if res_id in ids:
                ids.remove(res_id)
            if not ids:
                self._by_name.pop(res.name, None)
            self._version += 1
            return True

    def clear(self):
        with self._lock:
            self._items.clear()
            self._by_name.clear()
            self._version += 1

    # ---------- 查询 ----------
    def get(self, res_id):
        return self._items.get(res_id)

    def find_by_name(self, name, kind=None):
        out = []
        for rid in self._by_name.get(name, []):
            res = self._items.get(rid)
            if res and (kind is None or res.kind == kind):
                out.append(res)
        return out

    def list(self, kind=None, group=None, enabled_only=False):
        items = list(self._items.values())
        if kind:
            items = [r for r in items if r.kind == kind]
        if group:
            items = [r for r in items if r.group == group]
        if enabled_only:
            items = [r for r in items if r.enabled]
        return sorted(items, key=lambda r: (r.kind, r.group, r.name))

    def search(self, keyword, kind=None, limit=50):
        kw = (keyword or "").strip().lower()
        if not kw:
            return self.list(kind)[:limit]
        out = []
        for r in self.list(kind):
            hay = " ".join((r.name, r.id, r.description, r.group)).lower()
            if kw in hay:
                out.append(r)
                if len(out) >= limit:
                    break
        return out

    def stats(self):
        with self._lock:
            counts = {}
            for r in self._items.values():
                counts[r.kind] = counts.get(r.kind, 0) + 1
            conflicts = [r.id for r in self._items.values() if r.conflict]
            return {"total": len(self._items), "by_kind": counts,
                    "conflicts": conflicts, "version": self._version}

    @property
    def version(self):
        return self._version


# ---------- 自动登记 ----------
def _tool_group(name):
    """按名字前缀粗分组，仅用于展示。"""
    for p in ("ide_", "kb_", "app_", "blender_", "browser_", "train_",
              "fsr_", "video_", "self_", "mcp__"):
        if name.startswith(p):
            return p.rstrip("_")
    return "core"


def _skill_meta(path):
    """从技能 md 的 YAML 头读取 name / triggers / description。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            head = f.read(2000)
    except OSError:
        return None
    m = re.search(r"^\s*(\{.*\})\s*$", head, re.M)
    if not m:
        return None
    import json
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def sync_project(registry, include_skills=True, include_config=True):
    """把现有项目资源登记进 registry，返回本次登记数量。

    只读取，不触发 MCP 连接、不导入重型模块以外的副作用。
    """
    added = 0

    # 1) 工具：tools.TOOLS
    try:
        import tools
        for name, spec in list(tools.TOOLS.items()):
            if not isinstance(spec, dict):
                continue
            is_mcp = bool(spec.get("_mcp"))
            registry.register(Resource(
                id="", kind="tool", name=name,
                description=str(spec.get("description") or "")[:200],
                group=_tool_group(name),
                readonly=bool(spec.get("_readonly", not is_mcp)),
                source="tools.TOOLS",
                meta={"mcp": is_mcp,
                      "params": sorted((spec.get("parameters") or {}).keys())},
            ))
            added += 1
    except Exception as e:
        from . import logs
        logs.warn("登记工具失败", err="%s: %s" % (type(e).__name__, e))

    # 2) 服务：以 mcp.json 配置为准登记，运行状态尽量合并（连接不上也不丢条目）
    try:
        import mcp_hub
        runtime = {s.get("name"): s for s in (mcp_hub.status() or [])}
        cfg = mcp_hub.load_config() or {}
        for name, scfg in (cfg.get("mcpServers") or {}).items():
            if not isinstance(scfg, dict):
                continue
            rt = runtime.get(name) or {}
            registry.register(Resource(
                id="", kind="service", name=name,
                description="MCP %s，工具 %d 个" % (scfg.get("transport", "stdio"),
                                                len(rt.get("tools") or [])),
                group="mcp",
                readonly=False,
                source="mcp.json",
                enabled=bool(scfg.get("enabled", True)),
                meta={"transport": scfg.get("transport", "stdio"),
                      "namespace": scfg.get("namespace"),
                      "connected": bool(rt.get("connected")),
                      "error": rt.get("error") or "",
                      "tools": len(rt.get("tools") or [])},
            ))
            added += 1
    except Exception:
        pass

    # 3) 技能：skills/*.md
    if include_skills and os.path.isdir(SKILLS_DIR):
        for fn in sorted(os.listdir(SKILLS_DIR)):
            if not fn.endswith(".md"):
                continue
            meta = _skill_meta(os.path.join(SKILLS_DIR, fn)) or {}
            registry.register(Resource(
                id="", kind="skill", name=meta.get("name") or fn[:-3],
                description=str(meta.get("description") or "")[:200],
                group="skill", readonly=True, source="skills/" + fn,
                meta={"triggers": meta.get("triggers") or []},
            ))
            added += 1

    # 4) 配置项：config.json 顶层键
    if include_config:
        try:
            import config as cfg
            data = cfg.load() if hasattr(cfg, "load") else {}
            if isinstance(data, dict):
                for k, v in data.items():
                    registry.register(Resource(
                        id="", kind="config", name=k,
                        description="配置项，类型 %s" % type(v).__name__,
                        group="config", readonly=False,
                        source="config.json",
                        meta={"value_type": type(v).__name__},
                    ))
                    added += 1
        except Exception:
            pass

    return added
