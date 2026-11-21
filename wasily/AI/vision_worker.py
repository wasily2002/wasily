# -*- coding: utf-8 -*-
"""moondream2 视觉理解子进程（llama.cpp / moondream 架构）

独立进程运行，避免与 7B 主模型抢内存/显存；调用方 AI/vision.py 负责拉起与解析。
用法（由 vision.py 调用，不直接给用户）：
    python vision_worker.py --image <path> --prompt <text> [--device auto|cpu|gpu] [--layers N]
输出：一行 JSON 到 stdout
    {"ok":true,"text":"...","load_s":..,"infer_s":..,"device":"cpu|cuda","vram_mb":..}
失败：{"ok":false,"error":{"code":"...","message":".."}}
"""
import json
import os
import subprocess
import sys
import time
import base64

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# clip 加载器不接受中文路径：模型固定放在 ASCII 目录，其次才回退项目内目录
MODEL_DIRS = [r"D:\wasily_models\moondream2", os.path.join(BASE_DIR, "models", "moondream2")]
MMPROJ_NAME = "moondream2-mmproj-f16.gguf"
TEXT_NAMES = ["moondream2-text-model-q4_k_m.gguf", "moondream2-text-model-f16.gguf"]


def _dir():
    for d in MODEL_DIRS:
        if os.path.isdir(d):
            return d
    return MODEL_DIRS[0]


def pick_text_model():
    d = _dir()
    for n in TEXT_NAMES:
        p = os.path.join(d, n)
        if os.path.isfile(p) and os.path.getsize(p) > 100 * 1024 * 1024:
            return p
    return ""


def vram_mb():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.free",
                              "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=8).stdout.strip()
        u, f = [int(x) for x in out.split(",")]
        return u, f
    except Exception:
        return -1, -1


def image_data_uri(path):
    ext = os.path.splitext(path)[1].lower().lstrip(".") or "png"
    mime = {"jpg": "jpeg", "jpeg": "jpeg", "webp": "webp", "bmp": "bmp",
            "tif": "tiff", "tiff": "tiff"}.get(ext, "png")
    with open(path, "rb") as f:
        blob = f.read()
    return "data:image/%s;base64,%s" % (mime, base64.b64encode(blob).decode())


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--prompt", default="Describe this image in one short paragraph.")
    ap.add_argument("--device", default="auto")
    ap.add_argument("--layers", type=int, default=-999)
    ap.add_argument("--max-tokens", type=int, default=180)
    a = ap.parse_args()

    if not os.path.isfile(a.image):
        print(json.dumps({"ok": False, "error": {"code": "IMAGE_NOT_FOUND",
                                                  "message": "图片不存在: " + a.image}},
                         ensure_ascii=False))
        return 1
    text_model = pick_text_model()
    mmproj = os.path.join(_dir(), MMPROJ_NAME)
    missing = []
    if not text_model:
        missing.append("文本模型（%s 之一）" % " / ".join(TEXT_NAMES))
    if not (os.path.isfile(mmproj) and os.path.getsize(mmproj) > 100 * 1024 * 1024):
        missing.append(MMPROJ_NAME)
    if missing:
        print(json.dumps({"ok": False, "error": {
            "code": "VISION_MODEL_MISSING",
            "message": "视觉模型未安装，缺少：" + "、".join(missing),
            "model_dir": MODEL_DIR,
            "install": "把 moondream2-mmproj-f16.gguf 与 moondream2-text-model-q4_k_m.gguf "
                       "放到该目录（来源 moondream/moondream2-gguf）"}}, ensure_ascii=False))
        return 1

    # 设备决策：显式 cpu/gpu；auto 时按空闲显存判断（文本 Q4≈0.9GB + mmproj≈0.85GB）
    dev = str(a.device or "auto").lower()
    layers = a.layers
    if layers == -999:
        if dev == "cpu":
            layers = 0
        elif dev == "gpu":
            layers = -1          # 全部层卸载
        else:
            _, free = vram_mb()
            layers = -1 if free >= 1900 else 0
            dev = "cuda" if layers == -1 else "cpu"
    else:
        dev = "cuda" if layers > 0 else "cpu"

    t0 = time.time()
    try:
        from llama_cpp import Llama
        from llama_cpp.llama_chat_format import MoondreamChatHandler
        handler = MoondreamChatHandler(clip_model_path=mmproj, verbose=False)
        llm = Llama(model_path=text_model, chat_handler=handler, n_ctx=1024,
                    n_threads=max(4, (os.cpu_count() or 4)), n_gpu_layers=layers,
                    verbose=False, logits_all=False)
    except Exception as e:
        print(json.dumps({"ok": False, "error": {
            "code": "VISION_LOAD_FAILED", "message": "视觉模型加载失败: %s" % e}},
            ensure_ascii=False))
        return 1
    load_s = time.time() - t0

    t1 = time.time()
    try:
        resp = llm.create_chat_completion(
            messages=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": image_data_uri(a.image)}},
                {"type": "text", "text": a.prompt},
            ]}],
            max_tokens=int(a.max_tokens), temperature=0.2, top_p=0.9,
        )
        text = (resp["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        print(json.dumps({"ok": False, "error": {
            "code": "VISION_INFER_FAILED", "message": "视觉推理失败: %s" % e}},
            ensure_ascii=False))
        return 1
    infer_s = time.time() - t1
    vused, _vf = vram_mb()
    try:
        import psutil
        rss_gb = round(psutil.Process().memory_info().rss / 1024 ** 3, 2)
    except Exception:
        rss_gb = -1
    print(json.dumps({"ok": True, "text": text, "model": os.path.basename(text_model),
                      "device": dev, "layers": layers,
                      "load_s": round(load_s, 2), "infer_s": round(infer_s, 2),
                      "rss_gb": rss_gb, "vram_used_mb": vused}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
