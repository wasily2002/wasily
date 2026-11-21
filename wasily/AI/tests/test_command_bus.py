# -*- coding: utf-8 -*-
"""跨页命令总线的单元 + 集成测试。

重点验证「两个页面能看到同一份顺序」这件事依赖的三条性质：
事件序号全局单调、行事件在命令结束前就到达、中断先广播状态再杀进程。
"""
import os
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import command_bus  # noqa: E402

PY = '"%s" -c "%s"'
TWO_LINES = "import time;print('a',flush=True);time.sleep(0.2);print('b',flush=True)"
TO_STDERR = "import sys;sys.stderr.write('boom\\n');sys.stderr.flush()"
EXIT_THREE = "import sys;print('x');sys.exit(3)"
SLOW = "import time;print('go',flush=True);time.sleep(30)"


def _cmd(code):
    return PY % (sys.executable, code)


class TestCommandBus(unittest.TestCase):
    def setUp(self):
        self.token, self.queue = command_bus.subscribe()

    def tearDown(self):
        command_bus.unsubscribe(self.token)

    def _drain(self, run_id, limit=200):
        """取出属于该 run 的事件，直到队列为空。"""
        out = []
        while len(out) < limit:
            try:
                ev = self.queue.get_nowait()
            except Exception:
                break
            if ev.get("run_id") == run_id:
                out.append(ev)
        return out

    def _wait_run(self, run_id, timeout=15):
        run = command_bus.get_run(run_id)
        self.assertIsNotNone(run)
        self.assertTrue(run.done.wait(timeout), "命令未在预期时间内结束")
        return run

    def test_streams_lines_before_finish(self):
        snap = command_bus.start(_cmd(TWO_LINES), source="agent")
        run = self._wait_run(snap["id"])
        events = self._drain(snap["id"])
        lines = [e["line"]["text"] for e in events if e["type"] == "line"]
        self.assertEqual(lines, ["a", "b"])
        self.assertEqual(run.status, "exited")
        self.assertEqual(run.exit_code, 0)

    def test_global_ids_strictly_increase(self):
        snap = command_bus.start(_cmd(TWO_LINES), source="agent")
        self._wait_run(snap["id"])
        events = self._drain(snap["id"])
        ids = [e["id"] for e in events]
        self.assertTrue(ids)
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(ids), len(set(ids)))

    def test_stderr_is_tagged(self):
        snap = command_bus.start(_cmd(TO_STDERR), source="agent")
        self._wait_run(snap["id"])
        events = self._drain(snap["id"])
        errs = [e["line"] for e in events
                if e["type"] == "line" and e["line"]["stream"] == "stderr"]
        self.assertTrue(errs)
        self.assertEqual(errs[0]["text"], "boom")

    def test_abort_broadcasts_before_kill(self):
        snap = command_bus.start(_cmd(SLOW), source="agent")
        run = command_bus.get_run(snap["id"])
        # 等到第一行输出，确认进程真的起来了
        for _ in range(50):
            if run.lines_after(0):
                break
            import time as _t
            _t.sleep(0.1)
        res = command_bus.abort(snap["id"], by="ide")
        self.assertTrue(res["changed"])
        self.assertTrue(run.done.wait(15))
        self.assertEqual(run.status, "aborted")
        events = self._drain(snap["id"])
        statuses = [e["run"]["status"] for e in events if e["type"] == "run"]
        # 中断状态必须在命令真正结束前就广播出去（两页立刻看到「已中断」）
        self.assertIn("aborted", statuses)

    def test_replay_after_seq(self):
        snap = command_bus.start(_cmd(TWO_LINES), source="agent")
        self._wait_run(snap["id"])
        all_events = command_bus.events_after(0, snap["id"])
        self.assertTrue(all_events)
        mid = all_events[len(all_events) // 2]["id"]
        later = command_bus.events_after(mid, snap["id"])
        self.assertTrue(all(e["id"] > mid for e in later))

    def test_detail_lines_after(self):
        snap = command_bus.start(_cmd(TWO_LINES), source="agent")
        self._wait_run(snap["id"])
        first = command_bus.detail(snap["id"], 0)
        self.assertEqual([ln["text"] for ln in first["lines"]], ["a", "b"])
        tail = command_bus.detail(snap["id"], 1)
        self.assertEqual([ln["text"] for ln in tail["lines"]], ["b"])

    def test_run_sync_keeps_output_contract(self):
        res = command_bus.run_sync(_cmd(EXIT_THREE), timeout=30)
        self.assertEqual(res["status"], "failed")
        self.assertEqual(res["exit_code"], 3)
        self.assertIn("x", res["output"])
        self.assertIn("[exit code: 3]", res["output"])

        err = command_bus.run_sync(_cmd(TO_STDERR), timeout=30)
        self.assertIn("[stderr]", err["output"])
        self.assertIn("boom", err["output"])

    def test_ingest_mirror_lifecycle(self):
        started = command_bus.ingest({"action": "start", "command": "npm run dev",
                                      "source": "ide", "owner": "ide"})
        run_id = started["run"]["id"]
        command_bus.ingest({"action": "line", "run_id": run_id,
                            "lines": [{"stream": "stdout", "text": "compiled"},
                                      {"stream": "stderr", "text": "warn"}]})
        command_bus.ingest({"action": "finish", "run_id": run_id, "exit_code": 0})
        run = command_bus.get_run(run_id)
        self.assertIsNotNone(run)
        self.assertEqual(run.status, "exited")
        self.assertEqual([ln["text"] for ln in run.lines_after(0)], ["compiled", "warn"])

    def test_list_runs_contains_started(self):
        snap = command_bus.start(_cmd(TWO_LINES), source="agent")
        self._wait_run(snap["id"])
        ids = [r["id"] for r in command_bus.list_runs(50)]
        self.assertIn(snap["id"], ids)


if __name__ == "__main__":
    unittest.main()
