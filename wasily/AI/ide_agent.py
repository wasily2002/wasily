# -*- coding: utf-8 -*-
"""IDE 编程智能体规范：模式归一化 + 执行提示词。

与通用 Agent 共用工具循环，但 IDE 场景强调三件事：
1. 读编辑器缓冲区而不是只读磁盘；
2. 编辑走 WorkspaceEdit，保留撤销栈并立即保存；
3. 改完必须看诊断、跑验证，不能只输出代码块。
"""
import re

import config as cfg

VALID_MODES = ("ask", "plan", "solo")

# 动作型意图：出现这些动词说明本轮要「动手」，Ask/Plan 下需要自动升级到 Agent。
_ACTION_HINT_RE = re.compile(
    r"创建|新建|制作|生成|写入|写一|写个|保存|修改|改成|改为|替换|更新|修复|重构|实现|开发|"
    r"添加|增加|插入|删除|删掉|移除|重命名|改名|移动|复制|安装|卸载|部署|执行|运行|跑一下|"
    r"启动|重启|停止|截图|点击|输入|打开|下载|上传|编译|构建|测试|格式化|整理|清理|转换|合并|拆分")
# 纯疑问句：即使含动作词（如「怎么创建虚拟环境」）也应保持只读问答
_QUESTION_HEAD_RE = re.compile(
    r"^(怎么|如何|为什么|为何|什么是|是什么|能否解释|解释一下|介绍一下|可以吗|是否|有没有|"
    r"哪个|哪种|区别|原理|教程|示例|举例)")
_QUESTION_TAIL_RE = re.compile(r"(吗|呢|怎么办|是什么|什么意思|有什么区别|\?|？)\s*$")
_IMPERATIVE_RE = re.compile(r"帮我|给我|请|麻烦|替我把|替我|把")

IDE_AGENT_PROMPT = """

## IDE 编程智能体执行规范（必须遵守）
你现在在用户的 IDE 内工作，目标是「把代码改对并验证」，不是给建议。

1. 先定位再读：用 code_search / file_search / file_list 找位置，不要猜路径。
2. 读当前编辑器文件用 ide_read_file（能读到未保存的缓冲区）；读其他文件用 file_read，
   大文件必须用 offset/limit 分段读，禁止一次把整库或整文件塞进上下文。
3. 修改代码优先用 ide_edit_file（走编辑器 WorkspaceEdit，可撤销）；批量或多处改动用 apply_patch；
   不要输出「请把下面代码粘贴进去」这类人工步骤。
4. 每次改完立即调用 ide_diagnostics 查看当前文件诊断；有错误就继续修，直到没有错误。
5. 声称完成前必须验证：用 ide_run_terminal 执行项目已有的测试、构建或启动命令；
   命令失败要读输出并修复，不能把失败说成成功。
6. 需要操作桌面时用 screenshot / mouse_click / key_type / window_activate / open_app 等工具，
   操作前先截图确认目标位置；需要读网页时用 mcp__browser__browser_open + browser_snapshot，
   不要凭空编造页面内容。
7. 最终回复只写：改了哪些文件、关键改动、验证命令与结果；不要贴大段完整代码。
"""


def normalize_mode(client, mode):
    """把前端模式统一成 ask / plan / solo。

    IDE 客户端传 'ide'、'agent' 或空值时按 Agent 处理（可写文件、可执行命令），
    显式传 'ask'/'chat' 才保持只读，避免 Trae 的 Chat/Agent 语义被混淆。
    """
    raw = str(mode or "").strip().lower()
    is_ide = str(client or "").strip().lower() == "ide"
    if raw in ("chat", "ask"):
        return "ask"
    if raw == "plan":
        return "plan"
    if raw in ("solo", "agent"):
        return "solo"
    if is_ide and raw in ("", "ide"):
        return "solo"
    return None


def ide_prompt_enabled(client, mode):
    return str(client or "").strip().lower() == "ide" and mode == "solo"


def looks_like_action(text):
    """这句话是否要求「动手执行」，而不是只问原理 / 要解释。"""
    q = str(text or "").strip()
    if not q:
        return False
    if _QUESTION_HEAD_RE.search(q):
        return False
    if _QUESTION_TAIL_RE.search(q) and not _IMPERATIVE_RE.search(q):
        return False
    return bool(_ACTION_HINT_RE.search(q))


def validate_mode(mode, fallback="solo"):
    """把任意输入收敛成合法模式；非法值直接回退，避免出现「无有效工具可用」的空配置。"""
    m = str(mode or "").strip().lower()
    if m in ("agent", "ide"):
        return "solo"
    if m in VALID_MODES:
        return m
    return fallback


def resolve_mode(client, requested, text, current=None):
    """解析本轮最终模式，并在 IDE 下把「Ask/Plan + 动作意图」自动升级为 solo。

    返回 (mode, escalated)：
      mode      最终模式（ask / plan / solo）
      escalated 是否发生自动升级（前端据此提示、日志据此记录）
    """
    is_ide = str(client or "").strip().lower() == "ide"
    base = normalize_mode(client, requested) or validate_mode(current or "ask")
    if is_ide and base in ("ask", "plan") and looks_like_action(text):
        return "solo", True
    return base, False


def max_rounds_for(client, mode, requested):
    """IDE Agent 至少给 12 轮，避免读文件+改+验证被 8 轮截断。"""
    try:
        rounds = int(requested or 0)
    except (TypeError, ValueError):
        rounds = 0
    if rounds <= 0:
        return 0
    if str(client or "").strip().lower() == "ide" and mode == "solo":
        return max(rounds, 12)
    return rounds
