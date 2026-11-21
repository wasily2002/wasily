"""基于 Dulwich 的 Git 集成层，不依赖本机 git.exe。"""
from __future__ import annotations

import base64
import dataclasses
import difflib
import hashlib
import io
import json
import ntpath
import os
import re
import stat
import tempfile
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import dulwich.errors
import dulwich.index
import dulwich.objects
import dulwich.object_store
import dulwich.porcelain as porcelain
import dulwich.repo

AUTHOR = b"wasily-ai <ai@wasily.local>"
MAX_DIFF_BYTES = 400_000
_EMPTY_TREE = b""


def _decode(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _find_repo_dir(path: str) -> str | None:
    try:
        repo = dulwich.repo.Repo.discover(path or ".")
    except (dulwich.errors.NotGitRepository, FileNotFoundError):
        return None
    try:
        return os.path.abspath(repo.path)
    finally:
        repo.close()


_BYTE_EXACT_READY: set[str] = set()


def _ensure_byte_exact(repo: dulwich.repo.Repo) -> None:
    """固定换行语义：Git 里存什么，工作区就落什么。

    Windows 的系统级 gitconfig 常带 core.autocrlf=true，Dulwich 的
    checkout/revert/merge 会据此把 LF 写成 CRLF，导致回滚后的文件字节与
    提交内容不一致，精确清单提交和回滚审计都会失真。
    """
    try:
        key = os.path.normcase(os.path.abspath(str(repo.controldir())))
    except Exception:  # noqa: BLE001 - 老版本 Dulwich 没有 controldir
        key = os.path.normcase(os.path.abspath(repo.path))
    if key in _BYTE_EXACT_READY:
        return
    config = repo.get_config()
    try:
        current = config.get((b"core",), b"autocrlf")
    except KeyError:
        current = None
    if current != b"false":
        try:
            config.set((b"core",), b"autocrlf", b"false")
            config.write_to_path()
        except OSError:
            # 只读仓库写不了配置时保持原语义，不能因此阻断 Git 操作
            pass
    _BYTE_EXACT_READY.add(key)


def open_repo(path: str) -> dulwich.repo.Repo:
    """支持普通仓库、子目录及 .git 文件形式的 linked worktree。"""
    try:
        repo = dulwich.repo.Repo.discover(path or ".")
    except dulwich.errors.NotGitRepository as exc:
        raise FileNotFoundError("不是 git 仓库: %s" % path) from exc
    _ensure_byte_exact(repo)
    return repo


def repo_root(path: str) -> str:
    repo = open_repo(path)
    try:
        return os.path.abspath(repo.path)
    finally:
        repo.close()


def is_repo(path: str) -> bool:
    return _find_repo_dir(path) is not None


def init(path: str) -> dict[str, Any]:
    root = os.path.abspath(path)
    os.makedirs(root, exist_ok=True)
    if is_repo(root):
        return {"ok": True, "root": repo_root(root), "created": False}
    repo = porcelain.init(root)
    repo.close()
    return {"ok": True, "root": root, "created": True}


def _safe_rel(repo: dulwich.repo.Repo, rel: str,
              allow_empty: bool = False) -> str:
    """所有文件参数都按仓库内相对路径处理，防止读写越界及控制目录。"""
    value = os.fspath(rel or "").replace("\\", "/")
    if not value:
        if allow_empty:
            return ""
        raise ValueError("相对路径不能为空")
    drive, _tail = ntpath.splitdrive(value)
    if drive or value.startswith("/") or os.path.isabs(value):
        raise ValueError("Git 路径必须是仓库内相对路径: %s" % rel)
    parts = value.split("/")
    if "\x00" in value or any(part in ("", ".", "..") for part in parts):
        raise ValueError("Git 路径包含非法层级: %s" % rel)
    if parts[0].casefold() == ".git":
        raise ValueError("禁止直接访问 Git 控制目录")
    normalized = "/".join(parts)
    root_real = os.path.realpath(repo.path)
    candidate = os.path.realpath(os.path.join(root_real, *parts))
    try:
        inside = os.path.commonpath([root_real, candidate]) == root_real
    except ValueError:
        inside = False
    if not inside:
        raise ValueError("Git 路径越过仓库边界: %s" % rel)
    return normalized


def _head_sha(repo: dulwich.repo.Repo) -> bytes | None:
    try:
        value = repo.refs[b"HEAD"]
    except (KeyError, IndexError):
        return None
    if len(value) == 20:
        return value
    try:
        return bytes.fromhex(value.decode("ascii"))
    except (ValueError, UnicodeError):
        return None


def _head_hex(repo: dulwich.repo.Repo) -> str:
    value = _head_sha(repo)
    return value.hex() if value else ""


def _index_sha(repo: dulwich.repo.Repo) -> str:
    try:
        with open(repo.index_path(), "rb") as stream:
            return hashlib.sha256(stream.read()).hexdigest()
    except FileNotFoundError:
        return hashlib.sha256(b"").hexdigest()


def _open_index(repo: dulwich.repo.Repo,
                create: bool = False) -> dulwich.index.Index:
    try:
        return repo.open_index(config=repo.get_config_stack())
    except (dulwich.errors.NoIndexPresent, FileNotFoundError):
        if not create:
            return dulwich.index.Index(repo.index_path(), read=False)
        return dulwich.index.Index(repo.index_path(), read=False)


def _head_entries(repo: dulwich.repo.Repo) -> dict[str, tuple[int, bytes]]:
    result: dict[str, tuple[int, bytes]] = {}
    head = _head_sha(repo)
    if not head:
        return result
    commit = repo.object_store[head]
    for entry in dulwich.object_store.iter_tree_contents(
            repo.object_store, commit.tree):
        if entry.path is not None and entry.mode is not None and entry.sha:
            result[_decode(entry.path)] = (entry.mode, entry.sha)
    return result


def _index_entries(repo: dulwich.repo.Repo) -> dict[str, tuple[int, bytes]]:
    result: dict[str, tuple[int, bytes]] = {}
    index = _open_index(repo)
    for path, entry in index.items():
        if isinstance(entry, dulwich.index.ConflictedIndexEntry):
            continue
        result[_decode(path)] = (entry.mode, entry.sha)
    return result


def _conflicts(repo: dulwich.repo.Repo) -> list[str]:
    return sorted(
        _decode(path) for path, entry in _open_index(repo).items()
        if isinstance(entry, dulwich.index.ConflictedIndexEntry)
    )


def _blob_data(repo: dulwich.repo.Repo, sha: bytes) -> bytes:
    obj = repo.object_store[sha]
    if not isinstance(obj, dulwich.objects.Blob):
        return b""
    return obj.data


def _work_path(repo: dulwich.repo.Repo, rel: str) -> str:
    return os.path.join(repo.path, *rel.split("/"))


def _work_raw(repo: dulwich.repo.Repo, rel: str) -> tuple[bool, bytes, int]:
    full = _work_path(repo, rel)
    try:
        info = os.lstat(full)
    except (FileNotFoundError, NotADirectoryError):
        return False, b"", 0
    if stat.S_ISLNK(info.st_mode):
        return True, os.fsencode(os.readlink(full)), 0o120000
    if not stat.S_ISREG(info.st_mode):
        return False, b"", 0
    with open(full, "rb") as stream:
        data = stream.read()
    return True, data, dulwich.index.cleanup_mode(info.st_mode)


@dataclasses.dataclass
class _Snapshot:
    exists: bool
    data: bytes
    mode: int
    sha: bytes

    @property
    def binary(self) -> bool:
        if b"\x00" in self.data:
            return True
        try:
            self.data.decode("utf-8", "strict")
        except UnicodeDecodeError:
            return True
        return False

    @property
    def too_large(self) -> bool:
        return len(self.data) > MAX_DIFF_BYTES


def _missing_snapshot() -> _Snapshot:
    return _Snapshot(False, b"", 0, _EMPTY_TREE)


def _object_snapshot(
        repo: dulwich.repo.Repo, entries: dict[str, tuple[int, bytes]],
        rel: str) -> _Snapshot:
    try:
        mode, sha = entries[rel]
    except KeyError:
        return _missing_snapshot()
    return _Snapshot(True, _blob_data(repo, sha), mode, sha)


def _work_snapshot(repo: dulwich.repo.Repo, rel: str) -> _Snapshot:
    exists, data, mode = _work_raw(repo, rel)
    if not exists:
        return _missing_snapshot()
    sha = dulwich.objects.Blob.from_string(data).id
    return _Snapshot(True, data, mode, sha)


def _status_raw(repo: dulwich.repo.Repo) -> Any:
    # 禁止 status 刷新 stat 缓存，否则只读操作也会改变客户端持有的 index_sha。
    return porcelain.status(
        repo, untracked_files="all", optional_locks=False)


def _branch(repo: dulwich.repo.Repo) -> str:
    try:
        return _decode(porcelain.active_branch(repo))
    except (KeyError, ValueError, IndexError):
        return "(detached)"


def _reachable(repo: dulwich.repo.Repo, start: bytes) -> set[bytes]:
    seen: set[bytes] = set()
    pending = [start]
    while pending:
        sha = pending.pop()
        if not sha or sha in seen:
            continue
        try:
            commit = repo.object_store[sha]
        except KeyError:
            continue
        if not isinstance(commit, dulwich.objects.Commit):
            continue
        seen.add(sha)
        pending.extend(commit.parents)
    return seen


def _tracking_status(repo: dulwich.repo.Repo, branch: str) -> dict[str, Any]:
    # 没有显式 upstream 时不猜 origin/*，否则 UI 会把本地分支误报成已同步。
    empty = {"tracking": "", "ahead": 0, "behind": 0}
    if not branch or branch == "(detached)":
        return empty
    config = repo.get_config()
    section = (b"branch", branch.encode("utf-8"))
    if not config.has_section(section):
        return empty
    try:
        remote = _decode(config.get(section, b"remote"))
        merge_ref = _decode(config.get(section, b"merge"))
    except KeyError:
        return empty
    if not remote or not merge_ref.startswith("refs/heads/"):
        return empty
    upstream_name = merge_ref[len("refs/heads/"):]
    upstream_ref = (merge_ref if remote == "." else
                    "refs/remotes/" + remote + "/" + upstream_name)
    try:
        upstream = repo.refs[upstream_ref.encode("utf-8")]
        local = repo.refs[b"HEAD"]
    except KeyError:
        return empty
    local_seen = _reachable(repo, local)
    remote_seen = _reachable(repo, upstream)
    return {
        "tracking": (upstream_name if remote == "." else
                     remote + "/" + upstream_name),
        "ahead": len(local_seen - remote_seen),
        "behind": len(remote_seen - local_seen),
    }


def status(path: str) -> dict[str, Any]:
    repo = open_repo(path)
    try:
        st = _status_raw(repo)
        staged = []
        for code, items in (("A", st.staged.get("add", [])),
                            ("M", st.staged.get("modify", [])),
                            ("D", st.staged.get("delete", []))):
            for item in items:
                staged.append({"path": _decode(item), "code": code})
        # 旧面板直接渲染字符串，故新增结构只放在 staged，不改这两个旧字段。
        changed = [_decode(item) for item in st.unstaged]
        untracked = [_decode(item) for item in st.untracked]
        conflicts = _conflicts(repo)
        head = _head_hex(repo)
        branch = _branch(repo)
        result = {
            "ok": True,
            "root": os.path.abspath(repo.path),
            "branch": branch,
            "head": head,
            "head_sha": head,
            "index_sha": _index_sha(repo),
            "staged": staged,
            "changed": changed,
            "untracked": untracked,
            "conflicts": conflicts,
            "clean": not (staged or changed or untracked or conflicts),
        }
        result.update(_tracking_status(repo, branch))
        return result
    finally:
        repo.close()


def _split_lines(data: bytes) -> list[bytes]:
    return data.splitlines(keepends=True)


def _range(start: int, end: int) -> str:
    count = end - start
    first = start + 1 if count else start
    return str(first) if count == 1 else "%d,%d" % (first, count)


def _patch_line(prefix: bytes, line: bytes) -> bytes:
    result = prefix + line
    if not line.endswith(b"\n"):
        result += b"\n\\ No newline at end of file\n"
    return result


@dataclasses.dataclass
class _Hunk:
    ident: str
    old_start: int
    old_end: int
    replacement: list[bytes]
    patch: bytes
    public: dict[str, Any]


def _make_hunks(
        rel: str, target: str, before: _Snapshot, after: _Snapshot,
        head_sha: str, index_sha: str) -> tuple[list[_Hunk], int, int]:
    old_lines = _split_lines(before.data)
    new_lines = _split_lines(after.data)
    matcher = difflib.SequenceMatcher(
        None, old_lines, new_lines, autojunk=False)
    hunks: list[_Hunk] = []
    additions = 0
    deletions = 0
    for group in matcher.get_grouped_opcodes(3):
        old_start = group[0][1]
        old_end = group[-1][2]
        new_start = group[0][3]
        new_end = group[-1][4]
        old_range = _range(old_start, old_end)
        new_range = _range(new_start, new_end)
        patch = bytearray(
            ("@@ -%s +%s @@\n" % (old_range, new_range)).encode("ascii"))
        for tag, i1, i2, j1, j2 in group:
            if tag in ("replace", "delete"):
                deletions += i2 - i1
                for line in old_lines[i1:i2]:
                    patch.extend(_patch_line(b"-", line))
            if tag in ("replace", "insert"):
                additions += j2 - j1
                for line in new_lines[j1:j2]:
                    patch.extend(_patch_line(b"+", line))
            if tag == "equal":
                for line in old_lines[i1:i2]:
                    patch.extend(_patch_line(b" ", line))
        patch_bytes = bytes(patch)
        # ID 不含整个 index 文件哈希，暂存其他文件不会让本文件 hunk 全部失效。
        digest = hashlib.sha256()
        for value in (target.encode("ascii"), rel.encode("utf-8"),
                      before.sha, after.sha, old_range.encode("ascii"),
                      new_range.encode("ascii"), patch_bytes):
            digest.update(len(value).to_bytes(4, "big"))
            digest.update(value)
        ident = digest.hexdigest()
        public = {
            "id": ident,
            "file": rel,
            "old_range": old_range,
            "new_range": new_range,
            "patch": patch_bytes.decode("utf-8", "strict"),
            "head_sha": head_sha,
            "index_sha": index_sha,
        }
        hunks.append(_Hunk(
            ident, old_start, old_end, new_lines[new_start:new_end],
            patch_bytes, public))
    return hunks, additions, deletions


def _diff_file(
        repo: dulwich.repo.Repo, rel: str, target: str,
        head_entries: dict[str, tuple[int, bytes]],
        index_entries: dict[str, tuple[int, bytes]],
        head_sha: str, index_sha: str,
) -> tuple[dict[str, Any] | None, str, list[_Hunk]]:
    if target == "index":
        before = _object_snapshot(repo, head_entries, rel)
        after = _object_snapshot(repo, index_entries, rel)
    else:
        before = _object_snapshot(repo, index_entries, rel)
        after = _work_snapshot(repo, rel)
    if (before.exists == after.exists and before.mode == after.mode
            and before.sha == after.sha):
        return None, "", []
    if not before.exists:
        change = "A"
    elif not after.exists:
        change = "D"
    else:
        change = "M"
    binary = before.binary or after.binary
    too_large = before.too_large or after.too_large
    info: dict[str, Any] = {
        "path": rel,
        "code": change,
        "add": 0,
        "del": 0,
        "binary": binary,
        "too_large": too_large,
        "old_mode": before.mode,
        "new_mode": after.mode,
        "old_sha": before.sha.hex() if before.sha else "",
        "new_sha": after.sha.hex() if after.sha else "",
        "old_size": len(before.data),
        "new_size": len(after.data),
    }
    if binary or too_large:
        # 无效 UTF-8、NUL 和超大内容只给元信息，不能伪造可应用的文本补丁。
        return info, "", []
    hunks, additions, deletions = _make_hunks(
        rel, target, before, after, head_sha, index_sha)
    info["add"] = additions
    info["del"] = deletions
    if not hunks:
        return info, "", []
    old_name = ("a/" + rel) if before.exists else "/dev/null"
    new_name = ("b/" + rel) if after.exists else "/dev/null"
    chunks = ["--- " + old_name, "+++ " + new_name]
    chunks.extend(h.patch.decode("utf-8", "strict").rstrip("\n")
                  for h in hunks)
    return info, "\n".join(chunks) + "\n", hunks


def _diff_repo(
        repo: dulwich.repo.Repo, target: str,
        rel: str = "") -> tuple[dict[str, Any], dict[str, list[_Hunk]]]:
    rel = _safe_rel(repo, rel, allow_empty=True)
    st = _status_raw(repo)
    head_entries = _head_entries(repo)
    index_entries = _index_entries(repo)
    if rel:
        files = [rel]
    else:
        names = set(head_entries) | set(index_entries)
        if target == "work":
            names.update(_decode(item) for item in st.unstaged)
            names.update(_decode(item) for item in st.untracked)
        files = sorted(names)
    head_sha = _head_hex(repo)
    index_sha = _index_sha(repo)
    public_files = []
    chunks = []
    hunk_map: dict[str, list[_Hunk]] = {}
    for name in files:
        name = _safe_rel(repo, name)
        info, text, hunks = _diff_file(
            repo, name, target, head_entries, index_entries,
            head_sha, index_sha)
        if info is None:
            continue
        public_files.append(info)
        if text:
            chunks.append(text.rstrip("\n"))
        hunk_map[name] = hunks
    public_hunks = [h.public for name in files for h in hunk_map.get(name, [])]
    result = {
        "ok": True,
        "target": target,
        "files": public_files,
        "diff": "\n".join(chunks) + ("\n" if chunks else ""),
        "head_sha": head_sha,
        "index_sha": index_sha,
        "hunks": public_hunks,
    }
    return result, hunk_map


def diff(path: str, mode: str = "work", rel: str = "") -> dict[str, Any]:
    """work 为 Index→Worktree；index/cached/staged 为 HEAD→Index。"""
    normalized = str(mode or "work").lower()
    if normalized in ("index", "cached", "staged"):
        target = "index"
    elif normalized == "work":
        target = "work"
    else:
        raise ValueError("未知 diff 模式: %s" % mode)
    repo = open_repo(path)
    try:
        return _diff_repo(repo, target, rel)[0]
    finally:
        repo.close()


def stage(path: str, files: list[str] | None = None,
          exact: bool = False) -> dict[str, Any]:
    repo = open_repo(path)
    try:
        if files is None and exact:
            raise ValueError("exact=True 必须显式提供 files")
        targets = files
        if targets is None:
            st = _status_raw(repo)
            targets = [_decode(item) for item in st.unstaged]
            targets.extend(_decode(item) for item in st.untracked)
        targets = [_safe_rel(repo, item) for item in targets]
        if not targets:
            return {"ok": True, "staged": [], "index_sha": _index_sha(repo)}
        # exact 始终走显式路径，绝不能退化成 add -A。
        porcelain.add(repo, paths=targets)
        return {"ok": True, "staged": targets, "index_sha": _index_sha(repo)}
    finally:
        repo.close()


def _set_index_data(
        repo: dulwich.repo.Repo, index: dulwich.index.Index, rel: str,
        data: bytes | None, mode: int = 0) -> None:
    key = rel.encode("utf-8")
    if data is None:
        try:
            del index[key]
        except KeyError:
            pass
        return
    blob = dulwich.objects.Blob.from_string(data)
    repo.object_store.add_object(blob)
    try:
        old = index[key]
    except KeyError:
        old = None
    if old is not None and not isinstance(
            old, dulwich.index.ConflictedIndexEntry):
        entry = dataclasses.replace(
            old, mode=mode or old.mode, size=len(data), sha=blob.id)
    else:
        entry = dulwich.index.index_entry_from_tree_entry(
            mode or 0o100644, blob.id, size=len(data))
    index[key] = entry


def unstage(path: str, files: list[str] | None = None) -> dict[str, Any]:
    """仅把索引恢复至 HEAD，不重写工作区，也保留原始字节。"""
    repo = open_repo(path)
    try:
        index = _open_index(repo, create=True)
        targets = files
        if targets is None:
            targets = [_decode(item) for item in index]
        targets = [_safe_rel(repo, item) for item in targets]
        head_entries = _head_entries(repo)
        for rel in targets:
            try:
                mode, sha = head_entries[rel]
            except KeyError:
                _set_index_data(repo, index, rel, None)
            else:
                _set_index_data(repo, index, rel, _blob_data(repo, sha), mode)
        index.write()
        return {
            "ok": True,
            "unstaged": targets,
            "head_sha": _head_hex(repo),
            "index_sha": _index_sha(repo),
        }
    finally:
        repo.close()


def _stale_result(head_sha: str, index_sha: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "code": "stale_hash",
            "message": "HEAD 或 Index 已变化，请刷新 diff",
        },
        "head_sha": head_sha,
        "index_sha": index_sha,
    }


def _apply_hunks(
        before: _Snapshot, hunks: list[_Hunk], selected: set[str],
        invert: bool) -> bytes:
    old_lines = _split_lines(before.data)
    output: list[bytes] = []
    cursor = 0
    for hunk in hunks:
        output.extend(old_lines[cursor:hunk.old_start])
        apply_change = (hunk.ident not in selected) if invert else (
            hunk.ident in selected)
        if apply_change:
            output.extend(hunk.replacement)
        else:
            output.extend(old_lines[hunk.old_start:hunk.old_end])
        cursor = hunk.old_end
    output.extend(old_lines[cursor:])
    return b"".join(output)


def _change_hunks(
        path: str, rel: str, hunk_id: str | None,
        expected_head: str | None, expected_index: str | None,
        hunk_ids: list[str] | None, unstage_selected: bool) -> dict[str, Any]:
    repo = open_repo(path)
    try:
        rel = _safe_rel(repo, rel)
        current_head = _head_hex(repo)
        current_index = _index_sha(repo)
        if expected_head is None or expected_index is None:
            return {
                "ok": False,
                "error": {"code": "expected_hash_required",
                          "message": "必须提供 expected_head 和 expected_index"},
                "head_sha": current_head,
                "index_sha": current_index,
            }
        # 空仓库的合法 HEAD 就是空字符串，只有 None 才表示调用方漏传。
        if (expected_head.lower() != current_head.lower()
                or expected_index.lower() != current_index.lower()):
            return _stale_result(current_head, current_index)
        ids = list(hunk_ids or [])
        if hunk_id:
            ids.append(hunk_id)
        selected = set(ids)
        if not selected:
            return {"ok": False,
                    "error": {"code": "hunk_required", "message": "未指定 hunk"}}
        target = "index" if unstage_selected else "work"
        result, hunk_map = _diff_repo(repo, target, rel)
        hunks = hunk_map.get(rel, [])
        available = {hunk.ident for hunk in hunks}
        if not selected.issubset(available):
            return {
                "ok": False,
                "error": {"code": "stale_hunk",
                          "message": "hunk 不存在或内容已变化，请刷新 diff"},
                "head_sha": current_head,
                "index_sha": current_index,
                "hunks": result["hunks"],
            }
        if unstage_selected:
            entries = _head_entries(repo)
            before = _object_snapshot(repo, entries, rel)
            desired_mode = entries.get(rel, (0, b""))[0]
        else:
            entries = _index_entries(repo)
            before = _object_snapshot(repo, entries, rel)
            desired_mode = entries.get(rel, (0, b""))[0]
        data = _apply_hunks(before, hunks, selected, unstage_selected)
        if not unstage_selected:
            work_exists = _work_snapshot(repo, rel).exists
        else:
            work_exists = before.exists
        output_exists = work_exists or bool(data)
        index = _open_index(repo, create=True)
        _set_index_data(
            repo, index, rel, data if output_exists else None, desired_mode)
        index.write()
        key = "unstaged" if unstage_selected else "staged"
        return {
            "ok": True,
            key: sorted(selected),
            "hunk_ids": sorted(selected),
            "head_sha": current_head,
            "index_sha": _index_sha(repo),
        }
    finally:
        repo.close()


def discard(path: str, rel: str) -> dict[str, Any]:
    """仅放弃一个文件的未暂存改动；未跟踪文件只在确实未跟踪时删除。"""
    repo = open_repo(path)
    try:
        rel = _safe_rel(repo, rel)
        st = _status_raw(repo)
        unstaged = {_decode(item) for item in st.unstaged}
        untracked = {_decode(item) for item in st.untracked}
        if rel in _conflicts(repo):
            return _failure("conflict", "请先解决冲突，再暂存文件")
        if rel not in unstaged and rel not in untracked:
            return _failure(
                "not_discardable", "文件没有可放弃的未暂存改动，已拒绝操作")
        full = _work_path(repo, rel)
        if os.path.isdir(full) and not os.path.islink(full):
            return _failure("directory_forbidden", "不会递归删除或替换目录")

        if rel in untracked:
            if os.path.lexists(full):
                os.remove(full)
            return {"ok": True, "discarded": rel, "untracked": True}

        entries = _index_entries(repo)
        if rel not in entries:
            return _failure("index_missing", "索引中没有可恢复的文件版本")
        mode, sha = entries[rel]
        data = _blob_data(repo, sha)
        if os.path.lexists(full):
            os.remove(full)
        os.makedirs(os.path.dirname(full) or repo.path, exist_ok=True)
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
    finally:
        repo.close()


def stage_hunk(path: str, rel: str, hunk_id: str | None = None,
               expected_head: str | None = None,
               expected_index: str | None = None,
               hunk_ids: list[str] | None = None) -> dict[str, Any]:
    return _change_hunks(
        path, rel, hunk_id, expected_head, expected_index, hunk_ids, False)


def unstage_hunk(path: str, rel: str, hunk_id: str | None = None,
                 expected_head: str | None = None,
                 expected_index: str | None = None,
                 hunk_ids: list[str] | None = None) -> dict[str, Any]:
    return _change_hunks(
        path, rel, hunk_id, expected_head, expected_index, hunk_ids, True)


def content(path: str, ref: str, rel: str) -> dict[str, Any]:
    repo = open_repo(path)
    try:
        rel = _safe_rel(repo, rel)
        normalized = str(ref).lower()
        if normalized == "head":
            snapshot = _object_snapshot(repo, _head_entries(repo), rel)
        elif normalized == "index":
            snapshot = _object_snapshot(repo, _index_entries(repo), rel)
        elif normalized == "work":
            snapshot = _work_snapshot(repo, rel)
        else:
            raise ValueError("ref 必须是 head/index/work")
        result: dict[str, Any] = {
            "ok": True,
            "path": rel,
            "ref": normalized,
            "exists": snapshot.exists,
            "binary": False,
            "size": len(snapshot.data),
            "sha": snapshot.sha.hex() if snapshot.sha else "",
        }
        if not snapshot.exists:
            result["text"] = ""
        elif snapshot.binary:
            result["binary"] = True
            result["base64"] = base64.b64encode(snapshot.data).decode("ascii")
        else:
            result["text"] = snapshot.data.decode("utf-8", "strict")
        return result
    finally:
        repo.close()


def commit(path: str, message: str, author: str = "") -> dict[str, Any]:
    repo = open_repo(path)
    try:
        msg = (message or "").strip() or "chore: update workspace"
        identity = author.encode("utf-8") if author else AUTHOR
        sha = porcelain.commit(
            repo, message=msg.encode("utf-8"), author=identity,
            committer=identity)
        return {
            "ok": True,
            "commit": _decode(sha),
            "message": msg,
            "author": _decode(identity),
            "head_sha": _head_hex(repo),
            "index_sha": _index_sha(repo),
        }
    finally:
        repo.close()


def log(path: str, limit: int = 20) -> dict[str, Any]:
    repo = open_repo(path)
    try:
        entries = []
        try:
            walker = repo.get_walker(max_entries=int(limit or 20))
            for item in walker:
                current = item.commit
                entries.append({
                    "sha": _decode(current.id),
                    "message": _decode(current.message).splitlines()[0][:200],
                    "author": _decode(current.author),
                    "time": int(current.commit_time or 0),
                })
        except KeyError:
            pass
        return {"ok": True, "entries": entries}
    finally:
        repo.close()


def _failure(code: str, message: str, **extra: Any) -> dict[str, Any]:
    result = {"ok": False, "error": {"code": code, "message": message}}
    result.update(extra)
    return result


def _ensure_clean(repo: dulwich.repo.Repo) -> dict[str, Any] | None:
    st = _status_raw(repo)
    conflicts = _conflicts(repo)
    if st.staged.get("add") or st.staged.get("modify") or st.staged.get(
            "delete") or st.unstaged or st.untracked or conflicts:
        return _failure(
            "dirty_worktree", "工作区或暂存区有改动，已拒绝覆盖用户内容",
            conflicts=conflicts)
    return None


def branches(path: str) -> dict[str, Any]:
    repo = open_repo(path)
    try:
        current = _branch(repo)
        items = []
        for name in porcelain.branch_list(repo):
            decoded = _decode(name)
            ref = b"refs/heads/" + name
            items.append({
                "name": decoded,
                "current": decoded == current,
                "head_sha": _decode(repo.refs[ref]),
            })
        return {"ok": True, "current": current, "branches": items}
    finally:
        repo.close()


def branch_create(path: str, name: str, start: str = "HEAD") -> dict[str, Any]:
    repo = open_repo(path)
    try:
        porcelain.branch_create(repo, name, objectish=start)
        ref = b"refs/heads/" + name.encode("utf-8")
        return {"ok": True, "branch": name, "commit": _decode(repo.refs[ref])}
    finally:
        repo.close()


def branch_switch(path: str, name: str) -> dict[str, Any]:
    repo = open_repo(path)
    try:
        dirty = _ensure_clean(repo)
        if dirty:
            return dirty
        porcelain.checkout(repo, name, force=False)
        return {
            "ok": True, "branch": _branch(repo),
            "head_sha": _head_hex(repo),
        }
    finally:
        repo.close()


def _public_url(url: str) -> str:
    """返回值永远剥离 URL userinfo，避免配置中的旧凭证经 API 泄露。"""
    try:
        parsed = urlsplit(url)
        if parsed.scheme and parsed.netloc and "@" in parsed.netloc:
            host = parsed.netloc.rsplit("@", 1)[1]
            return urlunsplit((parsed.scheme, host, parsed.path,
                               parsed.query, parsed.fragment))
    except ValueError:
        pass
    return url


def _validate_remote_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise ValueError("远端 URL 无效") from exc
    if parsed.password is not None or (
            parsed.scheme and parsed.netloc and "@" in parsed.netloc):
        raise ValueError("远端 URL 不得包含 userinfo，请使用 username/password 参数")


_DEFAULT_CREDENTIALS: tuple[str, str] | None = None


def _default_credentials() -> tuple[str, str]:
    """推送凭据来源：环境变量优先，其次本机凭据文件。

    凭据只落在仓库之外（%LOCALAPPDATA%\\Wasily\\git-credentials.json），
    不写进代码、不写进 .git/config，避免把 token 带进提交或日志。
    """
    global _DEFAULT_CREDENTIALS
    if _DEFAULT_CREDENTIALS is not None:
        return _DEFAULT_CREDENTIALS
    username = os.environ.get("WASILY_GIT_USERNAME", "")
    token = os.environ.get("WASILY_GIT_TOKEN", "")
    if not token:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        path = os.path.join(base, "Wasily", "git-credentials.json")
        try:
            with open(path, "r", encoding="utf-8") as stream:
                data = json.load(stream)
            username = str(data.get("username") or username)
            token = str(data.get("token") or "")
        except (OSError, ValueError):
            token = ""
    _DEFAULT_CREDENTIALS = (username, token)
    return _DEFAULT_CREDENTIALS


def _validate_transport_remote(repo: dulwich.repo.Repo, remote: str) -> None:
    config = repo.get_config()
    section = (b"remote", remote.encode("utf-8"))
    if config.has_section(section):
        url = _decode(config.get(section, b"url"))
    else:
        url = remote
    # 旧配置也不能把 userinfo 带进 reflog、异常或远端配置文件。
    _validate_remote_url(url)


def remotes(path: str) -> dict[str, Any]:
    repo = open_repo(path)
    try:
        config = repo.get_config()
        items = []
        for section in config.sections():
            if len(section) == 2 and section[0] == b"remote":
                items.append({
                    "name": _decode(section[1]),
                    "url": _public_url(_decode(config.get(section, b"url"))),
                })
        return {"ok": True, "remotes": items}
    finally:
        repo.close()


def remote_add(path: str, name: str, url: str) -> dict[str, Any]:
    _validate_remote_url(url)
    repo = open_repo(path)
    try:
        porcelain.remote_add(repo, name, url)
        return {"ok": True, "name": name, "url": _public_url(url)}
    finally:
        repo.close()


def remote_remove(path: str, name: str) -> dict[str, Any]:
    repo = open_repo(path)
    try:
        porcelain.remote_remove(repo, name)
        return {"ok": True, "name": name}
    finally:
        repo.close()


def _safe_exception(exc: Exception, username: str = "",
                    password: str = "") -> str:
    # Dulwich 的传输异常可能带完整 URL，必须先遮蔽明文及 URL 编码后的密码。
    message = str(exc)
    secrets = [password, quote(password, safe="")] if password else []
    if username:
        secrets.extend([username + ":", quote(username, safe="") + ":"])
    for secret in secrets:
        if secret:
            message = message.replace(secret, "***")
    message = re.sub(r"://[^\s/@]+@<REDACTED_SECRET>", "://***@", message)
    return message or exc.__class__.__name__


def _transport_failure(
        exc: Exception, username: str = "",
        password: str = "") -> dict[str, Any]:
    name = exc.__class__.__name__.lower()
    if "unauthor" in name or "authentic" in name or "permission" in name:
        code = "authentication_failed"
        message = "远端认证失败"
    elif isinstance(exc, porcelain.DivergedBranches):
        code = "non_fast_forward"
        message = "分支已分叉，无法执行 fast-forward"
    else:
        code = "transport_failed"
        message = _safe_exception(exc, username, password)
    return _failure(code, message)


def clone(source: str, target: str, branch: str = "",
          username: str = "", password: str = "") -> dict[str, Any]:
    _validate_remote_url(source)
    try:
        repo = porcelain.clone(
            source, target, branch=branch or None,
            username=username or None, password=password or None,
            checkout=False, errstream=io.BytesIO())
        try:
            # 先落仓库再关换行转换，避免 checkout 阶段被系统级 autocrlf 写成 CRLF
            _ensure_byte_exact(repo)
            if _head_sha(repo):
                porcelain.reset(repo, "hard", "HEAD")
            return {
                "ok": True,
                "root": os.path.abspath(repo.path),
                "branch": _branch(repo),
                "head_sha": _head_hex(repo),
            }
        finally:
            repo.close()
    except Exception as exc:
        return _transport_failure(exc, username, password)


def fetch(path: str, remote: str = "origin",
          username: str = "", password: str = "") -> dict[str, Any]:
    repo = open_repo(path)
    try:
        _validate_transport_remote(repo, remote)
        result = porcelain.fetch(
            repo, remote, username=username or None,
            password=password or None, errstream=io.BytesIO(), force=False)
        refs = {_decode(key): _decode(value)
                for key, value in result.refs.items() if value is not None}
        return {"ok": True, "refs": refs}
    except Exception as exc:
        return _transport_failure(exc, username, password)
    finally:
        repo.close()


def pull(path: str, remote: str = "origin",
         username: str = "", password: str = "") -> dict[str, Any]:
    repo = open_repo(path)
    try:
        _validate_transport_remote(repo, remote)
        dirty = _ensure_clean(repo)
        if dirty:
            return dirty
        branch = _branch(repo)
        if branch == "(detached)":
            return _failure("detached_head", "detached HEAD 不能默认 pull")
        refspec = "refs/heads/" + branch
        porcelain.pull(
            repo, remote, refspecs=refspec, fast_forward=True,
            ff_only=True, force=False, username=username or None,
            password=password or None, errstream=io.BytesIO())
        return {"ok": True, "branch": branch, "head_sha": _head_hex(repo)}
    except Exception as exc:
        return _transport_failure(exc, username, password)
    finally:
        repo.close()


def push(path: str, remote: str = "origin", refspec: str = "",
         username: str = "", password: str = "",
         force: bool = False) -> dict[str, Any]:
    if force:
        return _failure("force_forbidden", "此接口永远禁止 force push")
    repo = open_repo(path)
    try:
        _validate_transport_remote(repo, remote)
        section = (b"remote", remote.encode("utf-8"))
        config = repo.get_config()
        if config.has_section(section):
            try:
                mirror = config.get_boolean(section, b"mirror", False)
            except ValueError:
                mirror = False
            if mirror:
                return _failure(
                    "force_forbidden", "remote.mirror 会强制更新，已拒绝 push")
        branch = _branch(repo)
        if not refspec:
            if branch == "(detached)":
                return _failure(
                    "detached_head", "detached HEAD 必须显式提供 refspec")
            refspec = "refs/heads/{0}:refs/heads/{0}".format(branch)
        if refspec.startswith("+") or ":+" in refspec:
            return _failure("force_forbidden", "refspec 禁止强制更新")
        if not username and not password:
            username, password = _default_credentials()
        result = porcelain.push(
            repo, remote, refspecs=[refspec], force=False,
            username=username or None, password=password or None,
            outstream=io.BytesIO(), errstream=io.BytesIO())
        errors = {
            key: value for key, value in (result.ref_status or {}).items()
            if value is not None
        }
        if errors:
            return _failure("push_rejected", "远端拒绝更新", refs={
                _decode(key): _safe_exception(Exception(_decode(value)),
                                               username, password)
                for key, value in errors.items()})
        return {"ok": True, "branch": branch, "refspec": refspec,
                "head_sha": _head_hex(repo)}
    except Exception as exc:
        return _transport_failure(exc, username, password)
    finally:
        repo.close()


def merge(path: str, committish: str, message: str = "",
          no_commit: bool = False) -> dict[str, Any]:
    repo = open_repo(path)
    try:
        dirty = _ensure_clean(repo)
        if dirty:
            return dirty
        sha, conflicts = porcelain.merge(
            repo, committish, no_commit=no_commit,
            message=message.encode("utf-8") if message else None,
            author=AUTHOR, committer=AUTHOR)
        decoded = [_decode(item) for item in conflicts]
        if decoded:
            return _failure(
                "merge_conflict", "合并产生冲突，请解决后提交",
                commit=None, conflicts=decoded)
        return {"ok": True, "commit": _decode(sha) if sha else "",
                "conflicts": [], "head_sha": _head_hex(repo)}
    finally:
        repo.close()


def worktrees(path: str) -> dict[str, Any]:
    repo = open_repo(path)
    try:
        items = []
        for info in porcelain.worktree_list(repo):
            branch = _decode(info.branch)
            if branch.startswith("refs/heads/"):
                branch = branch[len("refs/heads/"):]
            items.append({
                "path": os.path.abspath(info.path) if info.path else "",
                "branch": branch,
                "commit": _decode(info.head),
                "detached": bool(info.detached),
                "locked": bool(info.locked),
                "prunable": bool(info.prunable),
            })
        return {"ok": True, "worktrees": items}
    finally:
        repo.close()


def worktree_add(path: str, target: str, branch: str = "",
                 commitish: str = "") -> dict[str, Any]:
    repo = open_repo(path)
    try:
        actual = porcelain.worktree_add(
            repo, target, branch=branch or None,
            commit=commitish or None, force=False)
        linked = open_repo(actual)
        try:
            return {
                "ok": True,
                "path": os.path.abspath(linked.path),
                "branch": _branch(linked),
                "commit": _head_hex(linked),
            }
        finally:
            linked.close()
    finally:
        repo.close()


def worktree_remove(path: str, target: str,
                    force: bool = False) -> dict[str, Any]:
    repo = open_repo(path)
    linked = None
    try:
        # Dulwich 1.2.21 的 remove_worktree 尚未检查脏区，必须在此补上。
        if not force:
            try:
                linked = open_repo(target)
                dirty = _ensure_clean(linked)
            except FileNotFoundError:
                dirty = None
            if dirty:
                return dirty
        actual_target = target
        # Windows 临时目录可能同时出现 8.3 短路径与长路径；samefile 比字符串比较可靠。
        for info in porcelain.worktree_list(repo):
            if not info.path:
                continue
            try:
                if os.path.samefile(info.path, target):
                    actual_target = info.path
                    break
            except OSError:
                if os.path.normcase(os.path.realpath(info.path)) == os.path.normcase(
                        os.path.realpath(target)):
                    actual_target = info.path
                    break
        porcelain.worktree_remove(repo, actual_target, force=force)
        return {"ok": True, "path": os.path.abspath(target)}
    finally:
        if linked is not None:
            linked.close()
        repo.close()


def revert(path: str, committish: str, message: str = "",
           no_commit: bool = False) -> dict[str, Any]:
    repo = open_repo(path)
    try:
        dirty = _ensure_clean(repo)
        if dirty:
            return dirty
        sha = porcelain.revert(
            repo, committish, no_commit=no_commit,
            message=message or None, author=AUTHOR, committer=AUTHOR)
        if no_commit:
            # no_commit 要保留反向暂存；恢复删除的文件可能表现为 untracked。
            st = _status_raw(repo)
            targets = [_decode(item) for item in st.unstaged]
            targets.extend(_decode(item) for item in st.untracked)
            if targets:
                porcelain.add(repo, paths=targets)
        else:
            # porcelain.revert 不更新索引，同步 HEAD 才不会留下伪暂存差异。
            repo.get_worktree().reset_index(config=repo.get_config_stack())
        return {"ok": True, "commit": _decode(sha) if sha else "",
                "head_sha": _head_hex(repo)}
    except porcelain.Error as exc:
        return _failure("revert_failed", _safe_exception(exc))
    finally:
        repo.close()


if __name__ == "__main__":
    import json
    import tempfile

    tmp = tempfile.mkdtemp(prefix="wasily_git_")
    print(json.dumps(init(tmp), ensure_ascii=False))
    with open(os.path.join(tmp, "hello.txt"), "wb") as stream:
        stream.write(b"line1\nline2\n")
    staged = stage(tmp, ["hello.txt"], exact=True)
    print(json.dumps(staged, ensure_ascii=False))
    print(json.dumps(commit(tmp, "feat: initial commit"), ensure_ascii=False))
    print(json.dumps(status(tmp), ensure_ascii=False))
