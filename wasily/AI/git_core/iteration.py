"""自迭代事务：需求 → 隔离工作区 → 验证 → 提交 → 推送 → 审计。

为什么要有这一层：
- 直接让 Agent 在主工作区改文件，会和用户的未提交修改混在一起，无法回溯；
- 每轮迭代必须在独立 worktree 里完成，并在提交前用确定性校验把住质量关；
- 验证通过后还要固定快照（digest），否则「验证的是 A，提交的是 B」。

状态机：prepared → verified → committed → pushed
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
_ITER_RE = re.compile(r"^[a-f0-9]{12}$")
_JS_EXT = (".js", ".cjs", ".mjs")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _iter_root(rid: str) -> str:
    return os.path.join(audit.audit_dir(), "iterations", rid or "repo")


def _state_path(rid: str, iid: str) -> str:
    if not _ITER_RE.match(iid or ""):
        raise GitError("bad_iteration", "迭代编号不合法: %s" % iid)
    return os.path.join(_iter_root(rid), iid + ".json")


def _load(rid: str, iid: str) -> dict[str, Any]:
    path = _state_path(rid, iid)
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


def _note(state: dict[str, Any], kind: str, detail: str = "") -> None:
    state.setdefault("events", []).append(
        {"ts": _now(), "kind": kind, "detail": detail[:600]})


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


def list_iterations(root: str, limit: int = 30) -> dict[str, Any]:
    rid = repo_id(os.path.abspath(root))
    folder = _iter_root(rid)
    rows: list[dict[str, Any]] = []
    try:
        names = sorted(os.listdir(folder), reverse=True)
    except OSError:
        names = []
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(folder, name), "r", encoding="utf-8") as f:
                st = json.load(f)
        except (OSError, ValueError):
            continue
        rows.append({
            "id": st.get("id", ""), "state": st.get("state", ""),
            "branch": st.get("branch", ""), "requirement": st.get("requirement", ""),
            "commit": st.get("commit", ""), "created": st.get("created", ""),
            "updated": st.get("updated", ""),
            "worktree_exists": os.path.isdir(st.get("worktree", "")),
        })
        if len(rows) >= max(1, int(limit or 30)):
            break
    return {"ok": True, "repo_id": rid, "iterations": rows}


def get(root: str, iid: str = "") -> dict[str, Any]:
    handle, state = _resolve(root, iid)
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


def _run_tests(command: str, cwd: str, timeout: int) -> tuple[str, str]:
    try:
        res = subprocess.run(command, cwd=cwd, shell=True, capture_output=True,
                             text=True, timeout=timeout, encoding="utf-8",
                             errors="replace")
    except subprocess.TimeoutExpired:
        return "failed", "测试超时（%ds）" % timeout
    except Exception as exc:  # noqa: BLE001
        return "skipped", "测试命令无法执行: %s" % exc
    out = _tail((res.stdout or "") + "\n" + (res.stderr or ""))
    return ("passed" if res.returncode == 0 else "failed"), out


def verify(root: str, iid: str = "", timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    handle, state = _resolve(root, iid)
    try:
        if state["state"] not in ("prepared", "verified"):
            raise GitError("iteration_state", "当前状态 %s 不能验证" % state["state"])
        wt = state.get("worktree", "")
        if not os.path.isdir(wt):
            raise GitError("worktree_missing", "隔离目录不存在: %s" % wt)
        with RepoHandle(wt) as h:
            branch = refs.current_branch(h)
            if branch != state["branch"]:
                raise GitError("iteration_branch_mismatch",
                               "隔离目录分支被改动：%s != %s" % (branch, state["branch"]))
            changes = _changes(h)
            checks: list[dict[str, str]] = []
            for row in changes:
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
                checks.append({"name": "syntax:" + rel, "status": status,
                               "detail": detail})
            if state.get("tests"):
                status, detail = _run_tests(state["tests"], wt, max(30, int(timeout)))
                checks.append({"name": "tests", "status": status, "detail": detail})
            else:
                checks.append({"name": "tests", "status": "skipped",
                               "detail": "未配置测试命令"})
            passed = sum(1 for c in checks if c["status"] == "passed")
            failed = [c for c in checks if c["status"] == "failed"]
            skipped = [c for c in checks if c["status"] == "skipped"]
            digest = _digest(changes)
            head = head_sha(h.repo)
        ok = not failed and passed > 0
        state["checks"] = checks
        state["verified_changes"] = changes
        state["verified_head"] = head.hex() if head else ""
        state["digest"] = digest if ok else ""
        state["verified_at"] = _now()
        state["state"] = "verified" if ok else "prepared"
        if not ok and not failed:
            state["last_error"] = "没有可执行的校验（跳过的检查不算通过）"
        else:
            state["last_error"] = _tail("; ".join(
                "%s: %s" % (c["name"], c["detail"]) for c in failed))
        _note(state, "verify", "passed=%d failed=%d skipped=%d" %
              (passed, len(failed), len(skipped)))
        _save(state)
        audit.append_event(state["repo_id"], state["id"], {
            "actor": "agent", "operation": "verify",
            "branch": state["branch"], "result": "passed" if ok else "failed",
            "files": [c["path"] for c in changes],
            "checks": [{k: c[k] for k in ("name", "status")} for c in checks],
        })
        return {"ok": ok, "iteration": state, "checks": checks,
                "files": changes, "reason": state.get("last_error", "")}
    finally:
        handle.close()


# ---------- 生命周期 ----------

def start(root: str, requirement: str, branch: str = "", base: str = "",
          tests: str = "", iid: str = "") -> dict[str, Any]:
    """开一轮迭代：建 feature 分支 + 独立 worktree，返回可写目录。"""
    text = (requirement or "").strip()
    if not text:
        raise GitError("bad_requirement", "需求描述不能为空")
    handle = RepoHandle(root)
    try:
        base_name = _pick_base(handle, base)
        if branch:
            name = branch
        else:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            name = "feature/iter-%s-%s" % (stamp, _slug(text))
        if not _branch_exists(handle, name):
            refs.create_branch(handle, name, base_name)
        name_short = name.split("/", 1)[-1].replace("/", "-")
        dest = os.path.join(worktrees.worktree_root(handle.id), name_short)
        if not os.path.isdir(dest):
            worktrees.create(handle, name_short, name)
        if iid and _ITER_RE.match(iid) and os.path.exists(_state_path(handle.id, iid)):
            return {"ok": True, "iteration": _load(handle.id, iid), "reused": True}
        head = head_sha(handle.repo)
        state: dict[str, Any] = {
            "id": uuid.uuid4().hex[:12],
            "repo_id": handle.id,
            "root": handle.root,
            "branch": name,
            "base": base_name,
            "worktree": dest,
            "requirement": text,
            "tests": (tests or "").strip(),
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
            "last_error": "",
        }
        _note(state, "start", "从 %s 建分支 %s" % (base_name, name))
        _save(state)
        audit.append_event(state["repo_id"], state["id"], {
            "actor": "agent", "operation": "iteration_start",
            "requirement": text, "branch": name, "base": base_name,
            "worktree": dest, "result": "passed",
        })
        return {"ok": True, "iteration": state}
    finally:
        handle.close()


def commit(root: str, iid: str = "", message: str = "", push: bool = True,
           actor: str = "agent") -> dict[str, Any]:
    """验证通过后的唯一落点：固定快照 → 暂存 → 提交 → 推送 feature。"""
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
        with RepoHandle(wt) as h:
            branch = refs.current_branch(h)
            if branch != state["branch"]:
                raise GitError("iteration_branch_mismatch",
                               "隔离目录分支被改动：%s" % branch)
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
            push_info: dict[str, Any] = {"skipped": True}
            if push:
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
        state["changes"] = changes
        state["commit"] = sha
        state["push"] = push_info
        state["message"] = message.strip()
        state["state"] = "pushed" if push_info.get("ok") else "committed"
        if push_info.get("ok") is False:
            state["last_error"] = str(push_info.get("error") or push_info.get("code"))
        _note(state, "commit", "%s %s" % (sha[:8], message.splitlines()[0][:80]))
        _save(state)
        audit.append_event(state["repo_id"], state["id"], {
            "actor": actor, "operation": "iteration_commit",
            "branch": state["branch"], "head_after": sha,
            "prev_head": state.get("verified_head", ""),
            "message": message.strip(), "requirement": state.get("requirement", ""),
            "files": [{"path": c["path"], "status": c["status"],
                       "add": c["add"], "delete": c["delete"]} for c in changes],
            "checks": [{k: c[k] for k in ("name", "status")} for c in state["checks"]],
            "push": push_info, "result": "passed",
        })
        return {"ok": True, "iteration": state, "commit": sha,
                "push": push_info, "files": changes}
    finally:
        handle.close()


def abort(root: str, iid: str = "", force: bool = False) -> dict[str, Any]:
    handle, state = _resolve(root, iid)
    try:
        if state["state"] in ("pushed",):
            raise GitError("iteration_done", "已推送的迭代不能中止")
        wt = state.get("worktree", "")
        removed = False
        if os.path.isdir(wt):
            worktrees.remove(handle, wt, force)
            removed = True
        state["state"] = "aborted"
        state["worktree_removed"] = removed
        _note(state, "abort", "force=%s" % force)
        _save(state)
        audit.append_event(state["repo_id"], state["id"], {
            "actor": "user", "operation": "iteration_abort",
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
        if row["state"] not in ("prepared", "verified"):
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
