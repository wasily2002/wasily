# -*- coding: utf-8 -*-
"""训练数据闭环测试：采集 → 清洗 → 标注 → 导出 → 反馈。"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import train_data  # noqa: E402


class TestTrainLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.paths = {
            "TRAIN_DIR": os.path.join(root, "training"),
            "EVENTS": os.path.join(root, "training", "events.jsonl"),
            "DATASET": os.path.join(root, "training", "dataset.jsonl"),
            "LABELED": os.path.join(root, "training", "labeled.jsonl"),
            "TRAIN": os.path.join(root, "training", "train.jsonl"),
            "VAL": os.path.join(root, "training", "val.jsonl"),
            "FEEDBACK": os.path.join(root, "training", "feedback.jsonl"),
            "MANIFEST": os.path.join(root, "training", "manifest.json"),
            "SESSIONS_DIR": os.path.join(root, "sessions"),
        }
        self.patch = mock.patch.multiple(train_data, **self.paths)
        self.patch.start()
        os.makedirs(self.paths["SESSIONS_DIR"])
        self._session("s1", [
            ("请解释知识库的检索流程", "知识库先用混合检索召回候选，再按分块重排，" * 3),
            ("帮我总结这份报告", "报告包含背景、方法、结论三个部分，" * 3),
        ])
        self._session("s2", [
            ("帮我总结这份报告", "报告包含背景、方法、结论三个部分，" * 3),
            ("短回答测试", "好"),
        ])
        self._session("s3", [
            ("执行修改文件的工具", '已处理：{"name": "app_code_edit", "parameters": {}}'
             " 并完成写入，" * 3),
        ])

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def _session(self, sid, pairs):
        msgs = []
        for q, a in pairs:
            msgs.append({"role": "user", "content": q})
            msgs.append({"role": "assistant", "content": a})
        with open(os.path.join(self.paths["SESSIONS_DIR"], sid + ".json"),
                  "w", encoding="utf-8") as f:
            json.dump({"sid": sid, "updated": 1795000000, "messages": msgs}, f,
                      ensure_ascii=False)

    def test_collect_incremental(self):
        first = train_data.collect()
        self.assertEqual(first["added"], 5)
        second = train_data.collect()
        self.assertEqual(second["added"], 0)
        self.assertEqual(len(train_data._read_jsonl(self.paths["EVENTS"])), 5)

    def test_clean_dedupe_and_short(self):
        train_data.collect()
        res = train_data.clean(min_chars=8)
        self.assertTrue(res["ok"])
        self.assertGreaterEqual(res["dropped"]["dup"], 1)
        self.assertGreaterEqual(res["dropped"]["short"], 1)
        rows = train_data._read_jsonl(self.paths["DATASET"])
        self.assertTrue(rows)
        self.assertTrue(all(len(r["output"]) >= 8 for r in rows))

    def test_redaction(self):
        train_data.append_event("配置 api_key=sk-abcdef1234567890",
                                "已保存 password=hunter2 并完成")
        train_data.clean()
        rows = train_data._read_jsonl(self.paths["DATASET"])
        joined = json.dumps(rows, ensure_ascii=False)
        self.assertNotIn("sk-abcdef", joined)
        self.assertNotIn("hunter2", joined)
        self.assertIn("[REDACTED]", joined)

    def test_label(self):
        train_data.collect()
        train_data.clean()
        res = train_data.label()
        self.assertTrue(res["ok"])
        rows = train_data._read_jsonl(self.paths["LABELED"])
        self.assertTrue(all("task_type" in r["label"] for r in rows))
        self.assertTrue(any(r["label"]["task_type"] == "code" for r in rows))

    def test_export_split(self):
        train_data.collect()
        train_data.clean()
        train_data.label()
        res = train_data.export(val_ratio=0.5)
        self.assertTrue(res["ok"])
        train = train_data._read_jsonl(self.paths["TRAIN"])
        val = train_data._read_jsonl(self.paths["VAL"])
        self.assertEqual(len(train) + len(val), res["manifest"]["total"])
        self.assertTrue(train or val)
        sample = (train or val)[0]
        self.assertIn("messages", sample)
        self.assertTrue(os.path.isfile(self.paths["MANIFEST"]))

    def test_feedback(self):
        ev = train_data.append_event("问题", "回答内容足够长，用于测试反馈")
        res = train_data.feedback(ev["event"]["id"], 5, "很好")
        self.assertTrue(res["ok"])
        self.assertEqual(train_data.stats()["feedback"], 1)

    def test_pipeline_and_stats(self):
        res = train_data.run_pipeline()
        self.assertTrue(res["ok"])
        st = train_data.stats()
        self.assertGreaterEqual(st["dataset"], 1)
        self.assertGreaterEqual(st["train"] + st["val"], 1)
        self.assertTrue(st["dir"])

    def test_append_requires_content(self):
        with self.assertRaises(ValueError):
            train_data.append_event("", "x")

    def test_rating_validation(self):
        with self.assertRaises(ValueError):
            train_data.feedback("id", "not-a-number")


if __name__ == "__main__":
    unittest.main(verbosity=2)
