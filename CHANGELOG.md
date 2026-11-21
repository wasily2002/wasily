# Changelog

本文件记录面向用户的版本变化，格式参考 Keep a Changelog，
版本号遵循 Semantic Versioning。

## [Unreleased]

### Added

- 仓库基线：`.gitignore` / `.gitattributes` / 许可证 / 贡献规范。
- `scripts/repo_guard.py`：提交前的敏感信息与超大文件扫描。
- `wasily/AI/git_core`：基于 Dulwich 的内置 Git 内核（无系统 git 依赖）。
- `wasily/AI/git_api.py`：`/v1/git/*` 与 `/v1/iterations/*` 统一路由。
- `wasily/AI/git_core/iteration.py`：需求 → 隔离工作区 → 校验 → 提交 → 推送 → 审计的迭代事务。
- `wasily-git` 扩展：原生源代码管理、逐行差异、按块暂存、分支与推送、提交历史与迭代面板。
- `git_loop` 用户工具：Agent 可直接发起并完成一轮迭代。

### Changed

- `git_bridge.py` 的语义将逐步收敛到 `git_core`，旧的文本重建式取消暂存会被替换。
- `wasily-ai` 的 Git Review 改为走后端 `/v1/git/diff`；IDE 内不再解析或下载系统 git。
- 提交对象由 `git_core` 自行构造，修复 linked worktree 中父节点被写成 hex 字符串的问题。

### Fixed

- 取消暂存不再解码重写 Blob，二进制文件、空文件与可执行位不再被破坏。
