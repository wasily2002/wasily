SPEC = {"name": "npm_runner", "description": "在指定目录执行npm命令", "parameters": {"dir": "项目目录", "script": "install/run dev/build/test 等"}}
def run(dir="", script="install"):
    import subprocess
    if not dir: return "请提供项目目录 dir"
    args = ["cmd", "/c", "npm"] + script.split(" ")
    p = subprocess.run(args, cwd=dir, capture_output=True, text=True,
                       encoding="utf-8", errors="ignore", timeout=600)
    out = (p.stdout or "")[-1500:]
    err = (p.stderr or "")[-500:]
    return f"npm {script} 退出码 {p.returncode}\n{out}\n{err}".strip()
