"""跨端同步总线测试：顺序、增量、幂等、并发、断线重放与一致性核对。

为什么用模块级隔离：会话总线是进程内单例，测试必须显式清空，
否则上一个用例的事件会污染下一个用例的重放断言。
"""
from __future__ import annotations

import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import session_bus  # noqa: E402


def reset():
    with session_bus._LOCK:                     # noqa: SLF001 — 测试隔离需要直接清空
        session_bus._EVENTS.clear()             # noqa: SLF001
        session_bus._SESS.clear()               # noqa: SLF001
        session_bus._SUBS.clear()               # noqa: SLF001
        session_bus._SEQ[0] = 0                 # noqa: SLF001


class TestOrderAndIncrement(unittest.TestCase):
    def setUp(self):
        reset()

    def test_ids_monotonic_and_replay_has_no_duplicate(self):
        for i in range(50):
            session_bus.publish("report", session_id="s1", source="ide", kind="command",
                                title="t%d" % i)
        evs = session_bus.events_after(0)
        ids = [e["id"] for e in evs]
        self.assertEqual(ids, list(range(1, 51))
                         if ids[0] == 1 else sorted(ids))
        self.assertEqual(len(ids), len(set(ids)))
        # 增量重放：after 之后的必须是严格递增且不重复
        tail = session_bus.events_after(20)
        self.assertTrue(all(e["id"] > 20 for e in tail))
        self.assertEqual([e["id"] for e in tail], sorted([e["id"] for e in tail]))

    def test_slow_subscriber_does_not_block_publishers(self):
        token, q = session_bus.subscribe()
        self.assertEqual(len(q.queue), 0)
        for i in range(session_bus.SUB_QUEUE_SIZE + 500):
            session_bus.publish("report", session_id="s2", source="ide", kind="command")
        # 队列满后丢弃，不抛异常、不阻塞；重连可用 after 重放补齐
        self.assertEqual(len(q.queue), session_bus.SUB_QUEUE_SIZE)
        session_bus.unsubscribe(token)
        self.assertGreaterEqual(len(session_bus.events_after(0)),
                                session_bus.SUB_QUEUE_SIZE)


class TestIdempotentMessages(unittest.TestCase):
    def setUp(self):
        reset()

    def test_user_mid_is_stable_and_not_duplicated(self):
        mid = session_bus.user_mid("s1", "你好")
        session_bus.publish("user", session_id="s1", source="agent", msg_id=mid,
                            content="你好")
        again = session_bus.user_mid("s1", "你好")
        self.assertEqual(mid, again)
        session_bus.publish("user", session_id="s1", source="agent", msg_id=again,
                            content="你好")
        snap = session_bus.snapshot("s1")
        self.assertEqual(len(snap["messages"]), 1)
        self.assertEqual(snap["messages"][0]["content"], "你好")

    def test_assistant_same_mid_updates_in_place(self):
        mid = session_bus.assistant_mid()
        session_bus.publish("user", session_id="s2", source="agent",
                            msg_id=session_bus.user_mid("s2", "问题"), content="问题")
        session_bus.publish("assistant", session_id="s2", source="agent",
                            msg_id=mid, content="第一版")
        session_bus.publish("assistant", session_id="s2", source="agent",
                            msg_id=mid, content="定稿", steps=[{"tool": "shell_exec"}],
                            elapsed=1.2)
        snap = session_bus.snapshot("s2")
        self.assertEqual([m["role"] for m in snap["messages"]], ["user", "assistant"])
        self.assertEqual(snap["messages"][1]["content"], "定稿")
        self.assertEqual(snap["messages"][1]["steps"][0]["tool"], "shell_exec")

    def test_session_clear_resets_snapshot(self):
        session_bus.publish("user", session_id="s3", source="agent", msg_id="u:1", content="a")
        session_bus.publish("assistant", session_id="s3", source="agent", msg_id="a:1", content="b")
        session_bus.publish("session_clear", session_id="s3", source="server")
        snap = session_bus.snapshot("s3")
        self.assertEqual(snap["messages"], [])
        # rev 是「快照变更次数」，不是消息条数：user/assistant/clear 各 +1
        self.assertEqual(snap["rev"], 3)


class TestIsolationAndVerify(unittest.TestCase):
    def setUp(self):
        reset()

    def test_snapshots_are_isolated_by_session(self):
        session_bus.publish("user", session_id="a", source="agent", msg_id="u:a", content="A")
        session_bus.publish("user", session_id="b", source="ide", msg_id="u:b", content="B")
        self.assertEqual(len(session_bus.snapshot("a")["messages"]), 1)
        self.assertEqual(session_bus.snapshot("a")["messages"][0]["content"], "A")
        self.assertEqual(len(session_bus.snapshot("b")["messages"]), 1)

    def test_verify_detects_rev_count_hash_and_seq_gaps(self):
        session_bus.publish("user", session_id="v", source="agent", msg_id="u:v", content="X")
        snap = session_bus.snapshot("v")
        ok = session_bus.verify("v", snap["rev"], snap["hash"], len(snap["messages"]), snap["seq"])
        self.assertTrue(ok["ok"])
        bad = session_bus.verify("v", 999, "deadbeef", 0, 0)
        self.assertFalse(bad["ok"])
        self.assertIn("rev", bad["reasons"])
        self.assertIn("hash", bad["reasons"])
        self.assertIn("count", bad["reasons"])
        self.assertIn("seq_behind", bad["reasons"])

    def test_hash_is_stable_for_same_messages(self):
        session_bus.publish("user", session_id="h1", source="agent", msg_id="u:1", content="同")
        session_bus.publish("assistant", session_id="h1", source="agent", msg_id="a:1", content="答")
        h1 = session_bus.snapshot("h1")["hash"]
        session_bus.publish("user", session_id="h2", source="agent", msg_id="u:1", content="同")
        session_bus.publish("assistant", session_id="h2", source="agent", msg_id="a:1", content="答")
        self.assertEqual(h1, session_bus.snapshot("h2")["hash"])

    def test_seed_runs_once_and_keeps_runtime_snapshot(self):
        session_bus.seed("s", [{"role": "user", "content": "旧1"},
                               {"role": "assistant", "content": "旧2"}])
        self.assertEqual(len(session_bus.snapshot("s")["messages"]), 2)
        session_bus.seed("s", [{"role": "user", "content": "不应覆盖"}])
        self.assertEqual(session_bus.snapshot("s")["messages"][0]["content"], "旧1")


class TestConcurrency(unittest.TestCase):
    def setUp(self):
        reset()

    def test_concurrent_publish_ids_unique_and_ordered_by_replay(self):
        errors = []

        def worker(n):
            try:
                for i in range(200):
                    session_bus.publish("report", session_id="c%d" % n,
                                        source="ide", kind="command", n=n, i=i)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertFalse(errors)
        evs = session_bus.events_after(0)
        self.assertEqual(len(evs), 1200)
        ids = [e["id"] for e in evs]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(ids, list(range(1, 1201)))

    def test_reconnect_after_returns_all_missing(self):
        token, q = session_bus.subscribe()
        for i in range(10):
            session_bus.publish("report", session_id="r", source="ide", kind="command")
        got = []
        while not q.empty():
            got.append(q.get_nowait()["id"])
        session_bus.unsubscribe(token)          # 模拟网络断开
        for i in range(5):
            session_bus.publish("report", session_id="r", source="ide", kind="command")
        replay = [e["id"] for e in session_bus.events_after(got[-1])]
        self.assertEqual(replay, list(range(got[-1] + 1, got[-1] + 6)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
