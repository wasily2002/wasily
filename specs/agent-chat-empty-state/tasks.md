# 任务清单：对话页空状态改造

> 依据 `spec.md`，只改 `wasily/AI/chat.html`。

- [x] T1 在 `.chat-wrap` 内（`#ptrHint` 之后）插入 `#chatHero`：`.hero-title` + `#heroSlot` + `#heroChips`（4 个 `data-preset` 按钮）
- [x] T2 在 `<style>` 末尾追加空状态样式块（`.chat-hero` / `body.chat-empty` 显隐与居中约束 / `.hero-title` / `.hero-slot` / `.hero-chips`），并删除旧 `.agent-welcome` 规则（1024–1029、1188–1195）
- [x] T3 在 `addMsg()` 之前新增 `chatHero` / `heroSlot` / `hintEl` / `chatEmpty` 变量与 `msgExists()` / `setChatEmpty()` / `syncChatEmpty()`
- [x] T4 接入状态同步：`addMsg` 末尾、`clearChat`、`restoreChat`、`switchToSession`、init 处调用 `syncChatEmpty()`；删除 `renderAgentWelcome()` 与其调用、`addMsg` 里 `#agentWelcome` 清理逻辑
- [x] T5 修正 init 的自动聚焦：`if(!WB_MODE && msgExists()) input.focus();`
- [x] T6 新增 `leaveHero()` 与 `input` 的 focus/input 监听、`#heroChips` 点击委托
- [x] T7 自检：`node --check` 内联脚本；浏览器验证 `/chat?embed=agent&ipc=1` 空状态→聚焦落底→chip 预填；`&workbench=1` 空状态无工具条、发消息后恢复；控制台无报错
