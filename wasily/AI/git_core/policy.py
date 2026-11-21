"""提交与分支策略：Conventional Commits、SemVer 建议、分支写入权限。

为什么放在内核里：提交信息规范如果只靠提示词，模型随时能绕过；
这里用确定性校验，不合规的提交在写对象之前就被拒绝。
"""
from __future__ import annotations

import re
from typing import Any

TYPES = ("feat", "fix", "docs", "style", "refactor",
         "perf", "test", "build", "ci", "chore", "revert")

_COMMIT_RE = re.compile(
    r"^(?P<type>" + "|".join(TYPES) + r")"
    r"(?:\((?P<scope>[^()\r\n]{1,32})\))?"
    r"(?P<bang>!)?: (?P<subject>\S.{0,199})$"
)

# 分支 → 允许的自动动作。develop/main 明确禁止 Agent 直接写。
POLICY: dict[str, dict[str, bool]] = {
    "feature": {"commit": True, "push": True, "merge": False},
    "develop": {"commit": False, "push": False, "merge": False},
    "main": {"commit": False, "push": False, "merge": False},
}


def branch_kind(branch: str) -> str:
    b = (branch or "").strip()
    if b.startswith("feature/") or b.startswith("feat/"):
        return "feature"
    if b in ("develop", "dev"):
        return "develop"
    if b in ("main", "master"):
        return "main"
    return "other"


def validate_commit(message: str) -> tuple[bool, str]:
    msg = (message or "").strip()
    if not msg:
        return False, "提交信息不能为空"
    first = msg.splitlines()[0].strip()
    if not _COMMIT_RE.match(first):
        return False, ("提交信息需符合 Conventional Commits，"
                       "例如 feat(git): 增加部分暂存")
    if "BREAKING CHANGE:" in msg and not first.endswith("!"):
        # 允许正文声明破坏性变更，但首行必须能看出类型
        pass
    return True, ""


def bump_for(messages: list[str]) -> str:
    """由一组提交信息推导版本增量：破坏性 > 新功能 > 修复。"""
    seen = "patch"
    for msg in messages:
        first = (msg or "").strip().splitlines()[0] if (msg or "").strip() else ""
        head = first.split(":")[0]
        if "!" in head or "BREAKING CHANGE:" in (msg or ""):
            return "major"
        if head.startswith("feat"):
            seen = "minor"
    return seen


def bump_version(version: str, bump: str) -> str:
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)$", (version or "").strip())
    if not m:
        raise ValueError("版本号必须形如 1.2.3：%s" % version)
    major, minor, patch = (int(x) for x in m.groups())
    if bump == "major":
        return "%d.0.0" % (major + 1)
    if bump == "minor":
        return "%d.%d.0" % (major, minor + 1)
    return "%d.%d.%d" % (major, minor, patch + 1)


def can_auto(branch: str, action: str) -> tuple[bool, str]:
    kind = branch_kind(branch)
    rule = POLICY.get(kind, {})
    if rule.get(action):
        return True, ""
    if kind == "other":
        return False, "非 feature/develop/main 分支不参与自动闭环"
    return False, "%s 分支的 %s 需要人工确认" % (branch or kind, action)


def policy_snapshot(branch: str) -> dict[str, Any]:
    kind = branch_kind(branch)
    return {"branch": branch, "kind": kind,
            "auto": dict(POLICY.get(kind, {}))}
