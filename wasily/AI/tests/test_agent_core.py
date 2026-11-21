# -*- coding: utf-8 -*-
"""agent 统一管理层的单元 + 集成测试。"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import agent  # noqa: E402
import mcp_hub  # noqa: E402
import tools  # noqa: E402
from agent import errors, lifecycle, permissions, registry, router, state  # noqa: E402


class TestErrors(unittest.TestCase):
    def test_envelope(self):
        out = errors.envelope("ok", 7)
        self.assertTrue(out["ok"])
        self.assertEqual(out["ms"], 7)

    def test_from_exception_known(self):
        out = errors.from_exception(errors.NotFound("没找到"))
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "not_found")

    def test_from_exception_unknown(self):
        out = errors.from_exception(KeyError("kb_search"))
        self.assertEqual(out["error"]["code"], "internal_error")
        self.assertIn("kb_search", out["error"]["message"])


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.reg = registry.ResourceRegistry()

    def _res(self, name, kind="tool", source="a", desc=""):
        return registry.Resource(id="", kind=kind, name=name,
                                description=desc, source=source)

    def test_register_and_get(self):
        self.reg.register(self._res("file_read"))
        self.assertEqual(self.reg.get("tool:file_read").name, "file_read")

    def test_conflict_not_overwritten(self):
        self.reg.register(self._res("kb_search", source="user_tools"))
        second = self.reg.register(self._res("kb_search", source="mcp:anythingllm"))
        self.assertEqual(second.source, "user_tools")
        self.assertTrue(second.conflict)
        self.assertEqual(self.reg.stats()["conflicts"], ["tool:kb_search"])

    def test_search_and_stats(self):
        self.reg.register(self._res("kb_search", desc="知识库检索"))
        self.reg.register(self._res("说明", kind="skill"))
        self.assertEqual(len(self.reg.search("知识库")), 1)
        st = self.reg.stats()
        self.assertEqual(st["by_kind"]["tool"], 1)
        self.assertEqual(st["by_kind"]["skill"], 1)

    def test_bad_kind(self):
        with self.assertRaises(errors.InvalidArgument):
            self.reg.register(self._res("x", kind="unknown"))


class TestPermissions(unittest.TestCase):
    def test_caps(self):
        p = permissions.PermissionPolicy("viewer")
        self.assertEqual(p.required_cap("file_read"), "read")
        self.assertEqual(p.required_cap("file_write"), "write")
        self.assertEqual(p.required_cap("shell_exec"), "exec")
        self.assertEqual(p.required_cap("mouse_click"), "exec")

    def test_denied(self):
        p = permissions.PermissionPolicy("viewer")
        with self.assertRaises(errors.PermissionDenied):
            p.check("file_write")
        p.check("file_read")

    def test_path_scope(self):
        with tempfile.TemporaryDirectory() as d:
            p = permissions.PermissionPolicy("operator", roots=[d])
            p.check_path(os.path.join(d, "a.txt"))
            self.assertIn(os.path.realpath(d), p.to_dict()["roots"])
            with self.assertRaises(errors.PermissionDenied):
                p.check_path(os.path.dirname(d))


class TestState(unittest.TestCase):
    def test_update_and_events(self):
        st = state.StateStore()
        v0 = st.version
        st.update({"a": 1})
        self.assertGreater(st.version, v0)
        self.assertEqual(st.get("a"), 1)
        evs = st.since(0)
        self.assertEqual(evs[-1]["event"], "state.updated")

    def test_subscribe(self):
        st = state.StateStore()
        token, q = st.subscribe()
        st.publish("ping", {"n": 1})
        ev = q.get_nowait()
        self.assertEqual(ev["event"], "ping")
        st.unsubscribe(token)
        self.assertEqual(st.stats()["subscribers"], 0)


class TestLifecycle(unittest.TestCase):
    def test_order_and_cycle(self):
        lm = lifecycle.LifecycleManager()
        calls = []
        lm.register(lifecycle.ServiceSpec("db", start=lambda: calls.append("db")))
        lm.register(lifecycle.ServiceSpec("web", start=lambda: calls.append("web"),
                                          depends=["db"]))
        lm.start_all()
        self.assertEqual(calls, ["db", "web"])

        lm2 = lifecycle.LifecycleManager()
        lm2.register(lifecycle.ServiceSpec("a", depends=["b"]))
        lm2.register(lifecycle.ServiceSpec("b", depends=["a"]))
        with self.assertRaises(errors.DependencyMissing):
            lm2.start_all()

    def test_missing_dependency(self):
        lm = lifecycle.LifecycleManager()
        lm.register(lifecycle.ServiceSpec("web", depends=["nope"]))
        with self.assertRaises(errors.DependencyMissing):
            lm.start_all()

    def test_health_and_restart(self):
        lm = lifecycle.LifecycleManager()
        counter = {"n": 0}

        def start():
            counter["n"] += 1

        lm.register(lifecycle.ServiceSpec("svc", start=start,
                                          health=lambda: {"ok": True}))
        lm.start("svc")
        lm.restart("svc")
        self.assertEqual(counter["n"], 2)
        self.assertTrue(lm.health("svc")["svc"]["health"]["ok"])
        self.assertEqual(lm.stop_all(), ["svc"])

    def test_start_failure_recorded(self):
        lm = lifecycle.LifecycleManager()

        def boom():
            raise RuntimeError("炸了")

        lm.register(lifecycle.ServiceSpec("bad", start=boom))
        lm.start_all()
        self.assertEqual(lm.status()["bad"]["state"], "failed")
        self.assertIn("炸了", lm.status()["bad"]["error"])


class TestRouter(unittest.TestCase):
    def setUp(self):
        self.reg = registry.ResourceRegistry()
        self.reg.register(registry.Resource(id="", kind="tool", name="file_read",
                                            source="t"))
        self.reg.register(registry.Resource(id="", kind="config", name="demo",
                                            source="c"))
        self.lm = lifecycle.LifecycleManager()
        self.store = state.StateStore()
        self.policy = permissions.PermissionPolicy("operator")
        self.rt = router.Router(self.reg, self.lm, self.store, self.policy)
        self.tmp = tempfile.TemporaryDirectory()
        self.file = os.path.join(self.tmp.name, "a.txt")
        with open(self.file, "w", encoding="utf-8") as f:
            f.write("hello agent\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse(self):
        self.assertEqual(router.Router.parse("file_read"), ("tool", "file_read"))
        self.assertEqual(router.Router.parse("mcp:blender.blender_status"),
                         ("mcp", "blender.blender_status"))
        with self.assertRaises(errors.InvalidArgument):
            router.Router.parse("bad:xxx")
        with self.assertRaises(errors.InvalidArgument):
            router.Router.parse("")

    def test_tool_success(self):
        out = self.rt.call("tool:file_read", {"path": self.file})
        self.assertTrue(out["ok"])
        self.assertIn("hello agent", out["result"])

    def test_tool_not_found(self):
        out = self.rt.call("tool:no_such_tool", {})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "not_found")

    def test_permission_denied(self):
        self.rt.policy = permissions.PermissionPolicy("viewer")
        out = self.rt.call("tool:file_write", {"path": self.file, "content": "x"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "permission_denied")

    def test_path_scope(self):
        self.rt.policy = permissions.PermissionPolicy("operator", roots=[self.tmp.name])
        out = self.rt.call("tool:file_read", {"path": "C:/Windows/win.ini"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "permission_denied")

    def test_service_action(self):
        self.rt.register_action("demo", "ping", lambda n=1: {"pong": n})
        out = self.rt.call("service:demo.ping", {"n": 2})
        self.assertTrue(out["ok"])
        self.assertEqual(out["result"], {"pong": 2})
        miss = self.rt.call("service:demo.nope", {})
        self.assertFalse(miss["ok"])
        self.assertEqual(miss["error"]["code"], "not_found")

    def test_mcp_format(self):
        out = self.rt.call("mcp:blender", {})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "invalid_argument")

    def test_last_call_state(self):
        self.rt.call("tool:file_read", {"path": self.file})
        self.assertTrue(self.store.get("last_call")["ok"])


class TestIntegration(unittest.TestCase):
    def test_bootstrap_registers_project(self):
        snap = agent.bootstrap(force=True)
        self.assertTrue(snap["ok"])
        self.assertGreater(snap["registry"]["total"], 50)
        kinds = snap["registry"]["by_kind"]
        self.assertGreater(kinds.get("tool", 0), 20)
        self.assertGreater(kinds.get("skill", 0), 5)
        self.assertGreater(kinds.get("service", 0), 3)

    def test_real_tool_call(self):
        agent.bootstrap()
        out = agent.call("tool:file_read", {"path": "version.json"})
        self.assertTrue(out["ok"])
        self.assertIn("version", out["result"])

    def test_describe_skill(self):
        agent.bootstrap()
        items = agent.search("", "skill", 1)
        self.assertTrue(items)
        d = agent.describe("skill:" + items[0]["name"])
        self.assertEqual(d["kind"], "skill")

    def test_health_contains_services(self):
        agent.bootstrap()
        h = agent.health()
        self.assertIn("tools", h)
        self.assertTrue(h["tools"]["health"]["ok"])

    def test_policy_switch(self):
        old = agent.POLICY.role
        try:
            agent.set_policy(role="viewer", persist=False)
            out = agent.call("tool:shell_exec", {"command": "echo hi"})
            self.assertFalse(out["ok"])
            self.assertEqual(out["error"]["code"], "permission_denied")
        finally:
            agent.set_policy(role=old, persist=False)

    def test_default_roots_cover_project(self):
        agent.bootstrap()
        self.assertTrue(agent.POLICY.roots)
        self.assertIn(os.path.abspath(agent.PROJECT_ROOT),
                      [os.path.abspath(r) for r in agent.POLICY.roots])

    def test_events_flow(self):
        agent.bootstrap()
        agent.call("tool:file_read", {"path": "version.json"})
        evs = agent.events(0, 20)
        self.assertTrue(any(e["event"] == "state.updated" for e in evs))


class FakeHandler:
    def __init__(self):
        self.status = None
        self.payload = None

    def _send_json(self, status, payload):
        self.status = status
        self.payload = payload


class TestHttpApi(unittest.TestCase):
    def setUp(self):
        import agent.api as agent_api
        self.api = agent_api

    def test_handles(self):
        self.assertTrue(self.api.handles_get("/v1/agent/status"))
        self.assertTrue(self.api.handles_post("/v1/agent/call"))
        self.assertFalse(self.api.handles_get("/v1/agent/nope"))

    def test_status(self):
        h = FakeHandler()
        self.assertTrue(self.api.handle_get(h, "/v1/agent/status"))
        self.assertEqual(h.status, 200)
        self.assertTrue(h.payload["ok"])

    def test_registry_query(self):
        h = FakeHandler()
        self.api.handle_get(h, "/v1/agent/registry", "q=file_read")
        self.assertGreaterEqual(h.payload["count"], 1)

    def test_call_tool(self):
        h = FakeHandler()
        self.api.handle_post(h, "/v1/agent/call",
                             {"capability": "tool:file_read",
                              "args": {"path": "version.json"}})
        self.assertEqual(h.status, 200)
        self.assertTrue(h.payload["ok"])

    def test_call_bad_capability(self):
        h = FakeHandler()
        self.api.handle_post(h, "/v1/agent/call", {"capability": "bad:xxx"})
        self.assertEqual(h.status, 400)
        self.assertFalse(h.payload["ok"])

    def test_policy_endpoint(self):
        h = FakeHandler()
        self.api.handle_post(h, "/v1/agent/policy",
                             {"role": "editor", "persist": False})
        self.assertEqual(h.payload["policy"]["role"], "editor")
        agent.set_policy(role="operator", persist=False)


class TestRegressions(unittest.TestCase):
    """对应控制台截图中出现的 KeyError: 'kb_search' 与「重载 0 工具」。"""

    def test_user_tool_prompt_survives_removed_tool(self):
        name = "kb_search"
        entry = tools.TOOLS.get(name)
        if entry is None:
            self.skipTest("当前环境未加载 kb_search 用户工具")
        tools.TOOLS.pop(name, None)
        try:
            text = tools.get_user_tools_prompt()
        finally:
            tools.TOOLS[name] = entry
        self.assertIsInstance(text, str)

    def test_mcp_does_not_override_builtin_tool(self):
        hub = mcp_hub.MCPHub()
        hub.servers = {
            "fake": {"name": "fake", "cfg": {"namespace": ""},
                     "transport": "stdio", "connected": True,
                     "tools": [{"name": "file_read", "inputSchema": {"properties": {}}},
                               {"name": "zzz_unique_tool",
                                "inputSchema": {"properties": {}}}],
                     "readonly": {}},
        }
        reg = hub.tool_registry()
        self.assertIn("zzz_unique_tool", reg)
        self.assertNotIn("file_read", reg)
        self.assertFalse(reg["zzz_unique_tool"].get("_readonly", False))
        self.assertTrue(reg["zzz_unique_tool"]["_mcp"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
