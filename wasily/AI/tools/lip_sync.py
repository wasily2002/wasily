# -*- coding: utf-8 -*-
"""
本地 Wav2Lip 口型同步（免费开源，纯 CPU 推理）。

用法示例：
    python tools/lip_sync.py --face avatars/wxx_real_stand.mp4 --audio in.wav \
        --out avatars/render/talk_01.mp4 --duration 6

参数：
    --face      真人视频（脸部尽量正对镜头、少晃动）
    --audio     配音音频（任意格式，内部转 16k 单声道）
    --out       输出 mp4（自动带上音频轨）
    --start     从源视频第几秒开始截取（默认 0）
    --duration  输出时长上限（秒，默认按音频长度）
    --static    整段只检测一次人脸框（镜头不动时更快更稳）
    --batch     推理批大小（默认 16）
    --crop-scale 脸部裁剪框相对人脸尺寸的放大倍数（默认 1.7）
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time

import cv2
import librosa
import numpy as np
import soundfile as sf
import torch

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)                      # wasily/AI
SRC = os.path.join(HERE, "wav2lip_src")
sys.path.insert(0, SRC)

from models.wav2lip import Wav2Lip              # noqa: E402

FFMPEG = os.path.join(HERE, "ffmpeg.exe")
FFPROBE = os.path.join(HERE, "ffprobe.exe")
YUNET = os.path.join(HERE, "face_detection_yunet_2023mar.onnx")
CKPT = os.path.join(HERE, "wav2lip_gan.pth")

SR = 16000          # Wav2Lip 训练采样率
NFFT = 800
HOP = 200
N_MELS = 80
MEL_STEP = 16       # 每帧视频对应的 mel 帧数
IMG_SIZE = 96


# ---------------------------------------------------------------- 音频
def audio_to_wav(src, dst_ascii):
    """用 ffmpeg 把任意音频转成 16k 单声道 wav（解决中文路径/格式问题）"""
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
           "-i", src, "-ar", str(SR), "-ac", "1", dst_ascii]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0 or not os.path.exists(dst_ascii):
        raise RuntimeError("ffmpeg 解码音频失败: " + (r.stderr or b"").decode("utf-8", "replace")[:300])
    return dst_ascii


def melspectrogram(wav):
    """与 Wav2Lip 训练时完全一致的 mel 归一化"""
    wav = wav.astype(np.float32)
    # preemphasis 0.97
    wav = np.append(wav[0], wav[1:] - 0.97 * wav[:-1])
    D = librosa.stft(y=wav, n_fft=NFFT, hop_length=HOP, win_length=NFFT)
    S = np.abs(D)
    basis = librosa.filters.mel(sr=SR, n_fft=NFFT, n_mels=N_MELS, fmin=55, fmax=7600)
    S = np.dot(basis, S)
    S = 20.0 * np.log10(np.maximum(1e-5, S)) - 20.0      # ref_level_db=20
    # symmetric_mels=True, min_level_db=-100, max_abs_value=4, 允许裁剪
    S = (2 * 4.0) * ((S + 100.0) / 100.0) - 4.0
    return np.clip(S, -4.0, 4.0)


def build_mel_chunks(mel, fps, n_frames):
    """每帧视频取 16 个 mel 列，末尾不足则贴最后一窗"""
    per_frame = SR / HOP / fps
    chunks = np.zeros((n_frames, N_MELS, MEL_STEP), dtype=np.float32)
    last = mel.shape[1]
    for i in range(n_frames):
        st = int(i * per_frame)
        if st + MEL_STEP > last:
            st = max(0, last - MEL_STEP)
        chunks[i] = mel[:, st:st + MEL_STEP]
    return chunks


# ---------------------------------------------------------------- 视频
def read_frames(path, start=0.0, duration=None, fps=None):
    if os.path.splitext(path)[1].lower() in (".jpg", ".jpeg", ".png", ".bmp", ".webp"):
        img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError("无法读取图片: " + path)
        return [img], float(fps or 25.0)
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError("无法打开视频: " + path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    if start > 0:
        cap.set(cv2.CAP_PROP_POS_MSEC, start * 1000.0)
    frames = []
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        frames.append(fr)
        if duration and len(frames) / fps >= duration:
            break
    cap.release()
    if not frames:
        raise RuntimeError("视频里没有读到帧")
    return frames, fps


def ascii_model_path(path):
    """cv2 的 ONNX 解析器读不了中文路径，复制到纯英文临时目录"""
    if all(ord(c) < 128 for c in path):
        return path
    dst = os.path.join(tempfile.gettempdir(), "wasily_yunet.onnx")
    if not os.path.exists(dst) or os.path.getsize(dst) != os.path.getsize(path):
        shutil.copyfile(path, dst)
    return dst


def detect_boxes(frames, scale=1.15, static=False):
    """YuNet 人脸检测 → 矩形裁剪框（与 Wav2Lip 训练分布对齐：原始人脸矩形 + 下巴下延）。

    注意：不能用正方形裁剪——方形框会把过多脖子/背景拉进 96x96，
    嘴在训练数据中的相对位置发生偏移，模型会在错误位置"幻觉"出嘴，
    表现为嘴部融化、错位、暗红团块。
    """
    h, w = frames[0].shape[:2]
    det = cv2.FaceDetectorYN_create(ascii_model_path(YUNET), "", (w, h), 0.6, 0.3, 5000)
    boxes = []
    todo = frames[:1] if static else frames
    for fr in todo:
        det.setInputSize((fr.shape[1], fr.shape[0]))
        _, faces = det.detect(fr)
        if faces is None or len(faces) == 0:
            boxes.append(None)
            continue
        f = max(faces, key=lambda x: x[2] * x[3])
        x, y, fw, fh = f[0], f[1], f[2], f[3]
        # 与官方推理一致：矩形框四周仅小幅外扩（下巴方向多留）
        pad_t = int(fh * 0.04)
        pad_b = int(fh * 0.12)
        pad_x = int(fw * 0.06)
        x1, x2 = int(x) - pad_x, int(x + fw) + pad_x
        y1, y2 = int(y) - pad_t, int(y + fh) + pad_b
        boxes.append((x1, y1, x2, y2))

    if static:
        boxes = boxes * len(frames)
    else:
        # 5 帧滑动平均，抑制抖动
        T = 5
        for i in range(len(boxes)):
            if boxes[i] is None:
                continue
            win = boxes[max(0, i - T // 2): min(len(boxes), i + T // 2 + 1)]
            win = [b for b in win if b is not None]
            if win:
                boxes[i] = tuple(int(v) for v in np.mean(win, axis=0))
    # 空检测用上一有效框兜底
    last_ok = None
    for i, b in enumerate(boxes):
        if b is None:
            boxes[i] = last_ok
        else:
            last_ok = b
    if all(b is None for b in boxes):
        raise RuntimeError("未检测到人脸")
    # 头部仍为空则用第一个有效框
    first = next(b for b in boxes if b is not None)
    return [b or first for b in boxes]


def pad_for_box(frame, box):
    """裁剪框超出画面时用边缘像素补齐，保持方形不变形。
    返回 (补齐后的帧, 平移后的框, (上补, 左补, 原高, 原宽))"""
    x1, y1, x2, y2 = box
    h, w = frame.shape[:2]
    pt, pb = max(0, -y1), max(0, y2 - h)
    pl, pr = max(0, -x1), max(0, x2 - w)
    if pt or pb or pl or pr:
        frame = cv2.copyMakeBorder(frame, pt, pb, pl, pr, cv2.BORDER_REPLICATE)
    return frame, (x1 + pl, y1 + pt, x2 + pl, y2 + pt), (pt, pl, h, w)


def feather_alpha(w, h, fe=8, lower=1.0):
    """贴回用的 alpha：四边羽化；lower<1 时只保留画面下半部分（照片输入时保住清晰的上半脸）"""
    a = np.ones((h, w), np.float32)
    fe = max(2, min(fe, min(w, h) // 4))
    ramp = np.linspace(0.0, 1.0, fe, dtype=np.float32)
    a[:fe, :] *= ramp[:, None]
    a[-fe:, :] *= ramp[::-1, None]
    a[:, :fe] *= ramp[None, :]
    a[:, -fe:] *= ramp[::-1][None, :]
    if lower < 1.0:
        top = int(h * (1.0 - lower))
        a[:top, :] = 0.0
        a[top:top + fe, :] *= ramp[:min(fe, h - top)][:, None]
    return a


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--face", required=True)
    ap.add_argument("--audio", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--duration", type=float, default=0.0)
    ap.add_argument("--static", action="store_true")
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--crop-scale", type=float, default=1.15,
                    help="裁剪边长 = 人脸高 × 该倍数（含一点下巴）")
    ap.add_argument("--fps", type=float, default=25.0, help="静态图片输入时的帧率")
    ap.add_argument("--lower-only", type=float, default=1.0,
                    help="只把生成结果的下半部分(0~1)贴回，照片输入建议 0.62")
    args = ap.parse_args()

    face_path = args.face if os.path.isabs(args.face) else os.path.join(BASE, args.face)
    audio_path = args.audio if os.path.isabs(args.audio) else os.path.join(BASE, args.audio)
    out_path = args.out if os.path.isabs(args.out) else os.path.join(BASE, args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    tmp = tempfile.mkdtemp(prefix="w2l_")
    try:
        ext = os.path.splitext(face_path)[1].lower()
        tmp_face = os.path.join(tmp, "face" + ext)
        tmp_audio = os.path.join(tmp, "audio.wav")
        shutil.copyfile(face_path, tmp_face)          # 规避中文路径
        audio_to_wav(audio_path, tmp_audio)

        wav, _ = sf.read(tmp_audio, dtype="float32")
        if wav.ndim > 1:
            wav = wav.mean(axis=1)
        mel = melspectrogram(wav)
        audio_dur = len(wav) / float(SR)
        print("音频 %.2fs, mel %s" % (audio_dur, mel.shape))

        frames, fps = read_frames(tmp_face, args.start, args.duration, args.fps)
        n_frames = int(round(min(audio_dur, args.duration or audio_dur) * fps))
        n_frames = max(1, min(n_frames, int(round(audio_dur * fps))))
        print("源视频 %d 帧 %dx%d @%.2ffps，需要输出 %d 帧"
              % (len(frames), frames[0].shape[1], frames[0].shape[0], fps, n_frames))

        boxes = detect_boxes(frames, args.crop_scale, args.static)

        model = Wav2Lip()
        sd = torch.load(CKPT, map_location="cpu")
        if isinstance(sd, dict) and "state_dict" in sd:      # 官方 gan 权重是训练存档
            sd = sd["state_dict"]
        sd = {k.replace("module.", ""): v for k, v in sd.items()}
        model.load_state_dict(sd)
        model.eval()
        torch.set_num_threads(max(2, os.cpu_count() - 1))

        mel_chunks = build_mel_chunks(mel, fps, n_frames)
        h, w = frames[0].shape[:2]
        writer = cv2.VideoWriter(os.path.join(tmp, "out.mp4"),
                                 cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))

        alpha_cache = {}
        t0 = time.time()
        for b0 in range(0, n_frames, args.batch):
            b1 = min(n_frames, b0 + args.batch)
            faces, mels, metas = [], [], []
            for i in range(b0, b1):
                fr = frames[i % len(frames)]
                box = boxes[i % len(boxes)]
                padded, pbox, _ = pad_for_box(fr, box)
                x1, y1, x2, y2 = pbox
                face = padded[y1:y2, x1:x2]
                if face.size == 0:
                    continue
                faces.append(cv2.resize(face, (IMG_SIZE, IMG_SIZE)))
                mels.append(mel_chunks[i])
                metas.append((i, box))
            if not faces:
                continue
            img_batch = np.asarray(faces, np.float32) / 255.0
            masked = img_batch.copy()
            masked[:, IMG_SIZE // 2:] = 0                    # 遮住下半张脸，作为第二路输入
            img_batch = np.concatenate((masked, img_batch), axis=3)
            img_batch = img_batch.transpose((0, 3, 1, 2))            # B,6,96,96
            audio_batch = np.asarray(mels, np.float32).reshape(-1, N_MELS, MEL_STEP, 1)
            audio_batch = audio_batch.transpose((0, 3, 1, 2))        # B,1,80,16
            with torch.no_grad():
                pred = model(torch.from_numpy(audio_batch),
                             torch.from_numpy(img_batch)).numpy()
            for k, (i, box) in enumerate(metas):
                rgb = (np.clip(pred[k], 0, 1) * 255).astype(np.uint8)
                rgb = rgb.transpose(1, 2, 0)                           # 模型输入是 BGR，输出同序
                src = frames[i % len(frames)]
                padded, pbox, (pt, pl, oh, ow) = pad_for_box(src, box)
                x1, y1, x2, y2 = pbox
                cw, ch = x2 - x1, y2 - y1
                if cw <= 0 or ch <= 0:
                    continue
                img = cv2.resize(rgb, (cw, ch), interpolation=cv2.INTER_LANCZOS4)
                key = (cw, ch)
                if key not in alpha_cache:
                    alpha_cache[key] = feather_alpha(cw, ch, lower=args.lower_only)
                a = alpha_cache[key][..., None]
                roi = padded[y1:y2, x1:x2].astype(np.float32)
                padded[y1:y2, x1:x2] = np.clip(img * a + roi * (1 - a), 0, 255).astype(np.uint8)
                writer.write(padded[pt:pt + oh, pl:pl + ow])
            done = b1
            el = time.time() - t0
            print("  %d/%d 帧  %.2fs  预计剩余 %.0fs"
                  % (done, n_frames, el, el / done * (n_frames - done)), flush=True)
        writer.release()

        cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
               "-i", os.path.join(tmp, "out.mp4"), "-i", tmp_audio,
               "-c:v", "libx264", "-preset", "medium", "-crf", "18",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
               "-shortest", out_path]
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode != 0:
            raise RuntimeError("ffmpeg 合成失败: " + (r.stderr or b"").decode("utf-8", "replace")[:400])
        print("完成 ->", out_path, "%.1f MB" % (os.path.getsize(out_path) / 1048576.0))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
