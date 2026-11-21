# -*- coding: utf-8 -*-
SPEC = {
    "name": "read_image",
    "description": "读取图片(png/jpg/webp/bmp)：返回尺寸/格式+OCR文字；mode=describe 才调本地视觉模型，"
                   "auto 仅 OCR，不得编造画面语义。",
    "parameters": {
        "path": "图片完整路径",
        "mode": "auto/describe/ocr（默认 auto；describing 才调用视觉模型）",
        "max_chars": "OCR 文本最大字符数（可选，默认 20000）",
    },
}


def run(path="", mode="auto", max_chars=20000, **kwargs):
    import readers
    return readers.read_image(path, mode=mode, max_chars=max_chars)
