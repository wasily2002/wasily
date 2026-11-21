"""自迭代事务：需求 → 隔离工作区 → 验证 → 提交 → 推送 → 审计。

为什么只有这一套状态机：
- 之前 Agent 隔离运行（agent_runs.py）与迭代闭环（本模块）各自维护
  一份状态与提交逻辑，崩溃恢复和策略校验容易分出两套事实；
- 现在 Agent 运行只是 mode="agent" 的迭代：同为 worktree 隔离、固定快照、
  验证后提交；agent_runs 只负责把它映射成面板需要的运行清单。

状态机：prepared → verified → committed → pushed（awaiting_confirmation 为待人工确认）
任一环节失败都保留现场（worktree 不删），状态写入仓库外的迭代目录，
进程崩溃后可由 recover() 找回未完成的事务。
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from typing import Any

from . import audit, index_ops, policy, refs, remote, snapshot, worktrees
from .repository import GitError, RepoHandle, repo_id
from .snapshot import head_bytes, head_sha, worktree_bytes

MAX_FILES = 2000
MAX_DIFF_BYTES = 600_000
DEFAULT_TIMEOUT = 180
_ITER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{5,47}$")
_JS_EXT = (".js", ".cjs", ".mjs")
PROTECTED_KINDS = ("main", "develop")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _iter_root(rid: str) -> str:
    return os.path.join(audit.audit_dir(), "iterations", rid or "repo")


def _state_path(rid: str, iid: str) -> str:
    if not _ITER_RE.match(iid or ""):
        raise GitError("bad_iteration", "迭代编号不合法: %s" % iid)
    return os.path.join(_iter_root(rid), iid + ".json")


def _state_path_by_id(iid: str) -> str:
    """按迭代编号在仓库外目录中定位状态文件（Agent 运行只拿得到编号）。"""
    if not _ITER_RE.match(iid or ""):
        raise GitError("bad_iteration", "迭代编号不合法: %s" % iid)
    base = os.path.join(audit.audit_dir(), "iterations")
    try:
        names = os.listdir(base)
    except OSError:
        names = []
    for rid in names:
        path = os.path.join(base, rid, iid + ".json")
        if os.path.isfile(path):
            return path
    raise GitError("iteration_not_found", "迭代记录不存在: %s" % iid)


def state_file(root: str, iid: str) -> str:
    return _state_path(repo_id(os.path.abspath(root)), iid)


def state_path(iid: str) -> str:
    """按编号取状态文件路径（仓库外），供运行清单证明记录位置。"""
    return _state_path_by_id(iid)


def load(iid: str) -> dict[str, Any]:
    return _load_by_id(iid)


def _load(rid: str, iid: str) -> dict[str, Any]:
    path = _state_path(rid, iid)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except OSError as exc:
        raise GitError("iteration_not_found", "迭代记录不存在: %s" % iid) from exc


def _load_by_id(iid: str) -> dict[str, Any]:
    path = _state_path_by_id(iid)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except OSError as exc:
        raise GitError("iteration_not_found", "迭代记录不存在: %s" % iid) from exc


def _save(state: dict[str, Any]) -> None:
    state["updated"] = _now()
    folder = _iter_root(state["repo_id"])
    os.makedirs(folder, exist_ok=True)
    path = _state_path(state["repo_id"], state["id"])
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _emit(emit, state: dict[str, Any], kind: str, **detail: Any) -> None:
    if not emit:
        return
    try:
        emit({"action": kind, "iteration_id": state.get("id", ""),
              "detail": detail})
    except Exception:  # noqa: BLE001 - SSE 断开不能阻断交付
        pass


def _note(state: dict[str, Any], kind: str, detail: str = "",
          emit=None, **extra: Any) -> None:
    event = {"ts": _now(), "epoch": time.time(), "kind": kind,
             "detail": detail[:600]}
    if extra:
        event["data"] = extra
    events = state.setdefault("events", [])
    events.append(event)
    del events[:-200]
    _emit(emit, state, kind, message=detail, **extra)


def _slug(requirement: str) -> str:
    # 需求常是中文，分支名只允许 ASCII，所以用需求哈希保证可追溯且稳定
    return hashlib.sha1((requirement or "").strip().encode("utf-8")).hexdigest()[:6]


def _branch_exists(handle: RepoHandle, name: str) -> bool:
    return b"refs/heads/" + name.encode("utf-8") in handle.repo.refs.keys()


def _pick_base(handle: RepoHandle, base: str) -> str:
    if base:
        return base
    names = {r[len(b"refs/heads/"):].decode("utf-8", "replace")
             for r in handle.repo.refs.keys() if r.startswith(b"refs/heads/")}
    for cand in ("develop", "main", "master"):
        if cand in names:
            return cand
    cur = refs.current_branch(handle)
    if not cur:
        raise GitError("detached_head", "主工作区不在分支上，请先切到 develop 或 main")
    return cur


def _resolve(root: str, iid: str = "") -> tuple[RepoHandle, dict[str, Any]]:
    """按仓库根 + 迭代编号取状态；iid 为空时取最近一条。"""
    if not iid:
        items = list_iterations(root)["iterations"]
        if not items:
            raise GitError("iteration_not_found", "还没有任何迭代记录")
        iid = items[0]["id"]
    handle = RepoHandle(root)
    state = _load(handle.id, iid)
    return handle, state


def _resolve_by_id(iid: str) -> tuple[RepoHandle, dict[str, Any]]:
    state = _load_by_id(iid)
    return RepoHandle(state.get("root") or ""), state


def _row(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": state.get("id", ""), "state": state.get("state", ""),
        "branch": state.get("branch", ""), "base": state.get("base", ""),
        "requirement": state.get("requirement", ""),
        "session_id": state.get("session_id", ""),
        "workspace": state.get("root", ""),
        "mode": state.get("mode", "iteration"),
        "commit": state.get("commit", ""),
        "created": state.get("created", ""),
        "updated": state.get("updated", ""),
        "worktree_exists": os.path.isdir(state.get("worktree", "")),
    }


def _scan(limit: int = 30, session_id: str = "",
          workspace: str = "") -> list[dict[str, Any]]:
    base = os.path.join(audit.audit_dir(), "iterations")
    rows: list[dict[str, Any]] = []
    try:
        folders = os.listdir(base)
    except OSError:
        folders = []
    for rid in folders:
        folder = os.path.join(base, rid)
        if not os.path.isdir(folder):
            continue
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(folder, name), "r", encoding="utf-8") as f:
                    state = json.load(f)
            except (OSError, ValueError):
                continue
            if session_id and state.get("session_id") != session_id:
                continue
            if workspace and os.path.normcase(
                    state.get("root", "")) != os.path.normcase(workspace):
                continue
            rows.append(_row(state))
    rows.sort(key=lambda item: item.get("updated", ""), reverse=True)
    return rows[:max(1, int(limit or 30))]


def list_iterations(root: str, limit: int = 30) -> dict[str, Any]:
    rid = repo_id(os.path.abspath(root))
    rows = [item for item in _scan(200) if item["workspace"] and
            repo_id(os.path.abspath(item["workspace"])) == rid]
    return {"ok": True, "repo_id": rid,
            "iterations": rows[:max(1, int(limit or 30))]}


def list_all(limit: int = 50, session_id: str = "",
             workspace: str = "") -> dict[str, Any]:
    """跨仓库列迭代，供 Agent 运行清单按会话/工作区过滤。"""
    return {"ok": True, "iterations": _scan(limit, session_id, workspace)}


def get(root: str, iid: str = "") -> dict[str, Any]:
    handle, state = _resolve(root, iid)
    try:
        state["live"] = _live(handle, state)
    finally:
        handle.close()
    return {"ok": True, "iteration": state}


def get_any(iid: str) -> dict[str, Any]:
    """只凭迭代编号读取（Agent 运行面板断线重连时使用）。"""
    handle, state = _resolve_by_id(iid)
    try:
        state["live"] = _live(handle, state)
    finally:
        handle.close()
    return {"ok": True, "iteration": state}


def _live(handle: RepoHandle, state: dict[str, Any]) -> dict[str, Any]:
    """把磁盘上的真实情况与记录对照，避免前端看到过期状态。"""
    wt = state.get("worktree", "")
    if not os.path.isdir(wt):
        return {"worktree_exists": False, "drift": True,
                "reason": "隔离目录已不存在"}
    try:
        with RepoHandle(wt) as h:
            changes = _changes(h)
            digest = _digest(changes)
            branch = refs.current_branch(h)
            head = head_sha(h.repo)
    except GitError as exc:
        return {"worktree_exists": True, "drift": True, "reason": exc.message}
    return {
        "worktree_exists": True,
        "branch_now": branch,
        "head_now": head.hex() if head else "",
        "files_now": len(changes),
        "digest_now": digest,
        "drift": bool(state.get("digest")) and digest != state.get("digest"),
    }


def _blob(handle: RepoHandle, rel: str) -> bytes:
    return worktree_bytes(handle, rel) or b""


def _changes(handle: RepoHandle) -> list[dict[str, Any]]:
    """相对 HEAD 的全部变更（含未跟踪），供验证与提交共用同一份事实。"""
    st = snapshot.status(handle)
    paths = sorted({x["path"] for x in st["staged"]}
                   | set(st["changed"]) | set(st["untracked"]))
    if len(paths) > MAX_FILES:
        raise GitError("too_many_files", "单轮迭代变更文件过多（%d），请拆分" % len(paths))
    head = head_sha(handle.repo)
    head_text = (head.hex() if head else "")[:8]
    rows: list[dict[str, Any]] = []
    budget = MAX_DIFF_BYTES
    for rel in paths:
        before = head_bytes(handle.repo, rel) or b""
        after = _blob(handle, rel)
        binary = snapshot.is_binary(before) or snapshot.is_binary(after)
        row: dict[str, Any] = {
            "path": rel, "binary": binary,
            "before_head": head_text,
            "size_before": len(before), "size_after": len(after),
            "sha_after": hashlib.sha256(after).hexdigest()[:16] if after else "",
        }
        if binary:
            row.update(add=0, delete=0, status="modified" if before else "added")
        else:
            b = before.decode("utf-8", "replace").splitlines()
            a = after.decode("utf-8", "replace").splitlines()
            add = delete = 0
            if budget > 0:
                for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
                        None, b, a, autojunk=False).get_opcodes():
                    if tag in ("replace", "insert"):
                        add += j2 - j1
                    if tag in ("replace", "delete"):
                        delete += i2 - i1
                budget -= (len(b) + len(a)) * 60
            row.update(add=add, delete=delete,
                       status=("added" if not before else
                               ("deleted" if not after else "modified")))
        rows.append(row)
    return rows


def _digest(changes: list[dict[str, Any]]) -> str:
    """内容摘要：路径 + 大小 + 内容哈希，锁定验证时的快照。

    只比对大小会漏掉「等长改写」，所以落盘内容也进摘要。
    """
    payload = "\n".join("%s|%d|%s" % (c["path"], c["size_after"], c["sha_after"])
                        for c in sorted(changes, key=lambda x: x["path"]))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------- 校验 ----------

def _tail(text: str, limit: int = 600) -> str:
    text = (text or "").strip()
    return text[-limit:] if len(text) > limit else text


def _norm_commands(validation: Any) -> list[dict[str, Any]]:
    if validation is None:
        return []
    if isinstance(validation, str):
        validation = [validation]
    out = []
    for item in validation or []:
        if isinstance(item, str):
            command, timeout = item, 120
        else:
            command = str(item.get("command") or "")
            timeout = max(1, min(int(item.get("timeout") or 120), 600))
        out.append({"command": command, "timeout": timeout})
    return out


def _check_python(path: str) -> tuple[str, str]:
    import py_compile
    import tempfile
    cfile = os.path.join(tempfile.gettempdir(), "wasily_check_%s.pyc" % uuid.uuid4().hex[:8])
    try:
        py_compile.compile(path, cfile=cfile, doraise=True)
        return "passed", ""
    except py_compile.PyCompileError as exc:
        return "failed", _tail(str(exc))
    except Exception as exc:  # noqa: BLE001
        return "failed", _tail("%s: %s" % (type(exc).__name__, exc))
    finally:
        try:
            os.remove(cfile)
        except OSError:
            pass


def _check_json(path: str) -> tuple[str, str]:
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            json.load(f)
        return "passed", ""
    except Exception as exc:  # noqa: BLE001
        return "failed", _tail(str(exc))


def _check_js(path: str) -> tuple[str, str]:
    node = shutil.which("node")
    if not node:
        return "skipped", "未找到 node，无法执行语法校验"
    try:
        res = subprocess.run([node, "--check", path], capture_output=True,
                             text=True, timeout=60, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return "failed", "node --check 超时"
    except Exception as exc:  # noqa: BLE001
        return "skipped", "node --check 无法执行: %s" % exc
    if res.returncode == 0:
        return "passed", ""
    return "failed", _tail((res.stderr or "") + (res.stdout or ""))


def _run_tests(command: str, cwd: str, timeout: int) -> tuple[str, str, int | None]:
    try:
        res = subprocess.run(command, cwd=cwd, shell=True, capture_output=True,
                             text=True, timeout=timeout, encoding="utf-8",
                             errors="replace")
    except subprocess.TimeoutExpired:
        return "failed", "测试超时（%ds）" % timeout, None
    except Exception as exc:  # noqa: BLE001
        return "skipped", "测试命令无法执行: %s" % exc, None
    out = _tail((res.stdout or "") + "\n" + (res.stderr or ""))
    return ("passed" if res.returncode == 0 else "failed"), out, res.returncode


def _fail(state: dict[str, Any], code: str, message: str, emit=None) -> dict[str, Any]:
    if state.get("state") in ("verifying", "validating"):
        # 校验失败要能重试，不能停在中间态
        state["state"] = "prepared"
    state["error_code"] = code
    state["last_error"] = message
    _note(state, "verify_failed", message, emit, code=code)
    _save(state)
    return {"ok": False, "code": code, "reason": "%s: %s" % (code, message),
            "iteration": state}


def _head_hex(handle: RepoHandle) -> str:
    head = head_sha(handle.repo)
    return head.hex() if head else ""


def verify(root: str, iid: str = "", timeout: int = DEFAULT_TIMEOUT,
           validation: Any = None, allow_no_checks: bool = False,
           emit=None) -> dict[str, Any]:
    handle, state = _resolve(root, iid)
    try:
        if state["state"] not in ("prepared", "verified", "verifying", "validating"):
            raise GitError("iteration_state", "当前状态 %s 不能验证" % state["state"])
        wt = state.get("worktree", "")
        if not os.path.isdir(wt):
            raise GitError("worktree_missing", "隔离目录不存在: %s" % wt)
        # 隔离基线漂移检查：主工作区或 worktree 的 HEAD 被外部推进时禁止交付。
        base_sha = state.get("base_sha") or ""
        if base_sha and _head_hex(handle) != base_sha:
            return _fail(state, "primary_head_changed",
                         "主工作区 HEAD 已变化，拒绝交付", emit)
        state["state"] = "verifying"
        state["error_code"] = ""
        state["last_error"] = ""
        _save(state)
        with RepoHandle(wt) as h:
            branch = refs.current_branch(h)
            if branch != state["branch"]:
                raise GitError("iteration_branch_mismatch",
                               "隔离目录分支被改动：%s != %s" % (branch, state["branch"]))
            expected_head = state.get("head") or base_sha
            if expected_head and _head_hex(h) != expected_head:
                return _fail(state, "worktree_head_changed",
                             "隔离 worktree HEAD 已变化，拒绝交付", emit)
            before = _changes(h)
            if not before:
                state["state"] = "no_changes"
                state["changes"] = []
                _note(state, "no_changes", "隔离目录没有变更", emit)
                _save(state)
                return {"ok": False, "code": "no_changes", "reason": "隔离目录没有变更",
                        "iteration": state, "checks": [], "files": []}
            checks: list[dict[str, Any]] = []
            for row in before:
                rel = row["path"]
                if not row["size_after"]:
                    continue   # 删除的文件无需语法校验
                full = h.abs(rel)
                low = rel.lower()
                if low.endswith(".py"):
                    status, detail = _check_python(full)
                elif low.endswith(".json"):
                    status, detail = _check_json(full)
                elif low.endswith(_JS_EXT):
                    status, detail = _check_js(full)
                else:
                    continue
                checks.append({"name": "syntax:" + rel, "command": "syntax:" + rel,
                               "status": status, "detail": detail, "output": detail,
                               "returncode": None})
            commands = _norm_commands(validation)
            if commands:
                for item in commands:
                    state["state"] = "validating"
                    _save(state)
                    _emit(emit, state, "validation_started",
                          command=item["command"])
                    status, detail, code = _run_tests(
                        item["command"], wt, max(30, int(timeout)))
                    checks.append({"name": "validation:" + item["command"],
                                   "command": item["command"], "status": status,
                                   "detail": detail, "output": detail,
                                   "returncode": code})
                    _save(state)
                    _emit(emit, state, "validation_finished",
                          command=item["command"], status=status,
                          returncode=code, output=detail[-500:])
            elif state.get("tests"):
                status, detail, code = _run_tests(
                    state["tests"], wt, max(30, int(timeout)))
                checks.append({"name": "tests", "command": state["tests"],
                               "status": status, "detail": detail, "output": detail,
                               "returncode": code})
            else:
                checks.append({"name": "tests", "command": "", "status": "skipped",
                               "detail": "未配置测试命令", "output": "", "returncode": None})
            after = _changes(h)
            if _digest(after) != _digest(before):
                state["checks"] = checks
                return _fail(state, "validation_changed_files",
                             "校验产生了清单外文件变化", emit)
            passed = sum(1 for c in checks if c["status"] == "passed")
            failed = [c for c in checks if c["status"] == "failed"]
            skipped = [c for c in checks if c["status"] == "skipped"]
            digest = _digest(after)
            head = head_sha(h.repo)
        ok = not failed and (passed > 0 or allow_no_checks)
        state["checks"] = checks
        state["verified_changes"] = after
        state["changes"] = after
        state["verified_head"] = head.hex() if head else ""
        state["digest"] = digest if ok else ""
        state["verified_at"] = _now()
        state["state"] = "verified" if ok else "prepared"
        if not ok and not failed:
            state["error_code"] = "no_checks"
            state["last_error"] = "没有可执行的校验（跳过的检查不算通过）"
        elif failed:
            state["error_code"] = "validation_failed"
            state["last_error"] = _tail("; ".join(
                "%s: %s" % (c["name"], c["detail"]) for c in failed))
        else:
            state["error_code"] = ""
            state["last_error"] = ""
        _note(state, "verify", "passed=%d failed=%d skipped=%d" %
              (passed, len(failed), len(skipped)), emit,
              code=state.get("error_code") or "")
        _save(state)
        audit.append_event(state["repo_id"], state["id"], {
            "actor": "agent", "operation": "verify",
            "branch": state["branch"], "result": "passed" if ok else "failed",
            "files": [c["path"] for c in after],
            "checks": [{k: c[k] for k in ("name", "status")} for c in checks],
        })
        return {"ok": ok, "iteration": state, "checks": checks,
                "files": after, "reason": state.get("last_error", ""),
                "code": state.get("error_code", "")}
    finally:
        handle.close()


# ---------- 生命周期 ----------

def start(root: str, requirement: str, branch: str = "", base: str = "",
          tests: str = "", iid: str = "", mode: str = "iteration",
          session_id: str = "", validation: Any = None,
          auto_push: bool = True, worktree_path: str = "",
          worktree_base: str = "") -> dict[str, Any]:
    """开一轮迭代：建 feature 分支 + 独立 worktree，返回可写目录。

    mode="agent" 时用 wasily/<id> 作隔离分支，交付目标由 base 决定：
    feature 自动推送，main/develop 必须人工确认。
    """
    text = (requirement or "").strip()
    if not text:
        raise GitError("bad_requirement", "需求描述不能为空")
    handle = RepoHandle(root)
    try:
        base_name = _pick_base(handle, base)
        if branch:
            name = branch
        elif mode == "agent":
            name = "wasily/" + uuid.uuid4().hex[:12]
        else:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            name = "feature/iter-%s-%s" % (stamp, _slug(text))
        if not _branch_exists(handle, name):
            try:
                refs.create_branch(handle, name, base_name)
            except GitError:
                # detached HEAD 等没有分支基线的场景，明确拒绝，不能建悬空分支。
                raise GitError("detached_head",
                               "无法从 %s 建立隔离分支，请先切到具体分支" % base_name)
        name_short = name.split("/", 1)[-1].replace("/", "-")
        if worktree_path:
            dest = os.path.abspath(worktree_path)
        elif worktree_base:
            dest = os.path.join(os.path.abspath(worktree_base), name_short)
        else:
            dest = os.path.abspath(os.path.join(
                worktrees.worktree_root(handle.id), name_short))
        try:
            inside_primary = os.path.commonpath(
                [os.path.realpath(dest), os.path.realpath(handle.root)]
            ) == os.path.realpath(handle.root)
        except ValueError:
            inside_primary = False
        if inside_primary:
            raise GitError("bad_path", "隔离目录不能位于主工作区内: %s" % dest)
        if not os.path.isdir(dest):
            worktrees.create(handle, name_short, name, path=dest)
        if iid and _ITER_RE.match(iid) and os.path.exists(_state_path(handle.id, iid)):
            return {"ok": True, "iteration": _load(handle.id, iid), "reused": True}
        head = head_sha(handle.repo)
        state: dict[str, Any] = {
            "id": uuid.uuid4().hex[:12],
            "repo_id": handle.id,
            "root": handle.root,
            "mode": mode,
            "branch": name,
            "base": base_name,
            "base_sha": head.hex() if head else "",
            "worktree": dest,
            "requirement": text,
            "session_id": str(session_id or ""),
            "task": text,
            "tests": (tests or "").strip(),
            "validation_commands": _norm_commands(validation),
            "auto_push": bool(auto_push),
            "protected": policy.branch_kind(base_name) in PROTECTED_KINDS,
            "state": "prepared",
            "created": _now(),
            "updated": _now(),
            "head": head.hex() if head else "",
            "verified_head": "",
            "digest": "",
            "checks": [],
            "changes": [],
            "events": [],
            "commit": "",
            "push": {},
            "delivery": {},
            "last_error": "",
            "error_code": "",
            "worktree_removed": False,
        }
        _note(state, "start", "从 %s 建分支 %s" % (base_name, name), None,
              base_sha=state["base_sha"])
        _save(state)
        audit.append_event(state["repo_id"], state["id"], {
            "actor": "agent", "operation": "iteration_start",
            "requirement": text, "branch": name, "base": base_name,
            "worktree": dest, "result": "passed", "mode": mode,
            "session_id": state["session_id"],
        })
        return {"ok": True, "iteration": state}
    finally:
        handle.close()


def _remote_target(handle: RepoHandle) -> str:
    try:
        items = remote.remotes(handle).get("remotes") or {}
    except Exception:  # noqa: BLE001
        items = {}
    names = [str(name) for name in items]
    if not names:
        return ""
    return "origin" if "origin" in names else names[0]


def _deliver(handle: RepoHandle, state: dict[str, Any], emit=None) -> None:
    """Agent 交付：只做非强推；main/develop 停下来等人确认。"""
    dest = state.get("base") or ""
    kind = policy.branch_kind(dest)
    if kind in PROTECTED_KINDS:
        state["state"] = "awaiting_confirmation"
        state["delivery"] = {"status": "awaiting_confirmation", "branch": dest}
        _note(state, "awaiting_confirmation", "分支 %s 需要人工确认" % dest, emit,
              branch=dest)
        return
    if kind != "feature":
        state["state"] = "committed"
        state["delivery"] = {"status": "local", "branch": dest,
                             "reason": "非 feature 分支不自动推送"}
        return
    name = _remote_target(handle)
    if not name:
        state["state"] = "committed"
        state["delivery"] = {"status": "local", "branch": dest,
                             "reason": "未配置远端"}
        return
    try:
        info = remote.push(handle, dest, remote=name, set_upstream=False,
                           source=state["branch"])
    except GitError as exc:
        state["state"] = "committed"
        state["error_code"] = "push_failed"
        state["last_error"] = "%s: %s" % (exc.code, exc.message)
        state["delivery"] = {"status": "failed", "remote": name,
                             "branch": dest, "error": exc.code}
        _note(state, "push_failed", str(exc.message), emit,
              remote=name, branch=dest)
        return
    state["state"] = "pushed"
    state["delivery"] = {"status": "pushed", "remote": name, "branch": dest,
                         "commit": state.get("commit", ""),
                         "verified": info.get("verified")}
    _note(state, "delivered", "非强推送到 %s/%s" % (name, dest), emit,
          remote=name, branch=dest, commit=state.get("commit", ""))


def commit(root: str, iid: str = "", message: str = "", push: bool = True,
           actor: str = "agent", emit=None) -> dict[str, Any]:
    """验证通过后的唯一落点：固定快照 → 暂存 → 提交 → 推送/待确认。"""
    handle, state = _resolve(root, iid)
    try:
        if state["state"] != "verified":
            raise GitError("iteration_not_verified",
                           "当前状态 %s，必须先 verify 才能提交" % state["state"])
        ok, err = policy.validate_commit(message)
        if not ok:
            raise GitError("bad_message", err)
        wt = state.get("worktree", "")
        if not os.path.isdir(wt):
            raise GitError("worktree_missing", "隔离目录不存在: %s" % wt)
        mode = state.get("mode") or "iteration"
        with RepoHandle(wt) as h:
            branch = refs.current_branch(h)
            if branch != state["branch"]:
                raise GitError("iteration_branch_mismatch",
                               "隔离目录分支被改动：%s" % branch)
            if mode != "agent":
                allowed, why = policy.can_auto(branch, "commit")
                if not allowed:
                    raise GitError("need_confirm", why)
            changes = _changes(h)
            if not changes:
                raise GitError("nothing_to_commit", "隔离目录没有变更")
            if _digest(changes) != state.get("digest"):
                raise GitError("verification_stale",
                               "验证之后文件又发生变化，请重新 verify",
                               files=[c["path"] for c in changes])
            files = [c["path"] for c in changes]
            index_ops.stage(h, files)
            res = refs.commit(h, message, expected_head=state.get("verified_head", ""))
            sha = res["commit"]
            staged = _changes(h)
            if staged:
                raise GitError("commit_incomplete", "提交后仍有未提交变更")
            state["changes"] = changes
            state["commit"] = sha
            state["head"] = sha
            state["message"] = message.strip()
            push_info: dict[str, Any] = {"skipped": True}
            if mode != "agent" and push:
                can_push, why_push = policy.can_auto(branch, "push")
                if not can_push:
                    push_info = {"skipped": True, "reason": why_push}
                else:
                    try:
                        push_info = remote.push(h, branch)
                    except GitError as exc:
                        # 提交已落盘，推送失败不回滚，只把原因留在轨迹里
                        push_info = {"ok": False, "code": exc.code,
                                     "error": exc.message}
        if mode == "agent":
            if push and state.get("auto_push", True):
                _deliver(handle, state, emit)
            else:
                state["state"] = "committed"
                state["delivery"] = {"status": "local", "branch": state.get("base", ""),
                                     "reason": "已关闭自动推送"}
        state["push"] = push_info
        if mode != "agent":
            state["state"] = "pushed" if push_info.get("ok") else "committed"
            if push_info.get("ok") is False:
                state["last_error"] = str(push_info.get("error") or push_info.get("code"))
        _note(state, "commit", "%s %s" % (sha[:8], message.splitlines()[0][:80]),
              emit, commit=sha)
        _save(state)
        audit.append_event(state["repo_id"], state["id"], {
            "actor": actor, "operation": "iteration_commit",
            "branch": state["branch"], "base": state.get("base", ""),
            "head_after": sha,
            "prev_head": state.get("verified_head", ""),
            "message": message.strip(), "requirement": state.get("requirement", ""),
            "files": [{"path": c["path"], "status": c["status"],
                       "add": c["add"], "delete": c["delete"]} for c in changes],
            "checks": [{k: c[k] for k in ("name", "status")} for c in state["checks"]],
            "push": push_info, "delivery": state.get("delivery", {}),
            "result": "passed",
        })
        return {"ok": True, "iteration": state, "commit": sha,
                "push": push_info, "files": changes}
    finally:
        handle.close()


def _confirmed(state: dict[str, Any], emit=None) -> dict[str, Any]:
    dest = state.get("base") or ""
    kind = policy.branch_kind(dest)
    if kind not in PROTECTED_KINDS:
        raise GitError("iteration_state", "只有受保护分支才需要人工确认")
    handle = RepoHandle(state["root"])
    try:
        state["confirmed_at"] = _now()
        name = _remote_target(handle)
        if not name:
            state["state"] = "committed"
            state["delivery"] = {"status": "local", "branch": dest,
                                 "reason": "未配置远端"}
        else:
            info = remote.push(handle, dest, remote=name, set_upstream=False,
                               source=state["branch"])
            state["state"] = "pushed"
            state["delivery"] = {"status": "pushed", "remote": name,
                                 "branch": dest, "commit": state.get("commit", ""),
                                 "verified": info.get("verified")}
        _note(state, "confirmed", "人工确认后交付 %s" % dest, emit,
              branch=dest)
        _save(state)
        audit.append_event(state["repo_id"], state["id"], {
            "actor": "user", "operation": "iteration_confirm",
            "branch": state["branch"], "base": dest,
            "commit": state.get("commit", ""),
            "result": "passed" if state["state"] == "pushed" else "local",
        })
        return {"ok": True, "iteration": state}
    finally:
        handle.close()


def confirm(root: str = "", iid: str = "", emit=None) -> dict[str, Any]:
    if root:
        handle, state = _resolve(root, iid)
        handle.close()
    else:
        state = _load_by_id(iid)
    if state["state"] != "awaiting_confirmation":
        raise GitError("iteration_state", "当前状态 %s 不在等待确认" % state["state"])
    return _confirmed(state, emit)


def confirm_by_id(iid: str, emit=None) -> dict[str, Any]:
    return confirm("", iid, emit)


def rollback(root: str = "", iid: str = "", emit=None) -> dict[str, Any]:
    """回滚：已推送的迭代生成反向提交并非强推；未推送的只标记取消。"""
    if root:
        handle, state = _resolve(root, iid)
        handle.close()
    else:
        state = _load_by_id(iid)
    allowed = ("awaiting_confirmation", "committed", "pushed", "push_failed")
    if state["state"] not in allowed:
        raise GitError("iteration_state", "当前状态 %s 不能回滚" % state["state"])
    wt = state.get("worktree", "")
    delivery = state.get("delivery") or {}
    if delivery.get("status") == "pushed":
        if not os.path.isdir(wt):
            raise GitError("worktree_missing",
                           "交付 worktree 已清理，不能自动回滚；请在原生 SCM 中 revert")
        with RepoHandle(wt) as h:
            reverted = refs.revert(
                h, state["commit"],
                message="revert: agent iteration %s" % state["id"])
        main = RepoHandle(state["root"])
        try:
            name = delivery.get("remote") or _remote_target(main)
            if name:
                remote.push(main, delivery.get("branch") or state["base"],
                            remote=name, set_upstream=False, source=state["branch"])
        finally:
            main.close()
        state["rollback_commit"] = reverted.get("commit") or ""
    state["state"] = "rolled_back"
    state["rolled_back_at"] = _now()
    _note(state, "rolled_back", "回滚完成", emit,
          commit=state.get("rollback_commit") or "")
    _save(state)
    audit.append_event(state["repo_id"], state["id"], {
        "actor": "user", "operation": "iteration_rollback",
        "branch": state["branch"], "commit": state.get("commit", ""),
        "rollback_commit": state.get("rollback_commit", ""),
        "result": "passed",
    })
    return {"ok": True, "iteration": state}


def rollback_by_id(iid: str, emit=None) -> dict[str, Any]:
    return rollback("", iid, emit)


def fail(root: str = "", iid: str = "", code: str = "run_failed",
         message: str = "", emit=None) -> dict[str, Any]:
    """失败关闭：只标记失败并保留 worktree，供人工审计与恢复。"""
    if root:
        handle, state = _resolve(root, iid)
        handle.close()
    else:
        state = _load_by_id(iid)
    if state["state"] in ("pushed", "rolled_back"):
        return {"ok": True, "iteration": state}
    state["state"] = "failed"
    state["error_code"] = code
    state["last_error"] = "%s: %s" % (code, message)
    _note(state, "run_failed", message, emit, code=code)
    _save(state)
    audit.append_event(state["repo_id"], state["id"], {
        "actor": "agent", "operation": "iteration_fail",
        "branch": state["branch"], "code": code, "result": "failed",
    })
    return {"ok": True, "iteration": state}


def fail_by_id(iid: str, code: str, message: str, emit=None) -> dict[str, Any]:
    return fail("", iid, code, message, emit)


def abort(root: str, iid: str = "", force: bool = False,
          final_state: str = "aborted") -> dict[str, Any]:
    if final_state not in ("aborted", "no_changes"):
        raise ValueError("final_state 只支持 aborted/no_changes")
    handle, state = _resolve(root, iid)
    try:
        if state["state"] == "pushed" and final_state != "no_changes":
            raise GitError("iteration_done", "已推送的迭代不能中止")
        wt = state.get("worktree", "")
        removed = bool(state.get("worktree_removed"))
        if os.path.isdir(wt):
            worktrees.remove(handle, wt, force)
            removed = True
        state["state"] = final_state
        state["worktree_removed"] = removed
        _note(state, final_state, "force=%s" % force)
        _save(state)
        audit.append_event(state["repo_id"], state["id"], {
            "actor": "user", "operation":
                "iteration_abort" if final_state == "aborted" else "iteration_no_changes",
            "branch": state["branch"], "result": "passed",
        })
        return {"ok": True, "iteration": state, "removed": removed}
    finally:
        handle.close()


def recover(root: str) -> dict[str, Any]:
    """崩溃恢复：列出未完成事务并报告现场是否还在、是否已漂移。"""
    rid = repo_id(os.path.abspath(root))
    items = []
    for row in list_iterations(root, 200)["iterations"]:
        if row["state"] not in ("prepared", "verifying", "validating",
                                "verified", "awaiting_confirmation"):
            continue
        try:
            state = _load(rid, row["id"])
            with RepoHandle(root) as h:
                state["live"] = _live(h, state)
        except GitError as exc:
            state = {"id": row["id"], "state": row["state"],
                     "live": {"worktree_exists": False, "reason": exc.message}}
        items.append(state)
    return {"ok": True, "repo_id": rid, "pending": items, "count": len(items)}
