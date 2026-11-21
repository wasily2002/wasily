- [x] T1 服务端会话持久化（api_server.py）
  - 在 `sessions = {}` / `sessions_lock` 之后新增 `SESS_DIR`、`_sess_file`、`_sess_snapshot`、`sess_save`、`sess_clear_interrupted`、`_sess_mark_inflight`、`_sess_clear_inflight`、`_sess_restore_all`，并调用 `_sess_restore_all()`。
  - 聊天入口：会话缺失时优先从磁盘恢复；追加 user 后清断点；生成前写 inflight；生成结束（异常/正常）删除 inflight 并 `sess_save`。
  - 新增 `GET /session/state`（支持 `?sid=`，无 sid 返回最近会话）。

- [x] T2 扩展持久化过程与断点（extension.js）
  - assistant 消息写入 `steps` 与 `ms`（取 done 事件 `steps` / `elapsed`）。
  - `sess.interrupted` 在 AbortError/请求异常时写入，成功时清除；`pushRender` 附带 `interrupted`。
  - 新增 `case 'dismissInterrupted'`。
  - `node --check` 通过后复制到 `wasily-ide/resources/app/extensions/wasily-ai/extension.js`。

- [x] T3 面板恢复与续跑（ide_panel.html）
  - `renderAll(sid, messages, interrupted)`：历史消息传 `ms/steps`；末尾渲染断点卡片。
  - 处理 `renderMessages` 的 `interrupted` 与新增 `case 'interrupted'`。
  - 新增 `addInterruptCard`（继续 / 忽略）；`send()` 先清理旧卡片。
  - 直连模式初始化时从 `/session/state?sid=vscodium` 恢复。

- [x] T4 验证
  - `python -m py_compile wasily/AI/api_server.py`；`node --check ...extension.js`。
  - 重启服务，`/health` 正常；`/session/state` 有数据。
  - 面板刷新/重启恢复对话与过程；中断后出现卡片且「继续」可用。
  - 服务重启后再对话能带上下文。
