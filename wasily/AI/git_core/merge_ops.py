"""合并操作：merge / 冲突三阶段内容 / abort / continue。

合并是唯一会产生多父提交的路径，也是最容易把工作区搞脏的地方：
- 合并前必须干净，否则 abort 无法安全还原；
- 冲突必须以 stage 1/2/3 的形式交给 IDE，而不是自己猜内容；
- abort 只在确认存在 MERGE_HEAD 时执行。
"""
from __future__ import annotations

import os
from typing import Any

import dulwich.index
import dulwich.porcelain

from .refs import DEFAULT_AUTHOR
from .repository import GitError, RepoHandle
from .snapshot import head_sha, is_binary, status

MERGE_FILES = (b"MERGE_HEAD", b"MERGE_MSG", b"MERGE_MODE")


def _control_dir(repo: Any) -> str:
    try:
        return repo.controldir()
    except AttributeError:
        return repo.path


def in_progress(handle: RepoHandle) -> bool:
    return os.path.exists(os.path.join(_control_dir(handle.repo), "MERGE_HEAD"))


def _blob_text(repo: Any, entry: Any) -> tuple[str, bool]:
    if entry is None:
        return "", False
    try:
        data = repo.object_store[entry.sha].data
    except Exception:  # noqa: BLE001
        return "", False
    if is_binary(data):
        return "", True
    return data.decode("utf-8", "replace"), False


def conflicts(handle: RepoHandle) -> dict[str, Any]:
    repo = handle.repo
    raw: list[tuple[Any, Any]] = []
    try:
        index = repo.open_index()
        if index.has_conflicts():
            # 1.2.21 没有 Index.conflicts()，冲突条目藏在 items() 的
            # ConflictedIndexEntry（ancestor / this / other）里。
            raw = [(p, e) for p, e in index.items()
                   if isinstance(e, dulwich.index.ConflictedIndexEntry)]
    except Exception:  # noqa: BLE001
        raw = []
    files = []
    for path, entry in raw:
        rel = path.decode("utf-8", "replace") if isinstance(path, bytes) else str(path)
        item: dict[str, Any] = {"path": rel, "binary": False}
        for name, ent in (("base", entry.ancestor),
                          ("ours", entry.this),
                          ("theirs", entry.other)):
            text, binary = _blob_text(repo, ent)
            item[name] = text
            item["binary"] = item["binary"] or binary
        files.append(item)
    return {"ok": True, "paths": [f["path"] for f in files], "files": files}


def merge(handle: RepoHandle, branch: str, message: str = "",
          no_commit: bool = False, author: bytes = DEFAULT_AUTHOR) -> dict[str, Any]:
    st = status(handle)
    if not st["clean"]:
        raise GitError("dirty_tree", "合并前需要干净工作区",
                       staged=len(st["staged"]), changed=len(st["changed"]))
    msg = (message or ("chore: 合并 %s" % branch)).encode("utf-8")
    with handle.lock:
        try:
            sha, conflicted = dulwich.porcelain.merge(
                handle.repo, branch, no_commit=no_commit, message=msg,
                author=author, committer=author)
        except KeyError as exc:
            raise GitError("unknown_branch", "分支不存在: %s" % branch) from exc
        except Exception as exc:  # noqa: BLE001
            raise GitError("merge_failed", "合并失败: %s" % exc) from exc
    paths = [p.decode("utf-8", "replace") if isinstance(p, bytes) else str(p)
             for p in (conflicted or [])]
    return {"ok": not paths,
            "commit": sha.hex() if isinstance(sha, bytes) else (str(sha) if sha else ""),
            "conflicts": paths}


def abort(handle: RepoHandle, confirm: bool = True) -> dict[str, Any]:
    if not confirm:
        raise GitError("need_confirm", "放弃合并需要人工确认")
    if not in_progress(handle):
        raise GitError("no_merge", "当前没有进行中的合并")
    repo = handle.repo
    ctrl = _control_dir(repo)
    with handle.lock:
        dulwich.porcelain.reset(repo, mode="hard", treeish="HEAD")
        for name in MERGE_FILES:
            path = os.path.join(ctrl, name.decode("ascii"))
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
    return {"ok": True, "head": (head_sha(repo) or b"").hex()}


def continue_merge(handle: RepoHandle, message: str = "",
                   author: bytes = DEFAULT_AUTHOR) -> dict[str, Any]:
    """解决冲突并暂存后继续合并；仍存在未解决冲突时拒绝。"""
    if not in_progress(handle):
        raise GitError("no_merge", "当前没有进行中的合并")
    repo = handle.repo
    index = repo.open_index()
    if index.has_conflicts():
        raise GitError("unresolved", "仍有未解决的冲突文件", paths=list(conflicts(handle)["paths"]))
    msg = (message or "").strip()
    if not msg:
        try:
            with open(os.path.join(_control_dir(repo), "MERGE_MSG"), "rb") as f:
                msg = f.read().decode("utf-8", "replace").strip().splitlines()[0]
        except OSError:
            msg = "chore: 完成合并"
    if not msg:
        msg = "chore: 完成合并"
    with handle.lock:
        try:
            sha = dulwich.porcelain.commit(
                repo, message=msg.encode("utf-8"), author=author, committer=author)
        except Exception as exc:  # noqa: BLE001
            raise GitError("merge_commit_failed", "提交合并结果失败: %s" % exc) from exc
        for name in MERGE_FILES:
            path = os.path.join(_control_dir(repo), name.decode("ascii"))
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
    return {"ok": True,
            "commit": sha.hex() if isinstance(sha, bytes) else str(sha),
            "message": msg}
