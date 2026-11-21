# -*- coding: utf-8 -*-
SPEC = {
    "name": "read_office",
    "description": "读取 Office：docx / xlsx(逐表) / pptx(逐页)；旧版 doc/xls/ppt 需先另存。",
    "parameters": {
        "path": "文档完整路径",
        "sheet": "仅 xlsx：指定工作表名（可选）",
        "slide": "仅 pptx：指定页码（可选）",
        "max_chars": "单次最大返回字符数（可选，默认 30000）",
    },
}


def run(path="", sheet="", slide="", max_chars=30000, **kwargs):
    import readers
    return readers.read_office(path, sheet=sheet, slide=slide, max_chars=max_chars)
