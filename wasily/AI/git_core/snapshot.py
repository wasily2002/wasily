"""HEAD、index、worktree 三侧快照，以及字节安全的差异计算。"""
from __future__ import annotations

import difflib
import hashlib
import os
import stat
from dataclasses import dataclass
from typing import Any

import dulwich.objects
import dulwich.porcelain
from dulwich.index import Index

from .repository import GitError, RepoHandle

SNIFF = 8192
HUNK_CONTEXT = 2
PATCH_CONTEXT = 3
_ENC = ("utf-8", "surrogateescape")
_MODES = {
    "head_index": "head_index",
    "index_worktree": "index_worktree",
    "head_worktree": "head_worktree",
    "staged": "head_index",
    "unstaged": "index_worktree",
    "review": "head_worktree",
}


@dataclass(frozen=True)
class Snapshot:
    """空文件是 exists=True/data=b''；缺失文件则由 exists=False 单独表示。"""

    exists: bool
    data: bytes = b""
    mode: int = 0
    sha: str = ""

    @property
    def size(self) -> int:
        return len(self.data)

    def to_dict(self) -> dict[str, Any]:
        return {
            "exists": self.exists,
            "mode": self.mode,
            "sha": self.sha,
            "size": self.size,
        }


def _open_index(repo: Any) -> Index:
    path = repo.index_path()
    return Index(path, read=os.path.exists(path))


def _blob_sha(data: bytes) -> str:
    return dulwich.objects.Blob.from_string(data).id.decode("ascii")


def head_sha(repo: Any) -> bytes | None:
    """保留既有的 20 字节对象名约定，供包内其他迭代模块使用。"""
    try:
        ref = repo.refs[b"HEAD"]
    except (KeyError, IndexError):
        return None
    if not isinstance(ref, bytes):
        return None
    if len(ref) == 20:
        return ref
    try:
        return bytes.fromhex(ref.decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        return None


def head_entry(repo: Any, rel: str) -> tuple[int, bytes] | None:
    head = head_sha(repo)
    if not head:
        return None
    try:
        commit = repo.object_store[head.hex().encode("ascii")]
        tree = repo.object_store[commit.tree]
        mode, sha = tree.lookup_path(
            repo.object_store.__getitem__, rel.encode("utf-8"))
    except KeyError:
        return None
    if stat.S_ISDIR(mode):
        return None
    return int(mode), sha


def index_entry(repo: Any, rel: str):
    try:
        return _open_index(repo)[rel.encode("utf-8")]
    except KeyError:
        return None


def _blob(repo: Any, sha: bytes) -> bytes:
    try:
        obj = repo.object_store[sha]
    except KeyError as exc:
        raise GitError("object_missing", "Git 对象缺失: %s" % _sha_text(sha)) from exc
    if not isinstance(obj, dulwich.objects.Blob):
        raise GitError("object_invalid", "路径没有指向 Blob 对象")
    return obj.data


def head_snapshot(repo: Any, rel: str) -> Snapshot:
    ent = head_entry(repo, rel)
    if ent is None:
        return Snapshot(False)
    mode, sha = ent
    data = _blob(repo, sha)
    return Snapshot(True, data, mode, _sha_text(sha))


def index_snapshot(repo: Any, rel: str) -> Snapshot:
    ent = index_entry(repo, rel)
    if ent is None:
        return Snapshot(False)
    data = _blob(repo, ent.sha)
    return Snapshot(True, data, int(ent.mode), _sha_text(ent.sha))


def worktree_snapshot(handle: RepoHandle, rel: str) -> Snapshot:
    full = handle.abs(rel)
    try:
        st = os.lstat(full)
    except (FileNotFoundError, NotADirectoryError):
        return Snapshot(False)
    if stat.S_ISLNK(st.st_mode):
        data = os.readlink(os.fsencode(full))
        mode = 0o120000
    elif stat.S_ISREG(st.st_mode):
        with open(full, "rb") as f:
            data = f.read()
        mode = 0o100755 if st.st_mode & 0o111 else 0o100644
    else:
        return Snapshot(False)
    return Snapshot(True, data, mode, _blob_sha(data))


def head_bytes(repo: Any, rel: str) -> bytes | None:
    snap = head_snapshot(repo, rel)
    return snap.data if snap.exists else None


def index_bytes(repo: Any, rel: str) -> bytes | None:
    snap = index_snapshot(repo, rel)
    return snap.data if snap.exists else None


def worktree_bytes(handle: RepoHandle, rel: str) -> bytes | None:
    snap = worktree_snapshot(handle, rel)
    return snap.data if snap.exists else None


def is_binary(data: bytes) -> bool:
    if b"\x00" in data[:SNIFF]:
        return True
    try:
        data.decode("utf-8")
        return False
    except UnicodeDecodeError:
        return True


def _dec(value: Any) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def split_lines(data: bytes) -> list[bytes]:
    return data.splitlines(keepends=True)


def change_groups(base: list[bytes], target: list[bytes],
                  context: int = HUNK_CONTEXT) -> list[list[tuple]]:
    matcher = difflib.SequenceMatcher(None, base, target, autojunk=False)
    ops = [op for op in matcher.get_opcodes() if op[0] != "equal"]
    groups: list[list[tuple]] = []
    for op in ops:
        if groups and op[1] - groups[-1][-1][2] <= context:
            groups[-1].append(op)
        else:
            groups.append([op])
    return groups


def hunk_meta(rel: str, base_name: str, base: list[bytes], target: list[bytes],
              group: list[tuple]) -> dict[str, Any]:
    before = b"".join(base[group[0][1]:group[-1][2]])
    after = b"".join(target[group[0][3]:group[-1][4]])
    positions = (group[0][1], group[-1][2], group[0][3], group[-1][4])
    payload = b"\x00".join((
        rel.encode("utf-8"), base_name.encode("ascii"),
        "/".join(str(x) for x in positions).encode("ascii"), before, after,
    ))
    return {
        "id": hashlib.sha256(payload).hexdigest(),
        "base_start": positions[0],
        "base_end": positions[1],
        "target_start": positions[2],
        "target_end": positions[3],
        "before": before.decode(*_ENC),
        "after": after.decode(*_ENC),
    }


def apply_groups(base: list[bytes], target: list[bytes],
                 groups: list[list[tuple]], selected: set[int]) -> list[bytes]:
    out: list[bytes] = []
    cur = 0
    for i, group in enumerate(groups):
        start, end = group[0][1], group[-1][2]
        out.extend(base[cur:start])
        if i in selected:
            out.extend(target[group[0][3]:group[-1][4]])
        else:
            out.extend(base[start:end])
        cur = end
    out.extend(base[cur:])
    return out


def _same(left: Snapshot, right: Snapshot) -> bool:
    return (left.exists == right.exists and left.mode == right.mode
            and left.data == right.data)


def _head_paths(repo: Any) -> set[str]:
    head = head_sha(repo)
    if not head:
        return set()
    try:
        commit = repo.object_store[head.hex().encode("ascii")]
    except KeyError:
        return set()
    paths: set[str] = set()

    def walk(tree_id: bytes, prefix: bytes) -> None:
        tree = repo.object_store[tree_id]
        for name, mode, sha in tree.iteritems():
            path = prefix + name
            if stat.S_ISDIR(mode):
                walk(sha, path + b"/")
            else:
                paths.add(_dec(path))

    walk(commit.tree, b"")
    return paths


def _index_paths(repo: Any) -> set[str]:
    # dulwich 1.2.21 的 Index 没有 keys()，只能遍历 items()
    return {_dec(path) for path, _ in _open_index(repo).items()}


def _conflicts(repo: Any) -> list[str]:
    index = _open_index(repo)
    if not index.has_conflicts():
        return []
    # 同上：冲突项以 stage 1/2/3 标记，Index.conflicts() 在该版本不存在
    return sorted({_dec(path) for path, entry in index.items()
                   if getattr(entry, "stage", 0)})


class _IgnoreRules:
    """工作区忽略规则的最小实现。

    为什么不用 dulwich.IgnoreFilterManager：它对每个路径都要重新组装
    整条祖先链上的过滤器，两万文件的仓库单次判定要 50 秒以上。
    这里把每个目录的规则编译一次，用「组合正则快速否决」避免逐条匹配：
    绝大多数路径一条规则都不命中，只需两次正则调用。
    """

    def __init__(self, root: str) -> None:
        self.root = root
        self._cache: dict[str, tuple[Any, list[Any]]] = {}

    @staticmethod
    def _translate(pattern: str) -> str:
        import re
        out = []
        for ch in pattern:
            if ch == "*":
                out.append(".*")
            elif ch == "?":
                out.append(".")
            else:
                out.append(re.escape(ch))
        return "".join(out)

    def _rules(self, rel_dir: str):
        if rel_dir in self._cache:
            return self._cache[rel_dir]
        import re
        raw: list[tuple[bool, bool, bool, str]] = []
        path = os.path.join(self.root, rel_dir, ".gitignore") if rel_dir \
            else os.path.join(self.root, ".gitignore")
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    negated = line.startswith("!")
                    if negated:
                        line = line[1:]
                    dir_only = line.endswith("/")
                    if dir_only:
                        line = line[:-1]
                    anchored = "/" in line
                    if line.startswith("/"):
                        line = line[1:]
                    line = line.replace("**", "*")
                    if line:
                        raw.append((negated, dir_only, anchored, line))
        except OSError:
            pass
        items = [(neg, dir_only, anchored,
                  re.compile("(?s)" + self._translate(pat) + r"\Z"))
                 for neg, dir_only, anchored, pat in raw]
        combined = None
        if items:
            # 只是快速否决用，真正的胜负仍按原始顺序逐条判定
            bodies = "|".join("(?:" + self._translate(pat) + ")" for pat in
                              [x[3] for x in raw])
            combined = re.compile("(?s)(?:" + bodies + ")")
        self._cache[rel_dir] = (combined, items)
        return self._cache[rel_dir]

    def ignored(self, rel: str, is_dir: bool) -> bool:
        parts = rel.split("/")
        name = parts[-1]
        verdict = False
        for i in range(len(parts)):
            combined, items = self._rules("/".join(parts[:i]))
            if combined is None:
                continue
            rest = "/".join(parts[i:])
            if not (combined.search(name) or (rest != name and combined.search(rest))):
                continue
            for negated, dir_only, anchored, regex in items:
                if dir_only and not is_dir:
                    continue
                target = rest if anchored else name
                if regex.match(target):
                    verdict = not negated
        return verdict


def _scan_worktree(handle: RepoHandle, tracked: set[str]) -> tuple[
        dict[str, tuple[int, int, int, int]], set[str]]:
    """一次遍历得到「已跟踪文件的 stat」与「未跟踪文件集合」。

    被忽略的目录在遍历时就剪掉（node_modules / .venv / 模型目录），
    未跟踪文件不需要 stat，只有已跟踪文件才取 size/mtime 做短路比较；
    stat 是唯一慢 I/O，用线程池并发执行。
    """
    ign = _IgnoreRules(handle.root)
    root = handle.root
    found: list[str] = []
    stack = [""]
    while stack:
        rel_dir = stack.pop()
        full = os.path.join(root, rel_dir) if rel_dir else root
        try:
            rows = list(os.scandir(full))
        except OSError:
            continue
        for entry in rows:
            name = entry.name
            rel = name if not rel_dir else rel_dir + "/" + name
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if name == ".git":
                    continue
                if ign.ignored(rel, True):
                    continue
                stack.append(rel)
                continue
            if name == ".git":
                # linked worktree 里 .git 是普通文件，不能当成未跟踪内容
                continue
            # 文件先全部收集：已跟踪文件本来就不可能被忽略，
            # 只有未跟踪候选才需要跑忽略规则，省掉两万次模式匹配。
            found.append(rel)
    all_files = set(found)
    stats: dict[str, tuple[int, int, int, int]] = {}

    def probe(rel: str):
        try:
            st = os.lstat(os.path.join(root, rel))
        except OSError:
            return None
        if stat.S_ISDIR(st.st_mode):
            return None
        return (rel, int(st.st_size), int(st.st_mtime),
                _mode_key(st.st_mode), st.st_mtime_ns)

    targets = [rel for rel in found if rel in tracked]
    if targets:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=24) as pool:
            for row in pool.map(probe, targets, chunksize=64):
                if row is not None:
                    stats[row[0]] = row[1:]
    candidates = all_files - tracked
    untracked = {rel for rel in candidates if not ign.ignored(rel, False)}
    return stats, untracked


def _mode_key(mode: int) -> int:
    kind = int(mode) & 0o170000
    if kind == 0o100000:
        return 0o100755 if int(mode) & 0o111 else 0o100644
    return kind


def _sha_text(sha: Any) -> str:
    if isinstance(sha, bytes):
        return sha.hex() if len(sha) == 20 else sha.decode("ascii", "replace")
    return str(sha)


def _mtime_sec(ent: Any) -> int:
    value = ent.mtime
    if isinstance(value, tuple) and value:
        return int(value[0])
    return int(value)


def _worktree_fast_match(handle: RepoHandle, rel: str, ent: Any,
                         index_mtime_ns: int = 0) -> bool:
    """stat 与索引一致时直接判定未修改。

    只读内容会让 2 万文件的大仓库每次刷新耗时数分钟；git 本身也用
    size+mtime 做这道短路，差异再回退到内容比较。文件 mtime 不早于索引
    写入时间时属于同一时间片（racy），此时必须比内容，否则「同秒等长修改」
    会被误判为干净，分支切换前的脏树检查就形同虚设。
    """
    try:
        st = os.lstat(handle.abs(rel))
    except (FileNotFoundError, NotADirectoryError):
        return False
    if stat.S_ISDIR(st.st_mode):
        return False
    if (int(ent.size) != st.st_size
            or _mode_key(ent.mode) != _mode_key(st.st_mode)):
        return False
    if index_mtime_ns and st.st_mtime_ns >= index_mtime_ns:
        return False
    return _mtime_sec(ent) == int(st.st_mtime)


def _tree_map(repo: Any) -> dict[str, tuple[int, str]]:
    head = head_sha(repo)
    if not head:
        return {}
    out: dict[str, tuple[int, str]] = {}
    try:
        commit = repo.object_store[head.hex().encode("ascii")]
        stack: list[tuple[bytes, str]] = [(commit.tree, "")]
        while stack:
            tree_id, prefix = stack.pop()
            tree = repo.object_store[tree_id]
            for name, mode, sha in tree.iteritems():
                rel = prefix + _dec(name)
                if stat.S_ISDIR(mode):
                    stack.append((sha, rel + "/"))
                else:
                    out[rel] = (_mode_key(mode), _sha_text(sha))
    except KeyError:
        return {}
    return out


def status(handle: RepoHandle) -> dict[str, Any]:
    repo = handle.repo
    tree = _tree_map(repo)
    index = _open_index(repo)
    entries = {_dec(path): ent for path, ent in index.items()}
    staged: list[dict[str, str]] = []
    for rel in sorted(set(tree) | set(entries)):
        before = tree.get(rel)
        ent = entries.get(rel)
        if before and ent and before[0] == _mode_key(ent.mode) \
                and before[1] == _sha_text(ent.sha):
            continue
        code = "A" if not before else "D" if not ent else "M"
        staged.append({"path": rel, "code": code})
    changed: list[str] = []
    try:
        index_mtime_ns = os.stat(repo.index_path()).st_mtime_ns
    except OSError:
        index_mtime_ns = 0
    stats, untracked_paths = _scan_worktree(handle, set(entries))
    for rel in sorted(entries):
        ent = entries[rel]
        wt = stats.get(rel)
        if wt:
            size, mt, mk, ns = wt
            if (size == int(ent.size) and mk == _mode_key(ent.mode)
                    and (not index_mtime_ns or ns < index_mtime_ns)
                    and mt == _mtime_sec(ent)):
                continue
        if not _same(index_snapshot(repo, rel), worktree_snapshot(handle, rel)):
            changed.append(rel)
    present = set(stats) | untracked_paths
    for rel in sorted(set(tree) - set(entries)):
        if rel not in present:
            changed.append(rel)
    untracked = sorted(untracked_paths)
    try:
        branch = dulwich.porcelain.active_branch(repo).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        branch = ""
    head = head_sha(repo)
    conflicts = _conflicts(repo)
    return {
        "ok": True,
        "root": handle.root,
        "repo_id": handle.id,
        "branch": branch,
        "head": head.hex() if head else "",
        "staged": staged,
        "changed": changed,
        "untracked": untracked,
        "conflicts": conflicts,
        "clean": not (staged or changed or untracked or conflicts),
    }


def _side_pair(handle: RepoHandle, mode: str,
               rel: str) -> tuple[Snapshot, Snapshot]:
    repo = handle.repo
    if mode == "head_index":
        return head_snapshot(repo, rel), index_snapshot(repo, rel)
    if mode == "index_worktree":
        return index_snapshot(repo, rel), worktree_snapshot(handle, rel)
    return head_snapshot(repo, rel), worktree_snapshot(handle, rel)


def _emit_line(out: list[bytes], prefix: bytes, line: bytes) -> None:
    out.append(prefix + line)
    if not line.endswith((b"\n", b"\r")):
        out.append(b"\n\\ No newline at end of file\n")


def _patch(rel: str, before: Snapshot, after: Snapshot,
           base: list[bytes], target: list[bytes]) -> str:
    old_name = "a/" + rel if before.exists else "/dev/null"
    new_name = "b/" + rel if after.exists else "/dev/null"
    out = [("--- %s\n" % old_name).encode("utf-8"),
           ("+++ %s\n" % new_name).encode("utf-8")]
    matcher = difflib.SequenceMatcher(None, base, target, autojunk=False)
    for group in matcher.get_grouped_opcodes(n=PATCH_CONTEXT):
        old_start, old_end = group[0][1], group[-1][2]
        new_start, new_end = group[0][3], group[-1][4]
        old_line = old_start + 1 if old_end > old_start else old_start
        new_line = new_start + 1 if new_end > new_start else new_start
        header = "@@ -%d,%d +%d,%d @@\n" % (
            old_line, old_end - old_start, new_line, new_end - new_start)
        out.append(header.encode("ascii"))
        for tag, i1, i2, j1, j2 in group:
            if tag in ("equal", "delete", "replace"):
                for line in base[i1:i2]:
                    _emit_line(out, b" " if tag == "equal" else b"-", line)
            if tag in ("insert", "replace"):
                for line in target[j1:j2]:
                    _emit_line(out, b"+", line)
    return b"".join(out).decode(*_ENC)


def _file_diff(handle: RepoHandle, mode: str, rel: str) -> dict[str, Any] | None:
    before, after = _side_pair(handle, mode, rel)
    if _same(before, after):
        return None
    code = "A" if not before.exists else "D" if not after.exists else "M"
    result: dict[str, Any] = {
        "path": rel,
        "code": code,
        "old": before.to_dict(),
        "new": after.to_dict(),
        "binary": False,
        "add": 0,
        "del": 0,
        "patch": "",
        "hunks": [],
    }
    if before.exists and after.exists and before.data == after.data:
        return result
    if is_binary(before.data) or is_binary(after.data):
        result["binary"] = True
        result["patch"] = "Binary files %s and %s differ\n" % (
            "a/" + rel if before.exists else "/dev/null",
            "b/" + rel if after.exists else "/dev/null")
        return result
    base, target = split_lines(before.data), split_lines(after.data)
    groups = change_groups(base, target)
    fingerprint_base = {
        "head_index": "head", "index_worktree": "index",
        "head_worktree": "worktree",
    }[mode]
    result["hunks"] = [hunk_meta(rel, fingerprint_base, base, target, group)
                       for group in groups]
    result["add"] = sum(
        (op[4] - op[3]) for group in groups for op in group
        if op[0] in ("insert", "replace"))
    result["del"] = sum(
        (op[2] - op[1]) for group in groups for op in group
        if op[0] in ("delete", "replace"))
    result["patch"] = _patch(rel, before, after, base, target)
    return result


def diff(handle: RepoHandle, mode: str = "head_worktree", rel: str = "") -> dict[str, Any]:
    try:
        canonical = _MODES[mode]
    except KeyError as exc:
        raise GitError("bad_mode", "不支持的 diff 模式: %s" % mode) from exc
    if rel:
        files = [handle.rel(rel)]
    else:
        repo = handle.repo
        index_paths = _index_paths(repo)
        _, untracked = _scan_worktree(handle, index_paths)
        files = sorted(_head_paths(repo) | index_paths | untracked)
    entries = []
    for path in files:
        item = _file_diff(handle, canonical, path)
        if item is not None:
            entries.append(item)
    return {
        "ok": True,
        "mode": canonical,
        "files": entries,
        "diff": "\n".join(item["patch"] for item in entries if item["patch"]),
    }


def content(handle: RepoHandle, rel: str, side: str = "worktree") -> dict[str, Any]:
    """取某一侧内容，供 IDE 打开虚拟文档做逐行对比。"""
    rel = handle.rel(rel)
    if side == "head":
        snap = head_snapshot(handle.repo, rel)
    elif side == "index":
        snap = index_snapshot(handle.repo, rel)
    elif side == "worktree":
        snap = worktree_snapshot(handle, rel)
    else:
        raise GitError("bad_side", "不支持的内容侧: %s" % side)
    if not snap.exists:
        raise GitError("not_found", "文件不存在: %s" % rel)
    binary = is_binary(snap.data)
    return {
        "ok": True,
        "path": rel,
        "side": side,
        "mode": snap.mode,
        "sha": snap.sha,
        "exists": True,
        "binary": binary,
        "size": snap.size,
        "text": "" if binary else snap.data.decode("utf-8"),
    }
