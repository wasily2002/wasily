# Spec v2：Agent 主窗口三栏 + 对话内嵌/弹出双形态 + 跨进程通信

> v1（已完成）：主窗口只留导航、对话完全弹出独立窗口。见文末「附录：v1 摘要」。
> v2 动因：用户要求「1234，需要跨进程通信」——主窗口恢复中间对话面板（Trae 三栏），
> 独立窗口保留为可选形态，两者必须实时同步，不能只靠「聚焦旧窗口」。

## 1. 目标（DoD）

- G1 主窗口三栏：左导航 | 中对话面板（内嵌 `/chat?embed=agent&ipc=1`）| 右工作台（终端 + 代码变更）。
- G2 对话面板右上「弹出」按钮 → 复用 `Api.open_chat` 打开独立窗口；独立窗口保留，样式对齐 Trae。
- G3 跨进程通信：后端内存队列做枢纽。主窗口可下发 `set_session` / `clear` / `prefill` / `focus`；
  对话窗口轮询执行，并回传 `state`（当前会话、标题、是否生成中）。内嵌面板与独立窗口共享同一份对话。
- G4 「我的产物」重做：本地 / 云端 / 飞书 三个分页 + 分类筛选 chips + 卡片缩略图网格。
- G5 左侧导航细节对齐 Trae：顶部搜索框、新建任务、我的站点、分组折叠 + hover 更多操作。
- G6 不改 `chat.html` 的主对话、工具循环、侧边对话（#sideDock）逻辑；其它 pane 业务不变。

## 2. 非目标

- 不接入云端存储 / 飞书账号（无凭据），两个分页只做入口与空态引导，不伪造数据。
- 不做多对话窗口并存（同一时刻最多一个独立窗口 + 一个内嵌面板）。
- 不做 git diff（本机无 git 命令），「代码变更」用后端写操作快照 `/undo/list`。

## 3. 已核实事实

- `api_server.py:6026` `path = _urlparse(self.path).path`；GET 分支自 6476；POST 用 `parsed_path`，`/agent/tool_run` 在 9272。
- `/agent/outputs`（6715）返回 `{outputs:[{name,path,size_kb,mtime}]}`，目录 `AGENT_OUT_DIR`。
- `/undo/list`（6728）返回 `{entries:[{i,time,tool,path,snap,existed}]}`，最新在最前。
- `/agent/tool_run`（9272）收 `{name,args,mode}`，可执行 `shell_exec`。
- `chat.html`：`agentEmbed`(1389)、`sessionParam`(1414)、`promptParam/newChatParam`(1416-1417)、`sessionId`(1426)、
  `input`(1398)、`clearChat`(2417)、`switchToSession`(3663)、`sendMessage`(3187)。
- `agent/index.html`：`agentChatUrl`(902)、`openChatWindow`(916)、`openChat`(972)、`newTask`(995)、
  `loadOutputs`(1921)、侧栏调宽 IIFE(1969)、初始化(2018)。
- `agent_window.py` 的 `Api.open_chat` 已存在且单实例生效。

## 4. 方案

### 4.1 后端 IPC（`wasily/AI/api_server.py`）

**A. 模块级状态与函数**（插在 `_DISCUSSION_ACTIVE` 之后，约 1700 行）

```python
_IPC_LOCK = threading.RLock()
_IPC_LOG = []      # [{id, to, action, payload, ts}] 最近若干条命令
_IPC_CURSOR = {}   # client -> 已消费到的最大 id
_IPC_SEEN = {}     # client -> 最近轮询时间
_IPC_STATE = {}    # client -> 最近上报状态
_IPC_SEQ = {"n": 0}
_IPC_ONLINE_SEC = 30
_IPC_KEEP = 300
_IPC_GRACE = 5     # 新客户端只补最近 5 秒的命令

def _ipc_match(to, client)     # to=chat 覆盖所有 chat* 客户端
def _ipc_push(to, action, payload=None)   # 追加日志，返回命令 id
def _ipc_floor(now=None)       # 新客户端起始游标
def _ipc_take(client, limit=20)  # 按游标增量取命令，返回 (commands, peers)
```

**为什么用游标而不是「取走即删」**：内嵌面板与独立窗口可能同时在线，
队列式投递会让命令只落到先轮询的那一个，另一个永远收不到。
每个页面实例用带随机后缀的 client 名（`chat_embed_xxx` / `chat_xxx`），各自维护游标。

`peers` 只包含 30 秒内轮询过的客户端，值为 `{last, state}`；主窗口取 `chat*` 里 `last` 最新者作为当前对话。
新客户端起始游标只补最近 5 秒的命令，避免刷新页面就重放历史清空/预填。

**B. GET `/agent/ipc`**（插在 `/undo/list` 分支之后）

- `client = parse_qs(...).get("client", ["chat"])`。
- 返回 `{ok:true, client, commands:[...], peers:{...}}`。

**C. POST `/agent/ipc`**（插在 `/agent/tool_run` 之后）

- body `{client, to, action, payload}`。
- `action == "state"` → `_IPC_STATE[client] = payload`，返回 `{ok:true}`。
- 其它非空 action → `_ipc_push(to, action, payload)`，返回 `{ok:true, id}`。
- 缺 action → `{ok:false, error:"缺少 action"}`。

### 4.2 主窗口三栏（`wasily/AI/agent/index.html`）

**A. DOM**：`#pane-chat` 内 `.chat-pop/.cp-*` 全部删除，改为

```html
<div class="chat-cols">
  <div class="chat-col">
    <div class="chat-top">
      <div class="agent-id">…W / wasily 本地助手…</div>
      <div class="chat-title" id="chatTitle">新对话</div>
      <div class="chat-ops">
        <button class="btn ghost" onclick="popoutChat()">弹出</button>
        <button class="btn pri" onclick="newTask()">新对话</button>
      </div>
    </div>
    <iframe id="chatFrame" title="wasily 对话"></iframe>
  </div>
  <div class="wb-resizer" id="wbResizer" title="拖动调整工作台宽度"></div>
  <aside class="wb" id="wbPane">
    <div class="wb-tabs">
      <button class="on" data-wb="term" onclick="wbTab('term')">终端</button>
      <button data-wb="diff" onclick="wbTab('diff')">代码变更</button>
    </div>
    <div class="wb-body" id="wbTerm">
      <pre id="termOut"></pre>
      <div class="term-in"><span>PS&gt;</span><input id="termCmd" placeholder="PowerShell 命令，回车执行"></div>
    </div>
    <div class="wb-body" id="wbDiff" style="display:none"><div id="diffList"></div></div>
  </aside>
</div>
```

**B. CSS**：删 `.chat-pop/.cp-card/.cp-ico/.cp-t/.cp-d`；新增 `.chat-cols/.chat-col/#chatFrame/.chat-top/.chat-title/.chat-ops/.wb-resizer/.wb/.wb-tabs/.wb-body/#termOut/.term-in/#diffList`，
沿用现有深色（`#141513` / `#1b1c19` / `#34362f` / `#e6e8e1` / `#8bd5a8`）；`.task-rail` 保留。

**C. JS**

- `agentChatUrl()` 追加 `&ipc=1`；初始化 `document.getElementById('chatFrame').src = agentChatUrl();`
- 新增 `ipcSend(to, action, payload)`（POST `/agent/ipc`）、`ipcPump()`（`GET /agent/ipc?client=agent`，读取 peers 里 `chat` / `chat_embed` 的 state → 更新 `#chatTitle`、会话高亮），`setInterval(ipcPump, 2000)`。
- `openChat(prompt)`：`go('chat')` + `ipcSend('chat','prefill',{text:prompt})` + `ipcSend('chat','focus')`，不再开窗。
- `newTask()`：`go('chat')` + `ipcSend('chat','clear')` + `focus`。
- 新增 `popoutChat()`：`openChatWindow(currentSessionId)`，`openChatWindow` 保留。
- `chatAction('histBtn')`：拉 `/v1/agent/sessions?limit=20` 渲染下拉；点选 → `ipcSend('chat','set_session',{session_id})`。
- 工作台：`termRun()`（POST `/agent/tool_run` `{name:'shell_exec', args:{command}}`，输出追加到 `#termOut`）、`loadDiff()`（GET `/undo/list`，渲染条目 + 「撤销」POST `/undo/step`）、`wbTab(name)`、`#wbResizer` 拖拽调宽（localStorage `wasily.agent.wbWidth`）。
- `go('chat')` 时调用一次 `loadDiff()`。

### 4.3 对话页 IPC 客户端（`wasily/AI/chat.html`）

- 常量（`newChatParam` 之后）：`IPC_MODE`（`?ipc=1` 或 `agentEmbed`）、
  `IPC_CLIENT` = `chat_embed_<随机>`（iframe 内）或 `chat_<随机>`（独立窗口），每实例独立游标。
- 新增 `ipcApply(cmd)`：`set_session` → `switchToSession`；`clear` → `clearChat()`；`prefill` → 写入 `input.value` 并触发 `input` 事件 + 聚焦；`focus` → `input.focus()`。
- 新增 `ipcPoll()`（900ms）与 `ipcReport()`（3s，POST `action:'state'`，payload `{session_id, title, busy}`）。
- `sendMessage()` 结束、`switchToSession()` 后各调一次 `ipcReport()`。
- 独立窗口顶部身份条：`#header` 内新增 `<div class="id-chip" id="idChip">本地 · 免费</div>`，用 `/account/info` 填充；`agent-embed` 下已隐藏 header，不影响内嵌。

### 4.4 我的产物页（`agent/index.html`）

- DOM：`.ph` + `#outTabs`（本地/云端/飞书）+ `#outChips`（全部/文档/图片/视频/代码/其他）+ `#outGrid`。
- `loadOutputs()`：GET `/agent/outputs` → `OUT_ITEMS`；按扩展名归入 文档/图片/视频/代码/其他。
- 本地：卡片网格，图片用 `/agent/output?path=` 当缩略图，其余用图标；显示大小与时间 + 「打开」。
- 云端 / 飞书：空态卡片（说明未接入 + 配置指引），不伪造条目。

### 4.5 左侧导航（`agent/index.html`）

- `.side-search` + `#sideSearch` 置顶，`oninput="filterNav()"`：按文本过滤 `.ni` 与 `.ti`，无结果分组隐藏。
- 新增 `.ni`「我的站点」（`data-v="sites"`，`onclick="go('sites')"`）与 `#pane-sites`：列出 `/agent/outputs` 里的 `.html` 作为可预览站点（点开 `/agent/output?path=`），无则空态。
- 分组 hover 操作（`.acts`）沿用现状，不再改结构。

## 5. 影响面 / 风险

- 轮询 900ms / 2s / 3s，后端为多线程 HTTP 服务，开销可忽略。
- 内嵌面板与独立窗口同时打开时，同一条命令会在两边各执行一次；`set_session` / `clear` / `prefill` 均幂等，可接受。
- 命令日志保留最近 300 条，超出后丢弃最旧的；新页面只补最近 5 秒命令。
- `/undo/list` 只覆盖写操作快照，未走工具的文件改动不会出现在「代码变更」。
- 云端/飞书分页无真实数据源，明确展示空态，避免误导。

## 6. 验证

1. `python -c "import py_compile;py_compile.compile(r'wasily/AI/api_server.py',doraise=True)"`，重启后 `/health` 为 ok。
2. 提取两个 HTML 的内联脚本到 `%TEMP%` 后 `node --check`。
3. `POST /agent/ipc {"to":"chat","action":"prefill","payload":{"text":"测试"}}` → `GET /agent/ipc?client=chat` 取到该命令，再取为空。
4. `/agent`：三栏显示；iframe 输入框出现「测试」；「新对话」清空；终端跑 `Get-Date` 有输出；「代码变更」有条目。
5. 「弹出」出现独立窗口；主窗口点历史会话，内嵌与弹窗都切换。
6. 产物页三个分页可切换，本地有真实卡片。
7. 侧栏搜索「插」后只剩插件市场相关项。

---

## 附录：v1 摘要（已完成）

- `agent_window.py` 支持 `--chat` 模式（`IS_CHAT` / `CHAT_TITLE` / `CHAT_LOCK` / `_arg_value` / `Api.open_chat`）。
- 主窗口曾改为「对话在独立窗口中运行」占位卡片；v2 已恢复内嵌面板并保留弹出。
- `chat.html` 支持 `?prompt=` 预填与 `?new=1` 清空（v2 继续保留，作为 IPC 的降级通道）。
