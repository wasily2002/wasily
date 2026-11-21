"""
wasily 自我重启器（分离进程，由 self_mcp_server 的 self_restart 拉起）

独立于 api_server 运行：即使目标是 api_server 自身，杀死后仍能把它重新拉起。
流程：按 match 杀旧进程 → 等待退出 → 按 cmd 拉起新进程 → 健康检查(如有)。
用法: python self_restarter.py <程序名>
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "self_mcp_config.json")
LOG = os.path.join(BASE_DIR, "wasily_log", "self_restarter.log")


def log(msg):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_prog(name):
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    root = os.path.realpath(cfg.get("root") or BASE_DIR)
    p = dict(cfg.get("programs", {}).get(name) or {})
    if not p:
        raise SystemExit(f"未知程序 {name}")
    p["cmd"] = [c.replace("{root}", root) for c in p.get("cmd", [])]
    p["cwd"] = (p.get("cwd") or root).replace("{root}", root)
    return p


def find_pids(match):
    """按命令行特征找目标进程。

    注意两个坑（曾导致 16:59 杀掉后再也没拉起来）：
    1) 用 powershell -Command 查进程时，powershell 自己的命令行里就带着 match 字符串，
       会被一起匹配进来；改为优先用 psutil，回退时才写临时 .ps1（命令行不含 match）。
    2) 不能把 self_restarter 自己（及其祖先进程链）算进去，否则 taskkill /T 会连带杀掉重启器。
    """
    if not match:
        return []
    me = os.getpid()
    out = []
    try:
        import psutil
        for proc in psutil.process_iter(["name"]):
            try:
                if proc.pid == me:
                    continue
                cl = " ".join(proc.cmdline())
                if match in cl and "self_restarter.py" not in cl:
                    out.append(proc.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return out
    except Exception:
        pass
    # 回退：把查询写进临时 .ps1，powershell 命令行只含脚本路径，不含 match
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".ps1")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("Get-CimInstance Win32_Process | Where-Object "
                    "{ $_.CommandLine -and $_.CommandLine -like '*%s*' } | "
                    "ForEach-Object { $_.ProcessId }" % match.replace("'", "''"))
        r = subprocess.run(["powershell", "-NoProfile", "-File", path],
                           capture_output=True, text=True, timeout=20)
        return [int(x) for x in r.stdout.split() if x.isdigit() and int(x) != me]
    except Exception:
        return []
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


def _exclude_ancestors(pids):
    """排除重启器自身的祖先 PID：对祖先 taskkill 会连带杀掉重启器。"""
    try:
        import psutil
        anc = {p.pid for p in psutil.Process().parents()}
        return [p for p in pids if p not in anc]
    except Exception:
        return pids


def pid_alive(pid):
    r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                       capture_output=True, text=True, timeout=10)
    return str(pid) in (r.stdout or "")


def wait_health(url, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


def main():
    name = sys.argv[1]
    p = load_prog(name)
    match = p.get("match") or ""

    # 1) 杀旧进程（不用 /T：重启器自己可能是 api_server → self_mcp_server → restarter
    #    这棵树里的后代，/T 会把自己一起杀掉，出现「杀了但没拉起来」的掉线）
    pids = _exclude_ancestors(find_pids(match))
    log(f"[restart] {name}: 匹配 '{match}' → PID {pids}")
    # 重启前先落一条「生成中断」记录，方便排查与客户端提示
    try:
        with open(os.path.join(BASE_DIR, "wasily_log", "_restarting.json"), "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "name": name, "note": "服务重启：本会话生成可能被中断"}, f)
        log(f"[restart] {name}: 已记录生成中断标记，开始停旧进程")
    except Exception:
        pass
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       capture_output=True, timeout=15)
    # 2) 等待退出（最多 10 秒）
    t0 = time.time()
    while time.time() - t0 < 10:
        if not any(pid_alive(pid) for pid in pids):
            break
        time.sleep(0.5)
    time.sleep(1)

    # 3) 拉起新进程
    if not p.get("cmd"):
        log(f"[restart] {name}: 无启动命令，结束")
        return
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    proc = subprocess.Popen(p["cmd"], cwd=p["cwd"], creationflags=flags,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            close_fds=True)
    log(f"[restart] {name}: 已拉起 PID {proc.pid} cmd={' '.join(p['cmd'])}")

    # 4) 单实例收敛：新进程之外如果还有残留匹配进程，只留新拉起的这个
    time.sleep(0.5)
    extra = [x for x in _exclude_ancestors(find_pids(match)) if x != proc.pid]
    if extra:
        log(f"[restart] {name}: 发现残留实例 {extra}，清理以保持单实例")
        for pid in extra:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=15)

    # 5) 健康检查（仅记录，不干预——watchdog 会兜底）
    if p.get("health_url"):
        ok = wait_health(p["health_url"], timeout=90)
        log(f"[restart] {name}: 健康检查 {'通过' if ok else '超时未通过'}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"[restart] 异常: {e}")
