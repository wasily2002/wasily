# Checklist：IDE 会话记忆与中断续跑

- [x] `python -m py_compile wasily/AI/api_server.py` 通过（输出 PY_OK）。
- [x] `node --check wasily/tools/vscodium-ext/wasily-ai/extension.js` 通过（输出 JS_OK）。
- [x] `ide_panel.html` 内联脚本语法检查通过（HTML_JS_OK blocks=1）。
- [x] 扩展源码已同步到 `wasily-ide/resources/app/extensions/wasily-ai/extension.js`。
- [x] 服务经 watchdog 重启后 `GET /health` 返回 ok。
- [x] 轻量对话后 `sessions_store/<sid>.json` 落盘，`_inflight.json` 已清理。
- [x] `GET /session/state?sid=persist-test` 返回 2 条消息、最后一条为 assistant。
- [x] 写入模拟 inflight 并重启服务后，同一会话恢复正常，且 `interrupted.text=simulated interrupt`。
- [x] 浏览器打开 `http://127.0.0.1:8000/panel_ui`：输入框/发送按钮可见，控制台无 JS 错误。
- [x] 测试用会话文件 `persist-test.json` 已清理，未污染用户会话。
