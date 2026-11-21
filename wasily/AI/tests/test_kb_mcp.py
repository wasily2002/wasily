# -*- coding: utf-8 -*-
"""知识库 MCP 能力总线测试（离线优先）。

运行：
    python tests/test_kb_mcp.py -v
    set RUN_PERF=1 && python tests/test_kb_mcp.py -v   # 追加真实索引检索压测
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

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import kb_digest  # noqa: E402
import kb_files  # noqa: E402

SERVER = os.path.join(BASE, "kb_mcp_server.py")
PY = sys.executable


class TestDigest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "项目说明.txt")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("Wasily 知识库支持全类型文件检索。\n"
                    "本次升级新增了 MCP 能力总线。\n"
                    "摘要功能可以按查询生成结构化要点。\n"
                    "报告功能可以把多个文件聚合为一份文档。\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_document_from_parser(self):
        doc = kb_digest.read_document(path=self.path)
        self.assertTrue(doc["ok"])
        self.assertEqual(doc["source"], "parser")
        self.assertIn("MCP 能力总线", doc["text"])

    def test_extractive_summary_prefers_query(self):
        doc = kb_digest.read_document(path=self.path)
        points = kb_digest.extractive_summary(doc["text"], "报告", max_points=2)
        self.assertTrue(points)
        self.assertTrue(any("报告" in p for p in points))

    def test_summarize_structure(self):
        res = kb_digest.summarize(path=self.path, query="摘要", mode="extractive",
                                  max_points=3)
        self.assertTrue(res["ok"])
        self.assertEqual(res["mode"], "extractive")
        self.assertEqual(res["name"], "项目说明.txt")
        self.assertTrue(res["overview"])
        self.assertLessEqual(len(res["key_points"]), 3)
        self.assertIn("keywords", res)

    def test_multi_file_aggregate(self):
        second = os.path.join(self.tmp.name, "部署.md")
        with open(second, "w", encoding="utf-8") as f:
            f.write("部署流程包含后端启动与 MCP 注册。\n"
                    "MCP 能力总线提供统一调用入口。\n")
        res = kb_digest.summarize_many(paths=[self.path, second], query="MCP",
                                       mode="extractive", max_points=2)
        self.assertTrue(res["ok"])
        self.assertEqual(res["count"], 2)
        self.assertTrue(res["summary"])
        self.assertTrue(res["themes"])

    def test_report_markdown(self):
        res = kb_digest.build_report("能力总线报告", paths=[self.path],
                                     mode="extractive", save=False)
        self.assertTrue(res["ok"])
        self.assertIn("# 能力总线报告", res["content"])
        self.assertEqual(res["files"], 1)


class TestNameSearch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        for name, data in (("季度总结.mp3", b"ID3"), ("预算表.xlsx", b"PK"),
                           ("演示视频.mp4", b"\x00" * 8)):
            with open(os.path.join(self.root, name), "wb") as f:
                f.write(data)

    def tearDown(self):
        self.tmp.cleanup()

    def test_all_types_by_name(self):
        res = kb_files.search_names("季度", roots=[self.root])
        self.assertTrue(res["ok"])
        self.assertEqual(res["results"][0]["kind"], "audio")

    def test_kind_filter(self):
        res = kb_files.search_names("", roots=[self.root], kinds=["video"])
        # 空查询应当直接拒绝，避免全盘扫描
        self.assertFalse(res["ok"])
        res = kb_files.search_names("演示", roots=[self.root], kinds=["video"])
        self.assertEqual(len(res["results"]), 1)

    def test_supported_kinds(self):
        kinds = {k["kind"] for k in kb_files.supported_kinds()}
        self.assertTrue({"audio", "video", "archive", "code", "table"} <= kinds)


class McpClient:
    """最小 stdio MCP 客户端，带超时读取，避免测试卡死。"""

    def __init__(self):
        self.proc = subprocess.Popen(
            [PY, "-X", "utf8", SERVER], cwd=BASE, text=True, encoding="utf-8",
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
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


class TestMcpProtocol(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = McpClient()

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def test_01_initialize(self):
        res = self.client.request("initialize", {"protocolVersion": "2024-11-05"})
        self.assertEqual(res["result"]["serverInfo"]["name"], "wasily-kb")

    def test_02_tools_list(self):
        res = self.client.request("tools/list")
        names = {t["name"] for t in res["result"]["tools"]}
        self.assertTrue({"kb_search", "kb_summarize", "kb_summarize_many",
                         "kb_report", "kb_name_search", "kb_types"} <= names)

    def test_03_call_types(self):
        res = self.client.request("tools/call", {
            "name": "kb_types", "arguments": {}})
        payload = json.loads(res["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"])
        self.assertTrue(any(k["kind"] == "video" for k in payload["kinds"]))

    def test_04_call_name_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            fp = os.path.join(tmp, "发布会录像.mp4")
            with open(fp, "wb") as f:
                f.write(b"\x00" * 16)
            res = self.client.request("tools/call", {
                "name": "kb_name_search",
                "arguments": {"query": "发布会", "limit": 5}})
            payload = json.loads(res["result"]["content"][0]["text"])
            self.assertTrue(payload["ok"])

    def test_05_unknown_tool_is_error(self):
        res = self.client.request("tools/call", {
            "name": "no_such_tool", "arguments": {}})
        self.assertTrue(res["result"]["isError"])

    @unittest.skipUnless(os.environ.get("RUN_PERF") == "1", "设置 RUN_PERF=1 启用")
    def test_06_search_latency_via_mcp(self):
        """MCP 通道额外开销：与直连共用同一个 kb_search，仅多一层 JSON-RPC。"""
        t0 = time.time()
        self._call_search("wasily")
        warm_ms = (time.time() - t0) * 1000
        samples = []
        for q in ("项目", "报告", "文档"):
            t0 = time.time()
            self._call_search(q)
            samples.append((time.time() - t0) * 1000)
        print("\n[perf] mcp warmup=%.0fms steady ms=%s"
              % (warm_ms, [round(x) for x in samples]))
        self.assertLess(max(samples), 15000)

    def _call_search(self, query):
        res = self.client.request("tools/call", {
            "name": "kb_search", "arguments": {"query": query, "limit": 5}},
            timeout=120)
        payload = json.loads(res["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"])


class TestIndexCompatibility(unittest.TestCase):
    """存量索引只读兼容性：状态统计与分块读取不能改变既有语义。"""

    def test_status_counts_fast(self):
        import kb_store
        st = kb_store.get_store()
        t0 = time.time()
        counts = st.status_counts()
        elapsed = time.time() - t0
        self.assertIn("indexed_files", counts)
        self.assertIn("chunks", counts)
        self.assertLess(elapsed, 2.0, "status_counts 超过 2 秒，疑似走了全表扫描")

    @unittest.skipUnless(os.environ.get("RUN_PERF") == "1", "设置 RUN_PERF=1 启用")
    def test_search_latency(self):
        import kb_search
        # 首次查询包含整库向量缓存加载与嵌入模型拉起，单独计时说明，
        # 不计入稳态样本——存量系统本身也是这个行为。
        t0 = time.time()
        kb_search.search("wasily", limit=5)
        warm_ms = (time.time() - t0) * 1000
        samples = []
        for q in ("wasily", "项目", "报告", "文档", "配置"):
            t0 = time.time()
            kb_search.search(q, limit=5)
            samples.append((time.time() - t0) * 1000)
        p95 = sorted(samples)[-1]
        self.assertLess(p95, 15000, "稳态单次检索超过 15 秒")
        print("\n[perf] warmup=%.0fms steady ms=%s p95=%.0f"
              % (warm_ms, [round(x) for x in samples], p95))


if __name__ == "__main__":
    unittest.main(verbosity=2)
