# -*- coding: utf-8 -*-
"""agent 统一调用路由。

对外只暴露一个入口：router.call("tool:file_read", path="...")。
能力标识格式：
    tool:<name>              普通工具（tools.execute_tool）
    mcp:<server>.<tool>      MCP 工具（mcp_hub.call_tool）
    service:<name>.<action>  注册在 lifecycle 的服务动作
    skill:<name>             技能元数据查询（不执行）
    config:<key>             读取配置项
不带前缀时按普通工具名处理，便于与现有调用方式兼容。
"""
from __future__ import annotations

import time

from . import errors, logs, permissions

KINDS = ("tool", "mcp", "service", "skill", "config")


class Router:
    def __init__(self, registry, lifecycle, store, policy=None):
        self.registry = registry
        self.lifecycle = lifecycle
        self.store = store
        self.policy = policy or permissions.PermissionPolicy()
        self._actions = {}       # "service.name.action" -> callable

    # ---------- 注册服务动作 ----------
    def register_action(self, service, action, fn):
        key = "%s.%s" % (service, action)
        if not callable(fn):
            raise errors.InvalidArgument("动作必须是可调用对象: %s" % key)
        self._actions[key] = fn
        return key

    def actions(self):
        return sorted(self._actions.keys())

    # ---------- 解析 ----------
    @staticmethod
    def parse(capability):
        cap = str(capability or "").strip()
        if not cap:
            raise errors.InvalidArgument("capability 不能为空")
        if ":" in cap:
            kind, _, rest = cap.partition(":")
            if kind in KINDS and rest:
                return kind, rest
            raise errors.InvalidArgument("未知能力前缀: %s" % kind)
        return "tool", cap

    def describe(self, capability):
        kind, name = self.parse(capability)
        if kind == "skill":
            items = self.registry.find_by_name(name, "skill")
            if not items:
                raise errors.NotFound("未知技能: %s" % name)
            return items[0].to_dict()
        if kind == "config":
            res = self.registry.get("config:" + name)
            if not res:
                raise errors.NotFound("未知配置项: %s" % name)
            return res.to_dict()
        if kind == "tool":
            res = self.registry.get("tool:" + name)
            if res:
                return res.to_dict()
            return {"name": name, "kind": "tool", "registered": False}
        if kind == "mcp":
            return {"name": name, "kind": "mcp",
                    "registered": "mcp:" + name in self.registry._items}
        if kind == "service":
            return {"name": name, "kind": "service",
                    "status": self.lifecycle.status().get(name.split(".")[0])}
        raise errors.InvalidArgument("无法描述的能力: %s" % capability)

    # ---------- 执行 ----------
    def call(self, capability, args=None, timeout=None):
        args = args if isinstance(args, dict) else {}
        kind, name = self.parse(capability)
        t0 = time.time()
        try:
            if kind == "skill":
                out = self.describe(capability)
            elif kind == "config":
                out = self._read_config(name)
            elif kind == "tool":
                out = self._call_tool(name, args)
            elif kind == "mcp":
                out = self._call_mcp(name, args, timeout)
            else:
                out = self._call_service(name, args)
            ms = int((time.time() - t0) * 1000)
            self.store.update({"last_call": {"capability": capability, "ms": ms,
                                             "ok": True}})
            logs.timed("router.call", t0, capability=capability, ok=True)
            return errors.envelope(out, ms)
        except Exception as e:
            ms = int((time.time() - t0) * 1000)
            self.store.update({"last_call": {"capability": capability, "ms": ms,
                                             "ok": False,
                                             "error": str(e)[:200]}})
            logs.error("router.call 失败", e, capability=capability)
            return errors.from_exception(e, ms)

    def _call_tool(self, name, args):
        # 校验注册表：未登记的工具交给 tools 层给出「未知工具 + 可用列表」
        res = self.registry.get("tool:" + name)
        readonly = True if res is None else res.readonly
        self.policy.check(name, readonly)
        self.policy.check_args(args)
        try:
            import tools
        except Exception as e:
            raise errors.UpstreamError("工具层不可用: %s" % e)
        out = tools.execute_tool(name, args)
        if isinstance(out, str) and out.startswith("未知工具"):
            raise errors.NotFound(out)
        return out

    def _call_mcp(self, name, args, timeout=None):
        server, _, tool = name.partition(".")
        if not server or not tool:
            raise errors.InvalidArgument(
                "MCP 能力格式应为 mcp:<server>.<tool>，收到: %s" % name)
        if not self.policy.allow_mcp_write:
            self.policy.check(tool, readonly=True)
        try:
            import mcp_hub
            hub = mcp_hub.active()
        except Exception as e:
            raise errors.UpstreamError("MCP 中枢不可用: %s" % e)
        text = hub.call_tool(server, tool, args)
        if isinstance(text, str) and text.startswith("[MCP 调用失败]"):
            raise errors.UpstreamError(text)
        return text

    def _call_service(self, name, args):
        fn = self._actions.get(name)
        if fn is None:
            raise errors.NotFound("未注册的服务动作: %s" % name)
        if not self.policy.allow_mcp_write:
            self.policy.check(name, readonly=False)
        return fn(**args)

    @staticmethod
    def _read_config(key):
        try:
            import config as cfg
            data = cfg.load() if hasattr(cfg, "load") else {}
        except Exception as e:
            raise errors.UpstreamError("配置读取失败: %s" % e)
        if key not in (data or {}):
            raise errors.NotFound("未知配置项: %s" % key)
        return data[key]
