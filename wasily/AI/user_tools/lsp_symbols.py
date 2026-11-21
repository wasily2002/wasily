"""LSP 语义工具：列符号（代替 grep 逐个文件找函数/类）。"""
import tools as _tools

SPEC = {
    "name": "ide_symbols",
    "description": "列出文件内或工作区中的符号（函数/类/变量），比 grep 更准（LSP 语义级）",
    "parameters": {"path": "文件路径(可选)", "query": "符号名关键词(可选,不传=列当前文件符号)"},
}


def run(path: str = "", query: str = "") -> str:
    res = _tools._ide_bridge("/lsp_symbols", {"path": path, "query": query})
    if not res.get("ok"):
        return "列符号失败: %s" % res.get("error")
    syms = res.get("symbols") or []
    if not syms:
        return "没有返回符号（该文件类型可能没有语言服务器）"
    out = ["符号 %d 个：" % len(syms)]
    for s in syms[:80]:
        out.append("  L%s %s%s" % (s.get("line"), (s.get("container") + ".") if s.get("container") else "",
                                   s.get("name")))
    return "\n".join(out)
