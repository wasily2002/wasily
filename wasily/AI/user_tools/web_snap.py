SPEC = {"name": "web_snap", "description": "保存网页到本地并打开", "parameters": {"url": "网址"}}
def run(url=""):
    import urllib.request, time
    if not url: return "请提供 url 参数"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        html = r.read()
    dst = os.path.join(os.path.expanduser("~"), "Desktop", f"snap_{int(time.time())}.html")
    with open(dst, "wb") as f: f.write(html)
    os.startfile(dst)
    return f"快照已保存并打开: {dst}"
import os
