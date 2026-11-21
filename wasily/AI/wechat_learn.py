# -*- coding: utf-8 -*-
"""微信对话框学习（文字 + 语音）

文字学习：截图微信对话窗口 → OCR 识别 → 按消息气泡左右位置区分「对方/我」
        → 生成问答对写入训练集 + 记忆
语音学习：用户播放微信语音时同步录音 → faster_whisper 转文字 → 同上沉淀

与 screen_learn.py 区别：
- screen_learn 面向「AI 对话窗口」，提炼对方 AI 的风格/知识
- wechat_learn 面向「真实微信聊天」，按对话双方生成问答对，学对方说话方式
"""
import json
import os
import re
import hashlib
import threading
import time
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

import screen_learn as _sl
import memory_net

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WECHAT_DS = os.path.join(BASE_DIR, "training_data", "wechat_learn.jsonl")
REC_DIR = os.path.join(BASE_DIR, "wasily_log", "voice_rec")
os.makedirs(REC_DIR, exist_ok=True)
os.makedirs(os.path.dirname(WECHAT_DS), exist_ok=True)

_ocr_engine = None
_asr_model = None
_lock = threading.Lock()
_rec_thread = None
_rec_frames = []
_rec_active = False

# 微信聊天标题特征
_WECHAT_KW = re.compile(r"(微信|wechat)", re.I)


def _ocr():
    global _ocr_engine
    if _ocr_engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr_engine = RapidOCR()
    return _ocr_engine


def _asr():
    global _asr_model
    if _asr_model is None:
        from faster_whisper import WhisperModel
        # 优先 medium，显存不足时回退 small/base
        for size in ("medium", "small", "base"):
            try:
                _asr_model = WhisperModel(size, device="cpu", compute_type="int8")
                print(f"[wechat] ASR 模型加载: {size}", flush=True)
                break
            except Exception as e:
                print(f"[wechat] ASR {size} 加载失败: {e}", flush=True)
                continue
        if _asr_model is None:
            raise RuntimeError("faster_whisper 所有模型均加载失败")
    return _asr_model


# ==================== 窗口列表（含微信） ====================
def list_wechat_windows():
    """列出所有窗口，微信窗口标记 wechat=True"""
    wins = _sl.list_windows()
    for w in wins:
        w["wechat"] = bool(_WECHAT_KW.search(w["title"]))
    return wins


def auto_wechat_hwnd():
    for w in list_wechat_windows():
        if w["wechat"]:
            return w["hwnd"]
    return _sl.auto_hwnd()


# ==================== 对话解析：按左右区分双方 ====================
def parse_dialog(ocr_result, img_width):
    """从 OCR 结果解析对话。
    返回 [{"side":"L"|"R", "text":..., "y":...}, ...]
    L=左侧=对方，R=右侧=我自己
    """
    rows = []
    for item in (ocr_result or []):
        try:
            box, text, score = item[0], item[1], float(item[2])
        except Exception:
            continue
        t = re.sub(r"\s+", " ", str(text or "")).strip()
        if not t or score < 0.72:
            continue
        if len(t) <= 3 and re.match(
                r"^(\d{1,2}:\d{2}(:\d{2})?|昨天|今天|星期.|[○Oo]{1,3}|输入文字.*)$", t, re.I):
            continue
        xs = [p[0] for p in box]
        cx = sum(xs) / 4.0
        ys = sum(p[1] for p in box) / 4.0
        # 以屏幕中线划分：左半=对方，右半=我自己
        side = "L" if cx < img_width * 0.52 else "R"
        rows.append({"side": side, "text": t, "y": ys, "cx": cx})
    rows.sort(key=lambda r: r["y"])
    # 合并同一侧相邻且 y 接近的文本行（一条消息可能被 OCR 拆成多行）
    merged = []
    for r in rows:
        if merged and merged[-1]["side"] == r["side"] and r["y"] - merged[-1]["y"] < 28:
            merged[-1]["text"] += r["text"]
            merged[-1]["y"] = r["y"]
        else:
            merged.append(dict(r))
    return merged


def dialog_to_qa(dialog):
    """把对话流转成问答对：每条「对方说的」作为 question，下一条「我说的」作为 answer；
    反过来也生成一组（我问对方答），让模型学对方的说话方式。"""
    qa = []
    for i, m in enumerate(dialog):
        if m["side"] == "L" and i + 1 < len(dialog) and dialog[i + 1]["side"] == "R":
            qa.append({"q": m["text"][:200], "a": dialog[i + 1]["text"][:600],
                       "dir": "her→me"})
        elif m["side"] == "R" and i + 1 < len(dialog) and dialog[i + 1]["side"] == "L":
            # 我问对方答 → 学对方怎么回答
            qa.append({"q": m["text"][:200], "a": dialog[i + 1]["text"][:600],
                       "dir": "me→her"})
    return qa


def _save_qa(qa, target=""):
    if not qa:
        return 0
    added = 0
    with open(WECHAT_DS, "a", encoding="utf-8") as f:
        for r in qa:
            f.write(json.dumps({"ts": int(time.time()), "source": "wechat",
                                "target": target[:40],
                                "instruction": r["q"], "output": r["a"],
                                "dir": r["dir"]}, ensure_ascii=False) + "\n")
            added += 1
    # 同时并入主训练集 dataset.jsonl，供 LoRA 直接使用
    from lora_lab import DATASET_JSONL
    with open(DATASET_JSONL, "a", encoding="utf-8") as f:
        for r in qa:
            f.write(json.dumps({"instruction": r["q"], "input": "",
                                "output": r["a"], "tool": False,
                                "source": "wechat_" + r["dir"]},
                               ensure_ascii=False) + "\n")
    return added


def learn_window(hwnd=0):
    """截图学习微信对话窗口一次"""
    with _lock:
        t0 = time.time()
        hwnd = int(hwnd or 0) or auto_wechat_hwnd()
        img = _apply_region(_sl.grab(hwnd))
        w, h = img.size
        res, _ = _ocr()(np.array(img))
        dialog = parse_dialog(res, w)
        qa = dialog_to_qa(dialog)
        out = {"ok": True, "lines": len(dialog), "qa": len(qa),
               "target": _sl._LAST.get("title", ""),
               "dialog": dialog[:50], "qa_preview": qa[:10],
               "text": "\n".join(
                   ("对方: " if m["side"] == "L" else "我: ") + m["text"]
                   for m in dialog[:50]),
               "elapsed": 0}
        if not dialog:
            out["ok"] = False
            out["msg"] = "没识别到对话文字，请确认微信窗口已打开且显示聊天内容"
            out["elapsed"] = round(time.time() - t0, 1)
            return out
        if qa:
            out["dataset_added"] = _save_qa(qa, _sl._LAST.get("title", ""))
            # 把对方最近几条说话风格存记忆
            her_lines = [m["text"] for m in dialog if m["side"] == "L"][-6:]
            if her_lines:
                memory_net._add_entry(
                    2, "微信学习",
                    f"对话对象「{_sl._LAST.get('title', '')}」说话示例：" +
                    "；".join(her_lines)[:500], src="wechat_learn")
                out["memory_added"] = 1
        else:
            out["msg"] = "识别到文字但没有成对的问答（可能只有一方在说话）"
        out["elapsed"] = round(time.time() - t0, 1)
        return out


# ==================== 语音转文字 ====================
def start_record():
    """开始录音（用户播放微信语音前调用）"""
    global _rec_thread, _rec_frames, _rec_active
    if _rec_active:
        return {"ok": False, "msg": "已在录音中"}
    import sounddevice as sd
    _rec_frames = []
    _rec_active = True

    def _cb(indata, frames, t, status):
        if _rec_active:
            _rec_frames.append(indata.copy())

    def _run():
        try:
            with sd.InputStream(samplerate=16000, channels=1, dtype="int16",
                                callback=_cb):
                while _rec_active:
                    time.sleep(0.1)
        except Exception as e:
            print(f"[wechat] 录音异常: {e}", flush=True)

    _rec_thread = threading.Thread(target=_run, daemon=True)
    _rec_thread.start()
    return {"ok": True, "msg": "开始录音，请播放微信语音"}


def stop_record():
    """停止录音并转文字"""
    global _rec_active, _rec_frames
    if not _rec_active:
        return {"ok": False, "msg": "当前未在录音"}
    _rec_active = False
    if _rec_thread:
        _rec_thread.join(timeout=2)
    if not _rec_frames:
        return {"ok": False, "msg": "没录到声音，请检查麦克风/系统音频"}
    audio = np.concatenate(_rec_frames, axis=0)
    # 保存录音文件（调试用）
    fp = os.path.join(REC_DIR, f"rec_{int(time.time())}.wav")
    import soundfile as sf
    sf.write(fp, audio, 16000, subtype="PCM_16")
    # ASR 转文字
    try:
        segs, info = _asr().transcribe(audio.astype(np.float32) / 32768.0,
                                       language="zh", beam_size=5)
        text = "".join(s.text for s in segs).strip()
    except Exception as e:
        return {"ok": False, "msg": f"语音识别失败: {e}", "audio": fp}
    if not text:
        return {"ok": False, "msg": "没识别出文字", "audio": fp}
    # 沉淀：作为对方说的话入训练集（question 留空，answer 为语音内容）
    _save_qa([{"q": "（语音消息）", "a": text, "dir": "voice"}],
             target="wechat_voice")
    return {"ok": True, "text": text, "audio": fp,
            "chars": len(text)}


# ==================== 状态 ====================
def state():
    n = 0
    if os.path.exists(WECHAT_DS):
        try:
            with open(WECHAT_DS, encoding="utf-8") as f:
                n = sum(1 for l in f if l.strip())
        except Exception:
            pass
    return {"dataset_rows": n, "recording": _rec_active}


# ==================== 实时屏幕读取（类豆包） ====================
_mon = {
    "active": False, "hwnd": 0, "interval": 3.0,
    "shot_fp": os.path.join(BASE_DIR, "avatars", "wechat_monitor.png"),
    "latest_text": "", "latest_dialog": [], "latest_qa": 0,
    "last_sig": "", "new_lines": [], "learned_lines": set(),
    "round": 0, "last_error": ""
}
_mon_lock = threading.Lock()
_mon_thread = None

# ==================== 读取区域框选（像截图一样拖框，只读框内） ====================
_REGION = None  # 归一化 [x0,y0,x1,y1]，None=整窗


def set_region(region):
    """设置 OCR 读取区域（相对窗口截图的归一化坐标）；None/空=恢复整窗"""
    global _REGION
    if not region:
        _REGION = None
        return {"ok": True, "region": None, "msg": "已恢复整窗读取"}
    try:
        x0, y0, x1, y1 = [min(1.0, max(0.0, float(v))) for v in list(region)[:4]]
        if x1 - x0 < 0.03 or y1 - y0 < 0.03:
            return {"ok": False, "msg": "区域太小，请重新框选"}
        _REGION = [x0, y0, x1, y1]
        return {"ok": True, "region": _REGION}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


def get_region():
    return {"ok": True, "region": _REGION}


def _apply_region(img):
    """按框选区域裁剪截图（裁剪后再 OCR + 展示，所见即所读）"""
    if not _REGION:
        return img
    w, h = img.size
    x0, y0, x1, y1 = _REGION
    return img.crop((int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h)))


def grab_fullshot(hwnd=0):
    """不裁剪的整窗截图，供前端框选区域时显示"""
    hwnd = int(hwnd or 0) or auto_wechat_hwnd()
    img = _sl.grab(hwnd)
    fp = os.path.join(BASE_DIR, "avatars", "wechat_fullshot.png")
    w, h = img.size
    try:
        if w > 960:
            img = img.resize((960, int(h * 960 / w)), Image.LANCZOS)
        img.save(fp, "PNG", optimize=True)
    except Exception:
        pass
    return {"ok": True, "url": "/avatars/wechat_fullshot.png?t=" + str(int(time.time())),
            "w": w, "h": h, "title": _sl._LAST.get("title", "")}



def _sig(dialog):
    """对话内容指纹，用于检测是否有新内容"""
    return hashlib.md5("\n".join(m["text"] for m in dialog).encode("utf-8")).hexdigest()[:12] \
        if dialog else ""


def _monitor_loop():
    """后台定时截图+OCR 线程"""
    global _mon
    while _mon["active"]:
        try:
            hwnd = _mon["hwnd"] or auto_wechat_hwnd()
            img = _apply_region(_sl.grab(hwnd))
            w, h = img.size
            res, _ = _ocr()(np.array(img))
            dialog = parse_dialog(res, w)
            sig = _sig(dialog)
            # 保存截图供前端显示（压缩到宽 720 减少传输）
            try:
                if w > 720:
                    img = img.resize((720, int(h * 720 / w)), Image.LANCZOS)
                img.save(_mon["shot_fp"], "PNG", optimize=True)
            except Exception:
                pass
            with _mon_lock:
                _mon["latest_dialog"] = dialog
                _mon["latest_text"] = "\n".join(
                    ("对方: " if m["side"] == "L" else "我: ") + m["text"]
                    for m in dialog[:80])
                _mon["latest_qa"] = len(dialog_to_qa(dialog))
                _mon["round"] += 1
                # 检测新内容
                if sig != _mon["last_sig"]:
                    _mon["last_sig"] = sig
                    # 找出未学习过的新行
                    known = _mon["learned_lines"]
                    fresh = [m for m in dialog if m["text"] not in known]
                    if fresh:
                        _mon["new_lines"] = fresh
                        if _mon.get("auto_learn"):
                            qa = dialog_to_qa(fresh)
                            if qa:
                                _save_qa(qa, target="wechat_monitor")
                                for m in fresh:
                                    known.add(m["text"])
                                _mon["learned_lines"] = known
                                _mon["learned_count"] = _mon.get("learned_count", 0) + len(qa)
                _mon["last_error"] = ""
        except Exception as e:
            with _mon_lock:
                _mon["last_error"] = str(e)
            print(f"[wechat] 监控异常: {e}", flush=True)
        # 可中断的 sleep
        for _ in range(int(_mon["interval"] * 10)):
            if not _mon["active"]:
                break
            time.sleep(0.1)


def start_monitor(hwnd=0, interval=3.0, auto_learn=False):
    """开始实时读取微信屏幕"""
    global _mon_thread
    if _mon["active"]:
        return {"ok": False, "msg": "已在实时读取中"}
    with _mon_lock:
        _mon["active"] = True
        _mon["hwnd"] = int(hwnd or 0)
        _mon["interval"] = max(1.0, float(interval))
        _mon["auto_learn"] = bool(auto_learn)
        _mon["new_lines"] = []
        _mon["learned_count"] = 0
        _mon["last_error"] = ""
    _mon_thread = threading.Thread(target=_monitor_loop, daemon=True)
    _mon_thread.start()
    return {"ok": True, "msg": f"开始实时读取（每{_mon['interval']}秒截一次）"}


def stop_monitor():
    """停止实时读取"""
    if not _mon["active"]:
        return {"ok": False, "msg": "当前未在读取"}
    with _mon_lock:
        _mon["active"] = False
    if _mon_thread:
        _mon_thread.join(timeout=2)
    return {"ok": True, "learned_count": _mon.get("learned_count", 0)}


def monitor_state():
    """实时读取状态：截图、最新识别文本、新内容"""
    with _mon_lock:
        shot_url = "/avatars/wechat_monitor.png?t=" + str(int(time.time())) \
            if os.path.exists(_mon["shot_fp"]) else ""
        return {
            "active": _mon["active"],
            "round": _mon["round"],
            "interval": _mon["interval"],
            "auto_learn": _mon.get("auto_learn", False),
            "region": _REGION,
            "shot": shot_url,
            "text": _mon["latest_text"],
            "lines": len(_mon["latest_dialog"]),
            "qa": _mon["latest_qa"],
            "new_count": len(_mon["new_lines"]),
            "new_preview": "\n".join(
                ("对方: " if m["side"] == "L" else "我: ") + m["text"]
                for m in _mon["new_lines"][:30]),
            "learned_count": _mon.get("learned_count", 0),
            "last_error": _mon["last_error"]
        }


def monitor_learn():
    """把当前识别到的新内容写入训练集"""
    with _mon_lock:
        fresh = list(_mon["new_lines"])
        if not fresh:
            return {"ok": False, "msg": "没有新内容可学习"}
        qa = dialog_to_qa(fresh)
        if not qa:
            return {"ok": False, "msg": "新内容里没有成对的问答"}
        added = _save_qa(qa, target="wechat_monitor")
        for m in fresh:
            _mon["learned_lines"].add(m["text"])
        _mon["new_lines"] = []
        _mon["learned_count"] = _mon.get("learned_count", 0) + added
        return {"ok": True, "added": added, "qa": len(qa),
                "preview": qa[:5]}

