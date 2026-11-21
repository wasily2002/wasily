# -*- coding: utf-8 -*-
"""内置应用 AI 集成测试：Office 读写、代码编辑、视频抽帧、MCP 注册。"""
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import kb_apps  # noqa: E402

SERVER = os.path.join(BASE, "kb_mcp_server.py")


class AppTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.patches = [
            mock.patch.object(kb_apps, "allowed_roots", return_value=[self.root]),
            mock.patch.object(kb_apps, "BACKUP_DIR",
                              new=__import__("pathlib").Path(self.root) / "_backup"),
            mock.patch.object(kb_apps, "FRAME_DIR",
                              new=__import__("pathlib").Path(self.root) / "_frames"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()


class TestOffice(AppTestBase):
    def test_docx_read_edit(self):
        import docx
        path = os.path.join(self.root, "方案.docx")
        doc = docx.Document()
        doc.add_paragraph("原始内容：旧版本")
        doc.save(path)

        res = kb_apps.office_read(path)
        self.assertTrue(res["ok"])
        self.assertIn("原始内容", res["text"])
        self.assertTrue(res["text"])

        edit = kb_apps.office_edit(path, [
            {"op": "replace_text", "search": "旧版本", "replace": "新版本"},
            {"op": "append_paragraph", "text": "新增的结论段落"},
        ])
        self.assertTrue(edit["ok"])
        self.assertTrue(os.path.exists(edit["backup"]))
        text = "\n".join(p.text for p in docx.Document(path).paragraphs)
        self.assertIn("新版本", text)
        self.assertIn("新增的结论段落", text)

    def test_xlsx_edit(self):
        import openpyxl
        path = os.path.join(self.root, "数据.xlsx")
        wb = openpyxl.Workbook()
        wb.active["A1"] = "指标"
        wb.save(path)
        res = kb_apps.office_edit(path, [
            {"op": "set_cell", "cell": "B2", "value": 42},
            {"op": "append_row", "values": ["新增", 1, 2]},
            {"op": "create_sheet", "sheet": "汇总"},
        ])
        self.assertTrue(res["ok"])
        wb2 = openpyxl.load_workbook(path)
        self.assertEqual(wb2.active["B2"].value, 42)
        self.assertIn("汇总", wb2.sheetnames)

    def test_pptx_add_slide(self):
        from pptx import Presentation
        path = os.path.join(self.root, "演示.pptx")
        prs = Presentation()
        prs.save(path)
        before = len(Presentation(path).slides)
        res = kb_apps.office_edit(path, [
            {"op": "add_slide", "title": "能力总线", "bullets": ["存储", "摘要", "工具"]}
        ])
        self.assertTrue(res["ok"])
        self.assertEqual(len(Presentation(path).slides), before + 1)

    def test_reject_unsupported(self):
        path = os.path.join(self.root, "a.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("x")
        with self.assertRaises(kb_apps.AppError):
            kb_apps.office_edit(path, [{"op": "replace_text", "search": "x",
                                        "replace": "y"}])


class TestCode(AppTestBase):
    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.root, "demo.py")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("def hello():\n    return 'hi'\n")

    def test_read_range(self):
        res = kb_apps.code_read(self.path, start=1, end=1)
        self.assertIn("def hello", res["text"])
        self.assertEqual(res["total_lines"], 2)

    def test_edit_replace_and_append(self):
        res = kb_apps.code_edit(self.path, [
            {"op": "replace", "search": "'hi'", "replace": "'hello'"},
            {"op": "append", "text": "print(hello())"},
        ])
        self.assertTrue(res["changed"])
        self.assertTrue(os.path.exists(res["backup"]))
        with open(self.path, encoding="utf-8") as f:
            body = f.read()
        self.assertIn("'hello'", body)
        self.assertIn("print(hello())", body)

    def test_dry_run_does_not_write(self):
        with open(self.path, encoding="utf-8") as f:
            before = f.read()
        res = kb_apps.code_edit(self.path, [
            {"op": "replace", "search": "hello", "replace": "bye"}], dry_run=True)
        self.assertTrue(res["changed"])
        self.assertTrue(res["dry_run"])
        self.assertEqual(res["backup"], "")
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), before)

    def test_search(self):
        res = kb_apps.code_search("hello", root=self.root)
        self.assertTrue(res["count"] >= 1)
        self.assertEqual(res["results"][0]["match"], "content")

    def test_outside_root_denied(self):
        with self.assertRaises(kb_apps.AppError):
            kb_apps.code_read(os.path.join(self.root, "..", "outside.txt"))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "需要 ffmpeg/ffprobe")
class TestVideo(AppTestBase):
    def test_info_and_frames(self):
        path = os.path.join(self.root, "clip.mp4")
        r = subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i",
             "testsrc=duration=1:size=160x120:rate=10", "-pix_fmt", "yuv420p", path],
            capture_output=True, timeout=60)
        if r.returncode != 0:
            self.skipTest("本机 ffmpeg 无法生成测试视频")
        info = kb_apps.video_info(path)
        self.assertTrue(info["ok"])
        self.assertGreater(info["duration"], 0)
        frames = kb_apps.video_frames(path, count=3, width=160)
        self.assertEqual(frames["count"], 3)
        for f in frames["frames"]:
            self.assertTrue(os.path.exists(f["path"]))


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


class TestMcpAppTools(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = McpClient()

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def test_tools_registered(self):
        res = self.client.request("tools/list")
        names = {t["name"] for t in res["result"]["tools"]}
        self.assertTrue({"app_office_read", "app_office_edit", "app_video_info",
                         "app_video_frames", "app_code_read", "app_code_edit",
                         "app_code_search"} <= names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
