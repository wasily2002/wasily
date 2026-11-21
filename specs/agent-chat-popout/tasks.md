# Tasks v2：三栏主窗口 + 跨进程通信 + 产物页/导航对齐

- [x] T1 后端 IPC 枢纽（`wasily/AI/api_server.py`）
  - 模块级：`_IPC_LOCK/_IPC_LOG/_IPC_CURSOR/_IPC_SEEN/_IPC_STATE/_IPC_SEQ`、`_ipc_match/_ipc_push/_ipc_floor/_ipc_take`。
  - GET `/agent/ipc`：按游标增量取命令、刷新在线时间、返回 `peers`。
  - POST `/agent/ipc`：`action=="state"` 记录状态；否则写日志并返回 id。

- [x] T2 对话页 IPC 客户端（`wasily/AI/chat.html`）
  - `IPC_MODE` / `IPC_CLIENT`（每实例随机后缀）；`ipcApply` / `ipcPoll`(900ms) / `ipcReport`(3s)。
  - `switchToSession` 后上报一次；`clear` 同时清空草稿输入框。
  - 顶部身份条 `#idChip`（独立窗口显示，读 `/account/info`）。

- [x] T3 主窗口三栏骨架（`wasily/AI/agent/index.html`）
  - `#pane-chat` 改为 `.chat-cols`：中栏 `#chatFrame` + `chat-top`，右栏 `#wbPane`。
  - 删除 `.chat-pop/.cp-*` CSS，新增三栏与工作台样式。
  - 初始化 `chatFrame.src = agentChatUrl()`（带 `&ipc=1`）。

- [x] T4 主窗口 IPC 联动（`agent/index.html`）
  - `ipcSend` / `ipcPump`(2s，取最新的 `chat*` 客户端状态更新 `#chatTitle`)。
  - `openChat` 发 `prefill`+`focus`；`newTask` 发 `clear`；`popoutChat` 调 `openChatWindow`。
  - `chatAction('histBtn')` 历史下拉 → `set_session`。

- [x] T5 右侧工作台（`agent/index.html`）
  - 终端：`termRun()` 走 `/agent/tool_run`，命令用 `powershell -EncodedCommand`（UTF-16LE base64）执行，避免 cmd 报错与中文乱码。
  - 代码变更：`loadDiff()` 走 `/undo/list` 渲染，撤销走 `/undo/step`。
  - `wbTab()` 切换、`#wbResizer` 拖拽调宽（`wasily.agent.wbWidth`）。

- [x] T6 我的产物页重做（`agent/index.html`）
  - 分页（本地/云端/飞书）+ 分类 chips + 卡片网格。
  - 本地用真实 `/agent/outputs`；云端/飞书为空态引导。

- [x] T7 左侧导航细节（`agent/index.html`）
  - 顶部搜索框 `#sideSearch` + `filterNav()`。
  - 新增「我的站点」导航与 `#pane-sites`（列 `/agent/outputs` 中的 `.html`）。

- [x] T8 自检
  - `py_compile` 通过；后端重启后 `/health` 为 ok。
  - 两个 HTML 内联脚本 `node --check` 通过，浏览器控制台无报错。
  - IPC 游标广播：两个 chat 客户端都收到同一条命令，重复 GET 为空。
  - 浏览器验证：三栏、项目预填、新对话清空、终端 `Get-Date`、代码变更列表、产物分页、侧栏搜索。

- [x] T9 文档
  - 更新 `specs/agent-chat-popout/spec.md`（改为游标广播）、`tasks.md`、`checklist.md`。
