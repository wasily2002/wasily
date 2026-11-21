# -*- coding: utf-8 -*-
"""OfficeCLI 工具：让 Agent 直接读写 docx/xlsx/pptx。

后端进程启动早于 OfficeCLI 安装，PATH 里可能没有它，
所以这里显式使用安装目录的绝对路径，避免依赖环境变量。
"""
import os
import shlex
import subprocess

_EXE = os.path.join(os.environ.get("LOCALAPPDATA", ""), "OfficeCLI", "officecli.exe")

SPEC = {
    "name": "officecli",
    "description": "用 OfficeCLI 打开/读取/编辑 docx、xlsx、pptx，例如 view 读取内容、set 改单元格、add 加段落，command 直接传 CLI 参数。",
    "parameters": {
        "command": "CLI 参数，如 'view 报告.docx outline' 或 'set 数据.xlsx /Sheet1/A1 --prop value=姓名'",
        "cwd": "工作目录（可选，默认 wasily/AI 目录）",
        "timeout": "超时秒数（可选，默认 180）",
    },
}


def run(command="", cwd="", timeout=180, **kwargs):
    exe = _EXE if os.path.exists(_EXE) else "officecli"
    # posix=False 保留反斜杠（Windows 路径），但引号也会留在 token 里，需手动去掉
    args = [t[1:-1] if len(t) > 1 and t[0] == t[-1] and t[0] in "\"'" else t
            for t in shlex.split(str(command or ""), posix=False)]
    if not args:
        args = ["--version"]
    workdir = cwd or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        p = subprocess.run(
            [exe] + args,
            cwd=workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(5, int(timeout or 180)),
        )
    except FileNotFoundError:
        return "OfficeCLI 未找到，请先运行：irm https://d.officecli.ai/install.ps1 | iex"
    except subprocess.TimeoutExpired:
        return f"OfficeCLI 执行超时（{timeout}s）：{command}"
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    body = out + (("\n[stderr]\n" + err) if err else "")
    if len(body) > 20000:
        body = body[:20000] + "\n...（输出过长已截断）"
    return body or f"OfficeCLI 无输出（exit={p.returncode}）"
