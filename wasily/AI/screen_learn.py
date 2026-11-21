# -*- coding: utf-8 -*-
"""从屏幕学习（AI 自我进化）

流程：截屏 → OCR 识别屏幕上的 AI 对话 → LLM 提炼 →
  1) 写入长期记忆（memory_net L2，之后对话可召回）
  2) 生成微调数据集（training_data/screen_learn.jsonl，供 LoRA 训练）
  3) 提炼对方 AI 的说话风格（learned_style.json，注入吴晓欣人设）
"""
import json
import os
import re
import threading
import time

import ctypes
from ctypes import wintypes

import numpy as np
from PIL import Image, ImageGrab

import memory_net

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATASET_FP = os.path.join(BASE_DIR, "training_data", "screen_learn.jsonl")
STYLE_FP = os.path.join(BASE_DIR, "learned_style.json")
SHOT_FP = os.path.join(BASE_DIR, "avatars", "learn_shot.png")

_api = None
_engine = None
_lock = threading.Lock()

# 明显的界面噪音词（按钮/菜单/标题栏），不作为学习内容
_NOISE = re.compile(
    r"^(开始|确定|取消|关闭|发送|返回|设置|菜单|登录|注册|退出|搜索|帮助|分享|"
    r"复制|下载|上传|更多|编辑|删除|新建|保存|刷新|首页|消息|通讯录|收藏|"
    r"聊天|朋友圈|发现|我|文件|图片|视频|音乐|文档|应用|程序|桌面|"
    r"\d{1,2}:\d{2}(:\d{2})?|\d{1,3}%|-{1,2}\s*[○Oo]|[×✕✚+×▽▼▲◀▶]{1,3})$", re.I)


def init(api_mod):
    global _api
    _api = api_mod


def _ocr():
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _engine = RapidOCR()
    return _engine


# ==================== 窗口定位（只截目标 AI 窗口，避开桌面/IDE 干扰） ====================
_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)   # 物理像素，避免与 GetWindowRect 错位
except Exception:
    pass

_user32.GetWindowDC.argtypes = [wintypes.HWND]
_user32.GetWindowDC.restype = wintypes.HDC
_user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
_user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
_user32.PrintWindow.restype = wintypes.BOOL
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
_gdi32.CreateCompatibleDC.restype = wintypes.HDC
_gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
_gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
_gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
_gdi32.SelectObject.restype = wintypes.HGDIOBJ
_gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
_gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
_gdi32.DeleteDC.argtypes = [wintypes.HDC]
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.IsWindowVisible.argtypes = [wintypes.HWND]

# 可能是 AI 对话窗口的标题特征 / 应当排除的自身工具与自身页面窗口
_AI_KW = re.compile(r"(chatgpt|claude|gemini|copilot|kimi|豆包|deepseek|通义|千问|qwen|文心|"
                    r"文小言|讯飞|星火|智谱|清言|天工|元宝|百川|海螺|minimax|grok|perplexity)", re.I)
_SKIP_KW = re.compile(r"(trae|visual studio|powershell|命令提示符|任务管理器|wasily_log|"
                      r"program manager|程序管理器|输入体验|msctfime|default ime|任务栏|"
                      r"训练中心|视频通话|wasily)", re.I)
_LAST = {"hwnd": 0, "title": ""}


class _BMIH(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


def _title_of(hwnd):
    try:
        n = _user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        _user32.GetWindowTextW(hwnd, buf, n + 1)
        return re.sub(r"\s+", " ", buf.value).strip()
    except Exception:
        return ""


def list_windows():
    """枚举可见窗口，供用户选择要学习的 AI 窗口（ai=true 表示标题像 AI 对话）"""
    out = []
    Proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _l):
        try:
            if not _user32.IsWindowVisible(hwnd):
                return True
            title = _title_of(hwnd)
            if not title:
                return True
            r = wintypes.RECT()
            _user32.GetWindowRect(hwnd, ctypes.byref(r))
            w, h = r.right - r.left, r.bottom - r.top
            if w >= 480 and h >= 300:
                out.append({"hwnd": int(hwnd), "title": title[:120], "w": w, "h": h,
                            "ai": bool(_AI_KW.search(title)) and not _SKIP_KW.search(title)})
        except Exception:
            pass
        return True

    try:
        _user32.EnumWindows(Proc(cb), 0)
    except Exception:
        return []
    out.sort(key=lambda x: (not x["ai"], -x["w"] * x["h"]))
    return out[:40]


def auto_hwnd():
    """未指定窗口时：优先标题像 AI 对话的窗口，其次最大的普通窗口（跳过桌面/输入框等）"""
    wins = list_windows()
    for w in wins:
        if w["ai"]:
            return w["hwnd"]
    for w in wins:
        if not _SKIP_KW.search(w["title"]):
            return w["hwnd"]
    return 0


def _print_window(hwnd):
    """PrintWindow 抓窗口内容（被遮挡也能抓，浏览器/Electron 需 RENDERFULLCONTENT）"""
    r = wintypes.RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    if w < 40 or h < 40:
        return None
    dc = _user32.GetWindowDC(hwnd)
    mdc = _gdi32.CreateCompatibleDC(dc)
    bmp = _gdi32.CreateCompatibleBitmap(dc, w, h)
    old = _gdi32.SelectObject(mdc, bmp)
    img = None
    try:
        _user32.PrintWindow(hwnd, mdc, 2)
        bi = _BMIH(ctypes.sizeof(_BMIH), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        if _gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0):
            img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
    except Exception as e:
        print(f"[learn] PrintWindow 失败: {e}", flush=True)
    finally:
        _gdi32.SelectObject(mdc, old)
        _gdi32.DeleteObject(bmp)
        _gdi32.DeleteDC(mdc)
        _user32.ReleaseDC(hwnd, dc)
    return img


def _blank(img):
    """纯黑/纯色（硬件加速窗口 PrintWindow 常见失败结果）"""
    try:
        lo, hi = img.convert("L").getextrema()
        return hi - lo < 8
    except Exception:
        return True


def _window_screen(hwnd):
    """回退方案：置前后用屏幕截图按窗口位置裁剪"""
    _user32.ShowWindow(hwnd, 9)          # SW_RESTORE
    _user32.SetForegroundWindow(hwnd)
    time.sleep(0.4)
    r = wintypes.RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(r))
    return ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom)).convert("RGB")


# ==================== 截屏 + 识别 ====================
def grab(hwnd=0):
    """截目标窗口（默认自动挑 AI 对话窗口）；全部失败时回退整屏"""
    hwnd = int(hwnd or 0) or auto_hwnd()
    img = None
    title = ""
    if hwnd:
        title = _title_of(hwnd) or f"窗口 {hwnd}"
        try:
            cand = _print_window(hwnd)
            if cand is not None and not _blank(cand):
                img = cand
        except Exception as e:
            print(f"[learn] 窗口截图异常: {e}", flush=True)
        if img is None:
            try:
                img = _window_screen(hwnd)
            except Exception as e:
                print(f"[learn] 窗口置前截图失败: {e}", flush=True)
    if img is None:
        img = ImageGrab.grab().convert("RGB")
        title = "整屏"
        w, h = img.size
        img = img.crop((int(w * 0.05), int(h * 0.04), int(w * 0.97), int(h * 0.94)))
    _LAST.update({"hwnd": hwnd, "title": title})
    try:
        img.save(SHOT_FP)
    except Exception:
        pass
    return img


def _merge_lines(res):
    """OCR 结果按纵向位置排序，过滤低置信度与界面噪音"""
    rows = []
    for item in (res or []):
        try:
            box, text, score = item[0], item[1], float(item[2])
        except Exception:
            continue
        t = re.sub(r"\s+", " ", str(text or "")).strip()
        if not t or score < 0.72:
            continue
        if len(t) <= 4 and _NOISE.match(t):
            continue
        ys = sum(p[1] for p in box) / 4.0
        rows.append((ys, t))
    rows.sort(key=lambda r: r[0])
    return [t for _, t in rows]


def screenshot_text(hwnd=0):
    """只做识别，不学习（前端预览用）"""
    img = grab(hwnd)
    res, _ = _ocr()(np.array(img))
    lines = _merge_lines(res)
    return {"lines": len(lines), "chars": len("\n".join(lines)),
            "text": "\n".join(lines)[:4000], "target": _LAST.get("title", "")}


# ==================== LLM 提炼 ====================
_REFINE_SYS = """你是吴晓欣的学习助手。用户会把屏幕上另一个 AI 的对话内容给你看。
请从中提炼：
1) style：对方 AI 的说话风格（语气、句式、口头禅，40字内）；
2) facts：值得长期记住的稳定事实（关于用户的偏好/情况，或明确的知识结论），最多4条；
3) qa：可让吴晓欣学会的问答对（对方回答得好的问题），最多2组。
严格只输出一行 JSON：{"style":"...","facts":["..."],"qa":[{"q":"...","a":"..."}]}
屏幕上没有可学内容时返回 {"style":"","facts":[],"qa":[]}。不要编造屏幕外的内容。"""


def _loads_tolerant(s):
    """7B 模型常输出带裸换行/尾逗号/中文引号的"JSON"，逐级修复后再解析"""
    for cand in (s, s.replace("\n", " ").replace("\r", " "),
                 re.sub(r",\s*([\]}])", r"\1", s.replace("\n", " "))):
        try:
            return json.loads(cand)
        except Exception:
            continue
    return None


def _regex_extract(s):
    """JSON 实在解析不了时，用正则兜底抓 facts / qa / style"""
    def unesc(x):
        return x.replace('\\"', '"').replace("\\n", " ").strip()

    facts = []
    mf = re.search(r'"facts"\s*:\s*\[([\s\S]*?)\]', s)
    if mf:
        facts = [unesc(x) for x in re.findall(r'"((?:[^"\\]|\\.)*)"', mf.group(1)) if unesc(x)]
    qa = []
    for q, a in re.findall(r'"q"\s*:\s*"((?:[^"\\]|\\.)*)"\s*,\s*"a"\s*:\s*"((?:[^"\\]|\\.)*)"', s):
        if unesc(q) and unesc(a):
            qa.append({"q": unesc(q)[:200], "a": unesc(a)[:600]})
    ms = re.search(r'"style"\s*:\s*"((?:[^"\\]|\\.)*)"', s)
    style = unesc(ms.group(1))[:120] if ms else ""
    return {"facts": facts[:5], "qa": qa[:5], "style": style}


def _llm_json(text):
    if not _api:
        return {"facts": [], "qa": [], "style": ""}
    out = ""
    try:
        out = _api.generate_text(
            [{"role": "system", "content": _REFINE_SYS},
             {"role": "user", "content": text[:1200]}],
            max_tokens=300, temperature=0.2).strip()
    except Exception as e:
        print(f"[learn] 提炼失败: {e}", flush=True)
        return {"facts": [], "qa": [], "style": ""}
    m = re.search(r"\{[\s\S]*\}", out)
    raw = m.group(0) if m else ""
    data = _loads_tolerant(raw) if raw else None
    if not isinstance(data, dict):
        data = _regex_extract(raw or out)
        print(f"[learn] JSON 容错解析，原始输出: {out[:200]}", flush=True)
    facts = [str(x).strip() for x in (data.get("facts") or []) if str(x).strip()][:5]
    qa = []
    for p in (data.get("qa") or [])[:5]:
        if isinstance(p, dict) and p.get("q") and p.get("a"):
            qa.append({"q": str(p["q"]).strip()[:200], "a": str(p["a"]).strip()[:600]})
    style = str(data.get("style") or "").strip()[:120]
    return {"facts": facts, "qa": qa, "style": style}


# ==================== 三处沉淀 ====================
def _save_memory(facts):
    if not facts:
        return 0
    memory_net._add_entry(2, "屏幕学习", "；".join(facts)[:600], src="screen_learn")
    return len(facts)


def _save_dataset(qa, facts):
    added = 0
    rows = list(qa)
    if not rows and facts:
        rows = [{"q": "你学到了什么新知识？", "a": "；".join(facts)}]
    if not rows:
        return 0
    os.makedirs(os.path.dirname(DATASET_FP), exist_ok=True)
    with open(DATASET_FP, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps({"ts": int(time.time()), "source": "screen_ai",
                                "instruction": r["q"], "output": r["a"]},
                               ensure_ascii=False) + "\n")
            added += 1
    return added


def _save_style(style):
    if not style:
        return []
    try:
        with open(STYLE_FP, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            data = []
    except Exception:
        data = []
    if style not in [d.get("style") for d in data]:
        data.append({"style": style, "ts": int(time.time())})
    data = data[-6:]
    try:
        with open(STYLE_FP, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except Exception:
        pass
    return [d.get("style") for d in data]


def style_hint():
    """给系统提示词用的风格片段（无内容返回空串）"""
    try:
        with open(STYLE_FP, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return ""
    styles = [d.get("style") for d in data if isinstance(d, dict) and d.get("style")]
    if not styles:
        return ""
    return ("## 你的表达风格（从你学习过的 AI 对话中体会，自然融进说话方式，别说明来源）\n- "
            + "\n- ".join(styles[-4:]))


# ==================== 主入口 ====================
def learn(hwnd=0, use_llm=True):
    """截屏学习一次，返回识别与沉淀结果"""
    with _lock:
        t0 = time.time()
        img = grab(hwnd)
        res, _ = _ocr()(np.array(img))
        lines = _merge_lines(res)
        text = "\n".join(lines)
        out = {"ok": True, "lines": len(lines), "chars": len(text),
               "target": _LAST.get("title", ""),
               "text": text[:1500], "facts": [], "qa": 0, "style": "",
               "memory_added": 0, "dataset_added": 0, "elapsed": 0}
        if len(text) < 12:
            out.update({"ok": False, "msg": "屏幕上没识别到足够文字，请先打开 AI 对话窗口并让它显示内容"})
            out["elapsed"] = round(time.time() - t0, 1)
            return out
        if use_llm:
            r = _llm_json(text)
            out["facts"] = r["facts"]
            out["qa"] = len(r["qa"])
            out["style"] = r["style"]
            out["memory_added"] = _save_memory(r["facts"])
            out["dataset_added"] = _save_dataset(r["qa"], r["facts"])
            _save_style(r["style"])
            if not (r["facts"] or r["qa"] or r["style"]):
                out["msg"] = "识别到文字，但里面没有可学的对话内容（可能截到的是列表/菜单）"
        out["elapsed"] = round(time.time() - t0, 1)
        return out


def state():
    """学习状态：数据集条数 + 最近风格 + 截图预览"""
    n = 0
    try:
        with open(DATASET_FP, encoding="utf-8") as f:
            n = sum(1 for line in f if line.strip())
    except Exception:
        pass
    try:
        with open(STYLE_FP, encoding="utf-8") as f:
            styles = [d.get("style") for d in json.load(f) if d.get("style")]
    except Exception:
        styles = []
    return {"dataset_rows": n, "styles": styles[-6:],
            "last_target": _LAST.get("title", ""),
            "shot": ("/avatars/learn_shot.png" if os.path.exists(SHOT_FP) else "")}
