# -*- coding: utf-8 -*-
"""本地视觉能力客户端（OCR + SmolVLM-500M 画面描述）。

对外只暴露 ocr() / caption()，内部维护一个常驻子进程 kb_worker.py。
模型 500M 是 CPU 全盘索引的折中：单图 2~5 秒、约 1GB 权重，
2B 级别模型（4.1GB、单图 5~8 分钟）只适合按需理解，不适合批量入库。
"""
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
WORKER = str(BASE_DIR / "kb_worker.py")
MODEL_DIR = Path(os.environ.get("WASILY_VLM_DIR")
                 or (BASE_DIR / "_model_cache" / "SmolVLM-500M-Instruct"))
REPO_ID = "HuggingFaceTB/SmolVLM-500M-Instruct"

_LOCK = threading.RLock()
_PROC = None
_QUEUE = None
_CAPTION_OFF = False          # 连续失败后本进程内不再尝试，避免每张图都等超时
_DL_LOCK = threading.Lock()
_DL_STATE = {"running": False, "progress": 0, "error": "", "done": False}


def model_ready():
    return (MODEL_DIR / "config.json").exists() and any(
        MODEL_DIR.glob("*.safetensors"))


def model_info():
    size = 0
    if MODEL_DIR.exists():
        for p in MODEL_DIR.rglob("*"):
            if p.is_file():
                size += p.stat().st_size
    return {"dir": str(MODEL_DIR), "ready": model_ready(),
            "size_mb": round(size / 1048576, 1), "repo": REPO_ID,
            "downloading": _DL_STATE["running"], "error": _DL_STATE["error"]}


def _download_files():
    """直连镜像逐文件下载：huggingface_hub 的 HEAD 探测在本机被 403，
    但 resolve 直链 GET 正常，所以这里自己实现带断点续传的下载。"""
    import urllib.request
    base = "https://hf-mirror.com/%s/resolve/main/" % REPO_ID
    files = ["config.json", "generation_config.json", "preprocessor_config.json",
             "processor_config.json", "special_tokens_map.json", "tokenizer.json",
             "tokenizer_config.json", "added_tokens.json", "chat_template.json",
             "vocab.json", "merges.txt", "model.safetensors"]
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    total = len(files)
    for idx, name in enumerate(files, 1):
        dest = MODEL_DIR / name
        if dest.exists() and dest.stat().st_size > 0:
            _DL_STATE["progress"] = int(idx * 100 / total)
            continue
        part = dest.with_suffix(dest.suffix + ".part")
        have = part.stat().st_size if part.exists() else 0
        headers = {"User-Agent": "Mozilla/5.0"}
        if have:
            headers["Range"] = "bytes=%d-" % have
        req = urllib.request.Request(base + name, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as r:
            append = bool(have) and r.status == 206   # 服务端不支持续传就重下
            with open(part, "ab" if append else "wb") as f:
                while True:
                    buf = r.read(1 << 20)
                    if not buf:
                        break
                    f.write(buf)
        os.replace(part, dest)
        _DL_STATE["progress"] = int(idx * 100 / total)


def ensure_model(allow_download=True):
    """确保 SmolVLM 权重就位；返回 (ok, message)。"""
    if model_ready():
        return True, "已就绪"
    if not allow_download:
        return False, "未下载"
    with _DL_LOCK:
        if _DL_STATE["running"]:
            return False, "下载中"
        free_gb = shutil.disk_usage(str(BASE_DIR)).free / (1024 ** 3)
        if free_gb < 1.3:
            msg = "磁盘剩余 %.1fGB，不足 1.3GB，无法下载视觉模型" % free_gb
            _DL_STATE["error"] = msg
            return False, msg
        try:
            _download_files()
            return (True, "下载完成") if model_ready() else (False, "下载后校验失败")
        except Exception as exc:  # noqa: BLE001
            msg = "%s: %s" % (type(exc).__name__, exc)
            _DL_STATE["error"] = msg
            return False, msg


def download_async():
    if _DL_STATE["running"] or model_ready():
        return False
    _DL_STATE.update(running=True, error="", progress=0)

    def _run():
        try:
            ensure_model(True)
        finally:
            _DL_STATE["running"] = False
    threading.Thread(target=_run, name="kb-vlm-download", daemon=True).start()
    return True


def _alive():
    return _PROC is not None and _PROC.poll() is None


def _kill():
    global _PROC, _QUEUE
    p = _PROC
    _PROC, _QUEUE = None, None
    if p is not None and p.poll() is None:
        try:
            p.kill()
        except Exception:  # noqa: BLE001
            pass


def _spawn():
    global _PROC, _QUEUE
    _kill()
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", WORKER, str(MODEL_DIR)],
        cwd=str(BASE_DIR), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    q = queue.Queue()

    def _pump():
        try:
            for line in proc.stdout:
                q.put(line)
        except Exception:  # noqa: BLE001
            pass
        q.put(None)
    threading.Thread(target=_pump, name="kb-worker-pump", daemon=True).start()
    _PROC, _QUEUE = proc, q
    try:
        line = q.get(timeout=30)
        if not line:
            raise RuntimeError("视觉子进程启动即退出")
    except queue.Empty:
        raise RuntimeError("视觉子进程启动超时")
    return proc


def _call(cmd, path, timeout):
    with _LOCK:
        for attempt in (0, 1):
            try:
                if not _alive():
                    _spawn()
                req = json.dumps({"cmd": cmd, "path": path}, ensure_ascii=False)
                _PROC.stdin.write(req + "\n")
                _PROC.stdin.flush()
                line = _QUEUE.get(timeout=timeout)
                if line is None:
                    raise RuntimeError("视觉子进程已退出(rc=%s)" % _PROC.poll())
                return json.loads(line)
            except Exception as exc:  # noqa: BLE001
                _kill()
                if attempt:
                    return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    return {"ok": False, "error": "未知错误"}


def ocr(path, timeout=180):
    res = _call("ocr", path, timeout)
    return (res.get("text") or "").strip(), (res.get("error") or "")


def caption(path, timeout=300):
    global _CAPTION_OFF
    if _CAPTION_OFF or not model_ready():
        return "", "视觉模型未就绪"
    res = _call("caption", path, timeout)
    if res.get("ok"):
        return (res.get("text") or "").strip(), ""
    # 连续失败（例如权重损坏）就本次运行不再尝试，索引继续用 OCR 兜底
    _CAPTION_OFF = True
    return "", res.get("error") or "视觉描述失败"


def close():
    _kill()


def status():
    return {"worker_alive": _alive(), "model": model_info(),
            "caption_disabled": _CAPTION_OFF}
