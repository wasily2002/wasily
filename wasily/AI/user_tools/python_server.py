SPEC = {"name": "python_server", "description": "启动静态文件服务", "parameters": {"dir": "目录", "port": "端口(默认8080)"}}
def run(dir="", port=8080):
    import subprocess, sys, os
    if not dir: return "请提供目录 dir"
    p = subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--directory", dir],
                         creationflags=0x00000008)
    os.startfile(f"http://127.0.0.1:{port}")
    return f"静态服务已启动并打开: http://127.0.0.1:{port} (pid={p.pid})"
