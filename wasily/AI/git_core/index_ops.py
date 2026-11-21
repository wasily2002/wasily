"""整文件与按块索引操作；除切换分支外不得改写工作区内容。"""
from __future__ import annotations

import os
import stat
import tempfile
from typing import Any

import dulwich.objects
from dulwich.index import Index, IndexEntry

from .repository import GitError, RepoHandle
from .snapshot import (Snapshot, apply_groups, change_groups, head_snapshot,
                       hunk_meta, index_bytes, index_entry, index_snapshot,
                       is_binary, split_lines, status, worktree_snapshot)

MAX_HUNKS = 500


def _open_index(repo: Any) -> Index:
    path = repo.index_path()
    return Index(path, read=os.path.exists(path))


def _entry(mode: int, sha: bytes, size: int) -> IndexEntry:
    return IndexEntry((0, 0), (0, 0), 0, 0, mode, 0, 0, size, sha)


def _write_index_bytes(handle: RepoHandle, rel: str, data: bytes, mode: int) -> str:
    repo = handle.repo
    blob = dulwich.objects.Blob.from_string(data)
    repo.object_store.add_object(blob)
    index = _open_index(repo)
    # dulwich 1.2.21 的索引条目存放 40 字节 hex 对象名；传 20 字节原始名会被
    # write_cache_entry 直接拒绝。
    index[rel.encode("utf-8")] = _entry(mode, blob.id, len(data))
    index.write()
    return blob.id.decode("ascii")


def _remove_index_entry(handle: RepoHandle, rel: str) -> None:
    index = _open_index(handle.repo)
    try:
        del index[rel.encode("utf-8")]
    except KeyError:
        pass
    index.write()


def stage(handle: RepoHandle, paths: list[str]) -> dict[str, Any]:
    if not paths:
        return {"ok": True, "staged": [], "removed": []}
    rels = [handle.rel(path) for path in paths]
    removed = [rel for rel in rels if not os.path.lexists(handle.abs(rel))]
    adds = [rel for rel in rels if rel not in set(removed)]
    with handle.lock:
        # WorkTree.stage 同时处理新增、修改、删除、符号链接和文件模式。
        handle.repo.get_worktree().stage(rels)
    return {"ok": True, "staged": adds, "removed": removed}


def unstage(handle: RepoHandle, paths: list[str]) -> dict[str, Any]:
    if not paths:
        return {"ok": True, "unstaged": []}
    rels = [handle.rel(path) for path in paths]
    with handle.lock:
        try:
            handle.repo.get_worktree().unstage(rels)
        except KeyError as exc:
            raise GitError("unstage_failed", "文件不在 HEAD 或索引中: %s" % exc) from exc
    return {"ok": True, "unstaged": rels}


def discard(handle: RepoHandle, rel: str) -> dict[str, Any]:
    """放弃单个文件的未暂存改动；未跟踪文件仅在确认未跟踪时删除。

    这里恢复的是索引里的版本，而不是 HEAD 的版本：已暂存内容属于用户
    显式保留的状态，放弃未暂存改动不能顺带把它也清掉。
    """
    rel = handle.rel(rel)
    with handle.lock:
        info = status(handle)
        if rel in info["conflicts"]:
            raise GitError("conflict", "请先解决冲突，再放弃改动")
        if rel not in info["changed"] and rel not in info["untracked"]:
            raise GitError("not_discardable", "文件没有可放弃的未暂存改动")
        full = handle.abs(rel)
        if os.path.isdir(full) and not os.path.islink(full):
            raise GitError("directory_forbidden", "不会递归删除或替换目录")
        if rel in info["untracked"]:
            if os.path.lexists(full):
                os.remove(full)
            return {"ok": True, "discarded": rel, "untracked": True}

        entry = index_entry(handle.repo, rel)
        if entry is None:
            raise GitError("index_missing", "索引中没有可恢复的文件版本")
        data = index_bytes(handle.repo, rel) or b""
        mode = int(entry.mode)
        if os.path.lexists(full):
            os.remove(full)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        if stat.S_ISLNK(mode):
            os.symlink(os.fsdecode(data), full)
        else:
            # 临时文件 + replace，避免写入中断留下半个文件。
            fd, temporary = tempfile.mkstemp(
                prefix=".wasily-discard-", dir=os.path.dirname(full))
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                os.replace(temporary, full)
            finally:
                if os.path.exists(temporary):
                    os.remove(temporary)
            os.chmod(full, mode)
        return {"ok": True, "discarded": rel, "untracked": False}


def _pair(handle: RepoHandle, rel: str,
          base: str) -> tuple[Snapshot, Snapshot]:
    if base == "head":
        return head_snapshot(handle.repo, rel), index_snapshot(handle.repo, rel)
    if base == "index":
        return index_snapshot(handle.repo, rel), worktree_snapshot(handle, rel)
    raise GitError("bad_base", "块对比基线只支持 head 或 index: %s" % base)


def _groups(handle: RepoHandle, rel: str, base: str):
    before, after = _pair(handle, rel, base)
    if is_binary(before.data) or is_binary(after.data):
        raise GitError("binary_unsupported", "二进制文件只能整文件暂存: %s" % rel)
    base_lines = split_lines(before.data)
    target_lines = split_lines(after.data)
    groups = change_groups(base_lines, target_lines)
    if len(groups) > MAX_HUNKS:
        raise GitError("too_many_hunks", "变更块过多，请先整文件暂存: %s" % rel)
    return before, after, base_lines, target_lines, groups


def _selected(rel: str, base: str, base_lines: list[bytes],
              target_lines: list[bytes], groups: list[list[tuple]],
              ids: list[str]) -> set[int]:
    wanted = {str(item) for item in ids}
    available = {
        hunk_meta(rel, base, base_lines, target_lines, group)["id"]: i
        for i, group in enumerate(groups)
    }
    unknown = wanted - set(available)
    if unknown:
        raise GitError("stale_hunk", "变更块已过期，请刷新后重试: %s" % rel,
                       ids=sorted(unknown))
    return {available[item] for item in wanted}


def hunks(handle: RepoHandle, rel: str, base: str = "index") -> dict[str, Any]:
    rel = handle.rel(rel)
    _, _, base_lines, target_lines, groups = _groups(handle, rel, base)
    return {
        "ok": True,
        "path": rel,
        "base": base,
        "hunks": [hunk_meta(rel, base, base_lines, target_lines, group)
                  for group in groups],
    }


def stage_hunks(handle: RepoHandle, rel: str, ids: list[str]) -> dict[str, Any]:
    rel = handle.rel(rel)
    if not ids:
        return {"ok": True, "path": rel, "staged": [], "sha": ""}
    with handle.lock:
        before, after, base, target, groups = _groups(handle, rel, "index")
        selected = _selected(rel, "index", base, target, groups, ids)
        selected_ids = [
            hunk_meta(rel, "index", base, target, groups[i])["id"]
            for i in sorted(selected)
        ]
        if not after.exists and len(selected) == len(groups):
            _remove_index_entry(handle, rel)
            return {"ok": True, "path": rel, "staged": selected_ids, "sha": ""}
        data = b"".join(apply_groups(base, target, groups, selected))
        ent = index_entry(handle.repo, rel)
        mode = int(ent.mode) if ent else after.mode or before.mode or 0o100644
        sha = _write_index_bytes(handle, rel, data, mode)
    return {"ok": True, "path": rel, "staged": selected_ids, "sha": sha}


def unstage_hunks(handle: RepoHandle, rel: str, ids: list[str]) -> dict[str, Any]:
    """把选中的 HEAD→index 块退回 HEAD，未选中的块继续保留。"""
    rel = handle.rel(rel)
    if not ids:
        return {"ok": True, "path": rel, "unstaged": [], "sha": ""}
    with handle.lock:
        before, after, base, target, groups = _groups(handle, rel, "head")
        dropped = _selected(rel, "head", base, target, groups, ids)
        dropped_ids = [
            hunk_meta(rel, "head", base, target, groups[i])["id"]
            for i in sorted(dropped)
        ]
        if not before.exists and len(dropped) == len(groups):
            _remove_index_entry(handle, rel)
            return {"ok": True, "path": rel, "unstaged": dropped_ids, "sha": ""}
        keep = set(range(len(groups))) - dropped
        data = b"".join(apply_groups(base, target, groups, keep))
        ent = index_entry(handle.repo, rel)
        mode = int(ent.mode) if ent else before.mode or 0o100644
        sha = _write_index_bytes(handle, rel, data, mode)
    return {"ok": True, "path": rel, "unstaged": dropped_ids, "sha": sha}
