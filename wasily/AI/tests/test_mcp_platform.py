# -*- coding: utf-8 -*-
"""MCP 网关与浏览器 MCP 协议测试（离线）。"""
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import mcp_gateway  # noqa: E402

BROWSER_SERVER = os.path.join(BASE, "browser_mcp_server.py")


class FakeHub:
    def __init__(self):
        self.servers = {"kb": {"cfg": {"namespace": "mcp__kb__"},
                               "tools": [{"name": "kb_search",
                                          "description": "检索",
                                          "inputSchema": {"type": "object",
                                                          "properties": {}}}]}}
        self._names = {"mcp__kb__kb_search": ("kb", "kb_search", True)}
        self.reloaded = 0

    def status(self):
        return [{"name": "kb", "connected": True}]

    def reload_tools(self):
        self.reloaded += 1

    def call_tool(self, server, tool, args):
        return "result:%s/%s" % (server, tool)


class FakeHandler:
    def __init__(self, token=None, addr="127.0.0.1"):
        self.headers = {"Authorization": token} if token else {}
        self.client_address = (addr, 12345)


class TestGateway(unittest.TestCase):
    def setUp(self):
        self.hub = FakeHub()
        self.patch = mock.patch.object(mcp_gateway, "_hub", return_value=self.hub)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_initialize(self):
        res = mcp_gateway.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(res["result"]["serverInfo"]["name"], "wasily-capability-bus")

    def test_tools_list(self):
        res = mcp_gateway.rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(res["result"]["tools"][0]["name"], "mcp__kb__kb_search")

    def test_tools_call(self):
        res = mcp_gateway.rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                               "params": {"name": "mcp__kb__kb_search",
                                          "arguments": {"query": "x"}}})
        self.assertEqual(res["result"]["content"][0]["text"], "result:kb/kb_search")
        self.assertFalse(res["result"]["isError"])

    def test_unknown_tool(self):
        res = mcp_gateway.rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                               "params": {"name": "nope", "arguments": {}}})
        self.assertIn("error", res)

    def test_auth(self):
        with mock.patch.object(mcp_gateway, "_token", return_value="secret"):
            self.assertFalse(mcp_gateway.authorized(FakeHandler()))
            self.assertTrue(mcp_gateway.authorized(FakeHandler("Bearer secret")))
        self.assertTrue(mcp_gateway.authorized(FakeHandler()))
        self.assertFalse(mcp_gateway.authorized(FakeHandler(addr="192.168.1.9")))

    def test_register_and_unregister(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mcp.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"mcpServers": {}}, f)
            with mock.patch.object(mcp_gateway, "CONFIG_PATH", path):
                res = mcp_gateway.register_server({
                    "name": "third-party", "transport": "stdio",
                    "command": "python", "args": ["server.py"]})
                self.assertTrue(res["ok"])
                with open(path, encoding="utf-8") as f:
                    cfg = json.load(f)
                self.assertIn("third-party", cfg["mcpServers"])
                self.assertEqual(self.hub.reloaded, 1)
                mcp_gateway.unregister_server("third-party")
                with open(path, encoding="utf-8") as f:
                    cfg = json.load(f)
                self.assertNotIn("third-party", cfg["mcpServers"])

    def test_builtin_name_protected(self):
        with self.assertRaises(ValueError):
            mcp_gateway.register_server({"name": "kb", "transport": "stdio",
                                         "command": "python"})


class McpClient:
    def __init__(self, server):
        self.proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", server], cwd=BASE, text=True,
            encoding="utf-8", stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        self.lines = queue.Queue()
        threading.Thread(target=self._reader, daemon=True).start()
        self.seq = 0

    def _reader(self):
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def request(self, method, params=None, timeout=20):
        self.seq += 1
        mid = self.seq
        self.proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": mid, "method": method,
            "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                line = self.lines.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                break
            if not line:
                break
            msg = json.loads(line)
            if msg.get("id") == mid:
                return msg
        raise TimeoutError("MCP 响应超时：%s" % method)

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            self.proc.kill()


class TestBrowserProtocol(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = McpClient(BROWSER_SERVER)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def test_initialize_and_tools(self):
        init = self.client.request("initialize", {"protocolVersion": "2024-11-05"})
        self.assertEqual(init["result"]["serverInfo"]["name"], "wasily-browser")
        res = self.client.request("tools/list")
        names = {t["name"] for t in res["result"]["tools"]}
        self.assertTrue({"browser_status", "browser_open", "browser_navigate",
                         "browser_snapshot", "browser_click", "browser_fill",
                         "browser_screenshot", "browser_eval",
                         "browser_close"} <= names)

    def test_status_no_launch(self):
        res = self.client.request("tools/call", {
            "name": "browser_status", "arguments": {}})
        payload = json.loads(res["result"]["content"][0]["text"])
        self.assertEqual(payload["driver"], "cdp")
        self.assertIn("executable", payload)

    def test_close_is_safe(self):
        res = self.client.request("tools/call", {
            "name": "browser_close", "arguments": {}})
        payload = json.loads(res["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
