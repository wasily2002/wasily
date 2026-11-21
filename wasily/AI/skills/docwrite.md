---
{"name": "文档编写", "triggers": ["写个PPT", "做PPT", "做个PPT", "做幻灯片", "写幻灯片", "写文档", "写word", "写个word", "做word", "markdown文档", "写个md", "生成文档", "写报告文档"], "description": "原生编写 Word/PPT/Markdown 文档，结构化自动排版，与 Office/WPS 完全兼容"}
---
用户要 PPT / Word / Markdown 文档时，必须调用 doc_write 工具一次成型，禁止只给文字大纲。

## 调用方式
doc_write(path, kind, blocks, title)
- path: 输出路径（docx/pptx 放 agent/outputs 目录，如 D:\\煮饭仔点餐\\项目文件\\项目介绍\\wasily\\AI\\agent\\outputs\\xxx.docx）
- kind: word / ppt / markdown
- title: 文档总标题（Word 作为封面标题；PPT 自动生成标题页）
- blocks: JSON 数组字符串，内容块按顺序排版

## blocks 内容块类型
- {"t":"h1","text":"大标题"} / {"t":"h2","text":"小节"} / {"t":"h3","text":"子节"}
- {"t":"p","text":"正文段落"}
- {"t":"bullets","items":["要点1","要点2"]}
- {"t":"table","rows":[["表头1","表头2"],["数据1","数据2"]]}
- {"t":"img","path":"本地图片绝对路径","caption":"图注(可选)"}
- {"t":"quote","text":"引用"} / {"t":"code","lang":"python","text":"代码"}

## 分步使用引导
1. 先构思大纲：标题 → 各章节 → 每章要点/数据/配图。
2. 把大纲翻译成 blocks JSON，一次性调用 doc_write。
3. PPT 规则：每个 h1/h2 自动开一页幻灯片，其后的 p/bullets 归入该页；table/img 独立成页。
4. 生成后用 process_start 执行 `explorer "文件路径"` 打开给用户验收。

## 参数调优指南
- 单页 PPT 要点控制在 5 条以内，每条 ≤ 20 字，避免溢出。
- Word 长文档多用 h2/h3 分层，表格列数 ≤ 6。
- 插图必须给本地已存在的绝对路径，不存在的图片块会被跳过。
