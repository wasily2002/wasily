"""HTTP 路由层：/v1/git/* 与 /v1/iterations/*。

为什么单独成文件：api_server.py 已近 12000 行，Git 与迭代闭环的路由
再塞进去会让主服务更难维护；这里只做参数解析与错误映射，
所有仓库操作都委托给 git_core，不直接读写 .git。
"""
from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs

import git_core  # noqa: F401 — 确保包初始化先于子模块导入
from git_core import GitService, iteration
from git_core.repository import GitError


def handles_get(path: str) -> bool:
    return path.startswith("/v1/git/") or path.startswith("/v1/iterations")


def handles_post(path: str) -> bool:
    return handles_get(path)


def _q(query: str) -> dict[str, str]:
    return {k: v[-1] for k, v in parse_qs(query or "", keep_blank_values=True).items()}


def _bool(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _ids(value: Any) -> list[str]:
    # 变更块 id 是内容指纹（hex 字符串），不能转 int，否则按块暂存永远匹配不上
    if isinstance(value, list):
        raw = value
    else:
        raw = str(value or "").split(",")
    return [str(item).strip() for item in raw if str(item).strip()]


def _files(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x) for x in value if str(x).strip()]
    return [p for p in str(value or "").split(",") if p.strip()]


def _send(handler: Any, payload: dict[str, Any]) -> bool:
    handler._send_json(200 if payload.get("ok", True) else 400, payload)  # noqa: SLF001
    return True


def _fail(handler: Any, exc: GitError) -> bool:
    # 400 表示可修正的调用错误；前端按 code 决定是弹确认框还是提示重试
    handler._send_json(400, exc.to_dict())  # noqa: SLF001
    return True


# ---------- GET ----------

def handle_get(handler: Any, path: str, query: str = "") -> bool:
    if not handles_get(path):
        return False
    q = _q(query)
    repo = str(q.get("path") or q.get("repo") or "").strip()
    try:
        if path == "/v1/git/status":
            return _send(handler, GitService.status(repo, _bool(q.get("fresh"))))
        if path == "/v1/git/diff":
            return _send(handler, GitService.diff(
                repo, q.get("mode") or "review", q.get("rel") or ""))
        if path == "/v1/git/content":
            return _send(handler, GitService.content(
                repo, q.get("rel") or "", q.get("side") or "worktree"))
        if path == "/v1/git/hunks":
            return _send(handler, GitService.hunks(
                repo, q.get("rel") or "", q.get("base") or "index"))
        if path == "/v1/git/branches":
            return _send(handler, GitService.branches(repo))
        if path == "/v1/git/log":
            return _send(handler, GitService.log(repo, _int(q.get("limit"), 20)))
        if path == "/v1/git/remotes":
            return _send(handler, GitService.remotes(repo))
        if path == "/v1/git/worktrees":
            return _send(handler, GitService.worktree_list(repo))
        if path == "/v1/git/merge/conflicts":
            return _send(handler, GitService.merge_conflicts(repo))
        if path == "/v1/git/audit":
            return _send(handler, GitService.audit_trail(repo, _int(q.get("limit"), 50)))
        if path == "/v1/git/policy":
            st = GitService.status(repo)
            return _send(handler, {"ok": True, "policy": st.get("policy", {}),
                                   "branch": st.get("branch", "")})
        if path == "/v1/iterations/recover":
            return _send(handler, iteration.recover(repo))
        if path == "/v1/iterations/detail":
            return _send(handler, iteration.get(repo, q.get("id") or ""))
        if path == "/v1/iterations":
            # 迭代日志就是 CHANGELOG 之外的结构化变更轨迹
            return _send(handler, iteration.list_iterations(repo, _int(q.get("limit"), 30)))
    except GitError as exc:
        return _fail(handler, exc)
    handler._send_json(404, {"ok": False, "error": "接口不存在: %s" % path})  # noqa: SLF001
    return True


# ---------- POST ----------

def handle_post(handler: Any, path: str, data: dict[str, Any]) -> bool:
    if not handles_post(path):
        return False
    data = data or {}
    repo = str(data.get("path") or data.get("repo") or "").strip()
    try:
        if path == "/v1/git/stage":
            return _send(handler, GitService.stage(repo, _files(data.get("files"))))
        if path == "/v1/git/unstage":
            return _send(handler, GitService.unstage(repo, _files(data.get("files"))))
        if path == "/v1/git/stage_hunks":
            return _send(handler, GitService.stage_hunks(
                repo, str(data.get("file") or data.get("rel") or ""),
                _ids(data.get("ids"))))
        if path == "/v1/git/unstage_hunks":
            return _send(handler, GitService.unstage_hunks(
                repo, str(data.get("file") or data.get("rel") or ""),
                _ids(data.get("ids"))))
        if path == "/v1/git/commit":
            return _send(handler, GitService.commit(
                repo, str(data.get("message") or ""),
                str(data.get("expected_head") or ""),
                str(data.get("iteration_id") or ""),
                str(data.get("author") or "")))
        if path == "/v1/git/branch/create":
            return _send(handler, GitService.create_branch(
                repo, str(data.get("name") or ""), str(data.get("start") or "")))
        if path == "/v1/git/switch":
            return _send(handler, GitService.switch(repo, str(data.get("name") or "")))
        if path == "/v1/git/reset":
            return _send(handler, GitService.reset(
                repo, str(data.get("mode") or "mixed"),
                str(data.get("ref") or "HEAD"), _bool(data.get("confirm"))))
        # init/discard/revert/remote 增删已迁入 git_core；旧桥只保留给
        # Agent 运行记录等内部调用，/v1/git 表面不再依赖双栈。
        if path == "/v1/git/init":
            return _send(handler, GitService.init(repo))
        if path == "/v1/git/discard":
            return _send(handler, GitService.discard(
                repo, str(data.get("rel") or "")))
        if path == "/v1/git/revert":
            return _send(handler, GitService.revert(
                repo, str(data.get("committish") or data.get("sha") or ""),
                str(data.get("message") or ""), _bool(data.get("no_commit"))))
        if path == "/v1/git/remote/add":
            return _send(handler, GitService.remote_add(
                repo, str(data.get("name") or "origin"), str(data.get("url") or "")))
        if path == "/v1/git/remote/remove":
            return _send(handler, GitService.remote_remove(
                repo, str(data.get("name") or "origin")))
        if path == "/v1/git/clone":
            return _send(handler, GitService.clone(
                str(data.get("url") or ""),
                str(data.get("target") or repo),
                str(data.get("branch") or ""), _int(data.get("depth"), 0) or None,
                str(data.get("username") or ""), str(data.get("password") or "")))
        if path == "/v1/git/fetch":
            return _send(handler, GitService.fetch(
                repo, str(data.get("remote") or "origin"),
                _bool(data.get("prune")), str(data.get("username") or ""),
                str(data.get("password") or "")))
        if path == "/v1/git/pull":
            return _send(handler, GitService.pull(
                repo, str(data.get("remote") or "origin"),
                str(data.get("branch") or ""), _bool(data.get("ff_only"), True),
                str(data.get("username") or ""), str(data.get("password") or "")))
        if path == "/v1/git/push":
            return _send(handler, GitService.push(
                repo, str(data.get("branch") or ""),
                str(data.get("remote") or "origin"),
                _bool(data.get("set_upstream"), True),
                str(data.get("username") or ""), str(data.get("password") or "")))
        if path == "/v1/git/merge":
            return _send(handler, GitService.merge(
                repo, str(data.get("branch") or ""),
                str(data.get("message") or ""), _bool(data.get("no_commit"))))
        if path == "/v1/git/merge/abort":
            return _send(handler, GitService.merge_abort(
                repo, _bool(data.get("confirm"), True)))
        if path == "/v1/git/merge/continue":
            return _send(handler, GitService.merge_continue(
                repo, str(data.get("message") or "")))
        if path == "/v1/git/worktree/create":
            return _send(handler, GitService.worktree_create(
                repo, str(data.get("name") or ""), str(data.get("branch") or ""),
                str(data.get("dest") or "")))
        if path == "/v1/git/worktree/remove":
            return _send(handler, GitService.worktree_remove(
                repo, str(data.get("dest") or ""), _bool(data.get("force"))))
        if path == "/v1/iterations/start":
            return _send(handler, iteration.start(
                repo, str(data.get("requirement") or ""),
                str(data.get("branch") or ""), str(data.get("base") or ""),
                str(data.get("tests") or ""), str(data.get("id") or "")))
        if path == "/v1/iterations/verify":
            return _send(handler, iteration.verify(
                repo, str(data.get("id") or ""), _int(data.get("timeout"), 180)))
        if path == "/v1/iterations/commit":
            return _send(handler, iteration.commit(
                repo, str(data.get("id") or ""), str(data.get("message") or ""),
                _bool(data.get("push"), True), str(data.get("actor") or "agent")))
        if path == "/v1/iterations/abort":
            return _send(handler, iteration.abort(
                repo, str(data.get("id") or ""), _bool(data.get("force"))))
    except GitError as exc:
        return _fail(handler, exc)
    handler._send_json(404, {"ok": False, "error": "接口不存在: %s" % path})  # noqa: SLF001
    return True
