---
{"name": "Python开发", "triggers": ["写个脚本", "python", "跑个脚本", "数据分析", "统计一下", "批量处理", "自动化", "写个程序", "计算一下", "处理数据", "csv", "excel", "数据", "统计", "分析", "脚本"], "description": "编写并执行 Python 脚本完成数据处理/自动化任务"}
---
用户要写脚本/做数据处理时：

## 执行模式
1. 先用 sequential_thinking 想清楚：输入是什么、要做什么计算、输出到哪里
2. 用 file_write 写出完整 Python 脚本（放到项目下合适的 .tmp/ 或 scripts/ 目录）
3. run_code 执行（不要手动 subprocess，run_code 会直接返回 stdout/stderr）
4. 拿到输出后给用户总结结果

## 数据处理常用库
- CSV → import csv 或 import pandas
- Excel → import openpyxl / pandas.read_excel
- JSON → import json
- 数据统计 → from collections import Counter, defaultdict / pandas
- 爬虫 → import requests + BeautifulSoup
- 图表 → matplotlib（但优先用文本表格输出，简单高效）

## 脚本规范
- 文件开头写清楚脚本用途（注释）
- import 放在最前面
- 关键步骤 print 进度，方便用户看执行状态
- 处理完 print 最终结果摘要，不要只 silence

## 完成标记
脚本写好 + run_code 返回有效结果 → task_done(summary="脚本路径 + 核心结果")

禁止：只写脚本不执行、脚本里有 bug 就交付、让用户自己跑。
