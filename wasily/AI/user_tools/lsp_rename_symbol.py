"""LSP 语义工具：跨文件安全重命名（真正的语义重构，不是文本替换）。"""
import tools as _tools

SPEC = {
    "name": "ide_rename_symbol",
    "description": "语义级重命名（LSP rename）：把符号在全部文件里准确改名，保留撤销栈；比文本替换安全",
    "parameters": {"path": "文件路径(可选,默认当前文件)", "line": "行号(从1计)",
                   "column": "列号(从1计,可选)", "new_name": "新名称"},
}


def run(path: str = "", line: int = 0, column: int = 0, new_name: str = "") -> str:
    if not str(new_name or "").strip():
        return "new_name 不能为空"
    res = _tools._ide_bridge("/lsp_rename",
                             {"path": path, "line": int(line or 0),
                              "column": int(column or 0), "new_name": new_name})
    if not res.get("ok"):
        return "重命名失败: %s" % res.get("error")
    files = res.get("files") or []
    return "已重命名为 %s，影响 %d 个文件：\n%s" % (
        new_name, len(files), "\n".join("  " + f for f in files[:20]) or "（编辑器未回报文件列表）")
