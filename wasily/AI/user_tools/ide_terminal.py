# -*- coding: utf-8 -*-
"""IDE 桥工具：在 wasily IDE 的集成终端（wasily AI）中执行命令。
经扩展内嵌的 IDE 桥服务（127.0.0.1:18753）转发，IDE 未启动时给出明确提示。"""

import json
import urllib.request

SPEC = {
    "name": "ide_run_terminal",
    "description": "在 wasily IDE 集成终端执行命令（运行程序/装依赖/跑脚本）",
    "parameters": {
        "command": "要在 IDE 终端执行的完整命令行",
    },
}

_BRIDGE = "http://127.0.0.1:18753"


def run(command=""):
    command = (command or "").strip()
    if not command:
        return "错误：command 不能为空"
    req = urllib.request.Request(
        _BRIDGE + "/run_terminal",
        data=json.dumps({"command": command}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.loads(r.read().decode("utf-8"))
            if not d.get("ok"):
                return d.get("error") or json.dumps(d, ensure_ascii=False)
            out = str(d.get("output") or "").strip()
            code = d.get("exitCode")
            if out or code is not None:
                body = out if out else "（无输出）"
                if code is None:
                    return "[终端输出]\n" + body
                return "[终端输出]\n" + body + f"\n[exit code: {code}]"
            return d.get("detail") or json.dumps(d, ensure_ascii=False)
    except Exception as e:
        return f"IDE 桥未连接（wasily IDE 可能未启动）：{e}"
