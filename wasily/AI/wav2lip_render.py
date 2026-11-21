# -*- coding: utf-8 -*-
"""
Wav2Lip 真人口型同步渲染服务（异步任务队列）
参考 aigcpanel 视频换口型方案：输入人物照片/视频 + 语音 → 唇形同步真人视频。

流程：文本 → TTS 语音 → Wav2Lip CPU 推理 → 唇形同步真人视频(mp4)
输出直接写 avatars/w2l_<jobid>.mp4，复用 /avatars/ 静态路由（已支持 Range）。
单 worker 串行：CPU 推理一次只跑一个渲染。

支持：
  - 自定义人脸照片（face 参数，相对 avatars 目录或绝对路径）
  - 中文路径自动转 ASCII 临时副本（cv2.imread 不支持中文）
  - 全身照自动头部框：从同名 .body.json 的 head 推算（没有则用 YuNet 自动检测）
"""
import glob
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

# ============ 路径配置（本地免费开源 Wav2Lip，纯 CPU） ============
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(BASE_DIR, "tools")
LIP_SYNC = os.path.join(TOOLS, "lip_sync.py")        # 本地推理脚本（torch CPU）
W2L_CKPT = os.path.join(TOOLS, "wav2lip_gan.pth")
YUNET = os.path.join(TOOLS, "face_detection_yunet_2023mar.onnx")
FFMPEG_BIN = TOOLS
OUT_DIR = os.path.join(BASE_DIR, "avatars")
TMP_ASCII = tempfile.gettempdir()
# 基准人脸：正脸特写（静态照片，口型最稳）
FACE_IMG = os.path.join(BASE_DIR, "avatars", "wxx_me_2.jpg")

_jobs = {}            # job_id -> {status, video, err, created, elapsed}
_jobs_lock = threading.Lock()
_queue = queue.Queue()
_worker_started = False

# TTS 已移至 submit() 的 HTTP handler 线程内执行（worker 线程内 edge-tts/SAPI 异常）。


def available():
    """环境是否就绪（推理脚本 + 权重 + 人脸检测模型）"""
    return (os.path.exists(LIP_SYNC) and os.path.exists(W2L_CKPT)
            and os.path.exists(YUNET))


def _set(job_id, **kw):
    with _jobs_lock:
        _jobs.setdefault(job_id, {}).update(kw)


def status(job_id):
    with _jobs_lock:
        j = _jobs.get(job_id)
        if not j:
            return None
        d = dict(j)
        if j.get("status") in ("queued", "tts", "rendering") and j.get("created"):
            d["elapsed"] = round(time.time() - j["created"], 1)
        return d


def submit(text, voice=None, emotion="normal", face=None):
    """提交渲染任务，立即返回 job_id。

    在调用线程（HTTP handler）内完成 TTS：worker 线程内 edge-tts 会 NoAudioReceived、
    SAPI 空 WAV，但 handler 线程正常。TTS 音频存临时文件，路径随任务入队。
    face: 可选，相对 avatars 目录的文件名（如 wxx_full_1.jpg）或绝对路径。
    voice 缺省用训练好的克隆声音（clone:wxx = 微调权重 + 当前默认音色卡参考音）。
    """
    if not available():
        return None, "Wav2Lip 环境未就绪（缺 venv/模型/仓库）"
    text = (text or "")[:300]
    if not text.strip():
        return None, "text 为空"
    voice = voice or "clone:wxx"
    job_id = uuid.uuid4().hex[:12]
    _set(job_id, status="tts", created=time.time(), video=None, err=None,
         text=text, face=face, voice=voice)
    # ---- 在 handler 线程内做 TTS ----
    try:
        import voice as _voice
        audio_bytes, mime = _voice.synthesize(text, voice=voice, emotion=emotion or "normal")
        if not audio_bytes and str(voice).startswith("clone"):
            # 克隆失败（F5 模型缺失等）→ 回退离线语音包，保证视频仍带声
            print(f"[w2l] {job_id} 克隆合成失败({mime})，回退 kokoro", flush=True)
            audio_bytes, mime = _voice.synthesize(text, voice="kokoro",
                                                  emotion=emotion or "normal")
        if not audio_bytes:
            _set(job_id, status="error", err=str(mime)[:200])
            return None, "TTS 合成失败: " + str(mime)[:100]
    except Exception as e:
        _set(job_id, status="error", err=str(e)[:200])
        return None, "TTS 异常: " + str(e)[:100]
    # 保存 TTS 音频到临时目录
    tts_ext = ".wav" if (mime or "").startswith("audio/wav") else ".mp3"
    audio_path = os.path.join(TMP_ASCII, f"w2l_tts_{job_id}{tts_ext}")
    with open(audio_path, "wb") as f:
        f.write(audio_bytes)
    _set(job_id, status="queued")
    _queue.put((job_id, text, voice, emotion, face, audio_path))
    _ensure_worker()
    return job_id, None


def _ensure_worker():
    global _worker_started
    if not _worker_started:
        _worker_started = True
        threading.Thread(target=_worker, daemon=True).start()


def _cleanup_outputs(keep=20):
    """只保留最近 20 个渲染产物，防止 avatars 目录膨胀；历史记录同步剔除已删文件"""
    fs = sorted(glob.glob(os.path.join(OUT_DIR, "w2l_*.mp4")),
                key=os.path.getmtime)
    removed = set()
    for f in fs[:-keep]:
        try:
            os.remove(f)
            removed.add(os.path.basename(f))
        except OSError:
            pass
    if removed:
        with _hist_lock:
            hist = _hist_load()
            kept = [h for h in hist if os.path.basename(h.get("video", "")) not in removed]
            if len(kept) != len(hist):
                _hist_save(kept)


# ============ 生成历史记录（avatars/w2l_history.json，最新在前，上限 50 条） ============
HISTORY_PATH = os.path.join(OUT_DIR, "w2l_history.json")
_hist_lock = threading.Lock()


def _hist_load():
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, list) else []
    except Exception:
        return []


def _hist_save(hist):
    try:
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(hist[:50], f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _hist_add(job_id, text, face, voice, video, elapsed):
    with _hist_lock:
        hist = _hist_load()
        hist.insert(0, {
            "job": job_id, "video": video, "text": text, "face": face or "",
            "voice": voice or "", "elapsed": elapsed,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        _hist_save(hist)


def history():
    """返回历史记录（过滤掉文件已被清理的条目）"""
    with _hist_lock:
        hist = _hist_load()
        alive = [h for h in hist
                 if h.get("video") and os.path.exists(
                     os.path.join(OUT_DIR, os.path.basename(h["video"])))]
        if len(alive) != len(hist):
            _hist_save(alive)
        return alive


def _ascii_copy(src, dst_dir, prefix="face"):
    """把可能含中文路径的图片复制到 ASCII 临时路径，返回新路径。"""
    os.makedirs(dst_dir, exist_ok=True)
    ext = os.path.splitext(src)[1].lower() or ".jpg"
    dst = os.path.join(dst_dir, f"{prefix}_{os.getpid()}_{int(time.time()*1000)}{ext}")
    shutil.copyfile(src, dst)
    return dst


def _head_box_from_body(body_path, img_w, img_h):
    """从 .body.json 的 head 字段推算头部框（top, bottom, left, right）像素坐标。
    推算失败返回 None，让 Wav2Lip 走 S3FD 自动检测。"""
    try:
        with open(body_path, encoding="utf-8") as f:
            b = json.load(f)
        h = b.get("head")
        if not h:
            return None
        cx, cy, w, hh = h.get("cx"), h.get("cy"), h.get("w"), h.get("h")
        if None in (cx, cy, w, hh):
            return None
        # 头部框上下左右各扩 18%，保证含下巴
        x0 = max(0, int((cx - w * 0.5) * img_w))
        x1 = min(img_w - 1, int((cx + w * 0.5) * img_w))
        y0 = max(0, int((cy - hh * 0.5) * img_h))
        y1 = min(img_h - 1, int((cy + hh * 0.5) * img_h))
        pad_x = int((x1 - x0) * 0.18)
        pad_y = int((y1 - y0) * 0.18)
        x0 = max(0, x0 - pad_x); x1 = min(img_w - 1, x1 + pad_x)
        y0 = max(0, y0 - pad_y); y1 = min(img_h - 1, y1 + pad_y)
        if x1 - x0 < 40 or y1 - y0 < 40:
            return None
        return [y0, y1, x0, x1]   # top, bottom, left, right
    except Exception:
        return None


def _img_size(path):
    """读取图片尺寸（兼容中文路径）"""
    try:
        import numpy as np
        import cv2
        buf = np.fromfile(path, dtype=np.uint8)
        im = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if im is not None:
            return im.shape[1], im.shape[0]   # w, h
    except Exception:
        pass
    return None, None


def _resolve_face(face_arg):
    """解析 face 参数 → (实际图片路径, body.json 路径或 None)"""
    if not face_arg:
        return FACE_IMG, None
    # 绝对路径
    if os.path.isabs(face_arg) and os.path.exists(face_arg):
        p = face_arg
    else:
        # 相对 avatars 目录
        p = os.path.join(OUT_DIR, os.path.basename(face_arg))
        if not os.path.exists(p):
            p = os.path.join(BASE_DIR, face_arg)
    if not os.path.exists(p):
        return FACE_IMG, None
    body = os.path.splitext(p)[0] + ".body.json"
    return p, (body if os.path.exists(body) else None)


def _worker():
    while True:
        job_id, text, voice, emotion, face_arg, audio_path = _queue.get()
        t0 = time.time()
        tmpdir = tempfile.mkdtemp(prefix="w2l_", dir=TMP_ASCII)
        keep_tmp = False
        try:
            # ---- 1. 解析人脸素材 ----
            face_src, body_path = _resolve_face(face_arg)
            # cv2.imread 不支持中文路径 → 复制到 ASCII 临时路径
            face = _ascii_copy(face_src, TMP_ASCII, prefix="face")
            img_w, img_h = _img_size(face)
            box = None
            if body_path and img_w and img_h:
                box = _head_box_from_body(body_path, img_w, img_h)

            # ---- 2. 音频已由 submit() 在 handler 线程内合成好，直接重采样 ----
            import sys as _sys
            env = {}
            for _k in ("SYSTEMROOT", "SystemRoot", "windir", "COMPUTERNAME", "USERNAME",
                       "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP",
                       "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS", "PATH"):
                if _k in os.environ:
                    env[_k] = os.environ[_k]
            env["PATH"] = FFMPEG_BIN + os.pathsep + env.get("PATH", "")
            env["TMP"] = env["TEMP"] = TMP_ASCII
            wav_path = os.path.join(tmpdir, "tts.wav")
            # 重采样到 16k 单声道（Wav2Lip 要求）
            subprocess.run([os.path.join(FFMPEG_BIN, "ffmpeg.exe"), "-y", "-i", audio_path,
                            "-ar", "16000", "-ac", "1", wav_path],
                           check=True, capture_output=True, env=env, timeout=60)
            print(f"[w2l] {job_id} audio ready {time.time()-t0:.1f}s", flush=True)

            # ---- 3. Wav2Lip 本地 CPU 推理（免费开源模型，不依赖显卡） ----
            _set(job_id, status="rendering")
            out_raw = os.path.join(tmpdir, "out.mp4")
            cmd = [_sys.executable, LIP_SYNC,
                   "--face", face,
                   "--audio", wav_path,
                   "--out", out_raw,
                   "--crop-scale", "1.15",
                   "--lower-only", "0.62",
                   "--fps", "25",
                   "--batch", "16"]
            r = subprocess.run(cmd, cwd=BASE_DIR, env=env,
                               capture_output=True, text=True, timeout=900)
            if r.returncode != 0 or not os.path.exists(out_raw):
                os.makedirs(os.path.join(TMP_ASCII, "w2l_logs"), exist_ok=True)
                with open(os.path.join(TMP_ASCII, "w2l_logs", f"{job_id}.log"), "w",
                          encoding="utf-8", errors="replace") as lf:
                    lf.write("CMD: " + " ".join(cmd) + "\n\nSTDERR:\n" + (r.stderr or "")
                             + "\n\nSTDOUT:\n" + (r.stdout or ""))
                raise RuntimeError("Wav2Lip 推理失败: " + (r.stderr or r.stdout)[-1500:])

            # ---- 4. 落位 avatars/（lip_sync 已输出 h264+aac，直接搬运） ----
            out_final = os.path.join(OUT_DIR, f"w2l_{job_id}.mp4")
            shutil.copyfile(out_raw, out_final)
            el = round(time.time() - t0, 1)
            _set(job_id, status="done",
                 video=f"/avatars/w2l_{job_id}.mp4",
                 elapsed=el)
            _hist_add(job_id, text, face_arg, voice, f"/avatars/w2l_{job_id}.mp4", el)
            _cleanup_outputs()
            print(f"[w2l] {job_id} 渲染完成 {time.time()-t0:.1f}s", flush=True)
        except Exception as e:
            keep_tmp = True
            _set(job_id, status="error", err=str(e)[:300],
                 elapsed=round(time.time() - t0, 1))
            print(f"[w2l] {job_id} 失败: {str(e)[:200]} tmpdir={tmpdir}", flush=True)
        finally:
            # 清理 ASCII 临时人脸
            try:
                if os.path.exists(face):
                    os.remove(face)
            except OSError:
                pass
            # 清理 submit() 生成的 TTS 临时音频
            try:
                if os.path.exists(audio_path):
                    os.remove(audio_path)
            except OSError:
                pass
            if not keep_tmp:
                try:
                    for f in os.listdir(tmpdir):
                        os.remove(os.path.join(tmpdir, f))
                    os.rmdir(tmpdir)
                except OSError:
                    pass
