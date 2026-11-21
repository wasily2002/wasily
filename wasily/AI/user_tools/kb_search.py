# -*- coding: utf-8 -*-
"""用户工具：在本地知识库中做跨类型混合检索（文本 / PDF / Office / 图片）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SPEC = {
    "name": "kb_search",
    "description": "在本地全盘知识库中检索文件内容与图片内容，返回匹配片段和源文件路径。"
                   "适合回答“我电脑里关于X的资料”“找包含某句话的文件”“找某类图片”这类问题。",
    "parameters": {
        "query": "检索关键词或自然语言问题",
        "kind": "可选：text/pdf/docx/xlsx/pptx/image，留空为全部类型",
        "limit": "可选：返回文件数，默认 6",
    },
}


def run(query="", kind="", limit=6):
    import kb_search
    q = str(query or "").strip()
    if not q:
        return "请提供要检索的关键词。"
    res = kb_search.search(q, kind=(kind or None), limit=int(limit or 6))
    if not res.get("ok"):
        return "检索失败：%s" % res.get("error")
    if not res.get("results"):
        return ("知识库中没有找到与「%s」匹配的内容。"
                "如果尚未建立索引，可让用户先运行全盘索引。" % q)
    lines = ["本地知识库检索「%s」命中 %d 个文件：" % (q, res["count"])]
    for i, r in enumerate(res["results"], 1):
        lines.append("%d. [%s] %s" % (i, r["kind"], r["path"]))
        if r.get("caption"):
            lines.append("   图片描述：%s" % r["caption"][:160])
        for s in r.get("snippets", [])[:2]:
            lines.append("   片段：%s" % s["text"][:220].replace("\n", " "))
    lines.append("（引用时请标注上面的源文件路径）")
    return "\n".join(lines)
