"""仓库卫兵回归测试：公开候选、忽略规则与嵌套仓库边界。"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

import repo_guard


API_KEY = "sk-" + "a" * 24
PRIVATE_KEY = (
    "-----BEGIN " + "PRIVATE KEY-----\n"
    "dGVzdC1maXh0dXJlLW9ubHk=\n"
    "-----END PRIVATE KEY-----\n"
)


def _write(path: str, data: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(data)


def _blob_id(data: bytes) -> str:
    header = b"blob " + str(len(data)).encode("ascii") + b"\0"
    return hashlib.sha1(header + data).hexdigest()


class RepoGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="wasily_repo_guard_")
        self.addCleanup(shutil.rmtree, self.root, True)

    def _path(self, rel: str) -> str:
        return os.path.join(self.root, *rel.split("/"))

    def _findings(self, result: dict, rule: str) -> list[dict]:
        return [f for f in result["findings"] if f["rule"] == rule]

    def test_build_source_is_scanned(self) -> None:
        _write(self._path("build/secret.txt"), API_KEY)
        result = repo_guard.scan(root=self.root)
        paths = [f["path"] for f in self._findings(result, "secret_pattern")]
        self.assertIn("build/secret.txt", paths)
        self.assertFalse(result["ok"])

    def test_ignored_secret_is_not_reported_without_candidate(self) -> None:
        _write(self._path(".gitignore"), "private/\n")
        _write(self._path("private/secret.txt"), API_KEY)
        result = repo_guard.scan(root=self.root)
        self.assertTrue(result["ok"])
        self.assertFalse(result["findings"])

    def test_nested_gitignore_is_respected(self) -> None:
        _write(self._path("nested/.gitignore"), "secret.txt\n")
        _write(self._path("nested/secret.txt"), API_KEY)
        _write(self._path("nested/visible.txt"), "ok\n")
        result = repo_guard.scan(root=self.root)
        self.assertTrue(result["ok"])
        self.assertEqual(result["scanned_files"], 2)

    def test_ignored_candidate_secret_is_blocked(self) -> None:
        _write(self._path(".gitignore"), "private/\n")
        _write(self._path("private/secret.txt"), API_KEY)
        result = repo_guard.scan(
            root=self.root, paths=["private/secret.txt"])
        paths = [f["path"] for f in self._findings(result, "secret_pattern")]
        self.assertIn("private/secret.txt", paths)
        self.assertFalse(result["ok"])

    def test_ignored_candidate_directory_is_scanned_recursively(self) -> None:
        _write(self._path(".gitignore"), "private/\n")
        _write(self._path("private/nested/secret.txt"), API_KEY)
        result = repo_guard.scan(root=self.root, paths=["private"])
        self.assertFalse(result["ok"])
        self.assertEqual(
            self._findings(result, "secret_pattern")[0]["path"],
            "private/nested/secret.txt")

    def test_nested_git_directory_is_blocked(self) -> None:
        _write(self._path("nested/.git/HEAD"), "ref: refs/heads/main\n")
        result = repo_guard.scan(root=self.root)
        findings = self._findings(result, "nested_repository")
        self.assertEqual([f["path"] for f in findings], ["nested/.git"])
        self.assertFalse(result["ok"])

    def test_nested_git_file_is_blocked(self) -> None:
        _write(self._path("linked/.git"), "gitdir: ../elsewhere\n")
        result = repo_guard.scan(root=self.root)
        findings = self._findings(result, "nested_repository")
        self.assertEqual([f["path"] for f in findings], ["linked/.git"])
        self.assertFalse(result["ok"])

    def test_candidate_outside_root_is_rejected(self) -> None:
        other = tempfile.mkdtemp(prefix="wasily_repo_guard_outside_")
        self.addCleanup(shutil.rmtree, other, True)
        outside = os.path.join(other, "secret.txt")
        _write(outside, API_KEY)
        with self.assertRaises(ValueError):
            repo_guard.scan(root=self.root, paths=[outside])
        with self.assertRaises(ValueError):
            repo_guard.scan(root=self.root, paths=["../outside.txt"])

    def test_explicit_root_and_cli_json(self) -> None:
        _write(self._path("build/app.js"), "console.log('ok');\n")
        output = io.StringIO()
        argv = ["repo_guard.py", "--root", self.root, "--json"]
        with mock.patch.object(sys, "argv", argv), redirect_stdout(output):
            code = repo_guard.main()
        result = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(result["root"], os.path.realpath(self.root))
        self.assertEqual(result["scanned_files"], 1)
        json.dumps(result, ensure_ascii=False)

    def test_upstream_test_certificate_fixture_is_allowed(self) -> None:
        rel = ("wasily/tools/wasily-ide-src/src/vs/platform/test/node/"
               "fixtures/localhost.pem")
        _write(self._path(rel), PRIVATE_KEY)
        result = repo_guard.scan(root=self.root)
        self.assertTrue(result["ok"], result["findings"])
        self.assertEqual(result["scanned_files"], 1)

    def test_pem_parser_source_is_not_a_private_key(self) -> None:
        header = PRIVATE_KEY.splitlines()[0]
        _write(self._path(
            "wasily/tools/wasily-ide-src/build/lib/pfx.ts"),
            "const pemHeader = /" + header + "/;\n")
        result = repo_guard.scan(root=self.root)
        self.assertTrue(result["ok"], result["findings"])

    def test_upstream_test_placeholder_tokens_are_allowed(self) -> None:
        rel = ("wasily/tools/wasily-ide-src/src/vs/platform/terminal/test/"
               "node/terminalEnvironment.test.ts")
        github = "ghp_" + "x" * 36
        bearer = "Authorization: Bearer " + "private-value"
        aws = "AKIA" + "IOSFODNN7EXAMPLE"
        _write(self._path(rel), github + "\n" + bearer + "\n" + aws + "\n")
        result = repo_guard.scan(root=self.root)
        self.assertTrue(result["ok"], result["findings"])

    def test_random_token_after_placeholder_is_still_blocked(self) -> None:
        rel = ("wasily/tools/wasily-ide-src/src/vs/platform/terminal/test/"
               "node/terminalEnvironment.test.ts")
        placeholder = "ghp_" + "x" * 36
        random_token = "ghp_" + "aB3dE5fG7hI9jK1lM3nO5pQ7"
        _write(self._path(rel), placeholder + "\n" + random_token + "\n")
        result = repo_guard.scan(root=self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(
            self._findings(result, "secret_pattern")[0]["detail"],
            "GitHub Token")

    def test_fixture_exemption_does_not_allow_other_secrets(self) -> None:
        rel = ("wasily/tools/wasily-ide-src/src/vs/platform/test/node/"
               "fixtures/config.pem")
        _write(self._path(rel), API_KEY)
        result = repo_guard.scan(root=self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(
            self._findings(result, "secret_pattern")[0]["detail"], "API Key")

    def test_real_private_key_outside_fixture_is_blocked(self) -> None:
        _write(self._path("config/private.pem"), PRIVATE_KEY)
        _write(self._path(
            "wasily/tools/wasily-ide-src/src/vs/platform/test/node/key.pem"),
            PRIVATE_KEY)
        result = repo_guard.scan(root=self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(len(self._findings(result, "binary_artifact")), 2)

    def test_runtime_directory_remains_hard_skip_for_candidate(self) -> None:
        _write(self._path("sessions_store/secret.txt"), API_KEY)
        result = repo_guard.scan(
            root=self.root, paths=["sessions_store/secret.txt"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["scanned_files"], 0)

    def test_manifest_restores_ignored_upstream_source(self) -> None:
        content = b"VALUE = 1\n"
        _write(self._path(".gitignore"), "models/\n")
        _write(self._path("upstream/models/code.py"), content.decode())
        _write(self._path("scripts/upstream_sources.json"), json.dumps({
            "version": 1,
            "upstreams": [{
                "prefix": "upstream",
                "commit": "test",
                "files": {"models/code.py": [_blob_id(content), 33188]},
            }],
        }))
        collected = set()
        result = repo_guard.scan(root=self.root, collect=collected)
        self.assertTrue(result["ok"], result["findings"])
        self.assertEqual(result["scanned_files"], 3)
        self.assertIn("upstream/models/code.py", collected)

    def test_manifest_reports_missing_upstream_source(self) -> None:
        _write(self._path("scripts/upstream_sources.json"), json.dumps({
            "version": 1,
            "upstreams": [{
                "prefix": "upstream",
                "commit": "test",
                "files": {"missing.py": ["0" * 40, 33188]},
            }],
        }))
        result = repo_guard.scan(root=self.root)
        self.assertFalse(result["ok"])
        finding = self._findings(result, "missing_upstream_source")[0]
        self.assertEqual(finding["path"], "upstream/missing.py")

    def test_manifest_binary_requires_exact_upstream_blob(self) -> None:
        payload = b"\x00upstream-test-fixture"
        path = self._path("upstream/build/tool.exe")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as stream:
            stream.write(payload)
        _write(self._path(".gitignore"), "build/\n")
        _write(self._path("scripts/upstream_sources.json"), json.dumps({
            "version": 1,
            "upstreams": [{
                "prefix": "upstream",
                "commit": "test",
                "files": {"build/tool.exe": [_blob_id(payload), 33188]},
            }],
        }))
        result = repo_guard.scan(root=self.root)
        self.assertTrue(result["ok"], result["findings"])
        self.assertEqual(result["scanned_files"], 3)

        with open(path, "wb") as stream:
            stream.write(payload + b"changed")
        result = repo_guard.scan(root=self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(
            self._findings(result, "upstream_binary_changed")[0]["path"],
            "upstream/build/tool.exe")

    def test_manifest_include_dir_restores_source_named_data(self) -> None:
        _write(self._path(".gitignore"), "data/\n")
        _write(self._path("src/data/component.ts"), "export const ok = true;\n")
        _write(self._path("scripts/upstream_sources.json"), json.dumps({
            "version": 1,
            "include_dirs": ["src/data"],
            "upstreams": [],
        }))
        result = repo_guard.scan(root=self.root)
        self.assertTrue(result["ok"], result["findings"])
        self.assertEqual(result["scanned_files"], 3)


if __name__ == "__main__":
    unittest.main()
