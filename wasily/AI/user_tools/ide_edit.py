# -*- coding: utf-8 -*-
"""IDE 桥工具：直接编辑 wasily IDE 中的文件并保存。
old_string 非空时替换首个匹配片段，为空时整文件替换。"""

import json
import urllib.request

SPEC = {
    "name": "ide_edit_file",
    "description": "编辑 wasily IDE 文件并保存：old_string 精确替换首个匹配，留空=整文件替换",
    "parameters": {
        "path": "文件绝对路径",
        "old_string": "待替换的原文（精确匹配首个出现位置）；留空表示整文件替换",
        "new_string": "替换后的新内容",
    },
}

_BRIDGE = "http://127.0.0.1:18753"


def run(path="", old_string="", new_string=""):
    path = (path or "").strip()
    if not path:
        return "错误：path 不能为空"
    req = urllib.request.Request(
        _BRIDGE + "/edit_file",
        data=json.dumps({
            "path": path,
            "old_string": old_string or "",
            "new_string": new_string or "",
        }).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read().decode("utf-8"))
            return d.get("detail") or d.get("error") or json.dumps(d, ensure_ascii=False)
    except Exception as e:
        return f"IDE 桥未连接（wasily IDE 可能未启动）：{e}"
