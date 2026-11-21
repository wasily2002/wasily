# Spec：IDE 会话记忆与中断续跑

## 1. 背景与问题
wasily 的 IDE 对话面板（`ide_panel.html`，经扩展 `wasily-ai` 加载）存在两个体验缺口：

1. **重启后进度丢失**
   - 扩展把会话存在 `workspaceState`（`wasily-ai.sessions`），但只保存 `{role, content, display, ref}`，**不保存工具过程（steps）与耗时**，重置后只剩纯文本。
   - 本地服务 `api_server.py` 的 `sessions` 是**纯内存 dict**，服务进程一重启，模型完全忘掉上文，即使 IDE 里还留着聊天记录也答非所问。
2. **中断后无法续跑**
   - 扩展在用户点「停止」或请求异常时只弹一条「已停止生成」，不记录断点，也没有「继续」入口；刷新/重启后更看不到断点位置。
   - Trae 的行为是：中断处显示断点，并可一键从断点继续。

## 2. 目标（DoD）
- G1：本地服务重启后，`/v1/chat/completions` 对同一 `session_id` 仍能带上上次的消息上下文。
- G2：IDE 面板重启/刷新后，能恢复上次会话的**正文 + 工具过程步骤 + 耗时**。
- G3：生成被中断（用户点停止 / 服务重启）后，对话末尾出现「上次任务中断在这里」卡片，提供「继续」按钮；点击后把原文作为续跑指令发送，并清除断点状态。
- G4：不改变现有工具循环、流式渲染、任务编排逻辑；改动最小、可回滚。

## 3. 非目标
- 不做多会话与 `session_id` 的动态映射（IDE 仍固定 `vscodium` / `vscodium-solo`）。
- 不实现服务端真正的生成取消（用户点停止时服务端仍会跑完并落盘，属于既有行为）。
- 不改动加密历史库 `history_*` 的既有格式。

## 4. 方案

### 4.1 服务端：会话落盘 + 断点标记（`wasily/AI/api_server.py`）

**A. 在 `sessions = {}` / `sessions_lock`（约 L605-606）之后插入持久化模块：**

- 常量：
  - `SESS_DIR = os.path.join(BASE_DIR_LOG, "sessions_store")`，启动时 `os.makedirs(..., exist_ok=True)`。
  - `SESS_INFLIGHT = os.path.join(SESS_DIR, "_inflight.json")`。
- 函数：
  - `_sess_file(sid)`：把 sid 过滤为 `[A-Za-z0-9_.-]`（非法字符替换为 `_`，最长 60）后返回 `<SESS_DIR>/<safe>.json`。
  - `_sess_snapshot(sid)`：加 `sessions_lock` 取 `sessions[sid]` 中 `role != "system"` 的消息副本。
  - `sess_save(sid, interrupted=None)`：读取旧文件，写入 `{sid, updated: time.time(), messages: snapshot, interrupted?}`；`interrupted` 为 `None` 时保留旧值（用于续跑后不清断点）；原子写（`.tmp` + `os.replace`）。
  - `sess_clear_interrupted(sid)`：读取文件、`pop("interrupted")` 后原位写回；文件不存在直接返回。
  - `_sess_mark_inflight(sid, text)`：写 `_inflight.json = {sid, text, ts}`。
  - `_sess_clear_inflight()`：删除 `_inflight.json`（忽略异常）。
  - `_sess_restore_all()`：导入时执行。遍历 `SESS_DIR/*.json`（跳过 `_` 开头），把 `messages` 装入
    `sessions[sid] = [{"role":"system","content": BASE_PROMPT}] + messages`（不调用耗时较重的 `build_system_prompt`）。
    若 `_inflight.json` 中的 sid 命中某会话，则把该文件补写 `interrupted: {text, ts}`。
    最后删除 `_inflight.json`，并 `log` 一条恢复信息。
- 在函数定义后立即调用 `_sess_restore_all()`。

**B. 聊天主流程（`do_POST` 的 `/v1/chat/completions`，约 L3334-3536）：**
- 会话不存在时，除新建外**优先从磁盘恢复**：若 `_sess_file(session_id)` 存在则读入 messages（同上结构），否则才 `build_system_prompt()` 新建。
- 追加 user 消息后调用 `sess_clear_interrupted(session_id)`（新的一轮开始视为已续跑）。
- 调 `generate_with_tools` 之前调用 `_sess_mark_inflight(session_id, 最后一条用户原文)`。
- `generate_with_tools` 返回后（无论异常分支还是正常分支，且在 `history_append` 之后、SSE `done` 下发之前）：
  `_sess_clear_inflight(); sess_save(session_id)`。
  > 注意：异常路径 `response_text` 为「生成出错: ...」不落会话，但用户消息已在，故仍落盘以保证上下文连续。
- 非流式返回前同样已落盘。

**C. 新增只读接口（`do_GET`，紧随 `/history/list` 分支之后）：**
- `GET /session/state?sid=<sid>`：
  - 有 `sid` → 读 `_sess_file(sid)`；不存在返回 `{sid, messages: [], interrupted: null}`。
  - 无 `sid` → 扫描 `SESS_DIR`，返回 `updated` 最大的会话。
  - 响应字段固定：`{sid, updated, messages, interrupted}`。
  - 用途：面板直连模式（浏览器打开 `/panel_ui`）刷新后恢复对话；扩展模式作为兜底。

### 4.2 扩展：持久化过程详情 + 记录断点（`wasily/tools/vscodium-ext/wasily-ai/extension.js`）

会话消息结构扩展为：
```js
sess.messages = [
  { role:'user', content, display, ref },
  { role:'assistant', content, steps:[...], ms:Number }
]
sess.interrupted = { text:String, ts:Number }   // 可选，存在即表示有断点
```

改动点：
1. `sendPrompt(text, opts)`：
   - 取得会话后 `delete sess.interrupted`（开新轮即视为续跑），`saveState`。
   - 成功分支：
     `const steps = (data && data.steps) || []; const ms = Math.round(((data && data.elapsed) || 0) * 1000);`
     `sess.messages.push({ role:'assistant', content: reply, steps, ms });`
     再 `saveState`；`post({type:'append', ..., steps})`（保持原样）。
   - `catch` 的 AbortError 分支：在 `post notify` 之前写入
     `sess.interrupted = { text: String(text || ''), ts: Date.now() }`，`saveState`，
     `post({ type:'interrupted', sid: sess.id, interrupted: sess.interrupted })`。
   - 其他异常分支（服务离线等）也写入 `interrupted`，原因文案不同，便于重启后继续。
   - `finally` 中保持原有 `generating:false`。
2. `pushRender()`：`post({ type:'renderMessages', sid, messages, interrupted: sess ? (sess.interrupted || null) : null })`。
3. `onMessage` 新增 `case 'dismissInterrupted'`：清除当前活动会话的 `interrupted`，`saveState` + `pushRender()`。
4. `renderAll` 相关：扩展只负责传数据，面板负责渲染（见 4.3）。

### 4.3 面板：恢复过程详情 + 断点卡片（`wasily/AI/ide_panel.html`）

1. `renderAll(sid, messages, interrupted)`：
   - 历史 assistant 消息用 `addAssistant(m.content, m.ms, m.steps)` 渲染（当前是 `addAssistant(m.content)`，会丢过程与耗时）。
   - 循环结束后若 `interrupted` 存在 → 调 `addInterruptCard(interrupted)`。
2. `renderMessages` 分支改为 `renderAll(msg.sid, msg.messages, msg.interrupted)`。
3. 新增 `case 'interrupted'`：`if (msg.sid && currentSid && msg.sid !== currentSid) break;` 然后 `addInterruptCard(msg.interrupted)`。
4. 新增 `addInterruptCard(interrupted)`：
   - 结构：琥珀色左边框卡片，标题「上次任务中断在这里」，正文为中断时用户原文（超 120 字截断加 `…`），时间显示 `HH:MM`。
   - 按钮「继续」：取走卡片 → `inputEl.value = '继续上次未完成的任务：' + 原文` → 调 `send()`。
   - 按钮「忽略」：移除卡片 → `post({ type:'dismissInterrupted' })`。
   - 卡片带 `data-interrupt="1"`，`send()` 开头先移除所有卡片，避免续跑后残留。
5. 直连模式（`!HAS_API`）初始化：脚本末尾（`post({type:'ready'})` 附近）加一次
   `api('/session/state?sid=vscodium', 'GET', null, 8000)`，若返回 `messages.length` 则
   `renderAll('vscodium', data.messages, data.interrupted)`；为空则保持空态。
   （扩展模式下仍以扩展 `pushRender` 为准，不重复拉取，避免覆盖本地多会话。）

## 5. 涉及文件
- `d:\煮饭仔点餐\项目文件\项目介绍\wasily\AI\api_server.py`
- `d:\煮饭仔点餐\项目文件\项目介绍\wasily\tools\vscodium-ext\wasily-ai\extension.js`
- `d:\煮饭仔点餐\项目文件\项目介绍\wasily\AI\ide_panel.html`
- 运行副本（必须同步）：`d:\煮饭仔点餐\项目文件\项目介绍\wasily\tools\wasily-ide\resources\app\extensions\wasily-ai\extension.js`

## 6. 验证
1. `python -m py_compile wasily/AI/api_server.py` 通过。
2. `node --check wasily/tools/vscodium-ext/wasily-ai/extension.js` 通过。
3. 重启服务后 `GET /health` 返回 ok；`GET /session/state?sid=vscodium` 能返回上次消息。
4. 浏览器打开 `http://127.0.0.1:8000/panel_ui`：
   - 发一条消息 → 刷新页面 → 对话与「已读取 N 个文件 / 思考步骤」仍在。
   - 生成中刷新后重进 → 出现「上次任务中断在这里」卡片；点「继续」把原文发送并清除卡片。
5. 重启服务（不刷新面板）→ 再发消息，模型能引用上一轮内容（不重复提问）。

## 7. 风险与回滚
- 会话文件写在 `AI/sessions_store/`，可随时删除；删除后等同首次使用。
- `_sess_restore_all` 使用轻量占位 system 消息，不改变每轮 `build_system_prompt` 的重建逻辑。
- 回滚：移除新增函数/接口与扩展字段即可；旧 `workspaceState` 数据仍兼容（缺少 `steps/ms` 时按纯文本渲染）。

## 8. 需要确认
- 同意按上述 3 个文件（+1 个运行副本）实施。
