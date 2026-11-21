"""仓库访问层：发现仓库、分配锁、校验路径。"""
from __future__ import annotations

import hashlib
import os
import threading
from typing import Any

import dulwich.repo


class GitError(Exception):
    """带错误码的 Git 异常，让前端能区分「需人工处理」与「内部错误」。"""

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": False, "code": self.code, "error": self.message}
        out.update(self.extra)
        return out


_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def discover_root(path: str) -> str:
    """从文件、子目录或 linked worktree 向上定位工作区根目录。"""
    cur = os.path.abspath(path or ".")
    if os.path.isfile(cur):
        cur = os.path.dirname(cur)
    while True:
        entry = os.path.join(cur, ".git")
        if os.path.isdir(entry) or os.path.isfile(entry):
            return cur
        if (os.path.isdir(os.path.join(cur, "objects"))
                and os.path.isdir(os.path.join(cur, "refs"))):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            raise GitError("not_a_repo", "不是 Git 仓库（未找到 .git）: %s" % path)
        cur = parent


def repo_id(root: str) -> str:
    key = os.path.normcase(os.path.abspath(root)).encode("utf-8")
    return hashlib.sha256(key).hexdigest()[:16]


def lock_for(key: str) -> threading.RLock:
    with _LOCKS_GUARD:
        lk = _LOCKS.get(key)
        if lk is None:
            lk = threading.RLock()
            _LOCKS[key] = lk
        return lk


def safe_rel(root: str, rel: str) -> str:
    """返回规范的仓库相对路径，并阻止元数据访问与符号链接越界。"""
    if not isinstance(rel, str) or not rel or not rel.strip():
        raise GitError("bad_path", "路径不能为空")
    if "\x00" in rel:
        raise GitError("bad_path", "路径包含 NUL 字符")
    raw = rel.replace("\\", "/")
    drive, _ = os.path.splitdrive(raw)
    if drive or raw.startswith("/"):
        raise GitError("bad_path", "路径必须是仓库相对路径: %s" % rel)
    parts = raw.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise GitError("bad_path", "路径含空层级或越界层级: %s" % rel)
    if any(part.lower() == ".git" for part in parts):
        raise GitError("bad_path", "禁止访问 .git 元数据: %s" % rel)
    norm = "/".join(parts)
    root_real = os.path.realpath(os.path.abspath(root))
    candidate = os.path.realpath(os.path.join(root_real, *parts))
    try:
        inside = os.path.commonpath([root_real, candidate]) == root_real
    except ValueError:
        inside = False
    if not inside:
        raise GitError("bad_path", "路径越界: %s" % rel)
    return norm


def disable_autocrlf(repo: Any, root: str = "") -> None:
    """固定字节语义，避免 Windows 全局配置让未修改文件在检出后变脏。"""
    try:
        config = repo.get_config()
        try:
            current = config.get((b"core",), b"autocrlf")
        except KeyError:
            current = None
        if current != b"false":
            config.set((b"core",), b"autocrlf", b"false")
            config.write_to_path()
    except OSError:
        # 只读仓库不能因配置写入失败而失去只读能力。
        return


class RepoHandle:
    """一次操作持有的仓库上下文；with 语句结束时关闭底层句柄。"""

    def __init__(self, path: str) -> None:
        self.root = discover_root(path)
        self.id = repo_id(self.root)
        self.lock = lock_for("repo:" + self.id)
        try:
            self.repo = dulwich.repo.Repo.discover(path or ".")
        except Exception as exc:  # noqa: BLE001
            raise GitError("open_failed", "打开仓库失败: %s" % exc) from exc
        disable_autocrlf(self.repo, self.root)

    def rel(self, rel: str) -> str:
        return safe_rel(self.root, rel)

    def abs(self, rel: str) -> str:
        return os.path.join(self.root, self.rel(rel).replace("/", os.sep))

    def close(self) -> None:
        try:
            self.repo.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "RepoHandle":
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.close()
        return False
