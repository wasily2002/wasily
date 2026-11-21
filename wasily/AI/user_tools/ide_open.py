# -*- coding: utf-8 -*-
"""IDE 桥工具：在 wasily IDE 编辑器中打开指定文件（可选定位到某一行）。"""

import json
import urllib.request

SPEC = {
    "name": "ide_open_file",
    "description": "在 wasily IDE 中央编辑器中打开指定文件，可选跳转到指定行号",
    "parameters": {
        "path": "文件绝对路径",
        "line": "可选，跳转到的行号（从 1 开始）",
    },
}

_BRIDGE = "http://127.0.0.1:18753"


def run(path="", line=0):
    path = (path or "").strip()
    if not path:
        return "错误：path 不能为空"
    body = {"path": path}
    try:
        ln = int(line)
        if ln > 0:
            body["line"] = ln
    except (TypeError, ValueError):
        pass
    req = urllib.request.Request(
        _BRIDGE + "/open_file",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            d = json.loads(r.read().decode("utf-8"))
            return d.get("detail") or d.get("error") or json.dumps(d, ensure_ascii=False)
    except Exception as e:
        return f"IDE 桥未连接（wasily IDE 可能未启动）：{e}"
