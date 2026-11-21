"""分支、切换、提交、历史与人工确认重置。"""
from __future__ import annotations

import time
from typing import Any

import dulwich.objects
import dulwich.porcelain
from dulwich.refs import ZERO_SHA, check_ref_format

from . import policy
from .repository import GitError, RepoHandle
from .snapshot import _open_index, head_sha, status

DEFAULT_AUTHOR = b"wasily-ai <ai@wasily.local>"


def _tz_seconds() -> int:
    offset = -time.timezone
    if time.daylight and time.localtime().tm_isdst:
        offset = -time.altzone
    return offset


def current_branch(handle: RepoHandle) -> str:
    try:
        return dulwich.porcelain.active_branch(handle.repo).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def branches(handle: RepoHandle) -> dict[str, Any]:
    names = []
    for ref in handle.repo.refs.keys():
        if ref.startswith(b"refs/heads/"):
            names.append(ref[len(b"refs/heads/"):].decode("utf-8", "replace"))
    return {"ok": True, "current": current_branch(handle), "branches": sorted(names)}


def create_branch(handle: RepoHandle, name: str, start: str = "") -> dict[str, Any]:
    ref = b"refs/heads/" + (name or "").encode("utf-8")
    if not check_ref_format(ref):
        raise GitError("bad_branch", "非法 Git 分支名: %s" % name)
    with handle.lock:
        base = start or current_branch(handle)
        try:
            sha = handle.repo.refs[b"refs/heads/" + base.encode("utf-8")]
        except KeyError as exc:
            raise GitError("unknown_branch", "起始分支不存在: %s" % base) from exc
        if ref in handle.repo.refs:
            raise GitError("branch_exists", "分支已存在: %s" % name)
        handle.repo.refs[ref] = sha
    return {"ok": True, "branch": name, "start": base, "head": sha.decode("ascii")}


def switch(handle: RepoHandle, name: str) -> dict[str, Any]:
    """切换前要求三侧干净，避免 checkout 覆盖尚未提交的修改。"""
    st = status(handle)
    if not st["clean"]:
        raise GitError("dirty_tree", "工作区有未提交修改，请先提交或暂存",
                       staged=len(st["staged"]), changed=len(st["changed"]),
                       untracked=len(st["untracked"]))
    target = name.encode("utf-8")
    with handle.lock:
        if b"refs/heads/" + target not in handle.repo.refs:
            raise GitError("unknown_branch", "分支不存在: %s" % name)
        try:
            dulwich.porcelain.checkout(handle.repo, target, force=False)
        except Exception as exc:  # noqa: BLE001
            raise GitError("switch_failed", "切换分支失败: %s" % exc) from exc
    return {"ok": True, "branch": current_branch(handle)}


def commit(handle: RepoHandle, message: str, author: bytes = DEFAULT_AUTHOR,
           expected_head: str | None = None) -> dict[str, Any]:
    """用 expected_head 和原子引用更新共同防止并发覆盖。"""
    if expected_head is None:
        raise GitError("expected_head_required",
                       "提交必须携带 expected_head；空字符串表示尚无任何提交")
    ok, err = policy.validate_commit(message)
    if not ok:
        raise GitError("bad_message", err)
    repo = handle.repo
    with handle.lock:
        head = head_sha(repo)
        head_hex = head.hex() if head else ""
        if expected_head.lower() != head_hex:
            raise GitError("head_moved", "HEAD 已变化，拒绝覆盖",
                           expected=expected_head, actual=head_hex)
        index = _open_index(repo)
        if index.has_conflicts():
            raise GitError("merge_conflict", "存在未解决的冲突，先解决再提交")
        tree_id = index.commit(repo.object_store)
        now = int(time.time())
        timezone = _tz_seconds()
        obj = dulwich.objects.Commit()
        obj.tree = tree_id
        # Commit.parents 序列化要求 40 字节 hex，而不是 20 字节原始对象名。
        obj.parents = [head_hex.encode("ascii")] if head else []
        obj.author = author
        obj.committer = author
        obj.commit_time = now
        obj.commit_timezone = timezone
        obj.author_time = now
        obj.author_timezone = timezone
        obj.encoding = b"UTF-8"
        obj.message = message.strip().encode("utf-8") + b"\n"
        repo.object_store.add_object(obj)
        old_ref = head_hex.encode("ascii") if head else ZERO_SHA
        try:
            updated = repo.refs.set_if_equals(b"HEAD", old_ref, obj.id)
        except Exception as exc:  # noqa: BLE001
            raise GitError("commit_failed", "更新 HEAD 失败: %s" % exc) from exc
        if not updated:
            actual = head_sha(repo)
            raise GitError("head_moved", "HEAD 在提交期间被其他操作推进",
                           expected=head_hex, actual=actual.hex() if actual else "")
    return {"ok": True, "commit": obj.id.decode("ascii"),
            "message": message.strip(), "branch": current_branch(handle)}


def log(handle: RepoHandle, limit: int = 20) -> dict[str, Any]:
    entries = []
    try:
        walker = handle.repo.get_walker(max_entries=max(1, int(limit or 20)))
        for entry in walker:
            obj = entry.commit
            entries.append({
                "sha": obj.id.decode("ascii"),
                "message": (obj.message or b"").decode(
                    "utf-8", "replace").splitlines()[0][:200],
                "author": (obj.author or b"").decode("utf-8", "replace"),
                "time": int(obj.commit_time or 0),
            })
    except KeyError:
        pass
    return {"ok": True, "entries": entries}


def reset(handle: RepoHandle, mode: str = "mixed", ref: str = "HEAD",
          confirm: bool = False) -> dict[str, Any]:
    """重置只允许人工触发；hard 会覆盖已跟踪文件，绝不能由 Agent 自动调用。"""
    if not confirm:
        raise GitError("need_confirm", "重置历史需要人工确认: %s --%s" % (ref, mode))
    if mode not in ("soft", "mixed", "hard"):
        raise GitError("bad_mode", "不支持的 reset 模式: %s" % mode)
    with handle.lock:
        dulwich.porcelain.reset(handle.repo, mode=mode, treeish=ref)
    return {"ok": True, "mode": mode, "ref": ref}
