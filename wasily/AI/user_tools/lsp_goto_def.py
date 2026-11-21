"""LSP 语义工具：跳到定义（走 VSCode executeDefinitionProvider，不靠正则猜）。"""
import json

import tools as _tools

SPEC = {
    "name": "ide_goto_def",
    "description": "跳到符号定义（LSP 语义级）：给定文件与行列或符号名，返回定义所在文件/行",
    "parameters": {"path": "文件路径(可选,默认当前文件)", "line": "行号(从1计,可选)",
                   "column": "列号(从1计,可选)", "symbol": "符号名(可选,用工作区符号搜索)"},
}


def run(path: str = "", line: int = 0, column: int = 0, symbol: str = "") -> str:
    if symbol:
        res = _tools._ide_bridge("/lsp_symbols", {"query": symbol, "path": path})
        if not res.get("ok"):
            return "符号搜索失败: %s" % res.get("error")
        syms = res.get("symbols") or []
        if not syms:
            return "未找到符号: %s" % symbol
        out = ["符号 %s 命中 %d 处：" % (symbol, len(syms))]
        for s in syms[:15]:
            out.append("  L%s %s%s" % (s.get("line"), s.get("container") or "", s.get("name")))
        return "\n".join(out)
    res = _tools._ide_bridge("/lsp_definition",
                             {"path": path, "line": int(line or 0), "column": int(column or 0)})
    if not res.get("ok"):
        return "跳转失败: %s" % res.get("error")
    locs = res.get("locations") or []
    if not locs:
        return "语言服务未返回定义（该文件类型可能没有语言服务器，或光标不在符号上）"
    head = locs[0]
    _tools._ide_bridge("/open_file", {"path": head.get("path"), "line": head.get("line") or 0})
    body = ["定义 %d 处，已打开第一处：" % len(locs)]
    for l in locs[:10]:
        body.append("  %s:%s:%s" % (l.get("path"), l.get("line"), l.get("column")))
    return "\n".join(body)
