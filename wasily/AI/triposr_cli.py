# -*- coding: utf-8 -*-
"""TripoSR 图生三维子进程入口。

权重和推理都放在子进程，避免占满 MCP 服务进程内存。
用法：python triposr_cli.py <图片> --model-dir <权重目录> --output <GLB路径>
最后一行始终输出 JSON，便于调用方解析。
"""
import argparse
import json
import os
import sys
import time
import traceback

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TRIPOSR_ROOT = os.path.join(BASE_DIR, "vendor", "TripoSR")
VENV_SITE = os.path.join(BASE_DIR, "triposr_venv", "Lib", "site-packages")
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
# 本机 Python 根目录也放了一套旧 transformers，必须让隔离环境优先。
sys.path.insert(0, VENV_SITE)
sys.path.insert(0, TRIPOSR_ROOT)

import numpy as np  # noqa: E402
import torch  # noqa: E402
from PIL import Image  # noqa: E402
from tsr.system import TSR  # noqa: E402
from tsr.utils import remove_background, resize_foreground  # noqa: E402


def _prepare_image(image_path, output_path, no_remove_bg=False):
    """按 TripoSR 官方预处理把前景放到灰色背景上。"""
    source = Image.open(image_path)
    if no_remove_bg:
        image = source.convert("RGB")
    else:
        # 已有透明通道时不再跑 rembg，省一次模型下载和内存占用。
        session = None
        if source.mode != "RGBA" or source.getextrema()[3][0] == 255:
            import rembg
            session = rembg.new_session()
        image = remove_background(source, session)
        image = resize_foreground(image, 0.85)
        pixels = np.asarray(image).astype(np.float32) / 255.0
        pixels = (pixels[:, :, :3] * pixels[:, :, 3:4]
                  + (1 - pixels[:, :, 3:4]) * 0.5)
        image = Image.fromarray((pixels * 255.0).astype(np.uint8))
    preprocessed = os.path.splitext(output_path)[0] + "_input.png"
    image.save(preprocessed)
    return image, preprocessed


def run(args):
    if not os.path.isfile(args.image):
        raise ValueError("图片不存在：%s" % args.image)
    for filename in ("config.yaml", "model.ckpt"):
        path = os.path.join(args.model_dir, filename)
        if not os.path.isfile(path):
            raise ValueError("TripoSR 权重不完整，缺少：%s" % path)
    if not os.path.isfile(os.path.join(TRIPOSR_ROOT, "tsr", "system.py")):
        raise ValueError("TripoSR 代码不存在：%s" % TRIPOSR_ROOT)
    if not 32 <= args.resolution <= 512:
        raise ValueError("resolution 应在 32 到 512 之间")

    out_dir = os.path.dirname(os.path.abspath(args.output))
    os.makedirs(out_dir, exist_ok=True)
    torch.set_num_threads(max(1, min(8, os.cpu_count() or 1)))
    started = time.perf_counter()

    image, preprocessed = _prepare_image(
        args.image, args.output, no_remove_bg=args.no_remove_bg)

    load_started = time.perf_counter()
    model = TSR.from_pretrained(
        args.model_dir, config_name="config.yaml", weight_name="model.ckpt")
    model.renderer.set_chunk_size(args.chunk_size)
    model.to("cpu")
    model.eval()
    load_seconds = time.perf_counter() - load_started

    infer_started = time.perf_counter()
    with torch.inference_mode():
        scene_codes = model([image], device="cpu")
    infer_seconds = time.perf_counter() - infer_started

    mesh_started = time.perf_counter()
    with torch.inference_mode():
        meshes = model.extract_mesh(
            scene_codes, has_vertex_color=True, resolution=args.resolution)
    mesh = meshes[0]
    # TripoSR 使用 Z-up，glTF 规定 Y-up；导出前转换可避免导入 Blender 后躺倒。
    zup_to_yup = np.array([
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, -1.0, 0.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ])
    mesh.apply_transform(zup_to_yup)
    mesh.export(args.output, file_type="glb")
    mesh_seconds = time.perf_counter() - mesh_started

    if not os.path.isfile(args.output):
        raise RuntimeError("推理结束但 GLB 文件未生成")
    return {
        "ok": True,
        "backend": "triposr",
        "device": "cpu",
        "path": os.path.abspath(args.output),
        "preprocessed_image": preprocessed,
        "vertices": int(len(mesh.vertices)),
        "faces": int(len(mesh.faces)),
        "bytes": int(os.path.getsize(args.output)),
        "resolution": int(args.resolution),
        "load_seconds": round(load_seconds, 2),
        "inference_seconds": round(infer_seconds, 2),
        "mesh_seconds": round(mesh_seconds, 2),
        "total_seconds": round(time.perf_counter() - started, 2),
        "color_mode": "vertex_colors",
        "license": "MIT",
    }


def main():
    parser = argparse.ArgumentParser(description="本地免费 TripoSR CPU 图生三维")
    parser.add_argument("image", help="输入图片路径")
    parser.add_argument(
        "--model-dir",
        default=os.environ.get("TRIPOSR_MODEL_DIR", r"C:\wasily_models\TripoSR"),
        help="包含 config.yaml 和 model.ckpt 的目录")
    parser.add_argument("--output", required=True, help="输出 GLB 路径")
    parser.add_argument("--resolution", type=int, default=256,
                        help=" marching cubes 分辨率，默认 256")
    parser.add_argument("--chunk-size", type=int, default=8192,
                        help="三平面查询分块大小，默认 8192")
    parser.add_argument("--no-remove-bg", action="store_true",
                        help="不去背景，输入应已是灰色背景前景图")
    args = parser.parse_args()
    try:
        result = run(args)
        code = 0
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "backend": "triposr",
                  "error": "%s: %s" % (type(exc).__name__, exc),
                  "trace": traceback.format_exc(limit=8)}
        code = 1
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
