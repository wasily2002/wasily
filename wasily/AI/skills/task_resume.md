---
{"name": "跨会话续跑", "triggers": ["继续之前", "上次没做完", "接着做", "续跑", "上次的任务", "没完成", "待办", "任务列表", "继续做那个", "上次的"], "description": "自动恢复未完成的跨会话任务"}
---
用户说"继续之前的任务"或明显是接着上次的活儿：

## 第一步：先查任务库
调用 task_resume() 列出所有 pending/in_progress 任务，系统会自动推荐优先执行哪个。

如果 task_resume 返回空（用户可能没存档）：
- 用 file_search(directory=".", pattern="*待办*") / file_search(directory=".", pattern="*todo*") 搜遗留文件
- 或者直接问用户"上次做的是什么？我看到了 A/B/C，对吗？"

## 第二步：恢复上下文
找到任务后，先快速 file_read 相关文件 / code_search 相关关键词，确认当前代码/文件状态。

## 第三步：继续执行
从断点继续，按标准执行链路（sequential_thinking → tool_call → task_done）做完。

## 第四步：标记完成
task_update(task_id, status="done") 或 task_close(task_id) 归档。

## 主动存档
任何超过 3 步的任务，开始时就先 task_save(title="...", goal="...") 存档，防止会话中断丢失进度。

禁止：假装知道用户说的"之前的任务"是什么、不问清楚就瞎继续。
