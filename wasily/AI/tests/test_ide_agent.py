# -*- coding: utf-8 -*-
"""IDE 智能体链路测试：模式归一化、桥工具、工具注册。"""
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import ide_agent  # noqa: E402
import tools  # noqa: E402


class FakeBridge(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *args):  # noqa: D401
        return

    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/context"):
            self._json(200, {"ok": True, "file": "demo.py",
                             "diagnostics": ["demo.py:2 未定义变量 x"]})
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        FakeBridge.calls.append((self.path, body))
        if self.path == "/read_file":
            self._json(200, {"ok": True, "text": "line1\nline2\nline3",
                             "saved": False})
        elif self.path == "/open_file":
            self._json(200, {"ok": True, "detail": "已打开 " + body.get("path", "")})
        elif self.path == "/edit_file":
            self._json(200, {"ok": True, "detail": "已编辑并保存"})
        elif self.path == "/run_terminal":
            self._json(200, {"ok": True, "output": "Tests: 3 passed",
                             "exitCode": 0, "detail": "已执行"})
        else:
            self._json(404, {"ok": False, "error": "not found"})


class TestIdeMode(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(ide_agent.normalize_mode("ide", "ide"), "solo")
        self.assertEqual(ide_agent.normalize_mode("ide", ""), "solo")
        self.assertEqual(ide_agent.normalize_mode("ide", "agent"), "solo")
        self.assertEqual(ide_agent.normalize_mode("ide", "chat"), "ask")
        self.assertEqual(ide_agent.normalize_mode("web", "plan"), "plan")
        self.assertEqual(ide_agent.normalize_mode("web", ""), None)
        self.assertEqual(ide_agent.normalize_mode("web", "ide"), None)

    def test_rounds(self):
        self.assertEqual(ide_agent.max_rounds_for("ide", "solo", 3), 12)
        self.assertEqual(ide_agent.max_rounds_for("ide", "solo", 20), 20)
        self.assertEqual(ide_agent.max_rounds_for("ide", "solo", 0), 0)
        self.assertEqual(ide_agent.max_rounds_for("web", "solo", 3), 3)

    def test_prompt_gate(self):
        self.assertTrue(ide_agent.ide_prompt_enabled("ide", "solo"))
        self.assertFalse(ide_agent.ide_prompt_enabled("ide", "ask"))
        self.assertIn("ide_read_file", ide_agent.IDE_AGENT_PROMPT)
        self.assertIn("ide_diagnostics", ide_agent.IDE_AGENT_PROMPT)


class TestBridgeTools(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeBridge)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.tmp = tempfile.TemporaryDirectory()
        tools.set_workspace_root(cls.tmp.name)
        cls.file = os.path.join(cls.tmp.name, "demo.py")
        with open(cls.file, "w", encoding="utf-8") as f:
            f.write("line1\nline2\nline3\n")
        cls.patch = mock.patch.object(tools, "IDE_BRIDGE",
                                      "http://127.0.0.1:%d" % cls.port)
        cls.patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.patch.stop()
        cls.server.shutdown()
        cls.tmp.cleanup()

    def setUp(self):
        FakeBridge.calls.clear()

    def test_read_buffer_numbered(self):
        out = tools.ide_read_file(self.file, 2, 1)
        self.assertIn("2| line2", out)
        self.assertIn("未保存", out)
        self.assertEqual(FakeBridge.calls[0][0], "/read_file")

    def test_open_and_edit(self):
        out = tools.ide_open_file(self.file, 2)
        self.assertIn("已打开", out)
        out = tools.ide_edit_file(self.file, "line2", "LINE2")
        self.assertIn("已编辑", out)
        payload = [c for c in FakeBridge.calls if c[0] == "/edit_file"][0][1]
        self.assertEqual(payload["old_string"], "line2")
        self.assertEqual(payload["new_string"], "LINE2")

    def test_edit_full_content(self):
        tools.ide_edit_file(self.file, content="整文件替换")
        payload = [c for c in FakeBridge.calls if c[0] == "/edit_file"][0][1]
        self.assertEqual(payload["new_string"], "整文件替换")
        self.assertEqual(payload["old_string"], "")

    def test_run_terminal(self):
        out = tools.ide_run_terminal("pytest -q")
        self.assertIn("Tests: 3 passed", out)
        self.assertIn("exit=0", out)

    def test_diagnostics(self):
        out = tools.ide_diagnostics(self.file)
        self.assertIn("未定义变量", out)
        self.assertIn("/open_file", [c[0] for c in FakeBridge.calls])

    def test_offline_bridge(self):
        with mock.patch.object(tools, "IDE_BRIDGE", "http://127.0.0.1:9"):
            out = tools.ide_open_file(self.file)
        self.assertIn("IDE 桥未连接", out)


class TestRegistry(unittest.TestCase):
    def test_tools_registered(self):
        for name in ("ide_open_file", "ide_read_file", "ide_edit_file",
                     "ide_run_terminal", "ide_diagnostics"):
            self.assertIn(name, tools.TOOLS)
            self.assertTrue(callable(tools.TOOLS[name]["function"]))

    def test_desktop_visible_for_solo(self):
        names = tools.enabled_tool_names("", "solo", 0)
        self.assertIn("screenshot", names)
        self.assertIn("mouse_click", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
