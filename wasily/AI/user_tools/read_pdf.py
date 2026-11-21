# -*- coding: utf-8 -*-
SPEC = {
    "name": "read_pdf",
    "description": "解析 PDF 文本（扫描件可 OCR）；pages=1-3/1,2,5/all，默认最多 20 页、OCR 3 页。",
    "parameters": {
        "path": "PDF 文件完整路径",
        "pages": "页码范围，如 1-3 / 1,2,5 / all（可选）",
        "ocr": "auto/on/off：文本层为空时是否 OCR（默认 auto）",
        "max_chars": "单次最大返回字符数（可选，默认 30000）",
    },
}


def run(path="", pages="", ocr="auto", max_chars=30000, **kwargs):
    import readers
    return readers.read_pdf(path, pages=pages, ocr=ocr, max_chars=max_chars)
