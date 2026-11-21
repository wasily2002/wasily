# AGENTS.md — 本仓库规则（Wasily）

> 本文件由后端 kernel 规则层加载并注入系统提示词，请保持精简（规则会占用上下文）。

> **路径约定**：本文件位于工程根 `项目介绍\`，下文所有路径均相对 `wasily\`，即 `wasily/AI/` = `D:\煮饭仔点餐\项目文件\项目介绍\wasily\AI\`。
>
> **归属说明（2026-11-20 明确）**：本文件定位为「工程根的流程规则」，与 `wasily\AI\.traerules`（技术栈约束）分工——本文件管目录、端口、自检与红线，`.traerules` 管技术栈与代码风格。
>
> **为何不移到 `wasily\AI\`**：kernel 从 `wasily\AI` 向上 3 层查找规则，放根目录仍能命中（距 AI 2 层）；而 Trae 以工程根为 workspace 读取项目规则，移进子目录会让 Trae 侧失效。故保留原位，用上面的路径约定消除歧义。

## 项目结构

| 路径 | 作用 |
|---|---|
| `wasily/AI/` | Python 后端。`api_server.py` 主服务（:8000），OpenAI 兼容 `/v1/chat/completions`，本地 llama.cpp 模型 |
| `wasily/AI/user_tools/` | 用户工具，每个文件含 `SPEC = {name, description, parameters}` 与 `def run(**kwargs)`，自动加载 |
| `wasily/AI/mcp.json` | MCP 服务器配置（stdio / Streamable HTTP） |
| `wasily/tools/wasily-ide/` | 自研 IDE（VSCodium 派生） |
| `.../resources/app/extensions/wasily-ai/` | IDE 扩展：`extension.js`（AI 面板 + 18753 桥）、`mcpHost.js`（MCP 托管） |

## 端口

- `8000` 后端 API
- `9000` 本地 HTTP MCP 服务
- `18753` IDE 桥（`/health` `/context` `/open_url` `/command`）
- `8300+` 站点托管（记录在 `sites_store.json`，当前 3 个站点跑在 8300 / 8301 / 8302）
- `9222` 浏览器 CDP、`9876` Blender 插件 Socket（按需）

## 代码约定

- Python：PEP8，4 空格缩进，中文注释
- JS：单引号，2 空格缩进，字符串拼接优先用 `+`（与现有 `extension.js` 风格一致）
- 注释写「为什么」，不写「是什么」
- 不新建文档类文件，除非用户明确要求

## 改完必须自检

- 改 `*.py` → `python -c "import py_compile; py_compile.compile(r'<path>', doraise=True)"`
- 改 `extension.js` / `mcpHost.js` → `node --check <path>`
- 改后端逻辑 → 重启后确认 `http://127.0.0.1:8000/health` 返回 ok
- 声称「已打开/已完成」前，必须有工具真实返回结果作证据

## 行为红线

- **运行 ≠ 开发**：用户说「运行 / 打开 / 预览 网页」时只执行，禁止改造页面、禁止输出改造方案
- 不修改用户文件内容，除非用户明确要求改
- 不在代码里硬编码密钥、密码、token
- 删除文件、结束进程前先确认目标，不广谱 `taskkill python` 这类批量操作
- 破坏性操作（卸载、删目录、改系统设置）先列清单给用户确认

## 本地调试入口

- 后端健康：`http://127.0.0.1:8000/health`
- MCP 状态：`GET http://127.0.0.1:8000/v1/mcp/servers`
- IDE 桥健康：`http://127.0.0.1:18753/health`
