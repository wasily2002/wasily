# 贡献与版本管理规范

本仓库是 Wasily 的 monorepo，包含 AI 后端（`wasily/AI`）、完整 IDE 源树
（`wasily/tools/wasily-ide-src`）、VSCodium 构建源码与配套脚本。

## 分支模型

| 分支 | 用途 | 谁能写 |
|---|---|---|
| `main` | 稳定发布，每个提交都可对外 | 仅经批准的合并 |
| `develop` | 集成验证，功能在这里汇总 | 经批准的合并 |
| `feature/<任务>-<短ID>` | 单轮迭代，Agent 的默认落点 | Agent 可自动提交/推送 |

规则：

1. 任何功能改动都从 `develop` 切出 `feature/*`，不直接在 `main` 上开发。
2. `feature` 分支通过检查后允许自动提交并推送，但**不自动**合入 `develop`。
3. `develop` → `main` 只走人工确认的发布流程。
4. 禁止对 `main` / `develop` 强制推送，禁止改写已推送历史；回滚用 revert。
5. 多个功能方向并行时，各自使用独立分支与独立工作目录，互不共享未提交修改。

## 提交信息（Conventional Commits）

```
<type>(<scope>): <subject>

<body>
```

允许的 `type`：`feat` `fix` `docs` `style` `refactor` `perf` `test` `build` `ci` `chore` `revert`。

- `subject` 用中文或英文均可，但必须一句话说清“改了什么”。
- 破坏性变更：`type(scope)!: subject`，并在正文写 `BREAKING CHANGE:` 说明。
- 一个提交只做一件事，避免把格式化与功能改动混在一起。

示例：

```
feat(git): 增加二进制安全的取消暂存
fix(agent): 校验失败时不再创建提交
```

## 语义化版本（SemVer）

- 产品版本以 `wasily/AI/version.json` 为准，`wasily-ai` 扩展的
  `package.json` 必须与之保持一致。
- 版本号只在发布时由人工推进：`fix` → patch，`feat` → minor，破坏性 → major。
- 每个 `feature` 提交不代表一次发版，发版由 `main` 上的 `vX.Y.Z` 标签标记。

C1 基线已统一为 0.5.0；上游 VS Code/VSCodium 版本保留在各自源码与
`vscodium-build/upstream/` 中，不与 Wasily 产品 SemVer 混用。后续发布仍须人工确认。

## 提交前必须通过

1. 被改动的 `*.py` 通过 `python -m py_compile`。
2. 被改动的 JS 通过 `node --check`。
3. 相关测试通过（`wasily/AI/tests`）。
4. `scripts/repo_guard.py` 无阻断项：不含密钥、超大文件、本机运行状态。
5. 每轮迭代附结构化变更记录，至少包含：修改文件清单、功能变更点、
   依赖的上一个迭代 ID、验证结果。

## 不纳入版本控制的内容

依赖目录、构建产物、模型权重、虚拟环境、会话与训练数据、日志缓存、
真实运行配置、密钥与证书。详见 `.gitignore`，新增可疑文件先跑
`python scripts/repo_guard.py` 再提交。
