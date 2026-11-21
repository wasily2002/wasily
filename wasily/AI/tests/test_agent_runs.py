# -*- coding: utf-8 -*-
"""Agent 隔离运行测试，只使用临时仓库与 Dulwich。"""
import concurrent.futures
import os
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import agent_runs as ar  # noqa: E402
import git_bridge as gb  # noqa: E402
import tools  # noqa: E402
from dulwich import porcelain  # noqa: E402


class AgentRunsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent_runs_test_")
        self.root = self.temp.name
        self.repo = os.path.join(self.root, "repo")
        self.state = os.path.join(self.root, "state")
        self.old_home = os.environ.get("WASILY_AGENT_RUN_HOME")
        os.environ["WASILY_AGENT_RUN_HOME"] = self.state
        ar._ACTIVE.clear()
        gb.init(self.repo)
        self.commit_file("a.txt", b"base a\n", "feat: base a")
        self.commit_file("b.txt", b"base b\n", "feat: base b")
        self.base = gb.status(self.repo)["head_sha"]
        self.main_branch = gb.status(self.repo)["branch"]

    def tearDown(self):
        tools.set_workspace_root("")
        ar._ACTIVE.clear()
        if self.old_home is None:
            os.environ.pop("WASILY_AGENT_RUN_HOME", None)
        else:
            os.environ["WASILY_AGENT_RUN_HOME"] = self.old_home
        self.temp.cleanup()

    def path(self, rel, repo=None):
        return os.path.join(repo or self.repo, *rel.split("/"))

    def write(self, rel, data, repo=None):
        full = self.path(rel, repo)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as stream:
            stream.write(data)

    def read(self, rel, repo=None):
        with open(self.path(rel, repo), "rb") as stream:
            return stream.read()

    def commit_file(self, rel, data, message="feat: change", repo=None):
        repo = repo or self.repo
        self.write(rel, data, repo)
        self.assertTrue(gb.stage(repo, [rel], exact=True)["ok"])
        result = gb.commit(repo, message)
        self.assertTrue(result.get("commit"))
        return result["commit"]

    def feature(self):
        if not gb.branches(self.repo).get("current") == "feature/c1":
            self.assertTrue(gb.branch_create(self.repo, "feature/c1")["ok"])
        self.assertTrue(gb.branch_switch(self.repo, "feature/c1")["ok"])

    def start(self, **kwargs):
        run = ar.start(self.repo, "session-c1", "test isolated run", **kwargs)
        self.assertIsNotNone(run)
        self.assertTrue(ar._outside(run["worktree"], self.repo))
        tools.set_workspace_root(run["worktree"], isolated=True)
        return run

    def test_tools_are_confined_and_exact_files_commit_without_touching_primary(self):
        self.feature()
        run = self.start()
        result = tools.execute_tool("file_write", {"path": "a.txt", "content": "worktree\n"})
        self.assertIn("文件已写入", result)
        self.assertEqual(self.read("a.txt"), b"base a\n")
        outside = tools.execute_tool(
            "file_write", {"path": self.path("a.txt"), "content": "primary\n"})
        self.assertIn("禁止", outside)
        self.assertIn("禁止", tools.shell_exec("python ..\\escape.py"))
        self.assertIn("禁止", tools.shell_admin("whoami"))
        cwd = tools.shell_exec("python -c \"import os;print(os.getcwd())\"")
        self.assertIn(os.path.basename(run["worktree"]), cwd)
        os.unlink(self.path("b.txt", run["worktree"]))
        done = ar.finalize(run["id"])
        self.assertEqual(done["status"], "committed", done)
        self.assertEqual(done["files"], ["a.txt", "b.txt"])
        self.assertTrue(done["commit"])
        self.assertEqual(gb.status(self.repo)["head_sha"], self.base)
        self.assertEqual(self.read("a.txt"), b"base a\n")
        manifest = ar.get_run(run["id"], with_events=True)
        self.assertTrue(any(e["action"] == "commit_created" for e in manifest["events"]))
        self.assertTrue(ar._outside(ar._manifest_path(run["id"]), self.repo))

    def test_validation_failure_and_validation_generated_files_fail_closed(self):
        self.feature()
        run = self.start()
        tools.execute_tool("file_write", {"path": "a.txt", "content": "changed\n"})
        failed = ar.finalize(run["id"], validation=[
            "python -c \"import sys;sys.exit(7)\""])
        self.assertEqual(failed["status"], "failed")
        self.assertIn("validation_failed", failed["error"])
        self.assertFalse(failed["commit"])
        self.assertTrue(os.path.isdir(run["worktree"]))
        self.assertEqual(gb.status(self.repo)["head_sha"], self.base)

        run2 = self.start()
        tools.execute_tool("file_write", {"path": "a.txt", "content": "changed again\n"})
        drifted = ar.finalize(run2["id"], validation=[
            "python -c \"open('cache.tmp','w').write('x')\""])
        self.assertEqual(drifted["status"], "failed")
        self.assertIn("validation_changed_files", drifted["error"])
        self.assertFalse(drifted["commit"])

    def test_noop_protected_confirmation_and_reconnect(self):
        self.feature()
        noop = self.start()
        done = ar.finalize(noop["id"])
        self.assertEqual(done["status"], "no_changes")
        self.assertTrue(done["worktree_removed"])

        self.assertTrue(gb.branch_switch(self.repo, self.main_branch)["ok"])
        protected = self.start()
        tools.execute_tool("file_write", {"path": "a.txt", "content": "protected\n"})
        waiting = ar.finalize(protected["id"])
        self.assertEqual(waiting["status"], "awaiting_confirmation")
        loaded = ar.get_run(protected["id"], with_events=True)
        self.assertEqual(loaded["commit"], waiting["commit"])
        self.assertTrue(loaded["events"])
        self.assertEqual(ar.list_runs(session_id="session-c1")[0]["id"], protected["id"])
        confirmed = ar.confirm(protected["id"])
        self.assertEqual(confirmed["status"], "committed")
        self.assertTrue(confirmed.get("confirmed_at"))
        rolled = ar.rollback(protected["id"])
        self.assertEqual(rolled["status"], "rolled_back")
        self.assertTrue(os.path.isdir(protected["worktree"]))

    def test_primary_head_drift_is_rejected(self):
        self.feature()
        run = self.start()
        tools.execute_tool("file_write", {"path": "a.txt", "content": "isolated\n"})
        self.commit_file("b.txt", b"primary moved\n", "feat: primary moved")
        done = ar.finalize(run["id"])
        self.assertEqual(done["status"], "failed")
        self.assertIn("primary_head_changed", done["error"])
        self.assertFalse(done["commit"])
        self.assertEqual(self.read("a.txt"), b"base a\n")

    def test_parallel_workers_keep_distinct_tls_roots(self):
        self.feature()
        runs = [self.start(), self.start()]

        def worker(item):
            index, run = item
            tools.set_workspace_root(run["worktree"], isolated=True)
            name = "parallel-%d.txt" % index
            result = tools.execute_tool("file_write", {"path": name, "content": str(index)})
            return run["id"], name, result, tools.get_workspace_root()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(worker, enumerate(runs)))
        for run_id, name, result, root in results:
            self.assertIn("文件已写入", result)
            self.assertEqual(root, ar.get_run(run_id)["worktree"])
            done = ar.finalize(run_id)
            self.assertEqual(done["status"], "committed")
            self.assertEqual(done["files"], [name])
        self.assertEqual(gb.status(self.repo)["head_sha"], self.base)

    def test_remote_delivery_and_rollback_use_non_force_revert(self):
        self.feature()
        remote = os.path.join(self.root, "remote.git")
        porcelain.init(remote, bare=True).close()
        gb.remote_add(self.repo, "origin", remote)
        self.assertTrue(gb.push(self.repo)["ok"])
        run = self.start()
        tools.execute_tool("file_write", {"path": "a.txt", "content": "remote\n"})
        delivered = ar.finalize(run["id"])
        self.assertEqual(delivered["status"], "delivered", delivered)
        self.assertEqual(delivered["delivery"]["branch"], "feature/c1")
        rolled = ar.rollback(run["id"])
        self.assertEqual(rolled["status"], "rolled_back", rolled)
        self.assertTrue(rolled.get("rollback_commit"))
        self.assertNotEqual(rolled["rollback_commit"], run.get("commit"))
        self.assertTrue(os.path.isdir(run["worktree"]))


if __name__ == "__main__":
    unittest.main()
