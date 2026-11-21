# -*- coding: utf-8 -*-
"""插件市场：内置可安装插件目录（写入 user_tools/ 后热加载即生效）"""
import json
import os
from pathlib import Path

_DIR = Path(__file__).resolve().parent
USER_TOOLS_DIR = _DIR / "user_tools"

_T = '''# -*- coding: utf-8 -*-
{src}
'''

CATALOG = {
    "weather": {
        "file": "weather.py", "name": "天气查询", "icon": "🌤️", "cat": "网络",
        "desc": "查询任意城市实时天气与今日预报（wttr.in 数据源）",
        "src": '''SPEC = {"name": "weather", "description": "查询城市天气", "parameters": {"city": "城市名"}}
def run(city="深圳"):
    import urllib.request
    url = f"https://wttr.in/{city}?format=j1"
    with urllib.request.urlopen(url, timeout=15) as r:
        d = json.loads(r.read().decode("utf-8"))
    cur = d["current_condition"][0]
    return f"{city}: {cur['lang_zh'][0]['value']}, {cur['temp_C']}°C, 体感{cur['FeelsLikeC']}°C, 湿度{cur['humidity']}%"
import json
'''},
    "qrcode_maker": {
        "file": "qrcode_maker.py", "name": "二维码生成", "icon": "🔳", "cat": "效率",
        "desc": "把文本/链接生成二维码图片并在浏览器中打开",
        "src": '''SPEC = {"name": "qrcode_maker", "description": "生成二维码并打开", "parameters": {"text": "文本或链接"}}
def run(text=""):
    import os, urllib.parse
    if not text:
        return "请提供 text 参数"
    url = "https://api.qrserver.com/v1/create-qr-code/?size=300x300&data=" + urllib.parse.quote(text)
    os.startfile(url)
    return f"二维码已在浏览器打开: {url}"
'''},
    "todo_list": {
        "file": "todo_list.py", "name": "待办清单", "icon": "✅", "cat": "效率",
        "desc": "本地待办事项管理：添加/列出/完成/删除，持久化保存",
        "src": '''SPEC = {"name": "todo_list", "description": "待办事项管理", "parameters": {"action": "add/list/done/del", "text": "事项内容或编号"}}
import json as _json
from pathlib import Path as _P
_DB = _P(__file__).parent / "todo_db.json"
def _load():
    return _json.loads(_DB.read_text("utf-8")) if _DB.exists() else []
def _save(items):
    _DB.write_text(_json.dumps(items, ensure_ascii=False, indent=1), "utf-8")
def run(action="list", text=""):
    items = _load()
    if action == "add":
        items.append({"text": text, "done": False}); _save(items)
        return f"已添加: {text}（共{len(items)}项）"
    if action in ("done", "del"):
        i = int(text) - 1
        if 0 <= i < len(items):
            if action == "done": items[i]["done"] = True
            else: items.pop(i)
            _save(items)
            return "已更新"
        return "编号无效"
    lines = [f"{'☑' if t['done'] else '☐'} {i+1}. {t['text']}" for i, t in enumerate(items)]
    return "\\n".join(lines) if lines else "清单为空"
'''},
    "wallpaper": {
        "file": "wallpaper.py", "name": "每日壁纸", "icon": "🖼️", "cat": "创意",
        "desc": "下载必应每日美图并设为桌面壁纸",
        "src": '''SPEC = {"name": "wallpaper", "description": "下载必应每日壁纸并设置为桌面背景", "parameters": {}}
def run():
    import ctypes, json, urllib.request
    with urllib.request.urlopen("https://www.bing.com/HPImageArchive.aspx?format=js&idx=0&n=1", timeout=15) as r:
        d = json.loads(r.read().decode("utf-8"))
    url = "https://www.bing.com" + d["images"][0]["url"]
    dst = os.path.join(os.path.expanduser("~"), "Pictures", "bing_daily.jpg")
    urllib.request.urlretrieve(url, dst)
    ctypes.windll.user32.SystemParametersInfoW(20, 0, dst, 3)
    return f"壁纸已更换: {d['images'][0].get('copyright', '')}"
import os
'''},
    "sys_clean": {
        "file": "sys_clean.py", "name": "系统清理", "icon": "🧹", "cat": "系统",
        "desc": "清理临时目录与回收站，报告释放的空间",
        "src": '''SPEC = {"name": "sys_clean", "description": "清理系统临时文件与回收站", "parameters": {}}
def run():
    import shutil, tempfile
    freed = 0
    tmp = tempfile.gettempdir()
    for f in os.listdir(tmp):
        p = os.path.join(tmp, f)
        try:
            sz = os.path.getsize(p) if os.path.isfile(p) else 0
            if os.path.isfile(p): os.remove(p)
            elif os.path.isdir(p): shutil.rmtree(p, ignore_errors=True)
            freed += sz
        except Exception: pass
    try:
        import winshell
        winshell.recycle_bin().empty(confirm=False, show_progress=False, sound=False)
    except Exception: pass
    return f"临时目录已清理，释放约 {freed/1048576:.1f} MB（回收站已尝试清空）"
import os
'''},
    "currency": {
        "file": "currency.py", "name": "汇率换算", "icon": "💱", "cat": "网络",
        "desc": "实时汇率查询与换算（frankfurter.app 免费数据源）",
        "src": '''SPEC = {"name": "currency", "description": "汇率换算", "parameters": {"amount": "金额", "src": "源币种如USD", "dst": "目标币种如CNY"}}
def run(amount=100, src="USD", dst="CNY"):
    import urllib.request
    url = f"https://api.frankfurter.app/latest?amount={amount}&from={src}&to={dst}"
    with urllib.request.urlopen(url, timeout=15) as r:
        d = json.loads(r.read().decode("utf-8"))
    return f"{amount} {src} = {d['rates'][dst]} {dst}（{d['date']}）"
import json
'''},
    "web_snap": {
        "file": "web_snap.py", "name": "网页快照", "icon": "📸", "cat": "创意",
        "desc": "抓取网页正文保存为本地 HTML 文件并打开",
        "src": '''SPEC = {"name": "web_snap", "description": "保存网页到本地并打开", "parameters": {"url": "网址"}}
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
'''},
    "file_zip": {
        "file": "file_zip.py", "name": "压缩打包", "icon": "🗜️", "cat": "系统",
        "desc": "把指定目录打包成 zip 压缩包",
        "src": '''SPEC = {"name": "file_zip", "description": "压缩目录为zip", "parameters": {"path": "目录路径", "out": "输出zip路径(可选)"}}
def run(path="", out=""):
    import shutil
    if not path or not os.path.isdir(path): return "请提供有效目录 path"
    if not out: out = path.rstrip("\\\\/") + ".zip"
    shutil.make_archive(out[:-4], "zip", path)
    return f"已打包: {out}（{os.path.getsize(out)/1048576:.1f} MB）"
import os
'''},
    "php_server": {
        "file": "php_server.py", "name": "PHP 服务", "icon": "🐘", "cat": "服务",
        "desc": "一键启动/停止本地 PHP 内置服务器（需本机已装 php 并加入 PATH）",
        "src": '''SPEC = {"name": "php_server", "description": "管理本地PHP内置服务器", "parameters": {"action": "start/stop/status", "dir": "站点目录", "port": "端口(默认8000)"}}
import subprocess, os as _os
_PID = _os.path.join(_os.path.dirname(__file__), "php_server.pid")
def run(action="status", dir="", port=8000):
    if action == "start":
        if not dir: return "请提供站点目录 dir"
        p = subprocess.Popen(["php", "-S", f"127.0.0.1:{port}", "-t", dir],
                             creationflags=0x00000008)
        open(_PID, "w").write(str(p.pid))
        return f"PHP 服务已启动: http://127.0.0.1:{port} (pid={p.pid}, 目录={dir})"
    if action == "stop":
        if _os.path.exists(_PID):
            pid = int(open(_PID).read() or 0)
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
            _os.remove(_PID)
            return "PHP 服务已停止"
        return "没有运行中的服务记录"
    return "运行中 pid=" + open(_PID).read() if _os.path.exists(_PID) else "未运行"
'''},
    "npm_runner": {
        "file": "npm_runner.py", "name": "NPM 工程", "icon": "📦", "cat": "服务",
        "desc": "在项目目录执行 npm install/run/build，前端工程化一条龙",
        "src": '''SPEC = {"name": "npm_runner", "description": "在指定目录执行npm命令", "parameters": {"dir": "项目目录", "script": "install/run dev/build/test 等"}}
def run(dir="", script="install"):
    import subprocess
    if not dir: return "请提供项目目录 dir"
    args = ["cmd", "/c", "npm"] + script.split(" ")
    p = subprocess.run(args, cwd=dir, capture_output=True, text=True,
                       encoding="utf-8", errors="ignore", timeout=600)
    out = (p.stdout or "")[-1500:]
    err = (p.stderr or "")[-500:]
    return f"npm {script} 退出码 {p.returncode}\\n{out}\\n{err}".strip()
'''},
    "python_server": {
        "file": "python_server.py", "name": "静态文件服务", "icon": "🌐", "cat": "服务",
        "desc": "用 python -m http.server 把任意目录变成本地网站",
        "src": '''SPEC = {"name": "python_server", "description": "启动静态文件服务", "parameters": {"dir": "目录", "port": "端口(默认8080)"}}
def run(dir="", port=8080):
    import subprocess, sys, os
    if not dir: return "请提供目录 dir"
    p = subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--directory", dir],
                         creationflags=0x00000008)
    os.startfile(f"http://127.0.0.1:{port}")
    return f"静态服务已启动并打开: http://127.0.0.1:{port} (pid={p.pid})"
'''},
}


def catalog_view() -> list:
    """目录 + 安装状态（按 user_tools 中同名文件判断）"""
    out = []
    for key, it in CATALOG.items():
        fp = USER_TOOLS_DIR / it["file"]
        disabled = (USER_TOOLS_DIR / ("_" + it["file"])).exists()
        out.append({
            "key": key, "name": it["name"], "icon": it["icon"], "cat": it["cat"],
            "desc": it["desc"],
            "installed": fp.exists() or disabled,
            "enabled": fp.exists()})
    return out


def install(key: str) -> dict:
    it = CATALOG.get(key)
    if not it:
        return {"ok": False, "message": f"未知插件: {key}"}
    fp = USER_TOOLS_DIR / it["file"]
    if fp.exists():
        return {"ok": True, "message": f"{it['name']} 已安装", "already": True}
    dis = USER_TOOLS_DIR / ("_" + it["file"])
    if dis.exists():  # 曾停用：直接恢复启用
        dis.rename(fp)
    else:
        USER_TOOLS_DIR.mkdir(exist_ok=True)
        fp.write_text(it["src"], encoding="utf-8")
    import tools
    rep = tools.load_user_tools()
    st = rep["files"].get(it["file"], {})
    ok = st.get("status") == "ok"
    return {"ok": ok,
            "message": f"{it['name']} 安装{'成功，已热加载生效' if ok else '失败: ' + st.get('error', '')}"}


def uninstall(key: str) -> dict:
    key = (key or "").strip()
    if not key:
        return {"ok": False, "message": "缺少插件 key"}
    it = CATALOG.get(key)
    if not it:
        # 支持按文件名卸载自定义工具（仅允许 user_tools 内的 .py 文件）
        fname = os.path.basename(key)
        fp = USER_TOOLS_DIR / fname
        if (fp.exists() and fp.is_file() and fp.suffix == ".py"
                and not fp.name.startswith("_")):
            fp.rename(fp.with_name("_" + fp.name))
            import tools
            tools.load_user_tools()
            return {"ok": True, "message": f"已停用 {fp.name}"}
        return {"ok": False, "message": f"未找到插件: {key}"}
    fp = USER_TOOLS_DIR / it["file"]
    if not fp.exists():
        return {"ok": False, "message": f"{it['name']} 未安装或已停用"}
    fp.rename(fp.with_name("_" + fp.name))
    import tools
    tools.load_user_tools()
    return {"ok": True, "message": f"{it['name']} 已停用"}
