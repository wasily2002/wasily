# -*- coding: utf-8 -*-
"""本地视觉理解客户端：默认 Qwen2-VL-2B-Instruct（CPU），可一键回退旧 moondream2。

后端（config vision.backend）：
  qwenvl       Qwen2-VL-2B-Instruct，transformers + torch CPU（默认）
               - 权重 4.1GB，bf16；本机 2GB 显存塞不下，固定纯 CPU、子进程运行
               - 跑完进程退出，显存一字节不占 → 不会挤掉 GPU 上的 7B 主模型
  moondream2   旧模型（llama.cpp + mmproj），仅作回退/对比保留
               - 只认 ASCII 路径；CUDA 构建在纯 CPU 下会崩，所以只能走 GPU，
                 会和 7B 抢显存（实测 7B 首答 64s → 184s），不建议默认启用

设备策略（vision.device）：
  auto（默认）→ qwenvl 永远 cpu；moondream2 仅在空闲显存 ≥1900MB 时用 gpu
  cpu          → 强制 CPU（qwenvl 支持；moondream2 会失败，见上）
  gpu          → 仅 moondream2 有意义，显式开启才抢显存

超时（vision.timeout_s，默认 900 秒）：CPU 视觉单图实测 5–8 分钟，偏慢属正常。
"""
import json
import os
import subprocess
import sys
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORKER_QWENVL = os.path.join(BASE_DIR, "vision_worker_qwenvl.py")
WORKER_MOONDREAM = os.path.join(BASE_DIR, "vision_worker.py")

QWENVL_DIRS = [r"D:\wasily_models\qwen2_vl_2b",
               os.path.join(BASE_DIR, "models", "qwen2_vl_2b")]
QWENVL_NAME = "Qwen2-VL-2B-Instruct"

DEFAULT_DIR = r"D:\wasily_models\moondream2"
FALLBACK_DIR = os.path.join(BASE_DIR, "models", "moondream2")
TEXT_NAMES = ["moondream2-text-model-q4_k_m.gguf", "moondream2-text-model-f16.gguf"]
MMPROJ_NAME = "moondream2-mmproj-f16.gguf"

STRUCT_PROMPT = (
    "Describe this screenshot in at most 4 sentences: what application or window it is, "
    "the main panels or controls, and any error, warning or unusual detail. "
    "Do not repeat text verbatim for more than one line."
)

_CACHE = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 8


def _cfg():
    try:
        import config
        return config
    except Exception:
        return None


def _cfg_get(key, default=None):
    c = _cfg()
    try:
        return c.get(key, default) if c is not None else default
    except Exception:
        return default


def backend():
    b = str(_cfg_get("vision.backend", "qwenvl") or "qwenvl").lower()
    return b if b in ("qwenvl", "moondream2") else "qwenvl"


def model_dir(bk=None):
    bk = bk or backend()
    if bk == "qwenvl":
        d = str(_cfg_get("vision.model_dir", "") or "")
        if d and os.path.isdir(d):
            return d
        for p in QWENVL_DIRS:
            if os.path.isdir(p):
                return p
        return QWENVL_DIRS[0]
    d = str(_cfg_get("vision.model_dir", "") or "")
    if d and os.path.isdir(d):
        return d
    if os.path.isdir(DEFAULT_DIR):
        return DEFAULT_DIR
    return FALLBACK_DIR


def _qwenvl_weights_bytes(d):
    total = 0
    try:
        for n in os.listdir(d):
            if n.endswith(".safetensors"):
                total += os.path.getsize(os.path.join(d, n))
    except Exception:
        pass
    return total


def text_model_path():
    for n in TEXT_NAMES:
        p = os.path.join(model_dir("moondream2"), n)
        if os.path.isfile(p) and os.path.getsize(p) > 100 * 1024 * 1024:
            return p
    return ""


def mmproj_path():
    p = os.path.join(model_dir("moondream2"), MMPROJ_NAME)
    if os.path.isfile(p) and os.path.getsize(p) > 100 * 1024 * 1024:
        return p
    return ""


def installed():
    if backend() == "qwenvl":
        d = model_dir("qwenvl")
        return (os.path.isfile(os.path.join(d, "config.json"))
                and os.path.isfile(os.path.join(d, "preprocessor_config.json"))
                and _qwenvl_weights_bytes(d) > 1024 ** 3)
    return bool(text_model_path() and mmproj_path())


def status():
    bk = backend()
    d = model_dir(bk)
    if bk == "qwenvl":
        ok = installed()
        size = _qwenvl_weights_bytes(d)
        return {
            "backend": bk,
            "model": QWENVL_NAME,
            "installed": ok,
            "model_dir": d,
            "disk_bytes": size,
            "disk_mb": round(size / 1024 / 1024, 1),
            "device": "cpu",
            "missing": [] if ok else ["config.json / preprocessor_config.json / *.safetensors"],
            "install_hint": "" if ok else (
                "在 hf-mirror.com/Qwen/Qwen2-VL-2B-Instruct 下载权重与配置文件到 %s" % d),
            "note": "CPU 推理，单图实测约 5–8 分钟；独立子进程，不占用显存",
            "fallback": "失败时 read_image 自动退回纯 OCR",
        }
    tm, mp = text_model_path(), mmproj_path()
    size = 0
    for p in (tm, mp):
        if p:
            size += os.path.getsize(p)
    return {
        "backend": bk,
        "model": "moondream2 (llama.cpp mmproj)",
        "installed": bool(tm and mp),
        "model_dir": d,
        "text_model": os.path.basename(tm) if tm else None,
        "mmproj": os.path.basename(mp) if mp else None,
        "disk_bytes": size,
        "disk_mb": round(size / 1024 / 1024, 1),
        "device": str(_cfg_get("vision.device", "auto") or "auto"),
        "missing": [n for n, p in ((TEXT_NAMES[0], tm), (MMPROJ_NAME, mp)) if not p],
        "install_hint": "" if (tm and mp) else (
            "在 hf-mirror.com/moondream/moondream2-gguf 下载 %s 与 %s 放到 %s"
            % (TEXT_NAMES[0], MMPROJ_NAME, d)),
        "note": "旧模型：截图主体/文字经常编造与复读，不建议默认启用",
        "fallback": "失败时 read_image 自动退回纯 OCR",
    }


def _device():
    bk = backend()
    if bk == "qwenvl":
        return "cpu"                       # 显存留给 7B，视觉永远不碰显卡
    dev = str(_cfg_get("vision.device", "auto") or "auto").lower()
    if dev == "auto":
        # 旧模型在 CUDA 构建下纯 CPU 会崩，只能 GPU；但空闲显存不足时会挤掉 7B
        return "gpu" if _free_vram_mb() >= 1900 else "cpu"
    return dev


def _timeout():
    try:
        return int(_cfg_get("vision.timeout_s", 900) or 900)
    except Exception:
        return 900


def describe(path, prompt=None, timeout=None, device=None):
    """返回 {ok, text, elapsed_s, device, model, load_s, infer_s, error}；失败不抛异常。"""
    bk = backend()
    st = status()
    if not st["installed"]:
        return {"ok": False, "backend": bk,
                "error": "视觉模型未安装，缺少：" + "、".join(st["missing"]),
                "install_hint": st["install_hint"], "device": "none"}

    dev = str(device or _device()).lower()
    if bk == "qwenvl":
        dev = "cpu"
    interpreter = sys.executable
    prm = prompt or STRUCT_PROMPT
    tmo = int(timeout or _timeout())

    try:
        mtime = os.path.getmtime(path)
    except Exception:
        mtime = 0
    key = (os.path.normcase(os.path.abspath(path)), mtime, prm, dev, bk)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit:
        return dict(hit, cached=True)

    if bk == "qwenvl":
        cmd = [interpreter, WORKER_QWENVL, "--image", path, "--prompt", prm,
               "--max-tokens", str(int(_cfg_get("vision.max_tokens", 96) or 96))]
        mp = int(_cfg_get("vision.max_pixels", 0) or 0)
        if mp:
            cmd += ["--max-pixels", str(mp)]
    else:
        if dev == "cpu":
            return {"ok": False, "backend": bk, "device": "cpu",
                    "error": "旧 moondream2 在 CUDA 构建下纯 CPU 推理会崩溃（0xC0000005），"
                             "需 GPU；请改用 vision.backend=qwenvl"}
        cmd = [interpreter, WORKER_MOONDREAM, "--image", path, "--prompt", prm,
               "--device", "gpu"]

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["HF_HUB_OFFLINE"] = "1"           # 权重已落盘，别联网检查
    if dev == "cpu":
        env["CUDA_VISIBLE_DEVICES"] = "-1"  # 双保险：视觉进程绝对不碰显卡
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=tmo,
                           encoding="utf-8", errors="replace", env=env, cwd=BASE_DIR)
        out = (r.stdout or "").strip().splitlines()
        data = {}
        for line in reversed(out):
            line = line.strip()
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                    break
                except Exception:
                    continue
        if not data:
            err = (r.stderr or "").strip()[-200:] or "视觉子进程无输出"
            return {"ok": False, "error": err, "device": dev, "backend": bk, "elapsed_s": None}
        if not data.get("ok"):
            return {"ok": False, "backend": bk,
                    "error": (data.get("error") or {}).get("message", "视觉推理失败"),
                    "device": data.get("device", dev), "elapsed_s": None}
        res = {"ok": True, "backend": bk, "text": data.get("text", ""),
               "device": data.get("device", dev),
               "load_s": data.get("load_s"), "infer_s": data.get("infer_s"),
               "elapsed_s": round((data.get("load_s") or 0) + (data.get("infer_s") or 0), 1),
               "peak_vram_mb": data.get("vram_used_mb"),
               "rss_gb": data.get("rss_gb"),
               "in_tokens": data.get("in_tokens"),
               "out_tokens": data.get("out_tokens"),
               "tok_s": data.get("tok_s"),
               "model": data.get("model")}
        with _CACHE_LOCK:
            if len(_CACHE) >= _CACHE_MAX:
                _CACHE.pop(next(iter(_CACHE)))
            _CACHE[key] = res
        return res
    except subprocess.TimeoutExpired:
        return {"ok": False, "backend": bk, "device": dev,
                "error": "视觉推理超时（>%ss）；CPU 视觉单图约 5–8 分钟，"
                         "可调大 vision.timeout_s" % tmo}
    except Exception as e:
        return {"ok": False, "backend": bk, "device": dev, "error": "视觉推理异常: %s" % e}


def parse_sections(text):
    """字段化：首句→overview；含 error/warning 等词→anomalies；其余→key_elements。
    模型按 SCENE/PARTS/NOTES 输出时优先用该结构。"""
    out = {"overview": "", "key_elements": "", "anomalies": ""}
    if not text:
        return out
    keys = {}
    for raw in text.replace("\r", "\n").split("\n"):
        line = raw.strip().lstrip("-•* ").strip()
        up = line.upper()
        for k, tag in (("overview", "SCENE:"), ("key_elements", "PARTS:"),
                       ("anomalies", "NOTES:")):
            if up.startswith(tag):
                keys[k] = line.split(":", 1)[-1].strip()
    if keys:
        out.update({k: v for k, v in keys.items() if v})
        if not out["overview"]:
            out["overview"] = text.strip()[:600]
        return out
    bad = ("error", "warning", "fail", "exception", "错误", "失败", "异常", "警告")
    parts = []
    for seg in text.replace("\n", " ").replace("！", "。").replace("!", "。").split("。"):
        seg = seg.strip()
        if seg:
            parts.append(seg)
    if not parts:
        parts = [text.strip()[:600]]
    out["overview"] = parts[0][:600]
    rest, bads = [], []
    for seg in parts[1:]:
        (bads if any(b in seg.lower() for b in bad) else rest).append(seg)
    out["key_elements"] = "；".join(rest)[:600]
    out["anomalies"] = "；".join(bads)[:300]
    return out


def _free_vram_mb():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free",
                              "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=8).stdout.strip()
        return int(out.splitlines()[0])
    except Exception:
        return 0
