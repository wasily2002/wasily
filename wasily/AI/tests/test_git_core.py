"""C1 本地 Git 核心回归测试；只使用 Dulwich，不调用系统 git/gh。"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dulwich.repo
from dulwich.worktree import WorkTree

from git_core import GitError, GitService
from git_core import policy, snapshot


def _write(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if isinstance(data, bytes):
        with open(path, "wb") as f:
            f.write(data)
    else:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(data)


def _read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


class GitCoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="wasily_git_")
        self.extra = tempfile.mkdtemp(prefix="wasily_git_extra_")
        self.audit = tempfile.mkdtemp(prefix="wasily_audit_")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(shutil.rmtree, self.extra, True)
        self.addCleanup(shutil.rmtree, self.audit, True)
        self.old_audit = os.environ.get("WASILY_AUDIT_DIR")
        os.environ["WASILY_AUDIT_DIR"] = self.audit
        self.addCleanup(self._restore_audit)
        GitService.init(self.root)

    def _restore_audit(self) -> None:
        if self.old_audit is None:
            os.environ.pop("WASILY_AUDIT_DIR", None)
        else:
            os.environ["WASILY_AUDIT_DIR"] = self.old_audit

    def _repo(self):
        return dulwich.repo.Repo(self.root)

    def _head(self) -> str:
        return GitService.status(self.root, fresh=True)["head"]

    def _commit(self, message: str = "chore: 添加文件") -> dict:
        return GitService.commit(self.root, message, expected_head=self._head())

    def _commit_file(self, name: str, data,
                     message: str = "chore: 添加文件") -> dict:
        _write(os.path.join(self.root, name), data)
        GitService.stage(self.root, [name])
        return self._commit(message)

    def _side_bytes(self, side: str, rel: str) -> bytes | None:
        repo = self._repo()
        try:
            if side == "head":
                return snapshot.head_bytes(repo, rel)
            return snapshot.index_bytes(repo, rel)
        finally:
            repo.close()

    # ---------- 仓库发现与路径 ----------

    def test_init_open_and_discover_from_subdirectory(self) -> None:
        root = os.path.join(self.extra, "new", "repo")
        made = GitService.init(root)
        self.assertTrue(made["created"])
        nested = os.path.join(root, "a", "b")
        os.makedirs(nested)
        opened = GitService.open(nested)
        self.assertEqual(os.path.realpath(opened["root"]), os.path.realpath(root))
        self.assertEqual(opened["repo_id"], made["repo_id"])

    def test_open_linked_git_file(self) -> None:
        self._commit_file("linked.txt", "linked\n")
        branch = GitService.status(self.root, fresh=True)["branch"]
        common = os.path.join(self.root, ".git")
        meta = os.path.join(common, "worktrees", "linked")
        linked = os.path.join(self.extra, "linked")
        os.makedirs(meta)
        os.makedirs(linked)
        _write(os.path.join(meta, "commondir"), "../..\n")
        _write(os.path.join(meta, "HEAD"), "ref: refs/heads/%s\n" % branch)
        _write(os.path.join(meta, "gitdir"), os.path.join(linked, ".git") + "\n")
        _write(os.path.join(linked, ".git"),
               "gitdir: %s\n" % meta.replace("\\", "/"))
        _write(os.path.join(linked, "linked.txt"), "linked\n")
        opened = GitService.open(linked)
        self.assertEqual(os.path.realpath(opened["root"]), os.path.realpath(linked))
        self.assertEqual(opened["head"], self._head())

    def test_bad_paths_and_symlink_escape_rejected(self) -> None:
        bad = ["../x", "/abs", "C:/x", "a//b", "./x", ".git/config", "a\x00b"]
        for rel in bad:
            with self.subTest(rel=rel), self.assertRaises(GitError) as ctx:
                GitService.content(self.root, rel)
            self.assertEqual(ctx.exception.code, "bad_path")
        outside = os.path.join(self.extra, "outside.txt")
        _write(outside, "secret")
        link = os.path.join(self.root, "link.txt")
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            return
        with self.assertRaises(GitError) as ctx:
            GitService.content(self.root, "link.txt")
        self.assertEqual(ctx.exception.code, "bad_path")

    # ---------- 快照与三种 diff ----------

    def test_empty_file_is_distinct_from_missing(self) -> None:
        _write(os.path.join(self.root, "empty.txt"), b"")
        GitService.stage(self.root, ["empty.txt"])
        item = GitService.diff(self.root, "head_index", "empty.txt")["files"][0]
        self.assertFalse(item["old"]["exists"])
        self.assertTrue(item["new"]["exists"])
        self.assertEqual(item["new"]["size"], 0)
        self.assertTrue(item["new"]["sha"])
        content = GitService.content(self.root, "empty.txt", "index")
        self.assertEqual(content["text"], "")
        with self.assertRaises(GitError) as ctx:
            GitService.content(self.root, "missing.txt", "index")
        self.assertEqual(ctx.exception.code, "not_found")

    def test_three_diff_modes_have_exact_sides(self) -> None:
        self._commit_file("three.txt", b"base\n")
        path = os.path.join(self.root, "three.txt")
        _write(path, b"staged\n")
        GitService.stage(self.root, ["three.txt"])
        _write(path, b"worktree\n")
        cases = {
            "head_index": (b"base\n", b"staged\n"),
            "index_worktree": (b"staged\n", b"worktree\n"),
            "head_worktree": (b"base\n", b"worktree\n"),
        }
        for mode, (old, new) in cases.items():
            with self.subTest(mode=mode):
                result = GitService.diff(self.root, mode, "three.txt")
                self.assertEqual(result["mode"], mode)
                item = result["files"][0]
                self.assertEqual(item["old"]["size"], len(old))
                self.assertEqual(item["new"]["size"], len(new))
                self.assertEqual(item["add"], 1)
                self.assertEqual(item["del"], 1)
                self.assertIn("-" + old.decode().strip(), item["patch"])
                self.assertIn("+" + new.decode().strip(), item["patch"])
                json.dumps(result, ensure_ascii=False)
        with self.assertRaises(GitError) as ctx:
            GitService.diff(self.root, "not-a-mode")
        self.assertEqual(ctx.exception.code, "bad_mode")

    def test_binary_diff_is_metadata_only(self) -> None:
        _write(os.path.join(self.root, "blob.bin"), b"\x00\x01\xff")
        GitService.stage(self.root, ["blob.bin"])
        item = GitService.diff(self.root, "head_index", "blob.bin")["files"][0]
        self.assertTrue(item["binary"])
        self.assertEqual(item["hunks"], [])
        self.assertEqual(item["new"]["size"], 3)
        self.assertIn("Binary files", item["patch"])

    def test_mode_only_diff_is_not_content_change(self) -> None:
        self._commit_file("mode.txt", b"same\n")
        repo = self._repo()
        try:
            index = repo.open_index()
            key = b"mode.txt"
            index[key].mode = 0o100755
            index.write()
        finally:
            repo.close()
        item = GitService.diff(self.root, "head_index", "mode.txt")["files"][0]
        self.assertEqual(item["code"], "M")
        self.assertEqual(item["old"]["mode"], 0o100644)
        self.assertEqual(item["new"]["mode"], 0o100755)
        self.assertEqual(item["add"], 0)
        self.assertEqual(item["del"], 0)
        self.assertEqual(item["patch"], "")

    def test_crlf_and_chinese_path_roundtrip(self) -> None:
        repo = self._repo()
        try:
            config = repo.get_config()
            config.set((b"core",), b"autocrlf", b"false")
            config.write_to_path()
        finally:
            repo.close()
        rel = "目录/中文.txt"
        original = "第一行\r\n第二行\r\n".encode("utf-8")
        changed = "第一行\r\n已修改\r\n".encode("utf-8")
        self._commit_file(rel, original)
        _write(os.path.join(self.root, rel), changed)
        result = GitService.diff(self.root, "index_worktree", rel)
        patch = result["files"][0]["patch"]
        self.assertIn("-第二行\r\n", patch)
        self.assertIn("+已修改\r\n", patch)
        ids = [h["id"] for h in GitService.hunks(self.root, rel)["hunks"]]
        GitService.stage_hunks(self.root, rel, ids)
        self.assertEqual(self._side_bytes("index", rel), changed)
        self.assertEqual(_read(os.path.join(self.root, rel)), changed)
        self.assertEqual(GitService.diff(self.root, "index_worktree", rel)["files"], [])

    # ---------- 整文件与按块索引操作 ----------

    def test_empty_file_lists_do_not_call_native_worktree(self) -> None:
        with mock.patch.object(WorkTree, "stage", autospec=True) as stage:
            self.assertEqual(GitService.stage(self.root, [])["staged"], [])
            stage.assert_not_called()
        with mock.patch.object(WorkTree, "unstage", autospec=True) as unstage:
            self.assertEqual(GitService.unstage(self.root, [])["unstaged"], [])
            unstage.assert_not_called()

    def test_stage_deleted_file_and_unstage_empty_file(self) -> None:
        self._commit_file("gone.txt", b"x\n")
        self._commit_file("empty.txt", b"")
        os.remove(os.path.join(self.root, "gone.txt"))
        os.remove(os.path.join(self.root, "empty.txt"))
        result = GitService.stage(self.root, ["gone.txt", "empty.txt"])
        self.assertEqual(sorted(result["removed"]), ["empty.txt", "gone.txt"])
        staged = GitService.status(self.root, fresh=True)["staged"]
        self.assertEqual({x["code"] for x in staged}, {"D"})
        GitService.unstage(self.root, ["gone.txt", "empty.txt"])
        self.assertEqual(self._side_bytes("index", "gone.txt"), b"x\n")
        self.assertEqual(self._side_bytes("index", "empty.txt"), b"")

    def test_unstage_preserves_binary_and_mode(self) -> None:
        data = b"\x00\x01binary\xff"
        self._commit_file("blob.bin", data)
        repo = self._repo()
        try:
            head_mode, head_sha = snapshot.head_entry(repo, "blob.bin")
        finally:
            repo.close()
        _write(os.path.join(self.root, "blob.bin"), data + b"more")
        GitService.stage(self.root, ["blob.bin"])
        GitService.unstage(self.root, ["blob.bin"])
        repo = self._repo()
        try:
            ent = snapshot.index_entry(repo, "blob.bin")
            self.assertEqual(ent.sha, head_sha)
            self.assertEqual(int(ent.mode), head_mode)
        finally:
            repo.close()

    def test_stage_selected_hunk_preserves_all_three_sides(self) -> None:
        base = b"1\n2\n3\n4\n5\n6\n7\n8\n"
        changed = b"ONE\n2\n3\n4\n5\n6\n7\nEIGHT\n"
        self._commit_file("code.txt", base)
        _write(os.path.join(self.root, "code.txt"), changed)
        info = GitService.hunks(self.root, "code.txt")
        self.assertEqual(len(info["hunks"]), 2)
        self.assertTrue(all(len(h["id"]) == 64 for h in info["hunks"]))
        GitService.stage_hunks(self.root, "code.txt", [info["hunks"][0]["id"]])
        expected = b"ONE\n2\n3\n4\n5\n6\n7\n8\n"
        self.assertEqual(self._side_bytes("head", "code.txt"), base)
        self.assertEqual(self._side_bytes("index", "code.txt"), expected)
        self.assertEqual(_read(os.path.join(self.root, "code.txt")), changed)

    def test_stale_hunk_is_rejected_without_index_write(self) -> None:
        base = b"a\nb\nc\n"
        self._commit_file("stale.txt", base)
        path = os.path.join(self.root, "stale.txt")
        _write(path, b"A\nb\nc\n")
        old_id = GitService.hunks(self.root, "stale.txt")["hunks"][0]["id"]
        _write(path, b"DIFFERENT\nb\nc\n")
        with self.assertRaises(GitError) as ctx:
            GitService.stage_hunks(self.root, "stale.txt", [old_id])
        self.assertEqual(ctx.exception.code, "stale_hunk")
        self.assertEqual(self._side_bytes("index", "stale.txt"), base)
        self.assertEqual(_read(path), b"DIFFERENT\nb\nc\n")

    def test_unstage_selected_hunk_only(self) -> None:
        base = b"a\nb\nc\nd\ne\nf\ng\nh\n"
        changed = b"A\nb\nc\nd\ne\nf\ng\nH\n"
        self._commit_file("h.txt", base)
        _write(os.path.join(self.root, "h.txt"), changed)
        GitService.stage(self.root, ["h.txt"])
        info = GitService.hunks(self.root, "h.txt", base="head")
        self.assertEqual(len(info["hunks"]), 2)
        GitService.unstage_hunks(self.root, "h.txt", [info["hunks"][1]["id"]])
        self.assertEqual(self._side_bytes("head", "h.txt"), base)
        self.assertEqual(self._side_bytes("index", "h.txt"),
                         b"A\nb\nc\nd\ne\nf\ng\nh\n")
        self.assertEqual(_read(os.path.join(self.root, "h.txt")), changed)

    def test_binary_hunks_rejected(self) -> None:
        self._commit_file("b.bin", b"\x00\x01")
        _write(os.path.join(self.root, "b.bin"), b"\x00\x02")
        with self.assertRaises(GitError) as ctx:
            GitService.hunks(self.root, "b.bin")
        self.assertEqual(ctx.exception.code, "binary_unsupported")

    # ---------- 引用、提交与分支 ----------

    def test_commit_requires_expected_head_and_accepts_unborn_empty(self) -> None:
        _write(os.path.join(self.root, "first.txt"), "x\n")
        GitService.stage(self.root, ["first.txt"])
        with self.assertRaises(GitError) as ctx:
            GitService.commit(self.root, "feat(git): 首次提交")
        self.assertEqual(ctx.exception.code, "expected_head_required")
        result = GitService.commit(self.root, "feat(git): 首次提交", expected_head="")
        repo = self._repo()
        try:
            self.assertEqual(repo.head().decode("ascii"), result["commit"])
        finally:
            repo.close()

    def test_commit_head_mismatch_does_not_move_head(self) -> None:
        first = self._commit_file("e.txt", "x\n")
        _write(os.path.join(self.root, "e.txt"), "y\n")
        GitService.stage(self.root, ["e.txt"])
        with self.assertRaises(GitError) as ctx:
            GitService.commit(self.root, "fix(git): 修改",
                              expected_head="deadbeef" * 5)
        self.assertEqual(ctx.exception.code, "head_moved")
        self.assertEqual(self._head(), first["commit"])

    def test_commit_parent_and_log(self) -> None:
        first = self._commit_file("log.txt", "a\n", "feat(git): 第一版")
        _write(os.path.join(self.root, "log.txt"), "b\n")
        GitService.stage(self.root, ["log.txt"])
        second = self._commit("fix(git): 第二版")
        repo = self._repo()
        try:
            obj = repo[second["commit"].encode("ascii")]
            self.assertEqual(obj.parents, [first["commit"].encode("ascii")])
        finally:
            repo.close()
        entries = GitService.log(self.root)["entries"]
        self.assertEqual([e["message"] for e in entries[:2]],
                         ["fix(git): 第二版", "feat(git): 第一版"])

    def test_switch_blocked_when_dirty(self) -> None:
        self._commit_file("s.txt", "x\n")
        GitService.create_branch(self.root, "topic/demo")
        _write(os.path.join(self.root, "s.txt"), "y\n")
        with self.assertRaises(GitError) as ctx:
            GitService.switch(self.root, "topic/demo")
        self.assertEqual(ctx.exception.code, "dirty_tree")

    def test_create_and_switch_clean(self) -> None:
        self._commit_file("c.txt", "x\n")
        GitService.create_branch(self.root, "topic/clean")
        result = GitService.switch(self.root, "topic/clean")
        self.assertEqual(result["branch"], "topic/clean")
        self.assertTrue(GitService.status(self.root, fresh=True)["clean"])

    # ---------- 序列化与既有策略 ----------

    def test_public_results_are_json_serializable(self) -> None:
        self._commit_file("json.txt", "a\nb\n")
        _write(os.path.join(self.root, "json.txt"), "A\nb\n")
        hunk_id = GitService.hunks(self.root, "json.txt")["hunks"][0]["id"]
        results = [
            GitService.status(self.root, fresh=True),
            GitService.diff(self.root),
            GitService.content(self.root, "json.txt"),
            GitService.hunks(self.root, "json.txt"),
            GitService.stage_hunks(self.root, "json.txt", [hunk_id]),
            GitService.branches(self.root),
            GitService.log(self.root),
        ]
        for result in results:
            json.dumps(result, ensure_ascii=False, allow_nan=False)

    def test_policy_reset_and_audit_regressions(self) -> None:
        self.assertEqual(policy.bump_for(["fix: a", "feat: b"]), "minor")
        self.assertTrue(policy.can_auto("feature/x", "commit")[0])
        self._commit_file("audit.txt", "x\n")
        with self.assertRaises(GitError) as ctx:
            GitService.reset(self.root, mode="hard")
        self.assertEqual(ctx.exception.code, "need_confirm")
        events = GitService.audit_trail(self.root)["events"]
        self.assertTrue(any(e.get("operation") == "commit" for e in events))
        self.assertTrue(all(e.get("event_hash") for e in events))


if __name__ == "__main__":
    unittest.main()
