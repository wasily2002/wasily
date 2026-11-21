# -*- coding: utf-8 -*-
"""depth3d_cli.py — 深度估计子进程入口
服务端通过 subprocess 调用，避免 DPT 模型占用主进程内存。
用法:
  python depth3d_cli.py single <img_path> <out_w> <out_h>
  python depth3d_cli.py fuse <img1> <img2> ... --out_w 48 --out_h 90
输出 JSON 到 stdout。
"""
import sys
import os
import json
import argparse

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import depth3d  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["single", "fuse"])
    ap.add_argument("images", nargs="+")
    ap.add_argument("--out_w", type=int, default=48)
    ap.add_argument("--out_h", type=int, default=90)
    args = ap.parse_args()

    try:
        if args.mode == "single":
            info = depth3d.get_depth_b64(args.images[0], args.out_w, args.out_h)
        else:
            info = depth3d.fuse_depth_b64(args.images, args.out_w, args.out_h)
        info["ok"] = True
    except Exception as e:
        info = {"ok": False, "error": str(e)}
    sys.stdout.write(json.dumps(info, ensure_ascii=False))


if __name__ == "__main__":
    main()
