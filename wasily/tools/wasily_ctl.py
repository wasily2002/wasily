"""wasily 启停控制器（供 重启wasily.bat / 全部关闭wasily.bat 调用）

bat 里直接写 PowerShell 命令存在引号与中文路径编码问题，统一交给本脚本处理：
  python wasily_ctl.py stop     关闭全部组件
  python wasily_ctl.py start    启动服务 → 等待就绪 → 拉起大脑 → 启动 IDE
  python wasily_ctl.py chat     启动服务（若未运行）并打开 AI 对话窗口
  python wasily_ctl.py brain    只拉起桌面悬浮大脑
  python wasily_ctl.py server   只重启 AI 服务（不动大脑与 IDE）
  python wasily_ctl.py restart  = stop + start

所有路径都以本文件位置为基准推导，不依赖当前工作目录与 bat 的编码。
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
AI = ROOT / "AI"
IDE_DIR = TOOLS / "wasily-ide"
IDE_EXE = IDE_DIR / "wasily-ide.exe"
SERVER = AI / "api_server.py"
BRAIN_DIR = AI / "brain_electron"
BRAIN_EXE = BRAIN_DIR / "node_modules" / "electron" / "dist" / "electron.exe"
HEALTH = "http://127.0.0.1:8000/health"

# 按命令行特征识别的进程（避免误杀其他 python / electron）
KILL_RULES = {
    "python": ("api_server.py", "brain_desktop.py", "watchdog"),
    "electron": ("brain_electron",),
}
KILL_NAMES = ("wasily-ide", "wasily-ide.exe")


def log(msg):
    print(f"[wasily] {msg}", flush=True)


def _cmdline(proc):
    try:
        return " ".join(proc.cmdline())
    except Exception:
        return ""


def stop_all():
    import psutil

    log("正在关闭全部组件...")
    killed = 0
    for proc in psutil.process_iter(["name"]):
        name = (proc.info.get("name") or "").lower()
        try:
            if name in ("python.exe", "pythonw.exe"):
                cl = _cmdline(proc)
                if any(k in cl for k in KILL_RULES["python"]):
                    proc.kill()
                    killed += 1
            elif name == "electron.exe":
                cl = _cmdline(proc)
                if any(k in cl for k in KILL_RULES["electron"]):
                    proc.kill()
                    killed += 1
            elif name in KILL_NAMES:
                proc.kill()
                killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    time.sleep(1.5)
    log(f"已关闭 {killed} 个进程")


def _health_ok(timeout=2):
    try:
        with urllib.request.urlopen(HEALTH, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _watchdog_running():
    """按命令行特征判断守护进程是否已在运行（不依赖窗口/端口）"""
    import psutil
    me = os.getpid()
    for proc in psutil.process_iter(["name"]):
        try:
            if proc.pid == me:
                continue
            name = (proc.info.get("name") or "").lower()
            cl = _cmdline(proc)
            if name in ("python.exe", "pythonw.exe") and "watchdog.py" in cl:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def ensure_watchdog():
    """后台拉起 watchdog（pythonw 无控制台窗口，不在桌面显示）；
    api_server 已在线或守护进程已在跑时直接复用，避免重复拉起端口冲突"""
    if _health_ok():
        log("api_server 已在线，直接复用")
        return True
    if _watchdog_running():
        log("守护进程已在后台运行")
        return True
    log("启动守护进程（后台静默，无窗口）...")
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = str(pyw) if pyw.exists() else sys.executable
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [exe, str(AI / "watchdog.py")],
        cwd=str(AI),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
        close_fds=True,
    )
    return True


def wait_server(max_wait=180):
    """等待模型服务就绪（7B 首次加载较慢）"""
    log("等待服务就绪（模型加载需要时间）...")
    deadline = time.time() + max_wait
    while time.time() < deadline:
        if _health_ok():
            log("AI 服务已就绪")
            return True
        time.sleep(3)
    log("AI 服务未在预期时间内就绪，请查看 AI 目录下的 api_server 日志")
    return False


def start_server(max_wait=120):
    log("启动 AI 服务...")
    # 独立进程 + 指定工作目录：模型/配置的相对路径依赖 cwd
    subprocess.Popen(
        [sys.executable, str(SERVER)],
        cwd=str(AI),
        creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL,
    )
    return wait_server(max_wait)


def stop_server():
    """只关闭 AI 服务（不动大脑与 IDE），供单独重启服务使用"""
    import psutil

    for proc in psutil.process_iter(["name"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if name in ("python.exe", "pythonw.exe") and "api_server.py" in _cmdline(proc):
                proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    time.sleep(1.5)


def spawn_brain(retries=3):
    """直接拉起 Electron 大脑（比走 /spawn_brain HTTP 少一个失败环节）；
    Electron 未安装时回退服务端接口（pywebview 异形窗口）"""
    log("启动桌面大脑...")
    if BRAIN_EXE.exists():
        subprocess.Popen(
            [str(BRAIN_EXE), str(BRAIN_DIR / "main.js")],
            cwd=str(BRAIN_DIR),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(2)
        log("桌面大脑已启动")
        return True
    for i in range(retries):
        try:
            req = urllib.request.Request("http://127.0.0.1:8000/spawn_brain", method="POST")
            with urllib.request.urlopen(req, timeout=10) as r:
                if r.status == 200:
                    log("桌面大脑已拉起（pywebview 回退）")
                    return True
        except Exception as e:
            log(f"大脑拉起失败（第 {i + 1} 次）: {e}")
        time.sleep(3)
    return False


def _ide_cfg():
    """读取设置中心「通用」页的 IDE 行为配置（config.json 不可读时用默认值）"""
    try:
        with open(AI / "config.json", encoding="utf-8") as f:
            return (json.load(f) or {}).get("ide") or {}
    except Exception:
        return {}


def start_ide():
    log("启动 wasily IDE...")
    if not IDE_EXE.exists():
        log(f"未找到 IDE: {IDE_EXE}")
        return False
    # 默认启动窗口：system=恢复上次；new=新窗口；restore=复用窗口；none=空工作台
    mode = str(_ide_cfg().get("startup_window") or "system").lower()
    args = [str(IDE_EXE), "--locale=zh-cn"]
    if mode == "new":
        args += ["--new-window", str(AI)]
    elif mode == "restore":
        args += ["--reuse-window", str(AI)]
    elif mode == "none":
        pass  # 不带目录 → 空工作台
    else:
        args.append(str(AI))
    subprocess.Popen(
        args,
        cwd=str(IDE_DIR),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    log(f"IDE 启动参数: startup_window={mode}")
    return True


def _open_browser(url):
    """显式调用浏览器打开（os.startfile 在部分机器上会被默认程序接管成资源管理器）"""
    for exe in (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ):
        if Path(exe).exists():
            subprocess.Popen([exe, url], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    try:
        os.startfile(url)
        return True
    except Exception as e:
        log(f"打开浏览器失败: {e}")
        return False


def open_chat():
    """打开 AI 对话窗口：服务未运行则先拉起，就绪后用浏览器打开对话页"""
    log("打开 AI 对话窗口...")
    if not _health_ok():
        if not start_server():
            sys.exit(1)
    url = "http://127.0.0.1:8000/"
    _open_browser(url)
    log(f"对话窗口已打开：{url}")


def main():
    action = (sys.argv[1] if len(sys.argv) > 1 else "restart").lower()
    if action == "stop":
        stop_all()
    elif action == "watchdog":
        ensure_watchdog()
    elif action == "start":
        ensure_watchdog()
        if not wait_server():
            sys.exit(1)
        spawn_brain()
        start_ide()
        log("启动完成")
    elif action == "brain":
        spawn_brain()
    elif action == "server":
        stop_server()
        ensure_watchdog()
        if not wait_server():
            sys.exit(1)
        log("AI 服务已重启")
    elif action == "chat":
        open_chat()
    elif action == "restart":
        stop_all()
        ensure_watchdog()
        if not wait_server():
            sys.exit(1)
        spawn_brain()
        start_ide()
        log("重启完成")
    else:
        print("用法: python wasily_ctl.py [stop|start|restart]")
        sys.exit(2)


if __name__ == "__main__":
    main()
