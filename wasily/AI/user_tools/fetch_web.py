# -*- coding: utf-8 -*-
SPEC = {
    "name": "fetch_web",
    "description": "抓网页标题/正文/链接（不执行 JS，前端渲染页可能取不到）；PDF 链接转 read_pdf。",
    "parameters": {
        "url": "完整 http/https 网址",
        "max_chars": "正文最大字符数（可选，默认 20000）",
        "include_links": "是否返回页面链接 true/false（可选，默认 true）",
    },
}


def run(url="", max_chars=20000, include_links=True, **kwargs):
    import readers
    return readers.fetch_web(url, max_chars=max_chars, include_links=include_links)
