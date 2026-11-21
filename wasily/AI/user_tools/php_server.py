SPEC = {"name": "php_server", "description": "管理本地PHP内置服务器", "parameters": {"action": "start/stop/status", "dir": "站点目录", "port": "端口(默认8000)"}}
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
