"""LSP 语义工具：查引用（重构前必看，避免漏改）。"""
import tools as _tools

SPEC = {
    "name": "ide_find_refs",
    "description": "查找符号的全部引用（LSP 语义级）：重构/改名/删函数前必须调用，避免漏改",
    "parameters": {"path": "文件路径(可选,默认当前文件)", "line": "行号(从1计)",
                   "column": "列号(从1计,可选)"},
}


def run(path: str = "", line: int = 0, column: int = 0) -> str:
    res = _tools._ide_bridge("/lsp_references",
                             {"path": path, "line": int(line or 0), "column": int(column or 0)})
    if not res.get("ok"):
        return "查引用失败: %s" % res.get("error")
    locs = res.get("locations") or []
    if not locs:
        return "语言服务未返回引用（可能没有语言服务器，或光标不在符号上）"
    out = ["引用 %d 处：" % len(locs)]
    for l in locs[:60]:
        out.append("  %s:%s:%s" % (l.get("path"), l.get("line"), l.get("column")))
    if len(locs) > 60:
        out.append("  …（还有 %d 处未列出）" % (len(locs) - 60))
    return "\n".join(out)
