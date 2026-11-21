# -*- coding: utf-8 -*-
"""
depth3d.py — 单目深度估计 → 三维位移图
用 HuggingFace DPT (MiDaS) 从单张照片估计深度，
输出一张小尺寸灰度深度图（PNG base64），前端 photo3d.js
读取后把平面网格顶点沿 z 轴位移，实现真 3D 体积感。
"""
import os
import io
import base64
import numpy as np
import cv2
import torch

_MODEL = None
_PROC = None
_CACHE_DIR = os.path.join(os.path.dirname(__file__), "tools", "midas_cache")


def _load():
    global _MODEL, _PROC
    if _MODEL is not None:
        return _MODEL, _PROC
    # 模型/预处理器已完整缓存到本地，强制离线：
    # 避免 hf-mirror 上同名单元的新版预处理配置（256）与本地模型（384）不匹配
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    torch.set_num_threads(2)  # 减少线程内存开销
    from transformers import DPTForDepthEstimation, DPTImageProcessor
    _MODEL = DPTForDepthEstimation.from_pretrained(
        "Intel/dpt-hybrid-midas", cache_dir=_CACHE_DIR,
        torch_dtype=torch.float32)
    _PROC = DPTImageProcessor.from_pretrained(
        "Intel/dpt-hybrid-midas", cache_dir=_CACHE_DIR,
        size={"height": 384, "width": 384},
        keep_aspect_ratio=False, ensure_multiple_of=1)
    _MODEL.eval()
    return _MODEL, _PROC


def estimate_depth(img_path, out_w=48, out_h=90):
    """返回 (depth_uint8_HxW, info_dict)
    depth_uint8: 0=远, 255=近（已归一化到 0..255）
    """
    import gc
    img = cv2.imdecode(np.fromfile(img_path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise FileNotFoundError(img_path)
    img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

    model, proc = _load()
    inputs = proc(images=img_rgb, return_tensors="pt")
    with torch.inference_mode():
        outputs = model(**inputs)
        depth = outputs.predicted_depth.squeeze(0).cpu().numpy()
    del inputs, outputs
    gc.collect()

    # 归一化到 0..255（近=大值）
    dmin, dmax = float(depth.min()), float(depth.max())
    if dmax - dmin > 1e-6:
        depth = (depth - dmin) / (dmax - dmin)
    else:
        depth = np.zeros_like(depth)
    depth_u8 = (depth * 255).astype(np.uint8)

    # 缩放到目标网格分辨率
    depth_small = cv2.resize(depth_u8, (out_w, out_h),
                             interpolation=cv2.INTER_AREA)
    info = {
        "src_w": int(img.shape[1]),
        "src_h": int(img.shape[0]),
        "out_w": out_w,
        "out_h": out_h,
    }
    return depth_small, info


def depth_to_png_b64(depth_u8):
    ok, buf = cv2.imencode(".png", depth_u8)
    if not ok:
        raise RuntimeError("png encode failed")
    return base64.b64encode(buf.tobytes()).decode("ascii")


def get_depth_b64(img_path, out_w=48, out_h=90):
    """一步到位：图片路径 → base64 深度图 PNG + 信息"""
    depth, info = estimate_depth(img_path, out_w, out_h)
    info["depth_png"] = depth_to_png_b64(depth)
    return info


def _read_img(path):
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


def fuse_depth_b64(img_paths, out_w=48, out_h=90):
    """多帧深度融合：手机环绕拍摄多张 → 对齐 + 中值融合 → 更稳的深度图
    返回 {depth_png, out_w, out_h, n_frames, n_aligned}
    """
    model, proc = _load()
    # 第一帧为参考
    ref = _read_img(img_paths[0])
    H_ref, W_ref = ref.shape[:2]
    ref_gray = cv2.cvtColor(ref, cv2.COLOR_BGR2GRAY)
    orb = cv2.ORB_create(nfeatures=1000)
    kp_ref, des_ref = orb.detectAndCompute(ref_gray, None)
    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)

    depths = []
    n_aligned = 0
    import gc
    for idx, p in enumerate(img_paths):
        img = _read_img(p)
        if img is None:
            continue
        # 估计深度
        img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        inputs = proc(images=img_rgb, return_tensors="pt")
        with torch.inference_mode():
            d = model(**inputs).predicted_depth.squeeze(0).cpu().numpy()
        del inputs
        dmin, dmax = float(d.min()), float(d.max())
        if dmax - dmin > 1e-6:
            d = (d - dmin) / (dmax - dmin)
        else:
            d = np.zeros_like(d)
        # 统一缩放到参考帧尺寸，保证可堆叠
        d = cv2.resize(d, (W_ref, H_ref), interpolation=cv2.INTER_LINEAR)

        if idx == 0:
            depths.append(d)
            n_aligned += 1
            continue

        # ORB 匹配 → 单应矩阵 → 把深度图对齐到参考帧
        try:
            g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            kp, des = orb.detectAndCompute(g, None)
            if des is None or des_ref is None or len(kp) < 8:
                continue
            matches = bf.match(des_ref, des)
            if len(matches) < 8:
                continue
            matches = sorted(matches, key=lambda x: x.distance)[:80]
            src_pts = np.float32([kp_ref[m.queryIdx].pt for m in matches]).reshape(-1, 1, 2)
            dst_pts = np.float32([kp[m.trainIdx].pt for m in matches]).reshape(-1, 1, 2)
            H, mask = cv2.findHomography(dst_pts, src_pts, cv2.RANSAC, 5.0)
            if H is None:
                continue
            d_aligned = cv2.warpPerspective(d, H, (W_ref, H_ref),
                                            flags=cv2.INTER_LINEAR,
                                            borderMode=cv2.BORDER_CONSTANT,
                                            borderValue=0)
            depths.append(d_aligned)
            n_aligned += 1
        except Exception:
            continue

    if not depths:
        raise RuntimeError("没有可用帧")

    # 中值融合：对每个像素取所有对齐深度的中值（抗噪）
    stacked = np.stack(depths, axis=0)
    fused = np.median(stacked, axis=0)
    # 把未对齐区域（值为0）排除在中值外：用参考帧深度填充
    ref_d = depths[0]
    fused = np.where(fused > 1e-6, fused, ref_d)
    fmin, fmax = float(fused.min()), float(fused.max())
    if fmax - fmin > 1e-6:
        fused = (fused - fmin) / (fmax - fmin)
    fused_u8 = (fused * 255).astype(np.uint8)
    fused_small = cv2.resize(fused_u8, (out_w, out_h), interpolation=cv2.INTER_AREA)

    return {
        "depth_png": depth_to_png_b64(fused_small),
        "out_w": out_w, "out_h": out_h,
        "n_frames": len(img_paths), "n_aligned": n_aligned,
    }


if __name__ == "__main__":
    import sys
    p = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(__file__), "avatars", "wxx_body.cutout.png")
    info = get_depth_b64(p)
    print("depth map:", info["out_w"], "x", info["out_h"],
          "png bytes:", len(info["depth_png"]))
