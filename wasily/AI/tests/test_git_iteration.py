"""自迭代闭环测试：隔离工作区 → 校验 → 提交 → 轨迹。

这组用例固定住闭环最容易出问题的三件事：
1. 校验不通过时绝不能提交；
2. 校验之后文件再改动，提交必须被拒绝（防止「验 A 提 B」）；
3. 失败现场要保留，状态要能恢复。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dulwich.porcelain
import dulwich.repo

from git_core import GitError, iteration
from git_core.repository import repo_id


def _write(path: str, data: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(data)


class IterationLoopTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="wasily_iter_")
        self.audit = tempfile.mkdtemp(prefix="wasily_iter_audit_")
        self.wt = tempfile.mkdtemp(prefix="wasily_iter_wt_")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(shutil.rmtree, self.audit, True)
        self.addCleanup(shutil.rmtree, self.wt, True)
        os.environ["WASILY_AUDIT_DIR"] = self.audit
        os.environ["WASILY_WORKTREE_DIR"] = self.wt
        repo = dulwich.porcelain.init(self.root)
        _write(os.path.join(self.root, "app.py"), "VALUE = 1\n")
        dulwich.porcelain.add(repo, paths=["app.py"])
        dulwich.porcelain.commit(repo, message="chore: 初始化".encode("utf-8"))
        dulwich.porcelain.branch_create(repo, b"develop")
        repo.close()

    def _start(self, requirement: str = "增加健康检查接口") -> dict:
        res = iteration.start(self.root, requirement)
        self.assertTrue(res["ok"])
        return res["iteration"]

    def _write_wt(self, state: dict, name: str, data: str) -> None:
        _write(os.path.join(state["worktree"], name), data)

    # ---------- 正常闭环 ----------

    def test_start_creates_branch_and_isolated_worktree(self) -> None:
        state = self._start()
        self.assertEqual(state["state"], "prepared")
        self.assertEqual(state["base"], "develop")
        self.assertTrue(state["branch"].startswith("feature/iter-"))
        self.assertTrue(os.path.isdir(state["worktree"]))
        self.assertNotEqual(os.path.abspath(state["worktree"]),
                            os.path.abspath(self.root))
        # 主工作区不应出现迭代文件
        self.assertFalse(os.path.exists(os.path.join(self.root, "health.py")))

    def test_verify_then_commit(self) -> None:
        state = self._start()
        self._write_wt(state, "health.py", "def ping():\n    return 'ok'\n")
        res = iteration.verify(self.root, state["id"])
        self.assertTrue(res["ok"], res.get("reason"))
        self.assertEqual(res["iteration"]["state"], "verified")
        self.assertTrue(any(c["name"] == "syntax:health.py" for c in res["checks"]))
        done = iteration.commit(self.root, state["id"],
                                "feat(loop): 增加健康检查", push=False)
        self.assertEqual(done["iteration"]["state"], "committed")
        self.assertTrue(done["commit"])
        self.assertEqual([f["path"] for f in done["files"]], ["health.py"])
        # 提交后隔离目录应干净
        repo = dulwich.repo.Repo(state["worktree"])
        try:
            st = dulwich.porcelain.status(repo)
        finally:
            repo.close()
        staged = st.staged or {}
        self.assertFalse(any(staged.get(k) for k in ("add", "delete", "modify")))
        self.assertFalse(st.unstaged)
        self.assertFalse(st.untracked)

    def test_audit_trail_is_structured(self) -> None:
        state = self._start("补充审计字段")
        self._write_wt(state, "audit_extra.py", "X = 2\n")
        iteration.verify(self.root, state["id"])
        iteration.commit(self.root, state["id"], "feat(loop): 补充审计字段", push=False)
        trail = iteration.list_iterations(self.root)
        self.assertEqual(trail["iterations"][0]["state"], "committed")
        rid = repo_id(os.path.abspath(self.root))
        import git_core.audit as audit
        events = audit.read_events(rid, 50)
        kinds = [e.get("operation") for e in events]
        self.assertIn("iteration_start", kinds)
        self.assertIn("verify", kinds)
        self.assertIn("iteration_commit", kinds)
        last = [e for e in events if e.get("operation") == "iteration_commit"][-1]
        self.assertEqual(last["requirement"], "补充审计字段")
        self.assertTrue(last["files"])

    # ---------- 质量闸门 ----------

    def test_syntax_error_blocks_commit(self) -> None:
        state = self._start()
        self._write_wt(state, "broken.py", "def broken(:\n    pass\n")
        res = iteration.verify(self.root, state["id"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["iteration"]["state"], "prepared")
        with self.assertRaises(GitError) as ctx:
            iteration.commit(self.root, state["id"], "feat(loop): 破坏性提交", push=False)
        self.assertEqual(ctx.exception.code, "iteration_not_verified")
        # 现场必须保留，方便人去看
        self.assertTrue(os.path.isdir(state["worktree"]))
        self.assertTrue(os.path.exists(os.path.join(state["worktree"], "broken.py")))

    def test_change_after_verify_is_rejected(self) -> None:
        state = self._start()
        self._write_wt(state, "value.py", "V = 1\n")
        self.assertTrue(iteration.verify(self.root, state["id"])["ok"])
        self._write_wt(state, "value.py", "V = 2\n")
        with self.assertRaises(GitError) as ctx:
            iteration.commit(self.root, state["id"], "feat(loop): 偷偷改动", push=False)
        self.assertEqual(ctx.exception.code, "verification_stale")

    def test_bad_commit_message_rejected(self) -> None:
        state = self._start()
        self._write_wt(state, "x.py", "X = 1\n")
        iteration.verify(self.root, state["id"])
        with self.assertRaises(GitError) as ctx:
            iteration.commit(self.root, state["id"], "改了点东西", push=False)
        self.assertEqual(ctx.exception.code, "bad_message")

    def test_no_checkable_files_is_not_a_pass(self) -> None:
        state = self._start()
        self._write_wt(state, "notes.md", "# 说明\n")
        res = iteration.verify(self.root, state["id"])
        self.assertFalse(res["ok"])
        self.assertIn("跳过的检查不算通过", res["reason"])

    # ---------- 现场与恢复 ----------

    def test_abort_removes_worktree(self) -> None:
        state = self._start()
        self._write_wt(state, "tmp.py", "Y = 1\n")
        res = iteration.abort(self.root, state["id"], force=True)
        self.assertTrue(res["removed"])
        self.assertEqual(res["iteration"]["state"], "aborted")
        self.assertFalse(os.path.isdir(state["worktree"]))

    def test_recover_lists_unfinished_iteration(self) -> None:
        state = self._start()
        self._write_wt(state, "pending.py", "Z = 1\n")
        rec = iteration.recover(self.root)
        self.assertEqual(rec["count"], 1)
        item = rec["pending"][0]
        self.assertEqual(item["id"], state["id"])
        self.assertTrue(item["live"]["worktree_exists"])
        self.assertFalse(item["live"]["drift"])

    def test_iteration_scoped_to_repository(self) -> None:
        state = self._start()
        other = tempfile.mkdtemp(prefix="wasily_other_")
        self.addCleanup(shutil.rmtree, other, True)
        with self.assertRaises(GitError) as ctx:
            iteration.get(other, state["id"])
        self.assertEqual(ctx.exception.code, "not_a_repo")


if __name__ == "__main__":
    unittest.main()
