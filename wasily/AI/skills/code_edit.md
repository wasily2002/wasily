---
{"name": "代码编辑", "triggers": ["改代码", "加功能", "修bug", "修复错误", "重构", "优化代码", "修改函数", "加参数", "加路由", "补方法", "加个函数", "改一下", "fix", "debug", "调试", "排错"], "description": "定位→修改→验证完整闭环"}
---
改代码时，必须按 Trae 式闭环执行，绝不跳步：

## 标准闭环（4步，每步都是独立 tool_call）
1. **定位** → code_search(query=关键词) 或 file_read(path,start_line,end_line) 找到精确位置
2. **读上下文** → file_read 把要改的函数/类完整读出来，确保 old_string 精确唯一
3. **写代码** → file_write 或 apply_patch 写入完整新内容（包含 old_string 周围的上下文，确保唯一匹配）
4. **验证** → run_python(code="import py_compile; py_compile.compile('文件路径', doraise=True)") 编译检查无语法错误

## 修改自身源码的特殊约束
如果目标文件是 api_server.py / extension.js / self_mcp_server.py（wasily 自身）：
- 先走 self_symbols 精确定位符号
- 再走 self_read 读上下文
- self_edit 生成沙箱变更
- 最后 self_autofix 自动落盘+编译+重启
- 禁止直接 file_write 改自身源码（会破坏自我进化闭环）

## 批量修改
- 同一个模式改多处 → 先用 file_search 列出所有涉及文件，再逐个 apply_patch
- 改 import / 改函数签名 → 先 grep 搜所有调用点，确认不会遗漏

## 完成标记
所有修改落盘 + 编译通过 → task_done(summary="改了什么文件的哪些位置，验证通过")

禁止：瞎猜文件路径、old_string 太短导致多处匹配、跳过编译验证。
