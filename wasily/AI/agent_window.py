"""Agent 中心独立窗口（无边框）

为什么不用浏览器 --app：Edge/Chrome 的 App 窗口仍带系统标题栏和边框，
做不出无边框效果。这里用 pywebview(WebView2) 开一个 frameless 窗口承载 /agent。

无边框后系统按钮没了，所以：
- 顶部 #winbar 加 pywebview-drag-region 类，负责拖动窗口（draggable=True）；
- 页面里的最小化/最大化/关闭按钮通过 js_api 调到这里的方法。
"""
import ctypes
import os
import socket
import subprocess
import sys
import time
from urllib.parse import quote

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 主窗口只保留左侧导航，对话单独成一个窗口进程：
# 用 --chat 开关区分模式，避免和原有「argv[1] = tab 名」冲突
IS_CHAT = "--chat" in sys.argv[1:]
_TEMP = os.environ.get("TEMP") or BASE_DIR
CHAT_TITLE = "wasily · 对话"
TITLE = CHAT_TITLE if IS_CHAT else "Agent · wasily"
SERVER = (os.environ.get("WASILY_SERVER") or "http://127.0.0.1:8000").rstrip("/")
WIN_W, WIN_H = (560, 900) if IS_CHAT else (1320, 880)
MIN_W, MIN_H = (380, 560) if IS_CHAT else (960, 640)
CHAT_LOCK = os.path.join(_TEMP, "wasily_chat_window.pid")
LOCK = CHAT_LOCK if IS_CHAT else os.path.join(_TEMP, "wasily_agent_window.pid")


def _server_port():
    try:
        return int(SERVER.rsplit(":", 1)[1].split("/")[0])
    except Exception:
        return 8000


def _wait_server(timeout=25):
    """后端重启时点开 Agent 不至于立刻白屏：最多等 25 秒。"""
    host = "127.0.0.1"
    port = _server_port()
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection((host, port), 1.5):
                return True
        except OSError:
            time.sleep(0.6)
    return False


def _pid_alive(pid):
    if not pid:
        return False
    try:
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    except Exception:
        return False


def _read_lock(path):
    try:
        return int((open(path).read() or "0").strip() or 0)
    except Exception:
        return 0


def _arg_value(name):
    """取 --session=xxx / --session xxx 形式的参数；裸开关（如 --new）返回 '1'。"""
    flag = "--" + name
    args = sys.argv[1:]
    for a in args:
        if a.startswith(flag + "="):
            return a[len(flag) + 1:]
    for i, a in enumerate(args):
        if a == flag:
            nxt = args[i + 1] if i + 1 < len(args) else ""
            return nxt if nxt and not nxt.startswith("--") else "1"
    return ""


def _focus_existing(title=TITLE):
    """已有同名窗口时把它提到前台，而不是再开一个。"""
    try:
        hwnd = ctypes.windll.user32.FindWindowW(None, title)
        if not hwnd:
            return False
        ctypes.windll.user32.ShowWindow(hwnd, 9)      # SW_RESTORE
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


# 窗口对象不能挂在 Api 上：pywebview 会遍历 js_api 的属性去构建 JS 桥，
# 遇到 win.native(.NET 窗体) 会沿着属性链无限递归（maximum recursion depth exceeded）。
_WIN = {"obj": None}
_MAXED = {"on": False}


class Api:
    """给页面脚本调用的窗口控制（最小化/最大化/关闭）。"""

    def minimize(self):
        try:
            _WIN["obj"].minimize()
        except Exception:
            pass
        return True

    def toggle_max(self):
        try:
            if _MAXED["on"]:
                _WIN["obj"].restore()
            else:
                _WIN["obj"].maximize()
            _MAXED["on"] = not _MAXED["on"]
        except Exception:
            pass
        return _MAXED["on"]

    def close(self):
        try:
            _WIN["obj"].destroy()
        except Exception:
            pass
        return True

    def open_chat(self, session="", prompt="", new=""):
        """从 Agent 主窗口弹出独立对话窗口；已开着就只聚焦。

        pywebview 的 WebView2 会接管 window.open（在当前窗口内跳转），
        所以独立窗口只能由 Python 侧再起一个进程。
        """
        if IS_CHAT:
            return "focused"
        try:
            if _pid_alive(_read_lock(CHAT_LOCK)) and _focus_existing(CHAT_TITLE):
                return "focused"
            args = [sys.executable, "-X", "utf8", os.path.abspath(__file__), "--chat"]
            if session:
                args.append("--session=" + str(session))
            if prompt:
                args.append("--prompt=" + str(prompt))
            if new:
                args.append("--new")
            subprocess.Popen(
                args, cwd=BASE_DIR,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return "spawned"
        except Exception:
            return "error"


def main():
    if IS_CHAT:
        url = SERVER + "/chat?embed=agent"
        for name in ("session", "prompt"):
            val = _arg_value(name)
            if val:
                url += "&" + name + "=" + quote(val)
        if _arg_value("new"):
            url += "&new=1"
    else:
        tab = sys.argv[1] if len(sys.argv) > 1 else ""
        if tab.startswith("#"):
            tab = tab[1:]
        url = SERVER + "/agent" + (("#" + tab) if tab else "")

    # 单实例：已开过就聚焦旧窗口，避免每点一次多一个窗口
    try:
        old = _read_lock(LOCK)
        if old and old != os.getpid() and _pid_alive(old) and _focus_existing(TITLE):
            return
    except Exception:
        pass
    try:
        open(LOCK, "w").write(str(os.getpid()))
    except Exception:
        pass

    _wait_server()

    import webview
    api = Api()
    win = webview.create_window(
        TITLE,
        url,
        width=WIN_W,
        height=WIN_H,
        min_size=(MIN_W, MIN_H),
        frameless=True,
        draggable=True,      # 顶部 #winbar 的 pywebview-drag-region 可拖动窗口
        easy_drag=False,     # 关掉「整页都能拖」，否则选不中文字
        text_select=True,
        resizable=True,
        background_color="#ffffff",
        js_api=api,
    )
    _WIN["obj"] = win
    webview.start(debug=False)
    try:
        os.remove(LOCK)
    except Exception:
        pass


if __name__ == "__main__":
    main()
