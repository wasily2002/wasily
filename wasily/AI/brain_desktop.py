"""
wasily Brain Float - 独立大脑桌面悬浮窗
  - 无边框 + SetWindowRgn 异形窗：窗口按大脑轮廓裁剪，轮廓外完全透出桌面
    （WebView2 窗口模式内容走 DComp 子窗口，LWA_COLORKEY/NOREDIRECTIONBITMAP
    等常规 Win32 透明手段均无法穿透，异形区域是唯一可靠的真透明路径）
  - 由 api_server 的 /spawn_brain 端点拉起（也可单独运行 python brain_desktop.py）
  - 单实例：已存在悬浮窗时不再重复创建
  - 大脑页面自带右键菜单与状态轮询；菜单弹出时窗口临时恢复完整矩形
用法: python brain_desktop.py
"""
import os
import sys
import time
import ctypes
import ctypes.wintypes
import threading
import subprocess
import urllib.request
import webbrowser


BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

SERVER_URL = "http://127.0.0.1:8000"
BRAIN_URL = SERVER_URL + "/brain?float=1"   # 纯大脑模式：无标题/按钮/状态栏，窗口固定不可拖
WIN_TITLE = "wasily Brain Float"   # 与 agent.py 双窗口模式的 "wasily Brain" 区分
BRAIN_W, BRAIN_H = 640, 540
INITIAL_ZOOM_SCALE = 2.0   # 初始图标倍率：2x = 约 144px 直径大小

import webview


def server_alive(timeout=2):
    try:
        with urllib.request.urlopen(SERVER_URL + "/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def ensure_server():
    """独立运行时：若模型服务未启动，则后台拉起 api_server.py"""
    if server_alive():
        return
    print("[BrainFloat] 正在启动模型服务 api_server.py ...", flush=True)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    subprocess.Popen([sys.executable, os.path.join(BASE, "api_server.py")],
                     cwd=BASE, creationflags=flags)
    for _ in range(30):   # 等待服务就绪
        if server_alive():
            return
        time.sleep(1)


def get_primary_rect():
    """主显示器区域（多显示器时坐标可能为负）"""
    try:
        user32 = ctypes.windll.user32
        pt = ctypes.wintypes.POINT(0, 0)
        hm = user32.MonitorFromPoint(pt, 2)
        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.wintypes.DWORD),
                        ("rcMonitor", ctypes.wintypes.RECT),
                        ("rcWork", ctypes.wintypes.RECT),
                        ("dwFlags", ctypes.wintypes.DWORD)]
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        user32.GetMonitorInfoW(hm, ctypes.byref(mi))
        return mi.rcMonitor.left, mi.rcMonitor.top, mi.rcMonitor.right, mi.rcMonitor.bottom
    except Exception:
        return 0, 0, 1920, 1080


def _enum_float_hwnds():
    """枚举标题含 WIN_TITLE 的可见顶层窗口句柄"""
    results = []
    GA_ROOT = 2
    user32 = ctypes.windll.user32

    def callback(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            if WIN_TITLE in buf.value:
                root = user32.GetAncestor(hwnd, GA_ROOT)
                if root not in results:
                    results.append(root)
        return True

    EnumProc = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL,
                                  ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    user32.EnumWindows(EnumProc(callback), 0)
    return results


def _bring_to_front(hwnd):
    """已存在悬浮窗时将其前置"""
    try:
        SW_RESTORE = 9
        user32 = ctypes.windll.user32
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def _set_topmost(hwnd, on_top):
    HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
    SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE = 0x0002, 0x0001, 0x0010
    return bool(ctypes.windll.user32.SetWindowPos(
        hwnd, HWND_TOPMOST if on_top else HWND_NOTOPMOST,
        0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE))


def _strip_no_redirection(hwnd):
    """移除历史遗留的 WS_EX_NOREDIRECTIONBITMAP（实测对 WebView2 无效且会干扰异形窗）"""
    GWL_EXSTYLE = -20
    WS_EX_NOREDIRECTIONBITMAP = 0x00200000
    user32 = ctypes.windll.user32
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    if style & WS_EX_NOREDIRECTIONBITMAP:
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style & ~WS_EX_NOREDIRECTIONBITMAP)


def _set_window_shape(hwnd, rows):
    """按大脑轮廓栅格设置窗口异形区域；rows=None 恢复完整矩形（菜单弹出时）。
    rows 每行为连续段列表 [[x0,x1],...] 或 None；栅格 80x68 对应窗口 640x540，
    每格 8x8 物理像素（DPI 缩放时按窗口实际比例映射）。"""
    gdi32 = ctypes.windll.gdi32
    user32 = ctypes.windll.user32
    if rows is None:
        user32.SetWindowRgn(hwnd, None, True)
        return
    rect = ctypes.wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    cw, ch = rect.right - rect.left, rect.bottom - rect.top
    if cw <= 0 or ch <= 0:
        return
    SH_W, SH_H = 80, 68
    sx, sy = cw / SH_W, ch / SH_H
    region = None
    for y, runs in enumerate(rows):
        if not runs:
            continue
        for x0, x1 in runs:
            r = gdi32.CreateRectRgn(int(x0 * sx), int(y * sy),
                                    int((x1 + 1) * sx) + 1, int((y + 1) * sy) + 1)
            if region is None:
                region = r
            else:
                gdi32.CombineRgn(region, region, r, 2)   # RGN_OR
                gdi32.DeleteObject(r)
    if region:
        # SetWindowRgn 成功后系统接管 region 所有权，不可再 DeleteObject
        if not user32.SetWindowRgn(hwnd, region, True):
            gdi32.DeleteObject(region)


def apply_window_fx(home_x, home_y, on_top=True):
    """窗口就绪后应用置顶 + 尺寸硬锁（位置允许自由拖动，不再强制归位）。
    尺寸守护：每 2s 检查一次，窗口被外部改尺寸时恢复。
    位置不再守护：用户可自由拖动，拖到哪停到哪。"""
    def _work():
        target = None
        for i in range(60):
            hwnds = _enum_float_hwnds()
            if i < 6 or i % 10 == 0:
                print(f"[BrainFloat] 枚举第{i}轮：{len(hwnds)} 个候选窗口", flush=True)
            for hwnd in hwnds:
                rect = ctypes.wintypes.RECT()
                ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
                if (rect.right - rect.left) > 300 and (rect.bottom - rect.top) > 300:
                    target = hwnd
                    break
            if target:
                break
            time.sleep(0.5)
        if not target:
            print("[BrainFloat] 未找到窗口句柄", flush=True)
            return
        _set_topmost(target, on_top)
        _strip_no_redirection(target)
        # 主动归位到期望坐标（创建后窗口可能被 WebView2/DWM 挪动）
        SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
        ctypes.windll.user32.SetWindowPos(
            target, 0, home_x, home_y, BRAIN_W, BRAIN_H, SWP_NOZORDER | SWP_NOACTIVATE)
        print(f"[BrainFloat] 异形窗透明已就绪（初始位置 {home_x},{home_y}，可自由拖动）", flush=True)
        # 仅尺寸守护：每 2s 检查尺寸，被改则恢复；位置不再强制回位
        SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
        SWP_NOMOVE = 0x0002
        while True:
            if not ctypes.windll.user32.IsWindow(target):
                return   # 窗口已销毁，守护退出
            try:
                rect = ctypes.wintypes.RECT()
                if not ctypes.windll.user32.GetWindowRect(target, ctypes.byref(rect)):
                    return
                w, h = rect.right - rect.left, rect.bottom - rect.top
                if abs(w - BRAIN_W) > 2 or abs(h - BRAIN_H) > 2:
                    ctypes.windll.user32.SetWindowPos(
                        target, 0, 0, 0, BRAIN_W, BRAIN_H,
                        SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE)
                    print(f"[BrainFloat] 尺寸被改为 {w}x{h}，已恢复 {BRAIN_W}x{BRAIN_H}", flush=True)
            except Exception:
                return
            time.sleep(2)

    threading.Thread(target=_work, daemon=True).start()


class FloatApi:
    """暴露给 brain.html 的最小窗口控制接口（页面按钮调用同名方法）。
    注意：window 引用必须为 _window（下划线开头）——pywebview 注入时会递归遍历
    所有公开属性，若暴露 window 会递归进 .NET 对象图，严重阻塞 JS 桥注入。"""

    def __init__(self):
        self._window = None

    def quit(self):
        try:
            if self._window:
                self._window.destroy()
        except Exception:
            pass

    def hide_brain(self):
        try:
            if self._window:
                self._window.hide()
        except Exception:
            pass

    def toggle_chat(self):
        """独立悬浮窗没有内置对话面板：用默认浏览器打开对话页"""
        try:
            webbrowser.open(SERVER_URL + "/")
        except Exception:
            pass

    def open_chat_settings(self):
        """右键菜单「配置对话页面」：用默认浏览器打开系统设置页"""
        try:
            webbrowser.open(SERVER_URL + "/settings")
        except Exception:
            pass

    _PAGE_WHITELIST = ("/", "/brain", "/history", "/train", "/video_call",
                       "/settings", "/flowviz", "/docs")

    @classmethod
    def _page_allowed(cls, path):
        """只校验路径部分，允许 ?mode=edit 之类的受控查询参数"""
        return isinstance(path, str) and path.split("?")[0] in cls._PAGE_WHITELIST

    def open_page(self, path=None):
        """右键菜单「功能页面」组：白名单校验后用默认浏览器打开独立功能页"""
        try:
            if self._page_allowed(path):
                webbrowser.open(SERVER_URL + path)
        except Exception:
            pass

    def open_page_embed(self, path=None):
        """系统设置「内嵌窗口」打开方式：pywebview 应用内窗口打开功能页"""
        try:
            if self._page_allowed(path):
                import webview
                webview.create_window("wasily", SERVER_URL + path,
                                      width=1100, height=760)
        except Exception:
            pass

    def drag_start(self, pt=None):
        """HTCAPTION 原生窗口拖动：一次调用移交系统移动循环，绝对跟手；
        配合页面普通左键拖动触发（pt 兼容 Electron 迁移方案的上报参数，此处忽略）"""
        try:
            hwnds = _enum_float_hwnds()
            if not hwnds:
                return
            user32 = ctypes.windll.user32
            user32.ReleaseCapture()
            user32.SendMessageW(hwnds[0], 0xA1, 2, 0)  # WM_NCLBUTTONDOWN, HTCAPTION
        except Exception:
            pass

    def drag_end(self):
        """拖动结束：保存当前窗口位置（供下次启动恢复）"""
        try:
            hwnds = _enum_float_hwnds()
            if not hwnds:
                return
            user32 = ctypes.windll.user32
            rect = ctypes.wintypes.RECT()
            user32.GetWindowRect(hwnds[0], ctypes.byref(rect))
            # 持久化到配置，下次启动恢复到该位置
            try:
                sys.path.insert(0, BASE)
                import config as _cfg
                _cfg.load()
                _cfg.set_value("brain_float.x", rect.left)
                _cfg.set_value("brain_float.y", rect.top)
            except Exception:
                pass
            print(f"[BrainFloat] 拖动结束，位置已保存: {rect.left},{rect.top}", flush=True)
        except Exception:
            pass

    def set_shape(self, rows):
        """页面推送大脑轮廓栅格：设置异形窗口区域；rows=None 恢复完整矩形"""
        try:
            hwnds = _enum_float_hwnds()
            if not hwnds:
                return
            _set_window_shape(hwnds[0], rows)
            filled = sum(1 for r in rows if r) if rows else 0
            print(f"[BrainFloat] 轮廓已应用：{filled}/68 行有像素"
                  if rows else "[BrainFloat] 已恢复完整矩形（菜单）", flush=True)
        except Exception:
            pass


def _set_dpi_aware():
    """创建窗口前设置 DPI 感知，避免高缩放屏幕下窗口错位/白屏"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def main():
    _set_dpi_aware()
    ensure_server()
    # 单实例：已有悬浮窗则前置后退出
    existing = _enum_float_hwnds()
    if existing:
        print("[BrainFloat] 悬浮窗已存在，前置并退出", flush=True)
        for hwnd in existing:
            _bring_to_front(hwnd)
        return

    ml, mt, mr, mb = get_primary_rect()
    # 优先用上次保存的位置，否则默认右下角
    saved_x = None
    saved_y = None
    try:
        import config as _cfg
        _cfg.load()
        saved_x = _cfg.get("brain_float.x")
        saved_y = _cfg.get("brain_float.y")
    except Exception:
        pass
    if saved_x is not None and saved_y is not None:
        home_x = int(saved_x)
        home_y = int(saved_y)
        print(f"[BrainFloat] 恢复上次位置: {home_x},{home_y}", flush=True)
    else:
        home_x = max(ml, mr - BRAIN_W - 40)   # 初始：屏幕右下角
        home_y = max(mt, mb - BRAIN_H - 40)
    api = FloatApi()
    brain_url = f"{BRAIN_URL}&init_zoom_scale={INITIAL_ZOOM_SCALE}"
    win = webview.create_window(
        WIN_TITLE,
        brain_url,
        width=BRAIN_W, height=BRAIN_H,
        x=home_x,
        y=home_y,
        frameless=True,
        transparent=True,
        on_top=True,
        resizable=False,       # 尺寸硬锁：禁止任何自适应变宽
        js_api=api,
    )
    api._window = win
    print("[BrainFloat] 大脑悬浮窗已启动（固定位置 · 纯大脑 · 右键菜单）", flush=True)
    apply_window_fx(home_x, home_y)
    webview.start(debug=False)


if __name__ == "__main__":
    main()
