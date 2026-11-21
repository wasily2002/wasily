"""
wasily 守护进程（watchdog）
  - 守护 api_server.py（HTTP健康检查）、monitor.py（HTTP检查）、agent.py（进程检查）
  - 连续 3 次检查失败(约30秒)自动拉起；agent 仅在无人交互误杀时也能恢复
  - 子进程输出写入滚动日志(wasily_log/xxx.log, 5MB×5)
用法: python watchdog.py        （建议用「启动wasily.bat」拉起）
"""
import os
import json
import subprocess
import sys
import threading
import time
import urllib.request
from logging.handlers import RotatingFileHandler
import logging

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
LOG_DIR = os.path.join(BASE_DIR, "wasily_log")
os.makedirs(LOG_DIR, exist_ok=True)

# 生成中的标记文件（api_server 每轮开始时写入、结束时删除）
BUSY_FILE = os.path.join(BASE_DIR, "sessions_store", "_inflight.json")
BUSY_MAX_SEC = 1200   # 20 分钟：超时视为卡死，允许重启


def generating_now():
    """有正在进行的对话生成 → CPU 推理高峰时 /health 可能超时，不能误杀"""
    try:
        with open(BUSY_FILE, encoding="utf-8") as f:
            d = json.load(f)
        # 多会话标记按 sid 保存，不能只读旧版顶层 ts，否则忙时保护会失效
        records = [d] if "ts" in d else list(d.values())
        now = time.time()
        for rec in records:
            if not isinstance(rec, dict):
                continue
            try:
                ts = float(rec.get("ts") or 0)
            except (TypeError, ValueError):
                continue
            if ts > 0 and (now - ts) < BUSY_MAX_SEC:
                return True
        return False
    except Exception:
        return False



def make_logger(name):
    lg = logging.getLogger(name)
    lg.setLevel(logging.INFO)
    if not lg.handlers:
        h = RotatingFileHandler(os.path.join(LOG_DIR, f"{name}.log"),
                                maxBytes=5 * 1024 * 1024, backupCount=5,
                                encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        lg.addHandler(h)
    return lg


def pump_output(proc, logger):
    """把子进程 stdout/stderr 转写进滚动日志"""
    def _pump(pipe, fn):
        for line in iter(pipe.readline, ""):
            try:
                fn(line.rstrip())
            except Exception:
                pass
    threading.Thread(target=_pump, args=(proc.stdout, logger.info), daemon=True).start()
    threading.Thread(target=_pump, args=(proc.stderr, logger.warning), daemon=True).start()


class Guard:
    def __init__(self, name, args, health_url=None, logger=None):
        self.name = name
        self.args = args                      # 启动命令
        self.health_url = health_url          # HTTP 健康检查地址(可选)
        self.logger = logger or make_logger(name)
        self.proc = None
        self.fail_count = 0

    def _health(self):
        try:
            # 超时要放宽：CPU 推理(>40s)期间 /health 会明显变慢，3s 太短会误判死亡
            with urllib.request.urlopen(self.health_url, timeout=15) as r:
                return r.status == 200
        except Exception:
            return False

    def alive(self):
        if self.proc and self.proc.poll() is None:
            return self._health() if self.health_url else True
        # 不在本守护进程管理下的实例（手动启动/重启器拉起）：健康就视为存活，
        # 避免重复拉起第二个 api_server（双实例会争抢端口与内存）
        return bool(self.health_url) and self._health()

    def start(self):
        # 端口已被占用：健康就接管监控；连续未就绪则视为卡死，清理后重拉。
        pid = _listener_pid() if self.health_url else None
        if pid and self._health():
            self.logger.info("[watchdog] 8000 已有健康实例，接管监控（不重复拉起）")
            self.proc = None
            self.fail_count = 0
            return
        if pid and not self._health():
            if self.fail_count < 2:
                self.logger.info(f"[watchdog] 8000 被 PID {pid} 占用但未就绪，继续等待")
                self.proc = None
                return
            self.logger.warning(f"[watchdog] 8000 上 PID {pid} 长时间未就绪，强制清理")
            try:
                import psutil
                psutil.Process(pid).kill()
                time.sleep(1)
            except Exception as e:
                self.logger.warning(f"[watchdog] 清理失败: {e}")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.proc = subprocess.Popen(self.args, cwd=BASE_DIR,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, encoding="utf-8", errors="replace",
                                     creationflags=flags)
        pump_output(self.proc, self.logger)
        self.logger.info(f"[watchdog] 启动 {self.name} pid={self.proc.pid}")
        self.fail_count = 0

    def stop(self, grace=8):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(grace)
            except Exception:
                self.proc.kill()
        self.proc = None

    def watch(self, interval=5):
        # 退避：1s / 5s / 15s / 30s / 60s，之后维持 60s 低频重试（保证可用性优先）
        backoff = [1, 5, 15, 30, 60]
        attempt = 0
        while True:
            if self.alive():
                if self.fail_count:
                    self.logger.info(f"[watchdog] {self.name} 已恢复")
                self.fail_count = 0
                attempt = 0
            elif ((self.proc and self.proc.poll() is None) or _listener_pid()) and generating_now():
                # 进程还活着、正在生成：健康检查超时是 CPU 推理/排队导致的，
                # 重启会直接杀掉用户正在跑的对话（表现为「AI 做到一半失忆/乱答」）
                self.fail_count = 0
                self.logger.info("[watchdog] 检测到对话生成进行中，忽略本次健康失败")
            elif _starting_instance():
                # 有实例正在启动/加载模型（端口可能还没起来）：给足加载时间，别急着拉第二个
                self.fail_count = 0
                self.logger.info("[watchdog] 检测到实例正在启动或加载模型，等待就绪")
            else:
                self.fail_count += 1
                self.logger.warning(f"[watchdog] {self.name} 异常 x{self.fail_count}")
                # 嵌入/RAG 计算可能让存活实例暂时响应变慢，不能用进程退出同样的短宽限误杀
                fail_limit = 12 if ((self.proc and self.proc.poll() is None) or _listener_pid()) else 3
                if self.fail_count >= fail_limit:
                    delay = backoff[min(attempt, len(backoff) - 1)]
                    attempt += 1
                    self.logger.warning(f"[watchdog] 重启 {self.name}（第 {attempt} 次，退避 {delay}s）")
                    self.stop()
                    time.sleep(delay)
                    self.start()
                    self.fail_count = 0
                    if attempt >= len(backoff):
                        self.logger.warning("[watchdog] 连续多次重启失败，转 60s 低频重试")
            time.sleep(interval)


def _port_open(host="127.0.0.1", port=8000, timeout=1.0):
    """端口是否有进程在监听（用于避免重复拉起第二个实例）"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        try:
            s.close()
        except Exception:
            pass


def _starting_instance(max_age=150):
    """是否有 api_server 实例处于启动/加载模型的宽限期（避免重复拉起）"""
    try:
        import psutil
        me = os.getpid()
        now = time.time()
        for p in psutil.process_iter(["cmdline", "create_time"]):
            try:
                if p.pid == me:
                    continue
                cl = " ".join(p.info.get("cmdline") or [])
                if "api_server.py" in cl and "self_restarter.py" not in cl:
                    if now - float(p.info.get("create_time") or 0) < max_age:
                        return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        pass
    return False


def _listener_pid(port=8000):
    """返回监听该端口的进程 PID（找不到则 None）"""
    try:
        import psutil
        for c in psutil.net_connections(kind="inet"):
            if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port and c.pid:
                return c.pid
    except Exception:
        pass
    return None


def _acquire_single_instance():
    """单实例锁：防止同时跑多个 watchdog 各拉一个 api_server"""
    path = os.path.join(LOG_DIR, "watchdog.lock")
    try:
        import msvcrt
        f = open(path, "a+")
        f.seek(0)
        if f.read(1) == "":
            f.write("0")
            f.flush()
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        return f
    except Exception:
        return None


def main():
    lock = _acquire_single_instance()
    if lock is None:
        print("[watchdog] 已有守护进程在运行，本次退出", flush=True)
        return
    guards = [
        Guard("api_server", [sys.executable, os.path.join(BASE_DIR, "api_server.py")],
              health_url="http://127.0.0.1:8000/health"),
    ]
    for g in guards:
        g.start()
    threads = [threading.Thread(target=g.watch, daemon=True) for g in guards]
    for t in threads:
        t.start()
    print("[watchdog] 守护中: api_server(8000)，Ctrl+C 退出",
          flush=True)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("[watchdog] 退出，停止全部子进程")
        for g in guards:
            g.stop(grace=5)


if __name__ == "__main__":
    main()
