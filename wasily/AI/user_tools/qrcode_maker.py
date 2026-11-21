SPEC = {"name": "qrcode_maker", "description": "生成二维码并打开", "parameters": {"text": "文本或链接"}}
def run(text=""):
    import os, urllib.parse
    if not text:
        return "请提供 text 参数"
    url = "https://api.qrserver.com/v1/create-qr-code/?size=300x300&data=" + urllib.parse.quote(text)
    os.startfile(url)
    return f"二维码已在浏览器打开: {url}"
