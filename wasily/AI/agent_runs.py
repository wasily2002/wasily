"""Agent 隔离运行：git_core.iteration（mode="agent"）的运行清单视图。

为什么改写成适配层：
- 运行清单与迭代闭环原本各自维护状态机，崩溃恢复、策略校验、提交逻辑都有两份；
- 现在唯一的写入路径是 git_core.iteration，本模块只负责：
  1. 把 Agent 会话上下文（session/task/校验命令/自动推送）传下去；
  2. 把迭代状态映射成 IDE 面板需要的运行清单结构；
  3. 把 SSE 事件转成面板识别的 agent_run 事件。
"""
from __future__ import annotations

import os
import threading

import config as _cfg
import git_bridge as gb
from git_core import iteration
from git_core.repository import GitError

_LOCK = threading.RLock()
_ACTIVE = {}
PROTECTED_BRANCHES = {"main", "master", "develop"}

_STATUS = {
    "prepared": "running",
    "verifying": "running",
    "validating": "running",
    "verified": "running",
    "committed": "committed",
    "pushed": "delivered",
    "awaiting_confirmation": "awaiting_confirmation",
    "no_changes": "no_changes",
    "failed": "failed",
    "aborted": "failed",
    "rolled_back": "rolled_back",
}

_VERIFY_CODES = ("validation_failed", "validation_changed_files",
                 "primary_head_changed", "worktree_head_changed", "no_checks")

_ACTIONS = {
    "start": "run_started",
    "verify": "validation_finished",
    "verify_failed": "run_failed",
    "validation_started": "validation_started",
    "validation_finished": "validation_finished",
    "commit": "commit_created",
    "awaiting_confirmation": "awaiting_confirmation",
    "confirmed": "confirmed",
    "push_failed": "push_failed",
    "delivered": "delivered",
    "rolled_back": "rolled_back",
    "run_failed": "run_failed",
    "no_changes": "no_changes",
    "aborted": "aborted",
}


def _state_root():
    configured = str(os.environ.get("WASILY_AGENT_RUN_HOME") or "").strip()
    if configured:
        return os.path.abspath(configured)
    base = str(os.environ.get("LOCALAPPDATA") or "").strip()
    if not base:
        base = os.path.expanduser("~")
    return os.path.join(base, "Wasily", "agent-runs")


def _outside(path, workspace):
    try:
        child = os.path.normcase(os.path.realpath(path))
        parent = os.path.normcase(os.path.realpath(workspace))
        return os.path.commonpath([child, parent]) != parent
    except ValueError:
        return True


def _manifest_path(run_id):
    """运行状态文件落在仓库外；断线重连与审计都以此为准。"""
    try:
        return iteration.state_path(str(run_id or ""))
    except GitError:
        return os.path.join(_state_root(), "runs", str(run_id or ""), "run.json")


def _events(state):
    out = []
    for ev in state.get("events") or []:
        kind = str(ev.get("kind") or "")
        out.append({
            "ts": ev.get("epoch") or 0,
            "action": _ACTIONS.get(kind, kind),
            "kind": kind,
            "detail": {"message": ev.get("detail", "")},
        })
    return out


def _view(state):
    """把迭代状态映射成面板契约；字段名保持与旧运行清单一致。"""
    if not state:
        return None
    raw = str(state.get("state") or "")
    code = str(state.get("error_code") or "")
    status = _STATUS.get(raw, raw)
    if code in _VERIFY_CODES:
        status = "failed"
    elif code == "push_failed" and status == "committed":
        status = "push_failed"
    error = ""
    if code:
        error = "%s: %s" % (code, state.get("last_error") or "")
    checks = []
    for item in state.get("checks") or []:
        checks.append({
            "command": item.get("command") or item.get("name") or "",
            "name": item.get("name") or "",
            "status": item.get("status") or "",
            "returncode": item.get("returncode"),
            "output": item.get("output") or item.get("detail") or "",
        })
    changes = state.get("verified_changes") or state.get("changes") or []
    return {
        "id": state.get("id", ""),
        "session_id": state.get("session_id", ""),
        "task": state.get("task") or state.get("requirement", ""),
        "workspace": state.get("root", ""),
        "worktree": state.get("worktree", ""),
        "branch": state.get("base", "") or "(detached)",
        "run_branch": state.get("branch", ""),
        "base_sha": state.get("base_sha", ""),
        "protected": bool(state.get("protected")),
        "auto_push": bool(state.get("auto_push", True)),
        "status": status,
        "created_at": state.get("created", ""),
        "updated_at": state.get("updated", ""),
        "files": [c.get("path", "") for c in changes],
        "validation": checks,
        "commit": state.get("commit", ""),
        "delivery": state.get("delivery") or {},
        "error": error,
        "worktree_removed": bool(state.get("worktree_removed")),
        "confirmed_at": state.get("confirmed_at", ""),
        "rollback_commit": state.get("rollback_commit", ""),
    }


def _wrap(emit):
    """SSE 只做转发，任何断连异常都不能影响交付。"""
    if not emit:
        return None

    def send(event):
        state = None
        try:
            state = iteration.load(str(event.get("iteration_id") or ""))
        except GitError:
            pass
        try:
            emit({"ev": "agent_run", "action": event.get("action", ""),
                  "run_id": event.get("iteration_id", ""),
                  "run": _view(state) if state else None,
                  "detail": event.get("detail") or {}})
        except Exception:  # noqa: BLE001
            pass

    return send


def start(workspace, session_id, task, emit=None, validation=None,
          auto_push=True):
    """创建不可变基线上的独立 worktree；非 Git 目录返回 None。"""
    workspace = os.path.abspath(workspace or "")
    if not workspace or not gb.is_repo(workspace):
        return None
    state_root = _state_root()
    if not _outside(state_root, workspace):
        raise ValueError("Agent 运行清单目录必须位于主工作区之外")
    primary = gb.status(workspace)
    base_sha = primary.get("head_sha") or ""
    if not base_sha:
        raise ValueError("空仓库尚无可隔离的 HEAD，请先由用户创建初始提交")
    base = str(primary.get("branch") or "(detached)")
    commands = validation
    if commands is None:
        commands = _cfg.get("agent.validation_commands", []) or []
    res = iteration.start(
        workspace, task, base=base, mode="agent", session_id=session_id,
        validation=commands, auto_push=auto_push,
        worktree_base=os.path.join(state_root, "worktrees"))
    run = _view(res["iteration"])
    if run and emit:
        _wrap(emit)({"action": "run_started", "iteration_id": run["id"],
                     "detail": {"base_sha": base_sha, "branch": base}})
    return run


def get_run(run_id, with_events=False):
    try:
        state = iteration.load(str(run_id or ""))
    except GitError:
        return None
    view = _view(state)
    if with_events:
        view["events"] = _events(state)
    return view


def list_runs(session_id="", workspace="", limit=50):
    rows = iteration.list_all(limit, session_id, workspace)["iterations"]
    items = []
    for row in rows:
        try:
            state = iteration.load(row["id"])
        except GitError:
            continue
        view = _view(state)
        if view:
            items.append(view)
    return items


def active_run(run_id):
    return get_run(run_id)


def finalize(run_id, emit=None, validation=None, success=True):
    """冻结文件清单并校验；任何漂移、额外产物或校验失败都禁止提交。"""
    send = _wrap(emit)
    with _LOCK:
        try:
            state = iteration.load(str(run_id or ""))
        except GitError:
            return None
        if state.get("state") not in ("prepared", "verifying", "validating"):
            return _view(state)
        if not success:
            failed = iteration.fail_by_id(
                run_id, "generation_incomplete",
                "生成未完成，保留 worktree 供审计", send)
            return _view(failed["iteration"])
        commands = validation
        if commands is None:
            commands = state.get("validation_commands") or []
            if not commands:
                commands = _cfg.get("agent.validation_commands", []) or []
        allow_no_checks = not bool(iteration._norm_commands(commands))
        try:
            checked = iteration.verify(
                state["root"], run_id, validation=commands,
                allow_no_checks=allow_no_checks, emit=send)
        except GitError as exc:
            failed = iteration.fail_by_id(run_id, exc.code, exc.message, send)
            return _view(failed["iteration"])
        if not checked.get("ok"):
            if checked.get("code") == "no_changes":
                iteration.abort(state["root"], run_id, force=True,
                                final_state="no_changes")
                return _view(iteration.load(run_id))
            return _view(checked["iteration"])
        message = "feat: agent iteration %s\n\nWasily-Run: %s\nWasily-Base: %s" % (
            run_id, run_id, state.get("base_sha", ""))
        try:
            done = iteration.commit(state["root"], run_id, message,
                                    push=True, actor="agent", emit=send)
        except GitError as exc:
            failed = iteration.fail_by_id(run_id, exc.code, exc.message, send)
            return _view(failed["iteration"])
        return _view(done["iteration"])


def abort(run_id, message, emit=None):
    """外层集成异常也要失败关闭，不能让清单停留在 running。"""
    with _LOCK:
        try:
            failed = iteration.fail_by_id(
                run_id, "integration_error", str(message or "未知运行错误"),
                _wrap(emit))
        except GitError:
            return None
        return _view(failed["iteration"])


def confirm(run_id, emit=None):
    with _LOCK:
        res = iteration.confirm_by_id(run_id, _wrap(emit))
        return _view(res["iteration"])


def rollback(run_id, emit=None):
    with _LOCK:
        res = iteration.rollback_by_id(run_id, _wrap(emit))
        return _view(res["iteration"])
