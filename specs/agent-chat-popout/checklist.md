# Checklist v2：三栏主窗口 + 跨进程通信

- [x] `POST /agent/ipc` 能写入 `prefill`，`GET /agent/ipc?client=chat_t1` 取到且重复取为空
- [x] `to="chat"` 时两个对话客户端都能收到同一条命令（游标各自推进）
- [x] `action="state"` 上报后，`GET /agent/ipc?client=agent` 的 `peers` 能看到该状态
- [x] `/agent` 主窗口显示三栏：左导航、中对话 iframe、右工作台
- [x] 点击「新对话」后内嵌面板被清空（含草稿输入框）
- [x] 点项目进入对话时，内嵌面板输入框被预填（不再另开窗口）
- [x] 点「弹出」出现 `wasily · 对话` 独立窗口（沿用 v1 已验证的 `Api.open_chat`）
- [x] 主窗口历史下拉数据源与切换链路核对 `/v1/agent/sessions` 返回结构一致
- [x] 右侧终端执行 `Get-Date` 返回当前时间，中文输出不乱码
- [x] 「代码变更」列出 `/undo/list` 条目（含文件名、时间、工具）
- [ ] 撤销按钮实点验证（会回滚真实文件，未在验证中执行）
- [x] 工作台拖拽调宽后刷新仍保持（localStorage `wasily.agent.wbWidth`）
- [x] 我的产物：本地/云端/飞书 三个分页可切换，本地有真实卡片，另两页为空态
- [x] 左侧搜索框输入关键字能过滤导航与项目条目，清空后恢复
- [x] 「我的站点」页可列出 `.html` 产物并打开
- [x] `python -m py_compile wasily/AI/api_server.py` 通过，`/health` 为 ok
- [x] 两个 HTML 的内联脚本 `node --check` 通过，浏览器控制台无报错
