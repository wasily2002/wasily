"""远程 / 合并 / worktree 隔离的回归测试。

全部使用本地 bare 仓库作为远端，不访问网络，也不依赖系统 git。
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

from git_core import GitError, GitService


def _write(path: str, data) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(data)


def _read(path: str) -> str:
    # 检出时的换行由平台/配置决定，断言只关心内容，统一成 LF 再比较。
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read().replace("\r\n", "\n")


class GitRemoteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="wasily_remote_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.environ["WASILY_AUDIT_DIR"] = os.path.join(self.tmp, "_audit")
        os.environ["WASILY_WORKTREE_DIR"] = os.path.join(self.tmp, "_wt")

        self.remote = os.path.join(self.tmp, "remote.git")
        bare = dulwich.porcelain.init(self.remote, bare=True)
        bare.close()

        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.work)
        repo = dulwich.porcelain.init(self.work)
        repo.close()
        self._add_origin(self.work)

        self.branch = GitService.branches(self.work)["current"] or "master"
        self._commit(self.work, "chore: 初始化", "a.txt", "a\n")

    # ---------- 工具 ----------

    def _add_origin(self, path: str) -> None:
        repo = dulwich.repo.Repo(path)
        try:
            cfg = repo.get_config()
            cfg.set((b"remote", b"origin"), b"url", self.remote.encode("utf-8"))
            cfg.set((b"remote", b"origin"), b"fetch",
                    b"+refs/heads/*:refs/remotes/origin/*")
            cfg.write_to_path()
        finally:
            repo.close()

    def _commit(self, path: str, message: str, name: str, data: str) -> dict:
        expected_head = GitService.status(path, fresh=True)["head"]
        _write(os.path.join(path, name), data)
        GitService.stage(path, [name])
        return GitService.commit(path, message, expected_head=expected_head)

    # ---------- 远程 ----------

    def test_clone_rejects_url_credentials(self) -> None:
        with self.assertRaises(GitError) as ctx:
            GitService.clone("https://user:pass@example.com/a.git",
                             os.path.join(self.tmp, "x"))
        self.assertEqual(ctx.exception.code, "url_has_credentials")

    def test_push_then_clone(self) -> None:
        res = GitService.push(self.work, self.branch)
        self.assertTrue(res["ok"])
        self.assertTrue(res["head"])
        self.assertTrue(res["verified"], "推送后应能与远端 ref 对上")
        dest = os.path.join(self.tmp, "cloned")
        cloned = GitService.clone(self.remote, dest, self.branch)
        self.assertTrue(cloned["ok"])
        self.assertEqual(_read(os.path.join(dest, "a.txt")), "a\n")

    def test_fetch_reports_remote_refs(self) -> None:
        GitService.push(self.work, self.branch)
        refs = GitService.fetch(self.work)["refs"]
        self.assertTrue(any(k.endswith(self.branch) for k in refs))

    def test_pull_fast_forward(self) -> None:
        GitService.push(self.work, self.branch)
        other = os.path.join(self.tmp, "other")
        GitService.clone(self.remote, other, self.branch)

        self._commit(self.work, "feat: 第二版", "a.txt", "b\n")
        GitService.push(self.work, self.branch)

        res = GitService.pull(other, "origin", self.branch, ff_only=True)
        self.assertTrue(res["changed"])
        self.assertEqual(_read(os.path.join(other, "a.txt")), "b\n")

    def test_pull_diverged_is_rejected(self) -> None:
        GitService.push(self.work, self.branch)
        other = os.path.join(self.tmp, "other")
        GitService.clone(self.remote, other, self.branch)

        self._commit(self.work, "feat: 本地分支", "a.txt", "local\n")
        self._commit(other, "feat: 远端分支", "a.txt", "remote\n")
        GitService.push(self.work, self.branch)

        with self.assertRaises(GitError) as ctx:
            GitService.pull(other, "origin", self.branch, ff_only=True)
        self.assertIn(ctx.exception.code, ("diverged", "pull_failed"))

    # ---------- 合并 ----------

    def test_merge_creates_two_parents(self) -> None:
        GitService.create_branch(self.work, "feature/m")
        GitService.switch(self.work, "feature/m")
        self._commit(self.work, "feat: 功能文件", "f.txt", "f\n")
        GitService.switch(self.work, self.branch)
        self._commit(self.work, "fix: 主干文件", "m.txt", "m\n")

        res = GitService.merge(self.work, "feature/m", "chore: 合并功能")
        self.assertTrue(res["ok"])
        repo = dulwich.repo.Repo(self.work)
        try:
            commit = repo.object_store[bytes.fromhex(res["commit"])]
            parents = len(commit.parents)
        finally:
            repo.close()
        self.assertEqual(parents, 2)

    def test_conflict_then_abort_restores_clean_tree(self) -> None:
        self._commit(self.work, "feat: 冲突基座", "c.txt", "base\n")
        GitService.create_branch(self.work, "feature/c")
        GitService.switch(self.work, "feature/c")
        self._commit(self.work, "feat: 功能侧", "c.txt", "feature\n")
        GitService.switch(self.work, self.branch)
        self._commit(self.work, "fix: 主干侧", "c.txt", "main\n")

        res = GitService.merge(self.work, "feature/c")
        self.assertFalse(res["ok"])
        self.assertIn("c.txt", res["conflicts"])

        conf = GitService.merge_conflicts(self.work)
        self.assertIn("c.txt", conf["paths"])

        GitService.merge_abort(self.work)
        self.assertTrue(GitService.status(self.work)["clean"])
        self.assertEqual(_read(os.path.join(self.work, "c.txt")), "main\n")

    def test_merge_requires_clean_tree(self) -> None:
        GitService.create_branch(self.work, "feature/d")
        _write(os.path.join(self.work, "dirty.txt"), "x\n")
        with self.assertRaises(GitError) as ctx:
            GitService.merge(self.work, "feature/d")
        self.assertEqual(ctx.exception.code, "dirty_tree")

    # ---------- 并行隔离 ----------

    def test_worktree_create_list_remove(self) -> None:
        GitService.create_branch(self.work, "feature/wt")
        res = GitService.worktree_create(self.work, "wt1", "feature/wt")
        self.assertTrue(os.path.isdir(res["path"]))

        listed = GitService.worktree_list(self.work)["worktrees"]
        self.assertTrue(any(os.path.abspath(w["path"]) == os.path.abspath(res["path"])
                            for w in listed))

        GitService.worktree_remove(self.work, res["path"])
        self.assertFalse(os.path.isdir(res["path"]))

    def test_worktree_remove_blocks_dirty(self) -> None:
        GitService.create_branch(self.work, "feature/dirty")
        res = GitService.worktree_create(self.work, "wt2", "feature/dirty")
        _write(os.path.join(res["path"], "dirty.txt"), "x\n")
        with self.assertRaises(GitError) as ctx:
            GitService.worktree_remove(self.work, res["path"])
        self.assertEqual(ctx.exception.code, "worktree_dirty")


if __name__ == "__main__":
    unittest.main()
