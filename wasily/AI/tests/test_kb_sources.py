# -*- coding: utf-8 -*-
"""统一存储适配器测试：本地全链路 + 其他驱动离线自检 + MCP 注册。

网络型存储（SSH/WebDAV/S3）只验证「缺配置时优雅失败」，不做真实连接，
避免测试依赖外部环境。真实连通性由 kb_sources(include_status=true) 自检。
"""
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

import kb_sources  # noqa: E402

SERVER = os.path.join(BASE, "kb_mcp_server.py")


class TestLocalSource(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, "docs"))
        with open(os.path.join(self.root, "docs", "方案.md"), "w",
                  encoding="utf-8") as f:
            f.write("统一存储适配器支持本地、WSL、SSH 与云盘。\n摘要走知识库链路。\n")
        with open(os.path.join(self.root, "预算.xlsx"), "wb") as f:
            f.write(b"PK\x03\x04")
        self.src = kb_sources.LocalSource({
            "id": "t-local", "type": "local", "name": "测试本地",
            "options": {"roots": [self.root]}})

    def tearDown(self):
        self.tmp.cleanup()

    def test_status(self):
        st = self.src.test()
        self.assertTrue(st["ok"])

    def test_list_root(self):
        res = self.src.list("")
        names = {e["name"] for e in res["entries"]}
        self.assertTrue({"docs", "预算.xlsx"} <= names)

    def test_read(self):
        data = self.src.read_bytes(os.path.join(self.root, "docs", "方案.md"))
        self.assertIn("统一存储适配器", kb_sources.decode_bytes(data))

    def test_search_names(self):
        res = self.src.search("方案")
        self.assertEqual(res["results"][0]["name"], "方案.md")

    def test_path_traversal_denied(self):
        with self.assertRaises(kb_sources.SourceError):
            self.src.read_bytes(os.path.join(self.root, "..", "outside.txt"))

    def test_summarize_via_source(self):
        with mock.patch.object(kb_sources, "get_source", return_value=self.src):
            res = kb_sources.source_summarize(
                "t-local", os.path.join(self.root, "docs", "方案.md"),
                query="摘要", mode="extractive", max_points=2)
        self.assertTrue(res["ok"])
        self.assertEqual(res["source_id"], "t-local")
        self.assertTrue(res["key_points"])


class TestRemoteDriversOffline(unittest.TestCase):
    def test_wsl_missing_distro(self):
        src = kb_sources.WslSource({"id": "w", "type": "wsl",
                                    "options": {"distro": ""}})
        st = src.test()
        self.assertFalse(st["ok"])
        self.assertTrue(st["message"])

    def test_ssh_missing_host(self):
        src = kb_sources.SshSource({"id": "s", "type": "ssh", "options": {}})
        self.assertFalse(src.test()["ok"])

    def test_webdav_missing_url(self):
        src = kb_sources.WebdavSource({"id": "d", "type": "webdav", "options": {}})
        self.assertFalse(src.test()["ok"])

    def test_s3_missing_bucket(self):
        src = kb_sources.S3Source({"id": "b", "type": "s3", "options": {}})
        self.assertFalse(src.test()["ok"])

    def test_all_types_registered(self):
        self.assertEqual(set(kb_sources.DRIVERS),
                         {"local", "wsl", "ssh", "webdav", "s3"})


class TestRegistry(unittest.TestCase):
    def test_add_and_remove_without_touching_config(self):
        rows = []

        def fake_specs():
            return list(rows)

        def fake_save(new_rows):
            rows[:] = new_rows

        with mock.patch.object(kb_sources, "_specs", side_effect=fake_specs), \
                mock.patch.object(kb_sources, "_save", side_effect=fake_save):
            kb_sources.add_source({"id": "nas", "type": "webdav",
                                   "name": "NAS", "options": {}})
            self.assertEqual(kb_sources.list_sources()["sources"][0]["id"], "nas")
            kb_sources.remove_source("nas")
            self.assertEqual(kb_sources.list_sources()["sources"], [])

    def test_unknown_type_rejected(self):
        with mock.patch.object(kb_sources, "_specs", return_value=[]), \
                mock.patch.object(kb_sources, "_save"):
            with self.assertRaises(kb_sources.SourceError):
                kb_sources.add_source({"id": "x", "type": "ftp"})

    def test_get_source_unknown(self):
        with self.assertRaises(kb_sources.SourceError):
            kb_sources.get_source("no-such")


class McpClient:
    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", SERVER], cwd=BASE, text=True,
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


class TestMcpSourceTools(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = McpClient()

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def test_tools_registered(self):
        res = self.client.request("tools/list")
        names = {t["name"] for t in res["result"]["tools"]}
        self.assertTrue({"kb_sources", "kb_source_manage", "kb_source_list",
                         "kb_source_read", "kb_source_search",
                         "kb_source_summarize"} <= names)

    def test_sources_list(self):
        res = self.client.request("tools/call", {
            "name": "kb_sources", "arguments": {"include_status": False}})
        payload = json.loads(res["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"])
        self.assertIn("local", payload["types"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
