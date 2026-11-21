"""并行迭代的工作目录隔离。

每个 feature 迭代用独立 worktree，放在用户数据目录而不是仓库内：
- 仓库里的未提交修改不会混入 Agent 的提交；
- worktree 本身也不会被 .gitignore 漏掉而误入库。

Dulwich 1.2.21 的 force=False 脏检查尚未实现，所以删除前必须自己检查。
"""
from __future__ import annotations

import os
from typing import Any

import dulwich.porcelain
import dulwich.repo

from .repository import GitError, RepoHandle


def worktree_root(repo_id: str) -> str:
    base = os.environ.get("WASILY_WORKTREE_DIR")
    if base:
        return os.path.join(base, repo_id)
    root = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), ".local", "share")
    return os.path.join(root, "Wasily", "worktrees", repo_id)


def create(handle: RepoHandle, name: str, branch: str,
           path: str = "") -> dict[str, Any]:
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise GitError("bad_name", "工作目录名不合法: %s" % name)
    dest = os.path.abspath(path or os.path.join(worktree_root(handle.id), name))
    if os.path.exists(dest) and os.listdir(dest):
        raise GitError("target_not_empty", "目标目录非空: %s" % dest)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with handle.lock:
        try:
            dulwich.porcelain.worktree_add(handle.repo, dest, branch=branch)
        except Exception as exc:  # noqa: BLE001
            raise GitError("worktree_failed", "创建隔离目录失败: %s" % exc) from exc
    return {"ok": True, "path": dest, "branch": branch}


def list_all(handle: RepoHandle) -> dict[str, Any]:
    out = []
    try:
        infos = dulwich.porcelain.worktree_list(handle.repo)
    except Exception:  # noqa: BLE001
        infos = []
    for info in infos:
        path = getattr(info, "path", None) or getattr(info, "worktree_path", "")
        head = getattr(info, "head", None)
        branch = getattr(info, "branch", None)
        out.append({
            "path": path.decode("utf-8", "replace") if isinstance(path, bytes) else str(path),
            "head": head.hex() if isinstance(head, bytes) else (str(head) if head else ""),
            "branch": branch.decode("utf-8", "replace") if isinstance(branch, bytes)
                      else (branch or ""),
        })
    return {"ok": True, "worktrees": out}


def _dirty(path: str) -> bool:
    try:
        repo = dulwich.repo.Repo(path)
    except Exception:  # noqa: BLE001
        return True
    try:
        st = dulwich.porcelain.status(repo)
        # staged 是 {'add': [], 'delete': [], 'modify': []}，字典本身恒为真，
        # 必须逐个列表判断，否则任何目录都会被误判成脏。
        staged = st.staged or {}
        has_staged = any(staged.get(k) for k in ("add", "delete", "modify"))
        return bool(has_staged or st.unstaged or st.untracked)
    except Exception:  # noqa: BLE001
        return True
    finally:
        repo.close()


def remove(handle: RepoHandle, path: str, force: bool = False) -> dict[str, Any]:
    dest = os.path.abspath(path)
    if dest == os.path.abspath(handle.root):
        raise GitError("bad_path", "不能删除主工作区")
    if os.path.isdir(dest) and not force and _dirty(dest):
        raise GitError("worktree_dirty",
                       "隔离目录有未提交内容，拒绝删除（确认后可 force）: %s" % dest)
    with handle.lock:
        try:
            dulwich.porcelain.worktree_remove(handle.repo, dest, force=True)
        except Exception as exc:  # noqa: BLE001
            raise GitError("worktree_remove_failed", "删除隔离目录失败: %s" % exc) from exc
    return {"ok": True, "path": dest}
