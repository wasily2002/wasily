"""让 Agent 直接驱动自迭代闭环（与 IDE 的 Wasily Git 面板共用同一套内核）。

为什么做成工具：Agent 只能改文件，却没法自己建分支、校验、提交、推送。
把这四步收敛成一个确定性入口，才能形成「需求 → 改码 → 验证 → 提交」的闭环，
并且每次提交都会留下结构化迭代轨迹。
"""
import json
import os

from git_core import GitService, iteration
from git_core.repository import GitError

SPEC = {
    "name": "git_loop",
    "description": "内置 Git 与自迭代闭环：状态、开隔离工作区、校验、提交推送、查看迭代轨迹（不依赖系统 git）",
    "parameters": {
        "action": "status|start|verify|commit|iterations|audit|abort",
        "path": "仓库路径，默认当前目录",
        "requirement": "start 时的需求描述",
        "tests": "可选测试命令（verify 时执行）",
        "id": "迭代编号（verify/commit/abort 必填）",
        "message": "commit 信息，需符合 Conventional Commits",
        "push": "commit 后是否推送 feature 分支，默认 true",
    },
}


def _dump(data):
    text = json.dumps(data, ensure_ascii=False)
    return text[:12000]


def run(action="status", path="", requirement="", tests="", id="", message="", push=True):
    root = path or os.getcwd()
    try:
        act = str(action or "status").strip().lower()
        if act == "status":
            return _dump(GitService.status(root, fresh=True))
        if act == "start":
            res = iteration.start(root, requirement, tests=tests or "")
            return _dump(res)
        if act == "verify":
            return _dump(iteration.verify(root, id))
        if act == "commit":
            return _dump(iteration.commit(root, id, message, push=bool(push)))
        if act == "abort":
            return _dump(iteration.abort(root, id, force=False))
        if act == "iterations":
            return _dump(iteration.list_iterations(root))
        if act == "audit":
            return _dump(GitService.audit_trail(root))
        return _dump({"ok": False, "error": "未知 action: %s" % action})
    except GitError as exc:
        return _dump(exc.to_dict())
    except Exception as exc:  # noqa: BLE001
        return _dump({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})
