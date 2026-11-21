# -*- coding: utf-8 -*-
"""
wasily 分布式算力集群（主节点）
模型：主节点维护任务队列；worker（手机/电脑）HTTP 轮询领任务 → 本地算 → 回传结果。
网络：局域网直连；互联网经花生壳把本机 8000 映射出公网即可（worker 填公网地址）。
安全：worker 凭 config[dist.token] 接入；任务类型白名单；payload 上限 40MB。
任务类型第一版：whisper(转写) / cutout(抠图) / sysinfo(探机) / echo(联调)。
"""
import base64
import json
import os
import threading
import time
import uuid

_DIR = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(_DIR, "finetune", "dist")
os.makedirs(STORE, exist_ok=True)

_LOCK = threading.Lock()
_TASKS = {}      # task_id -> task dict
_QUEUE = []      # 待领取 task_id（FIFO）
_WORKERS = {}    # worker_id -> info
_RESULTS = {}    # task_id -> result（完成近 200 条内存缓存，同时落盘）

TASK_TYPES = {"whisper", "cutout", "sysinfo", "echo"}
MAX_PAYLOAD = 40 * 1024 * 1024
WORKER_TTL = 90          # 90s 无心跳视为离线
RESULT_KEEP = 200


def _token():
    import config as _cfg
    try:
        t = str(_cfg.get("dist.token") or "")
        if not t:
            t = uuid.uuid4().hex[:16]
            _cfg.set_value("dist.token", t)
        return t
    except Exception:
        p = os.path.join(STORE, "token.txt")
        if os.path.exists(p):
            return open(p).read().strip()
        t = uuid.uuid4().hex[:16]
        open(p, "w").write(t)
        return t


def _check_token(d):
    return str(d.get("token", "")) == _token()


def submit(task_type, payload: dict) -> str:
    """主节点内部/HTTP 提交任务，返回 task_id。"""
    if task_type not in TASK_TYPES:
        raise ValueError(f"未知任务类型 {task_type}")
    tid = uuid.uuid4().hex[:12]
    with _LOCK:
        _TASKS[tid] = {"id": tid, "type": task_type, "payload": payload,
                       "status": "queued", "worker": None, "created": time.time(),
                       "done_at": 0}
        _QUEUE.append(tid)
    return tid


def wait_result(tid, timeout=600):
    """阻塞等结果（供内部调用，如转写分发）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        with _LOCK:
            r = _RESULTS.get(tid)
            if r is not None:
                return r
            t = _TASKS.get(tid)
            if t and t["status"] == "failed":
                return {"ok": False, "error": t.get("error", "failed")}
        time.sleep(0.6)
    return {"ok": False, "error": "timeout"}


def register(d, ip):
    if not _check_token(d):
        return {"ok": False, "error": "token 错误"}
    wid = str(d.get("worker_id") or "") or uuid.uuid4().hex[:10]
    caps = [c for c in (d.get("caps") or []) if isinstance(c, str)][:20]
    with _LOCK:
        _WORKERS[wid] = {"id": wid, "name": str(d.get("name") or wid)[:40],
                         "caps": caps, "ip": ip, "last_seen": time.time(),
                         "done": int((_WORKERS.get(wid) or {}).get("done", 0)),
                         "platform": str(d.get("platform") or "")[:80]}
    return {"ok": True, "worker_id": wid}


def heartbeat(d, ip):
    if not _check_token(d):
        return {"ok": False, "error": "token 错误"}
    wid = str(d.get("worker_id") or "")
    with _LOCK:
        w = _WORKERS.get(wid)
        if w:
            w["last_seen"] = time.time(); w["ip"] = ip
    return {"ok": True, "server_time": time.time()}


def claim(d, ip, wait_sec=25):
    """worker 长轮询领任务。"""
    if not _check_token(d):
        return {"ok": False, "error": "token 错误"}
    wid = str(d.get("worker_id") or "")
    caps = set(d.get("caps") or [])
    with _LOCK:
        w = _WORKERS.get(wid)
        if w:
            w["last_seen"] = time.time(); w["ip"] = ip
            if caps:
                w["caps"] = list(caps)
    t0 = time.time()
    while time.time() - t0 < wait_sec:
        with _LOCK:
            for tid in list(_QUEUE):
                t = _TASKS.get(tid)
                if not t or t["status"] != "queued":
                    _QUEUE.remove(tid); continue
                # sysinfo/echo 任何 worker 可领；whisper/cutout 需要能力声明
                if t["type"] in ("whisper", "cutout") and t["type"] not in caps:
                    continue
                t["status"] = "running"; t["worker"] = wid
                _QUEUE.remove(tid)
                return {"ok": True, "task": {"id": tid, "type": t["type"],
                                             "payload": t["payload"]}}
        time.sleep(0.8)
    return {"ok": True, "task": None}


def report(d, ip):
    if not _check_token(d):
        return {"ok": False, "error": "token 错误"}
    tid = str(d.get("task_id") or "")
    wid = str(d.get("worker_id") or "")
    with _LOCK:
        t = _TASKS.get(tid)
        if not t:
            return {"ok": False, "error": "任务不存在"}
        res = d.get("result") or {}
        t["status"] = "done" if d.get("ok") else "failed"
        t["done_at"] = time.time()
        if not d.get("ok"):
            t["error"] = str(res.get("error") or "worker 执行失败")[:300]
        _RESULTS[tid] = {"ok": bool(d.get("ok")), "result": res}
        w = _WORKERS.get(wid)
        if w:
            w["done"] = int(w.get("done", 0)) + 1
            w["last_seen"] = time.time()
        # 落盘 + 内存上限
        try:
            with open(os.path.join(STORE, f"result_{tid}.json"), "w", encoding="utf-8") as f:
                json.dump(_RESULTS[tid], f, ensure_ascii=False)
        except Exception:
            pass
        if len(_RESULTS) > RESULT_KEEP:
            for k in sorted(_RESULTS, key=lambda x: _TASKS.get(x, {}).get("done_at", 0))[:50]:
                _RESULTS.pop(k, None)
    return {"ok": True}


def task_result(tid):
    with _LOCK:
        r = _RESULTS.get(tid)
        if r is not None:
            return r
        t = _TASKS.get(tid)
        if not t:
            return {"ok": False, "error": "任务不存在"}
        return {"ok": True, "pending": True, "status": t["status"]}


def status():
    now = time.time()
    with _LOCK:
        ws = []
        for w in _WORKERS.values():
            ws.append({**w, "online": (now - w["last_seen"]) < WORKER_TTL})
        ws.sort(key=lambda x: (not x["online"], x["name"]))
        q = sum(1 for t in _TASKS.values() if t["status"] == "queued")
        rn = sum(1 for t in _TASKS.values() if t["status"] == "running")
        done = sum(1 for t in _TASKS.values() if t["status"] == "done")
    return {"ok": True, "workers": ws, "queue": q, "running": rn, "done": done,
            "token": _token()}


def gpu_info():
    """本机 GPU/torch 状态（训练中心显示用）。"""
    info = {"nvidia_smi": False, "gpu": "", "vram_mb": 0,
            "sys_torch_cuda": None, "venv_torch_cuda": None}
    try:
        import subprocess as sp
        r = sp.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                   capture_output=True, text=True, timeout=8)
        if r.returncode == 0 and r.stdout.strip():
            parts = [x.strip() for x in r.stdout.strip().splitlines()[0].split(",")]
            info["nvidia_smi"] = True
            info["gpu"] = parts[0]
            info["vram_mb"] = int(parts[1].replace("MiB", "").strip()) if len(parts) > 1 else 0
    except Exception:
        pass

    def _torch_cuda(py):
        try:
            import subprocess as sp
            r = sp.run([py, "-c", "import torch;print(torch.cuda.is_available())"],
                       capture_output=True, text=True, timeout=30)
            return r.stdout.strip() == "True"
        except Exception:
            return None
    import sys
    info["sys_torch_cuda"] = _torch_cuda(sys.executable)
    venv_py = os.path.join(_DIR, "train_venv", "Scripts", "python.exe")
    info["venv_torch_cuda"] = _torch_cuda(venv_py) if os.path.exists(venv_py) else None
    return info
