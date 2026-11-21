# -*- coding: utf-8 -*-
"""Qwen2-VL-2B-Instruct 视觉理解子进程（transformers + torch CPU）

为什么走 CPU：
- 本机 2GB 显存要留给 7B 主模型；Qwen2-VL-2B 的 bf16 权重约 4.1GB，塞不进 2GB，
  一旦走 GPU 就会把 7B 的页挤出去（WDDM 换页），下一次对话要重新换页（实测 64s→184s）。
- 因此固定纯 CPU、独立进程：跑完进程退出，内存全部归还，显存一字节不占。

用法（由 vision.py 调用，不直接给用户）：
    python vision_worker_qwenvl.py --image <path> --prompt <text> [--max-tokens 96] [--max-pixels N]
输出：一行 JSON 到 stdout
    {"ok":true,"text":..,"model":"Qwen2-VL-2B-Instruct","device":"cpu",
     "load_s":..,"infer_s":..,"in_tokens":..,"out_tokens":..,"tok_s":..,"rss_gb":..,"vram_used_mb":..}
失败：{"ok":false,"error":{"code":"..","message":".."}}
"""
import argparse
import json
import os
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DIRS = [r"D:\wasily_models\qwen2_vl_2b",
                os.path.join(BASE_DIR, "models", "qwen2_vl_2b")]
MIN_WEIGHT_BYTES = 1 * 1024 ** 3


def model_dir():
    for d in DEFAULT_DIRS:
        if os.path.isdir(d) and os.path.isfile(os.path.join(d, "config.json")):
            return d
    return DEFAULT_DIRS[0]


def weights_bytes(d):
    total = 0
    try:
        for n in os.listdir(d):
            if n.endswith(".safetensors"):
                total += os.path.getsize(os.path.join(d, n))
    except Exception:
        pass
    return total


def vram():
    try:
        o = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.free",
                            "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=8).stdout.strip()
        u, _f = [int(x) for x in o.split(",")]
        return u
    except Exception:
        return -1


def fail(code, msg):
    print(json.dumps({"ok": False, "error": {"code": code, "message": msg}},
                     ensure_ascii=False))
    return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--max-tokens", type=int, default=96)
    ap.add_argument("--max-pixels", type=int, default=0)
    ap.add_argument("--threads", type=int, default=max(4, (os.cpu_count() or 4)))
    a = ap.parse_args()

    if not os.path.isfile(a.image):
        return fail("IMAGE_NOT_FOUND", "图片不存在: " + a.image)
    d = model_dir()
    if weights_bytes(d) < MIN_WEIGHT_BYTES:
        return fail("VISION_MODEL_MISSING",
                    "Qwen2-VL-2B-Instruct 权重未安装或体积异常（目录 %s）。"
                    "从 hf-mirror.com/Qwen/Qwen2-VL-2B-Instruct 下载 config.json、"
                    "preprocessor_config.json、tokenizer*、*.safetensors 放入该目录" % d)

    try:
        import torch
        from PIL import Image
        from transformers import AutoProcessor, Qwen2VLForConditionalGeneration
    except Exception as e:
        return fail("VISION_DEP_MISSING", "缺少 transformers/torch/Pillow: %s" % e)

    torch.set_num_threads(int(a.threads))
    t0 = time.time()
    try:
        proc = AutoProcessor.from_pretrained(d)
        if a.max_pixels:
            try:
                proc.image_processor.max_pixels = int(a.max_pixels)
            except Exception:
                pass
        model = Qwen2VLForConditionalGeneration.from_pretrained(
            d, dtype=torch.bfloat16, low_cpu_mem_usage=True, attn_implementation="sdpa")
        model.eval()
    except Exception as e:
        return fail("VISION_LOAD_FAILED", "视觉模型加载失败: %s" % e)
    load_s = time.time() - t0

    try:
        img = Image.open(a.image).convert("RGB")
        msgs = [{"role": "user", "content": [{"type": "image", "image": img},
                                             {"type": "text", "text": a.prompt}]}]
        text = proc.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
        inputs = proc(text=[text], images=[img], return_tensors="pt")
        n_in = int(inputs["input_ids"].shape[1])
        t1 = time.time()
        with torch.inference_mode():
            gen = model.generate(**inputs, max_new_tokens=int(a.max_tokens), do_sample=False,
                                 temperature=None, top_p=None, top_k=None)
        infer_s = time.time() - t1
        n_out = int(gen.shape[1]) - n_in
        out = proc.batch_decode(gen[:, n_in:], skip_special_tokens=True)[0].strip()
    except Exception as e:
        return fail("VISION_INFER_FAILED", "视觉推理失败: %s" % e)

    try:
        import psutil
        rss = round(psutil.Process().memory_info().rss / 1024 ** 3, 2)
    except Exception:
        rss = -1
    print(json.dumps({"ok": True, "text": out, "model": "Qwen2-VL-2B-Instruct",
                      "device": "cpu", "load_s": round(load_s, 1),
                      "infer_s": round(infer_s, 1), "in_tokens": n_in,
                      "out_tokens": n_out, "tok_s": round(n_out / infer_s, 2) if infer_s else 0,
                      "rss_gb": rss, "vram_used_mb": vram(), "model_dir": d},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
