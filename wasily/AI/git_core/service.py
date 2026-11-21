"""对内唯一门面：HTTP 路由与 Agent 闭环都通过它访问 Git 能力。

每次调用独立打开仓库句柄，避免长连接与状态泄漏；
提交和重置会自动写审计事件，形成可回溯的迭代轨迹。
"""
from __future__ import annotations

import copy
import os
import threading
import time
from typing import Any

import dulwich.porcelain

from . import audit, index_ops, merge_ops, policy, refs, remote, snapshot, worktrees
from .repository import GitError, RepoHandle, discover_root, repo_id

# 大仓库一次 status 需要数秒；SCM 面板会频繁刷新，必须缓存。
# 缓存键带上 HEAD/index 的 mtime：stage、commit、切分支后立刻失效，
# 不再依赖前端每次都记得传 fresh=1。
_STATUS_TTL = 20.0
_STATUS_CACHE: dict[str, tuple[float, tuple[float, ...], dict[str, Any]]] = {}
_STATUS_LOCK = threading.Lock()


def _repo_sig(handle: RepoHandle) -> tuple[float, ...]:
    """HEAD 与 index 的修改时间；两者变化即代表暂存区或提交已变。"""
    try:
        base = handle.repo.controldir()
    except Exception:  # noqa: BLE001
        base = os.path.join(handle.root, ".git")
    sig = []
    for name in ("HEAD", "index"):
        p = os.path.join(base, name)
        try:
            sig.append(os.path.getmtime(p))
        except OSError:
            sig.append(0.0)
    return tuple(sig)


def _cached_status(key: str, sig: tuple[float, ...]) -> dict[str, Any] | None:
    with _STATUS_LOCK:
        hit = _STATUS_CACHE.get(key)
        if hit and hit[1] == sig and time.time() - hit[0] <= _STATUS_TTL:
            return copy.deepcopy(hit[2])
    return None


def _store_status(key: str, sig: tuple[float, ...], value: dict[str, Any]) -> None:
    with _STATUS_LOCK:
        _STATUS_CACHE[key] = (time.time(), sig, copy.deepcopy(value))


class GitService:
    @staticmethod
    def init(path: str) -> dict[str, Any]:
        target = os.path.abspath(path or ".")
        os.makedirs(target, exist_ok=True)
        created = not os.path.exists(os.path.join(target, ".git"))
        try:
            repo = dulwich.porcelain.init(target)
            repo.close()
        except Exception as exc:  # noqa: BLE001
            raise GitError("init_failed", "初始化仓库失败: %s" % exc) from exc
        root = discover_root(target)
        return {"ok": True, "root": root, "repo_id": repo_id(root),
                "created": created}

    @staticmethod
    def open(path: str) -> dict[str, Any]:
        return GitService.status(path, fresh=True)

    @staticmethod
    def status(path: str, fresh: bool = False) -> dict[str, Any]:
        with RepoHandle(path) as h:
            sig = _repo_sig(h)
            if not fresh:
                hit = _cached_status(h.id, sig)
                if hit is not None:
                    hit["cached"] = True
                    return hit
            out = snapshot.status(h)
            out["policy"] = policy.policy_snapshot(out["branch"])
            out["cached"] = False
            _store_status(h.id, sig, out)
            return out

    @staticmethod
    def diff(path: str, mode: str = "head_worktree", rel: str = "") -> dict[str, Any]:
        with RepoHandle(path) as h:
            return snapshot.diff(h, mode, rel)

    @staticmethod
    def content(path: str, rel: str, side: str = "worktree") -> dict[str, Any]:
        with RepoHandle(path) as h:
            return snapshot.content(h, rel, side)

    @staticmethod
    def hunks(path: str, rel: str, base: str = "index") -> dict[str, Any]:
        with RepoHandle(path) as h:
            return index_ops.hunks(h, rel, base)

    @staticmethod
    def stage(path: str, files: list[str]) -> dict[str, Any]:
        with RepoHandle(path) as h:
            return index_ops.stage(h, files)

    @staticmethod
    def unstage(path: str, files: list[str]) -> dict[str, Any]:
        with RepoHandle(path) as h:
            return index_ops.unstage(h, files)

    @staticmethod
    def stage_hunks(path: str, rel: str, ids: list[str]) -> dict[str, Any]:
        with RepoHandle(path) as h:
            return index_ops.stage_hunks(h, rel, ids)

    @staticmethod
    def unstage_hunks(path: str, rel: str, ids: list[str]) -> dict[str, Any]:
        with RepoHandle(path) as h:
            return index_ops.unstage_hunks(h, rel, ids)

    @staticmethod
    def discard(path: str, rel: str) -> dict[str, Any]:
        with RepoHandle(path) as h:
            return index_ops.discard(h, rel)

    @staticmethod
    def branches(path: str) -> dict[str, Any]:
        with RepoHandle(path) as h:
            return refs.branches(h)

    @staticmethod
    def create_branch(path: str, name: str, start: str = "") -> dict[str, Any]:
        with RepoHandle(path) as h:
            return refs.create_branch(h, name, start)

    @staticmethod
    def switch(path: str, name: str) -> dict[str, Any]:
        with RepoHandle(path) as h:
            return refs.switch(h, name)

    @staticmethod
    def commit(path: str, message: str, expected_head: str | None = None,
               iteration_id: str = "", author: str = "") -> dict[str, Any]:
        with RepoHandle(path) as h:
            res = refs.commit(
                h, message,
                author=author.encode("utf-8") if author else refs.DEFAULT_AUTHOR,
                expected_head=expected_head)
            audit.append_event(h.id, iteration_id, {
                "actor": "agent" if iteration_id else "user",
                "operation": "commit",
                "branch": res.get("branch", ""),
                "head_after": res.get("commit", ""),
                "message": message,
                "result": "passed",
            })
            return res

    @staticmethod
    def log(path: str, limit: int = 20) -> dict[str, Any]:
        with RepoHandle(path) as h:
            return refs.log(h, limit)

    @staticmethod
    def reset(path: str, mode: str = "mixed", ref: str = "HEAD",
              confirm: bool = False) -> dict[str, Any]:
        with RepoHandle(path) as h:
            res = refs.reset(h, mode, ref, confirm)
            audit.append_event(h.id, "", {
                "actor": "user", "operation": "reset",
                "mode": mode, "ref": ref, "result": "passed",
            })
            return res

    @staticmethod
    def revert(path: str, committish: str, message: str = "",
               no_commit: bool = False) -> dict[str, Any]:
        with RepoHandle(path) as h:
            res = refs.revert(h, committish, message, no_commit)
            audit.append_event(h.id, "", {
                "actor": "user", "operation": "revert",
                "committish": committish, "no_commit": no_commit,
                "head_after": res.get("head_sha", ""), "result": "passed",
            })
            return res

    @staticmethod
    def audit_trail(path: str, limit: int = 50) -> dict[str, Any]:
        with RepoHandle(path) as h:
            return {"ok": True, "events": audit.read_events(h.id, limit)}

    # ---------- 远程 ----------

    @staticmethod
    def clone(url: str, target: str, branch: str = "", depth: int | None = None,
              username: str = "", password: str = "") -> dict[str, Any]:
        res = remote.clone(url, target, branch, depth, username, password)
        audit.append_event("", "", {
            "actor": "user", "operation": "clone",
            "target": target, "branch": branch, "result": "passed",
        })
        return res

    @staticmethod
    def remotes(path: str) -> dict[str, Any]:
        with RepoHandle(path) as h:
            return remote.remotes(h)

    @staticmethod
    def remote_add(path: str, name: str, url: str) -> dict[str, Any]:
        with RepoHandle(path) as h:
            res = remote.add_remote(h, name, url)
            audit.append_event(h.id, "", {
                "actor": "user", "operation": "remote_add",
                "remote": res.get("name", ""), "result": "passed",
            })
            return res

    @staticmethod
    def remote_remove(path: str, name: str) -> dict[str, Any]:
        with RepoHandle(path) as h:
            res = remote.remove_remote(h, name)
            audit.append_event(h.id, "", {
                "actor": "user", "operation": "remote_remove",
                "remote": res.get("name", ""), "result": "passed",
            })
            return res

    @staticmethod
    def fetch(path: str, name: str = "origin", prune: bool = False,
              username: str = "", password: str = "") -> dict[str, Any]:
        with RepoHandle(path) as h:
            return remote.fetch(h, name, prune, username, password)

    @staticmethod
    def pull(path: str, name: str = "origin", branch: str = "",
             ff_only: bool = True, username: str = "",
             password: str = "") -> dict[str, Any]:
        with RepoHandle(path) as h:
            res = remote.pull(h, name, branch, ff_only, username, password)
            if res.get("changed"):
                audit.append_event(h.id, "", {
                    "actor": "user", "operation": "pull", "result": "passed",
                    "head_after": res.get("after", ""),
                })
            return res

    @staticmethod
    def push(path: str, branch: str = "", name: str = "origin",
             set_upstream: bool = True, username: str = "",
             password: str = "") -> dict[str, Any]:
        with RepoHandle(path) as h:
            res = remote.push(h, branch, name, set_upstream, username, password)
            audit.append_event(h.id, "", {
                "actor": "user", "operation": "push", "result": "passed",
                "branch": res.get("branch", ""), "head_after": res.get("head", ""),
                "remote_ref": "refs/heads/%s" % res.get("branch", ""),
            })
            return res

    # ---------- 合并 ----------

    @staticmethod
    def merge(path: str, branch: str, message: str = "",
              no_commit: bool = False) -> dict[str, Any]:
        with RepoHandle(path) as h:
            res = merge_ops.merge(h, branch, message, no_commit)
            audit.append_event(h.id, "", {
                "actor": "user", "operation": "merge",
                "branch": branch, "head_after": res.get("commit", ""),
                "result": "conflicted" if res.get("conflicts") else "passed",
                "conflicts": res.get("conflicts", []),
            })
            return res

    @staticmethod
    def merge_conflicts(path: str) -> dict[str, Any]:
        with RepoHandle(path) as h:
            return merge_ops.conflicts(h)

    @staticmethod
    def merge_abort(path: str, confirm: bool = True) -> dict[str, Any]:
        with RepoHandle(path) as h:
            res = merge_ops.abort(h, confirm)
            audit.append_event(h.id, "", {
                "actor": "user", "operation": "merge_abort", "result": "passed",
            })
            return res

    @staticmethod
    def merge_continue(path: str, message: str = "") -> dict[str, Any]:
        with RepoHandle(path) as h:
            res = merge_ops.continue_merge(h, message)
            audit.append_event(h.id, "", {
                "actor": "user", "operation": "merge_continue",
                "head_after": res.get("commit", ""), "result": "passed",
            })
            return res

    # ---------- 并行隔离 ----------

    @staticmethod
    def worktree_create(path: str, name: str, branch: str,
                        dest: str = "") -> dict[str, Any]:
        with RepoHandle(path) as h:
            return worktrees.create(h, name, branch, dest)

    @staticmethod
    def worktree_list(path: str) -> dict[str, Any]:
        with RepoHandle(path) as h:
            return worktrees.list_all(h)

    @staticmethod
    def worktree_remove(path: str, dest: str, force: bool = False) -> dict[str, Any]:
        with RepoHandle(path) as h:
            return worktrees.remove(h, dest, force)
