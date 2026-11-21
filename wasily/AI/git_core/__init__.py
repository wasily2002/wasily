"""Wasily 内置 Git 内核（纯 Python，Dulwich 驱动）。

为什么新建包而不是继续堆在 git_bridge.py：
- 旧文件把仓库发现、索引重写、diff 和提交混在一起，且取消暂存会解码重写 Blob；
- 闭环需要按「仓库 / 快照 / 索引 / 引用 / 策略 / 审计」拆分职责，才能单独测试。

本包不调用系统 git 或 gh，所有对象读写都经过 Dulwich。
"""
from .repository import GitError, RepoHandle, discover_root, repo_id
from .service import GitService

__all__ = ["GitError", "RepoHandle", "discover_root", "repo_id", "GitService"]
