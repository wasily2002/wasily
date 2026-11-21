# WasilyIDE

WasilyIDE 与本地 AI Agent 的协同开发 monorepo。仓库包含自研 IDE 扩展、界面补丁、AI 后端及其他同工作区项目源码，并提供无需系统 Git 客户端的版本管理与自动迭代能力。

## 源码边界

本仓库托管完整 VS Code 源树、VSCodium 构建源码及 Wasily 定制，不包含 IDE 运行包、模型、虚拟环境或构建产物。权威 IDE 源码位于 `wasily/tools/wasily-ide-src/` 与 `wasily/tools/vscodium-build/`；`wasily/tools/wasily-ide/` 仅是本机部署目标，不进入公开仓库。

## 目录

- `wasily/AI/`：Python AI 服务、Agent、Git 与迭代服务。
- `wasily/tools/wasily-ide-src/`：完整 VS Code 源树及 Wasily 扩展、主题和界面定制。
- `wasily/tools/vscodium-build/`：VSCodium 构建脚本、上游补丁与 Wasily 品牌配置。
- `iterations/`：可审计的结构化迭代清单。
- 其他顶层目录：同一 monorepo 中的独立项目。

## 本地运行

后端依赖 Python 3.12。安装 `wasily/AI/requirements.txt` 中依赖后，使用现有启动脚本启动 Wasily；Git 功能由 Dulwich 提供，不要求安装 `git.exe` 或 GitHub CLI。首次配置可复制 `wasily/AI/config.example.json` 与 `mcp.example.json` 为本机 `config.json`、`mcp.json`，真实配置和凭据不会进入仓库。

## 开发流程

在 `develop` 上创建 `feature/<topic>`。Agent 迭代使用独立 worktree，验证通过后仅自动提交并推送对应 feature；合入 `develop` 和 `main` 需要人工确认。详见 `CONTRIBUTING.md`。

## 许可证

MIT，见 `LICENSE`。第三方运行组件仍遵循其各自许可证。
