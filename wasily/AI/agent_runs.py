"""Agent 隔离运行：仓库外清单、独立 worktree、失败关闭交付。"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import threading
import time
import uuid

import config as _cfg
import git_bridge as gb

_LOCK = threading.RLock()
_ACTIVE = {}
PROTECTED_BRANCHES = {"main", "master", "develop"}


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


def _run_dir(run_id):
    return os.path.join(_state_root(), "runs", run_id)


def _manifest_path(run_id):
    return os.path.join(_run_dir(run_id), "run.json")


def _public(run):
    return copy.deepcopy(run)


def _save(run):
    os.makedirs(_run_dir(run["id"]), exist_ok=True)
    run["updated_at"] = time.time()
    target = _manifest_path(run["id"])
    temp = target + ".tmp"
    with open(temp, "w", encoding="utf-8") as stream:
        json.dump(run, stream, ensure_ascii=False, indent=2)
    os.replace(temp, target)


def _emit(run, action, emit=None, **detail):
    event = {"action": action, "run_id": run["id"], "ts": time.time()}
    event.update(detail)
    os.makedirs(_run_dir(run["id"]), exist_ok=True)
    with open(os.path.join(_run_dir(run["id"]), "events.jsonl"), "a",
              encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    if emit:
        try:
            emit({"ev": "agent_run", "action": action, "run_id": run["id"],
                  "run": _public(run), "detail": detail})
        except Exception:
            # SSE 断开不能阻断交付，也不能丢失仓库外审计事件。
            pass


def _load(run_id):
    with open(_manifest_path(run_id), "r", encoding="utf-8") as stream:
        return json.load(stream)


def get_run(run_id, with_events=False):
    try:
        run = _load(str(run_id or ""))
    except (OSError, ValueError):
        return None
    if with_events:
        events = []
        try:
            with open(os.path.join(_run_dir(run["id"]), "events.jsonl"),
                      "r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        events.append(json.loads(line))
                    except ValueError:
                        pass
        except OSError:
            pass
        run["events"] = events
    return run


def list_runs(session_id="", workspace="", limit=50):
    root = os.path.join(_state_root(), "runs")
    items = []
    if not os.path.isdir(root):
        return []
    for name in os.listdir(root):
        try:
            run = _load(name)
        except (OSError, ValueError):
            continue
        if session_id and run.get("session_id") != session_id:
            continue
        if workspace and os.path.normcase(run.get("workspace", "")) != os.path.normcase(workspace):
            continue
        items.append(run)
    items.sort(key=lambda item: item.get("updated_at", 0), reverse=True)
    return items[:max(1, min(int(limit or 50), 200))]


def start(workspace, session_id, task, emit=None, validation=None,
          auto_push=True):
    """创建不可变基线上的独立 worktree；非 Git 目录返回 None。"""
    workspace = os.path.abspath(workspace or "")
    if not workspace or not gb.is_repo(workspace):
        return None
    state = _state_root()
    if not _outside(state, workspace):
        raise ValueError("Agent 运行清单目录必须位于主工作区之外")
    primary = gb.status(workspace)
    base = primary.get("head_sha") or ""
    if not base:
        raise ValueError("空仓库尚无可隔离的 HEAD，请先由用户创建初始提交")
    run_id = "run-" + uuid.uuid4().hex[:16]
    branch = str(primary.get("branch") or "(detached)")
    run_branch = "wasily/" + run_id
    worktree = os.path.join(state, "worktrees", run_id)
    run = {
        "id": run_id,
        "session_id": str(session_id or "default"),
        "task": str(task or "")[:1000],
        "workspace": workspace,
        "worktree": worktree,
        "branch": branch,
        "run_branch": run_branch,
        "base_sha": base,
        "protected": branch in PROTECTED_BRANCHES,
        "auto_push": bool(auto_push),
        "status": "creating",
        "created_at": time.time(),
        "updated_at": time.time(),
        "files": [],
        "validation": [],
        "commit": "",
        "delivery": {},
        "error": "",
        "worktree_removed": False,
    }
    _save(run)
    try:
        added = gb.worktree_add(workspace, worktree, branch=run_branch,
                                commitish=base)
        if not added.get("ok") or added.get("commit") != base:
            raise ValueError("worktree 基线校验失败")
        run["worktree"] = added["path"]
        run["status"] = "running"
        _save(run)
        with _LOCK:
            _ACTIVE[run_id] = run
        _emit(run, "run_started", emit, base_sha=base, branch=branch)
        return _public(run)
    except Exception as exc:
        run["status"] = "failed"
        run["error"] = str(exc)
        _save(run)
        _emit(run, "run_failed", emit, error=str(exc))
        raise


def active_run(run_id):
    with _LOCK:
        run = _ACTIVE.get(run_id)
        if run:
            return _public(run)
    return get_run(run_id)


def _changed_files(worktree):
    status = gb.status(worktree)
    files = list(status.get("changed") or [])
    files.extend(status.get("untracked") or [])
    files.extend(item["path"] for item in status.get("staged") or [])
    return sorted(set(files)), status


def _fail(run, code, message, emit=None, **detail):
    run["status"] = "failed"
    run["error"] = "%s: %s" % (code, message)
    _save(run)
    _emit(run, "run_failed", emit, code=code, error=message, **detail)
    return _public(run)


def _check_drift(run):
    primary = gb.status(run["workspace"])
    if primary.get("head_sha") != run["base_sha"]:
        return "primary_head_changed", "主工作区 HEAD 已变化，拒绝交付"
    linked = gb.status(run["worktree"])
    expected = run.get("commit") or run["base_sha"]
    if linked.get("head_sha") != expected:
        return "worktree_head_changed", "隔离 worktree HEAD 已变化，拒绝交付"
    if linked.get("conflicts"):
        return "conflicts", "隔离 worktree 存在冲突"
    return "", ""


def _validation_commands(run, validation):
    selected = validation
    if selected is None:
        selected = _cfg.get("agent.validation_commands", []) or []
    if isinstance(selected, str):
        selected = [selected]
    return list(selected or [])


def _validate(run, commands, emit):
    run["validation"] = []
    for index, item in enumerate(commands):
        if isinstance(item, str):
            command, timeout = item, 120
        else:
            command = str(item.get("command") or "")
            timeout = max(1, min(int(item.get("timeout") or 120), 600))
        result = {"command": command, "status": "running", "output": ""}
        run["validation"].append(result)
        run["status"] = "validating"
        _save(run)
        _emit(run, "validation_started", emit, index=index, command=command)
        if not command.strip():
            result.update(status="failed", returncode=-1, output="校验命令为空")
        else:
            try:
                proc = subprocess.run(
                    command, shell=True, cwd=run["worktree"],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=timeout,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                output = (proc.stdout or "") + (proc.stderr or "")
                result.update(returncode=proc.returncode, output=output[-4000:],
                              status="passed" if proc.returncode == 0 else "failed")
            except (OSError, subprocess.SubprocessError) as exc:
                result.update(status="failed", returncode=-1, output=str(exc))
        _save(run)
        _emit(run, "validation_finished", emit, index=index,
              status=result["status"], returncode=result.get("returncode"),
              output=result["output"][-500:])
        if result["status"] != "passed":
            return "validation_failed", "校验失败或校验工具不可用: %s" % command
    return "", ""


def _cleanup(run, emit=None):
    if run.get("worktree_removed") or not os.path.exists(run["worktree"]):
        run["worktree_removed"] = True
        _save(run)
        return
    removed = gb.worktree_remove(run["workspace"], run["worktree"], force=False)
    if removed.get("ok"):
        run["worktree_removed"] = True
        _save(run)
        _emit(run, "worktree_removed", emit)
    else:
        _emit(run, "cleanup_deferred", emit, error=removed.get("error"))


def finalize(run_id, emit=None, validation=None, success=True):
    """冻结文件清单并校验；任何漂移、额外产物或精确暂存差异都禁止提交。"""
    with _LOCK:
        run = _ACTIVE.get(run_id) or _load(run_id)
        if run.get("status") != "running":
            return _public(run)
        if not success:
            return _fail(run, "generation_incomplete", "生成未完成，保留 worktree 供审计", emit)
        files, status = _changed_files(run["worktree"])
        run["files"] = files
        _save(run)
        code, message = _check_drift(run)
        if code:
            return _fail(run, code, message, emit)
        if not files:
            run["status"] = "no_changes"
            _save(run)
            _emit(run, "no_changes", emit)
            _cleanup(run, emit)
            return _public(run)
        _emit(run, "files_collected", emit, files=files)
        code, message = _validate(run, _validation_commands(run, validation), emit)
        if code:
            return _fail(run, code, message, emit)
        after, _status = _changed_files(run["worktree"])
        if after != files:
            return _fail(run, "validation_changed_files", "校验产生了清单外文件变化",
                         emit, before=files, after=after)
        code, message = _check_drift(run)
        if code:
            return _fail(run, code, message, emit)
        staged = gb.stage(run["worktree"], files, exact=True)
        if not staged.get("ok"):
            return _fail(run, "stage_failed", "精确暂存失败", emit, detail=staged)
        current = gb.status(run["worktree"])
        staged_files = sorted(set(item["path"] for item in current.get("staged") or []))
        remaining = sorted(set((current.get("changed") or []) +
                               (current.get("untracked") or [])))
        if staged_files != files or remaining:
            return _fail(run, "manifest_mismatch", "暂存结果与本轮文件清单不一致",
                         emit, manifest=files, staged=staged_files, remaining=remaining)
        message = "feat: agent iteration %s\n\nWasily-Run: %s\nWasily-Base: %s" % (
            run["id"], run["id"], run["base_sha"])
        committed = gb.commit(run["worktree"], message)
        run["commit"] = committed.get("commit") or ""
        if not run["commit"]:
            return _fail(run, "commit_failed", "提交未生成", emit, detail=committed)
        run["status"] = "committed"
        _save(run)
        _emit(run, "commit_created", emit, commit=run["commit"], files=files)
        if run.get("protected"):
            run["status"] = "awaiting_confirmation"
            _save(run)
            _emit(run, "awaiting_confirmation", emit, branch=run["branch"])
            return _public(run)
        return _deliver(run, emit)


def _deliver(run, emit=None):
    code, message = _check_drift(run)
    if code:
        return _fail(run, code, message, emit)
    remotes = gb.remotes(run["worktree"]).get("remotes") or []
    remote = next((item["name"] for item in remotes if item["name"] == "origin"),
                  remotes[0]["name"] if remotes else "")
    if not remote or not run.get("auto_push", True):
        run["status"] = "committed"
        run["delivery"] = {"status": "local", "branch": run["run_branch"]}
        _save(run)
        _emit(run, "committed_local", emit, branch=run["run_branch"])
        return _public(run)
    if run["branch"] == "(detached)":
        return _fail(run, "detached_head", "detached HEAD 必须显式选择交付分支", emit)
    refspec = "HEAD:refs/heads/{0}".format(run["branch"])
    pushed = gb.push(run["worktree"], remote=remote, refspec=refspec, force=False)
    if not pushed.get("ok"):
        run["status"] = "push_failed"
        run["error"] = "push_failed: 远端拒绝或连接失败"
        run["delivery"] = {"status": "failed", "remote": remote,
                           "branch": run["branch"], "detail": pushed.get("error")}
        _save(run)
        _emit(run, "push_failed", emit, remote=remote, error=pushed.get("error"))
        return _public(run)
    run["status"] = "delivered"
    run["error"] = ""
    run["delivery"] = {"status": "pushed", "remote": remote,
                       "branch": run["branch"], "commit": run["commit"]}
    _save(run)
    _emit(run, "delivered", emit, remote=remote, branch=run["branch"],
          commit=run["commit"])
    return _public(run)


def abort(run_id, message, emit=None):
    """外层集成异常也要失败关闭，不能让清单停留在 running。"""
    with _LOCK:
        run = _ACTIVE.get(run_id) or _load(run_id)
        if run.get("status") not in ("running", "validating"):
            return _public(run)
        return _fail(run, "integration_error", str(message or "未知运行错误"), emit)


def confirm(run_id, emit=None):
    with _LOCK:
        run = _load(run_id)
        if run.get("status") != "awaiting_confirmation":
            raise ValueError("当前运行不在等待确认状态")
        run["confirmed_at"] = time.time()
        _save(run)
        _emit(run, "confirmed", emit, branch=run.get("branch"))
        return _deliver(run, emit)


def rollback(run_id, emit=None):
    with _LOCK:
        run = _load(run_id)
        if run.get("status") not in ("awaiting_confirmation", "committed",
                                     "delivered", "push_failed"):
            raise ValueError("当前状态不能回滚")
        delivered = run.get("delivery", {}).get("status") == "pushed"
        if delivered:
            if run.get("worktree_removed"):
                raise ValueError("交付 worktree 已清理，不能自动回滚；请在原生 SCM 中 revert")
            reverted = gb.revert(run["worktree"], run["commit"],
                                 message="revert: agent iteration %s" % run["id"])
            if not reverted.get("ok"):
                run["status"] = "rollback_failed"
                run["error"] = "rollback_failed: 反向提交失败"
                _save(run)
                _emit(run, "rollback_failed", emit, error=reverted.get("error"))
                return _public(run)
            remote = run["delivery"]["remote"]
            refspec = "HEAD:refs/heads/{0}".format(run["delivery"]["branch"])
            pushed = gb.push(run["worktree"], remote=remote,
                             refspec=refspec, force=False)
            if not pushed.get("ok"):
                run["status"] = "rollback_failed"
                run["error"] = "rollback_failed: 回滚推送失败"
                _save(run)
                _emit(run, "rollback_failed", emit, error=pushed.get("error"))
                return _public(run)
            run["rollback_commit"] = reverted.get("commit") or ""
        run["status"] = "rolled_back"
        run["rolled_back_at"] = time.time()
        _save(run)
        _emit(run, "rolled_back", emit, commit=run.get("rollback_commit") or "")
        return _public(run)
