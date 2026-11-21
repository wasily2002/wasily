"""git_api 路由测试：真实 HTTP 之外最容易被写错的一层（参数、错误码、分派）。"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dulwich.porcelain

import git_api


class FakeHandler:
    def __init__(self) -> None:
        self.status = None
        self.payload = None

    def _send_json(self, status, obj):
        self.status = status
        self.payload = obj


class GitApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="wasily_api_")
        self.audit = tempfile.mkdtemp(prefix="wasily_api_audit_")
        self.wt = tempfile.mkdtemp(prefix="wasily_api_wt_")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(shutil.rmtree, self.audit, True)
        self.addCleanup(shutil.rmtree, self.wt, True)
        os.environ["WASILY_AUDIT_DIR"] = self.audit
        os.environ["WASILY_WORKTREE_DIR"] = self.wt
        repo = dulwich.porcelain.init(self.root)
        path = os.path.join(self.root, "a.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("hello\n")
        dulwich.porcelain.add(repo, paths=["a.txt"])
        dulwich.porcelain.commit(repo, message="chore: init".encode("utf-8"))
        dulwich.porcelain.branch_create(repo, b"develop")
        repo.close()

    def _get(self, path, query):
        h = FakeHandler()
        handled = git_api.handle_get(h, path, query)
        self.assertTrue(handled)
        return h

    def _post(self, path, body):
        h = FakeHandler()
        handled = git_api.handle_post(h, path, body)
        self.assertTrue(handled)
        return h

    def test_status_and_branches(self) -> None:
        h = self._get("/v1/git/status", "path=" + self.root + "&fresh=1")
        self.assertEqual(h.status, 200)
        self.assertTrue(h.payload["ok"])
        self.assertIn("develop", self._get("/v1/git/branches", "path=" + self.root).payload["branches"])
        self.assertFalse(git_api.handles_get("/v1/chat/completions"))

    def test_stage_commit_and_log(self) -> None:
        with open(os.path.join(self.root, "b.txt"), "w", encoding="utf-8") as f:
            f.write("world\n")
        h = self._post("/v1/git/stage", {"path": self.root, "files": ["b.txt"]})
        self.assertEqual(h.status, 200)
        st = self._get("/v1/git/status", "path=" + self.root + "&fresh=1").payload
        self.assertEqual([x["path"] for x in st["staged"]], ["b.txt"])
        head = st["head"]
        h = self._post("/v1/git/commit", {
            "path": self.root, "message": "feat(api): 增加路由", "expected_head": head})
        self.assertEqual(h.status, 200, h.payload)
        self.assertTrue(h.payload["commit"])
        log = self._get("/v1/git/log", "path=" + self.root).payload
        self.assertEqual(log["entries"][0]["message"], "feat(api): 增加路由")

    def test_errors_are_400_with_code(self) -> None:
        h = self._post("/v1/git/commit", {"path": self.root, "message": "随手改改"})
        self.assertEqual(h.status, 400)
        self.assertEqual(h.payload["code"], "bad_message")
        h = self._get("/v1/git/status", "path=" + tempfile.gettempdir())
        self.assertEqual(h.status, 400)
        self.assertEqual(h.payload["code"], "not_a_repo")

    def test_iteration_routes(self) -> None:
        h = self._post("/v1/iterations/start", {
            "path": self.root, "requirement": "路由闭环"})
        self.assertEqual(h.status, 200, h.payload)
        iid = h.payload["iteration"]["id"]
        wt = h.payload["iteration"]["worktree"]
        with open(os.path.join(wt, "loop.py"), "w", encoding="utf-8") as f:
            f.write("LOOP = 1\n")
        h = self._post("/v1/iterations/verify", {"path": self.root, "id": iid})
        self.assertTrue(h.payload["ok"], h.payload)
        h = self._post("/v1/iterations/commit", {
            "path": self.root, "id": iid,
            "message": "feat(api): 路由闭环", "push": False})
        self.assertEqual(h.status, 200, h.payload)
        self.assertEqual(h.payload["iteration"]["state"], "committed")
        rows = self._get("/v1/iterations", "path=" + self.root).payload["iterations"]
        self.assertEqual(rows[0]["state"], "committed")


if __name__ == "__main__":
    unittest.main()
