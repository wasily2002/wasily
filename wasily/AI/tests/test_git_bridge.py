# -*- coding: utf-8 -*-
"""Git 桥测试只使用 tempfile 与 Dulwich，不调用系统 Git。"""
import base64
import os
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import git_bridge as gb  # noqa: E402
from dulwich import porcelain  # noqa: E402


class GitBridgeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="git_bridge_test_")
        self.root = self.temp.name
        self.repo = os.path.join(self.root, "repo")
        self.assertTrue(gb.init(self.repo)["created"])
        self.main_branch = gb.status(self.repo)["branch"]

    def tearDown(self):
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
        staged = gb.stage(repo, [rel], exact=True)
        self.assertTrue(staged["ok"], staged)
        result = gb.commit(repo, message)
        self.assertTrue(result["ok"], result)
        return result["commit"]

    def bare_repo(self, name="remote.git"):
        path = os.path.join(self.root, name)
        repo = porcelain.init(path, bare=True)
        repo.close()
        return path

    def test_init_commit_log_author_and_status_hashes(self):
        initial = gb.status(self.repo)
        self.assertEqual(initial["head_sha"], "")
        self.assertTrue(initial["clean"])
        self.write("dir/a.txt", b"hello\n")
        self.assertTrue(gb.stage(self.repo, ["dir/a.txt"], exact=True)["ok"])
        result = gb.commit(
            self.repo, "feat: initial commit", "Tester <tester@example.com>")
        self.assertEqual(result["author"], "Tester <tester@example.com>")
        current = gb.status(self.repo)
        self.assertEqual(current["head"], result["commit"])
        self.assertEqual(current["head_sha"], result["commit"])
        self.assertEqual(len(current["index_sha"]), 64)
        self.assertTrue(current["clean"])
        entries = gb.log(self.repo)["entries"]
        self.assertEqual(entries[0]["message"], "feat: initial commit")
        self.assertIn("Tester", entries[0]["author"])

    def test_work_diff_is_index_to_work_and_index_diff_head_to_index(self):
        self.commit_file("a.txt", b"old\n", "feat: base")
        self.write("a.txt", b"new\n")
        work = gb.diff(self.repo, "work", "a.txt")
        self.assertIn("-old", work["diff"])
        self.assertIn("+new", work["diff"])
        self.assertEqual(work["files"][0]["path"], "a.txt")
        self.assertEqual(gb.diff(self.repo, "index", "a.txt")["diff"], "")
        gb.stage(self.repo, ["a.txt"], exact=True)
        self.assertEqual(gb.diff(self.repo, "work", "a.txt")["diff"], "")
        cached = gb.diff(self.repo, "cached", "a.txt")
        self.assertIn("-old", cached["diff"])
        self.assertIn("+new", cached["diff"])
        self.assertEqual(len(cached["hunks"]), 1)
        for key in ("id", "file", "old_range", "new_range", "patch",
                    "head_sha", "index_sha"):
            self.assertIn(key, cached["hunks"][0])

    def test_exact_stage_does_not_touch_sibling(self):
        self.commit_file("a.txt", b"a0\n", "feat: base a")
        self.commit_file("b.txt", b"b0\n", "feat: base b")
        self.write("a.txt", b"a1\n")
        self.write("b.txt", b"b1\n")
        self.write("c.txt", b"c1\n")
        result = gb.stage(self.repo, ["a.txt"], exact=True)
        self.assertEqual(result["staged"], ["a.txt"])
        current = gb.status(self.repo)
        self.assertEqual([x["path"] for x in current["staged"]], ["a.txt"])
        self.assertEqual(current["changed"], ["b.txt"])
        self.assertEqual(current["untracked"], ["c.txt"])
        empty = gb.stage(self.repo, [], exact=True)
        self.assertEqual(empty["staged"], [])
        self.assertEqual(gb.status(self.repo)["untracked"], ["c.txt"])

    def test_unstage_all_preserves_worktree(self):
        original = b"a0\n"
        changed = b"a1\n"
        self.commit_file("a.txt", original, "feat: base")
        self.write("a.txt", changed)
        gb.stage(self.repo, ["a.txt"], exact=True)
        result = gb.unstage(self.repo)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.read("a.txt"), changed)
        self.assertEqual(gb.status(self.repo)["staged"], [])
        self.assertEqual(gb.content(self.repo, "index", "a.txt")["text"],
                         original.decode("utf-8"))

    def test_partial_hunk_stage_unstage_and_hunk_id_list(self):
        original = b"".join(b"line%02d\n" % i for i in range(1, 25))
        self.commit_file("a.txt", original, "feat: base")
        changed = original.replace(b"line02\n", b"line two\n")
        changed = changed.replace(b"line22\n", b"line twenty two\n")
        self.write("a.txt", changed)
        first_diff = gb.diff(self.repo, "work", "a.txt")
        self.assertEqual(len(first_diff["hunks"]), 2)
        first, second = first_diff["hunks"]
        before_work = self.read("a.txt")
        staged = gb.stage_hunk(
            self.repo, "a.txt", first["id"], first_diff["head_sha"],
            first_diff["index_sha"])
        self.assertTrue(staged["ok"], staged)
        self.assertEqual(self.read("a.txt"), before_work)
        index_text = gb.content(self.repo, "index", "a.txt")["text"]
        self.assertIn("line two", index_text)
        self.assertIn("line22", index_text)
        self.assertNotIn("line twenty two", index_text)

        remaining = gb.diff(self.repo, "work", "a.txt")
        self.assertEqual(len(remaining["hunks"]), 1)
        staged_all = gb.stage_hunk(
            self.repo, "a.txt", expected_head=remaining["head_sha"],
            expected_index=remaining["index_sha"],
            hunk_ids=[remaining["hunks"][0]["id"]])
        self.assertTrue(staged_all["ok"], staged_all)
        self.assertEqual(gb.diff(self.repo, "work", "a.txt")["diff"], "")

        cached = gb.diff(self.repo, "index", "a.txt")
        self.assertEqual(len(cached["hunks"]), 2)
        unstaged = gb.unstage_hunk(
            self.repo, "a.txt", expected_head=cached["head_sha"],
            expected_index=cached["index_sha"],
            hunk_ids=[cached["hunks"][0]["id"]])
        self.assertTrue(unstaged["ok"], unstaged)
        index_text = gb.content(self.repo, "index", "a.txt")["text"]
        self.assertIn("line02", index_text)
        self.assertIn("line twenty two", index_text)
        self.assertEqual(self.read("a.txt"), before_work)

    def test_unborn_head_hunk_stage_and_unstage(self):
        payload = b"first\nsecond"
        self.write("new.txt", payload)
        work = gb.diff(self.repo, "work", "new.txt")
        self.assertEqual(work["head_sha"], "")
        self.assertEqual(len(work["hunks"]), 1)
        staged = gb.stage_hunk(
            self.repo, "new.txt", hunk_id=work["hunks"][0]["id"],
            expected_head="", expected_index=work["index_sha"])
        self.assertTrue(staged["ok"], staged)
        self.assertEqual(self.read("new.txt"), payload)
        self.assertEqual(
            gb.content(self.repo, "index", "new.txt")["text"],
            payload.decode("utf-8"))
        self.assertEqual(gb.diff(self.repo, "work", "new.txt")["diff"], "")

        cached = gb.diff(self.repo, "index", "new.txt")
        self.assertEqual(cached["head_sha"], "")
        self.assertEqual(len(cached["hunks"]), 1)
        unstaged = gb.unstage_hunk(
            self.repo, "new.txt", hunk_id=cached["hunks"][0]["id"],
            expected_head="", expected_index=cached["index_sha"])
        self.assertTrue(unstaged["ok"], unstaged)
        self.assertFalse(gb.content(self.repo, "index", "new.txt")["exists"])
        self.assertEqual(self.read("new.txt"), payload)
        current = gb.status(self.repo)
        self.assertEqual(current["staged"], [])
        self.assertEqual(current["untracked"], ["new.txt"])

    def test_multiple_hunk_ids_apply_in_one_index_write(self):
        original = b"".join(b"line%02d\n" % i for i in range(1, 35))
        self.commit_file("a.txt", original, "feat: base")
        changed = original.replace(b"line02\n", b"second\n")
        changed = changed.replace(b"line18\n", b"eighteenth\n")
        changed = changed.replace(b"line32\n", b"thirty second\n")
        self.write("a.txt", changed)
        result = gb.diff(self.repo, "work", "a.txt")
        self.assertEqual(len(result["hunks"]), 3)
        ids = [item["id"] for item in result["hunks"][:2]]
        staged = gb.stage_hunk(
            self.repo, "a.txt", expected_head=result["head_sha"],
            expected_index=result["index_sha"], hunk_ids=ids)
        self.assertTrue(staged["ok"], staged)
        self.assertEqual(set(staged["hunk_ids"]), set(ids))
        index_text = gb.content(self.repo, "index", "a.txt")["text"]
        self.assertIn("second\n", index_text)
        self.assertIn("eighteenth\n", index_text)
        self.assertIn("line32\n", index_text)
        self.assertEqual(self.read("a.txt"), changed)

    def test_stale_hash_and_stale_hunk_are_rejected(self):
        self.commit_file("a.txt", b"a\n", "feat: base a")
        self.commit_file("b.txt", b"b\n", "feat: base b")
        self.write("a.txt", b"a1\n")
        first = gb.diff(self.repo, "work", "a.txt")
        same = gb.diff(self.repo, "work", "a.txt")
        self.assertEqual(first["hunks"][0]["id"], same["hunks"][0]["id"])
        self.write("b.txt", b"b1\n")
        gb.stage(self.repo, ["b.txt"], exact=True)
        stale = gb.stage_hunk(
            self.repo, "a.txt", first["hunks"][0]["id"],
            first["head_sha"], first["index_sha"])
        self.assertFalse(stale["ok"])
        self.assertEqual(stale["error"]["code"], "stale_hash")

        refreshed = gb.diff(self.repo, "work", "a.txt")
        old_id = refreshed["hunks"][0]["id"]
        self.write("a.txt", b"a2\n")
        changed = gb.diff(self.repo, "work", "a.txt")
        self.assertNotEqual(old_id, changed["hunks"][0]["id"])
        rejected = gb.stage_hunk(
            self.repo, "a.txt", old_id, refreshed["head_sha"],
            refreshed["index_sha"])
        self.assertFalse(rejected["ok"])
        self.assertEqual(rejected["error"]["code"], "stale_hunk")

    def test_crlf_partial_stage_preserves_every_byte(self):
        original = b"".join(b"row%02d\r\n" % i for i in range(1, 15))
        self.commit_file("crlf.txt", original, "feat: crlf")
        changed = original.replace(b"row02\r\n", b"changed\r\n")
        self.write("crlf.txt", changed)
        result = gb.diff(self.repo, "work", "crlf.txt")
        hunk = result["hunks"][0]
        self.assertIn("\r\n", hunk["patch"])
        staged = gb.stage_hunk(
            self.repo, "crlf.txt", hunk["id"], result["head_sha"],
            result["index_sha"])
        self.assertTrue(staged["ok"], staged)
        index_data = gb.content(
            self.repo, "index", "crlf.txt")["text"].encode()
        self.assertEqual(index_data, changed)
        self.assertEqual(self.read("crlf.txt"), changed)
        self.assertNotIn(b"\n", index_data.replace(b"\r\n", b""))

    def test_binary_large_and_unstage_use_raw_bytes(self):
        original = b"\x00\xff\x01binary"
        self.commit_file("bin.dat", original, "feat: binary")
        changed = b"\x00\xfe\x02changed"
        self.write("bin.dat", changed)
        result = gb.diff(self.repo, "work", "bin.dat")
        self.assertTrue(result["files"][0]["binary"])
        self.assertEqual(result["diff"], "")
        self.assertEqual(result["hunks"], [])
        binary = gb.content(self.repo, "work", "bin.dat")
        self.assertTrue(binary["binary"])
        self.assertEqual(base64.b64decode(binary["base64"]), changed)
        gb.stage(self.repo, ["bin.dat"], exact=True)
        gb.unstage(self.repo, ["bin.dat"])
        restored = gb.content(self.repo, "index", "bin.dat")
        self.assertEqual(base64.b64decode(restored["base64"]), original)

        self.write("large.txt", b"x" * (gb.MAX_DIFF_BYTES + 1))
        large = gb.diff(self.repo, "work", "large.txt")
        self.assertTrue(large["files"][0]["too_large"])
        self.assertEqual(large["diff"], "")
        self.assertEqual(large["hunks"], [])

    def test_added_deleted_and_missing_final_newline(self):
        self.commit_file("gone.txt", b"bye", "feat: base")
        os.remove(self.path("gone.txt"))
        self.write("new.txt", b"new")
        result = gb.diff(self.repo, "work")
        codes = {item["path"]: item["code"] for item in result["files"]}
        self.assertEqual(codes, {"gone.txt": "D", "new.txt": "A"})
        self.assertIn("/dev/null", result["diff"])
        self.assertIn("\\ No newline at end of file", result["diff"])
        gb.stage(self.repo, ["gone.txt", "new.txt"], exact=True)
        self.assertFalse(gb.content(self.repo, "index", "gone.txt")["exists"])
        self.assertEqual(
            gb.content(self.repo, "index", "new.txt")["text"], "new")

    def test_relative_path_validation(self):
        self.commit_file("a.txt", b"a\n", "feat: base")
        for rel in ("../outside", "sub/../../outside", "/absolute",
                    "C:/outside", ".git/config"):
            with self.subTest(rel=rel):
                with self.assertRaises(ValueError):
                    gb.diff(self.repo, "work", rel)
                with self.assertRaises(ValueError):
                    gb.content(self.repo, "work", rel)
                with self.assertRaises(ValueError):
                    gb.stage(self.repo, [rel], exact=True)

    def test_branch_switch_rejects_dirty_worktree(self):
        self.commit_file("a.txt", b"main\n", "feat: base")
        self.assertTrue(gb.branch_create(self.repo, "feature")["ok"])
        self.write("a.txt", b"do not overwrite\n")
        result = gb.branch_switch(self.repo, "feature")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "dirty_worktree")
        self.assertEqual(gb.status(self.repo)["branch"], self.main_branch)
        self.assertEqual(self.read("a.txt"), b"do not overwrite\n")

    def test_local_bare_remote_clone_fetch_pull_push(self):
        self.commit_file("a.txt", b"one\n", "feat: one")
        remote = self.bare_repo()
        gb.remote_add(self.repo, "origin", remote)
        pushed = gb.push(
            self.repo, username="local-user", password="never-stored")
        self.assertTrue(pushed["ok"], pushed)
        self.assertNotIn("never-stored", str(pushed))
        clone_path = os.path.join(self.root, "clone")
        cloned = gb.clone(
            remote, clone_path, username="local-user", password="never-stored")
        self.assertTrue(cloned["ok"], cloned)
        self.assertEqual(self.read("a.txt", clone_path), b"one\n")
        second = self.commit_file("a.txt", b"two\n", "feat: two")
        self.assertTrue(gb.push(self.repo)["ok"])
        fetched = gb.fetch(clone_path)
        self.assertTrue(fetched["ok"], fetched)
        self.assertNotEqual(gb.status(clone_path)["head_sha"], second)
        behind = gb.status(clone_path)
        self.assertEqual(behind["tracking"], "origin/master")
        self.assertEqual(behind["behind"], 1)
        self.assertEqual(behind["ahead"], 0)
        pulled = gb.pull(clone_path)
        self.assertTrue(pulled["ok"], pulled)
        self.assertEqual(gb.status(clone_path)["head_sha"], second)
        self.assertEqual(self.read("a.txt", clone_path), b"two\n")
        third = self.commit_file(
            "a.txt", b"three\n", "feat: three", repo=clone_path)
        ahead = gb.status(clone_path)
        self.assertEqual(ahead["ahead"], 1)
        self.assertEqual(ahead["behind"], 0)
        self.assertTrue(gb.push(clone_path)["ok"])
        remote_repo = gb.open_repo(remote)
        try:
            self.assertEqual(
                remote_repo.refs[b"refs/heads/master"].decode(), third)
        finally:
            remote_repo.close()
        with open(os.path.join(self.repo, ".git", "config"), "rb") as stream:
            self.assertNotIn(b"never-stored", stream.read())

    def test_non_fast_forward_push_is_rejected(self):
        self.commit_file("a.txt", b"base\n", "feat: base")
        remote = self.bare_repo()
        gb.remote_add(self.repo, "origin", remote)
        self.assertTrue(gb.push(self.repo)["ok"])
        clone_path = os.path.join(self.root, "clone")
        self.assertTrue(gb.clone(remote, clone_path)["ok"])
        remote_commit = self.commit_file(
            "a.txt", b"remote\n", "feat: remote", repo=clone_path)
        self.assertTrue(gb.push(clone_path)["ok"])
        self.commit_file("a.txt", b"local\n", "feat: local")
        rejected = gb.push(self.repo)
        self.assertFalse(rejected["ok"])
        self.assertEqual(rejected["error"]["code"], "non_fast_forward")
        self.assertTrue(gb.push(self.repo, force=True)["ok"] is False)
        remote_repo = gb.open_repo(remote)
        try:
            self.assertEqual(
                remote_repo.refs[b"refs/heads/master"].decode(), remote_commit)
        finally:
            remote_repo.close()

    def test_worktree_discover_isolation_and_safe_remove(self):
        base = self.commit_file("a.txt", b"base\n", "feat: base")
        linked_path = os.path.join(self.root, "linked")
        added = gb.worktree_add(self.repo, linked_path, branch="feature")
        self.assertTrue(added["ok"], added)
        self.assertEqual(added["commit"], base)
        self.assertEqual(added["branch"], "feature")
        self.assertTrue(os.path.isfile(self.path(".git", linked_path)))
        linked_repo = gb.open_repo(linked_path)
        try:
            self.assertEqual(os.path.realpath(linked_repo.path),
                             os.path.realpath(linked_path))
        finally:
            linked_repo.close()
        feature_commit = self.commit_file(
            "a.txt", b"feature\n", "feat: feature", repo=linked_path)
        self.assertEqual(gb.status(self.repo)["head_sha"], base)
        self.assertEqual(self.read("a.txt"), b"base\n")
        self.write("a.txt", b"dirty\n", linked_path)
        rejected = gb.worktree_remove(self.repo, linked_path)
        self.assertFalse(rejected["ok"])
        self.assertTrue(os.path.exists(linked_path))
        self.write("a.txt", b"feature\n", linked_path)
        removed = gb.worktree_remove(self.repo, linked_path)
        self.assertTrue(removed["ok"], removed)
        self.assertFalse(os.path.exists(linked_path))
        self.assertNotEqual(feature_commit, base)
        self.assertEqual(len(gb.worktrees(self.repo)["worktrees"]), 1)

    def test_merge_conflict_is_reported_in_status(self):
        self.commit_file("a.txt", b"base\n", "feat: base")
        gb.branch_create(self.repo, "feature")
        self.assertTrue(gb.branch_switch(self.repo, "feature")["ok"])
        self.commit_file("a.txt", b"feature\n", "feat: feature")
        self.assertTrue(gb.branch_switch(self.repo, self.main_branch)["ok"])
        self.commit_file("a.txt", b"main\n", "feat: main")
        merged = gb.merge(self.repo, "feature")
        self.assertFalse(merged["ok"])
        self.assertEqual(merged["error"]["code"], "merge_conflict")
        self.assertEqual(merged["conflicts"], ["a.txt"])
        self.assertEqual(gb.status(self.repo)["conflicts"], ["a.txt"])
        self.assertIn(b"feature", self.read("a.txt"))
        self.assertIn(b"main", self.read("a.txt"))

    def test_merge_and_revert_reject_dirty_worktree(self):
        self.commit_file("a.txt", b"base\n", "feat: base")
        gb.branch_create(self.repo, "feature")
        self.write("a.txt", b"dirty\n")
        merged = gb.merge(self.repo, "feature")
        self.assertFalse(merged["ok"])
        self.assertEqual(merged["error"]["code"], "dirty_worktree")
        reverted = gb.revert(self.repo, "HEAD")
        self.assertFalse(reverted["ok"])
        self.assertEqual(reverted["error"]["code"], "dirty_worktree")
        self.assertEqual(self.read("a.txt"), b"dirty\n")

    def test_revert_no_commit_stages_inverse_change(self):
        self.commit_file("a.txt", b"old\n", "feat: old")
        target = self.commit_file("a.txt", b"new\n", "feat: new")
        head = gb.status(self.repo)["head_sha"]
        result = gb.revert(self.repo, target, no_commit=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(gb.status(self.repo)["head_sha"], head)
        self.assertEqual(self.read("a.txt"), b"old\n")
        self.assertEqual(gb.content(self.repo, "index", "a.txt")["text"],
                         "old\n")
        staged_paths = [
            item["path"] for item in gb.status(self.repo)["staged"]]
        self.assertEqual(staged_paths, ["a.txt"])

    def test_revert_restores_content_and_clean_status(self):
        self.commit_file("a.txt", b"old\n", "feat: old")
        target = self.commit_file("a.txt", b"new\n", "feat: new")
        result = gb.revert(self.repo, target)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.read("a.txt"), b"old\n")
        self.assertTrue(gb.status(self.repo)["clean"], gb.status(self.repo))
        self.assertEqual(gb.log(self.repo)["entries"][0]["message"],
                         'Revert "feat: new"')

    def test_discard_restores_index_and_only_deletes_untracked(self):
        self.commit_file("a.txt", b"head\n", "feat: base")
        self.write("a.txt", b"index\n")
        gb.stage(self.repo, ["a.txt"], exact=True)
        self.write("a.txt", b"work\n")
        result = gb.discard(self.repo, "a.txt")
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.read("a.txt"), b"index\n")
        self.assertEqual(gb.content(self.repo, "index", "a.txt")["text"],
                         "index\n")

        self.write("new.txt", b"temporary\n")
        removed = gb.discard(self.repo, "new.txt")
        self.assertTrue(removed["ok"], removed)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "new.txt")))
        clean = self.commit_file("clean.txt", b"clean\n", "feat: clean")
        self.assertTrue(clean)
        rejected = gb.discard(self.repo, "clean.txt")
        self.assertFalse(rejected["ok"])
        self.assertEqual(rejected["error"]["code"], "not_discardable")
        self.assertEqual(self.read("clean.txt"), b"clean\n")


if __name__ == "__main__":
    unittest.main()
