"""
wasily 语音合成引擎
- edge-tts 在线合成：多中文音色，语速/音调可调（mp3）
- Windows SAPI 离线兜底：无网可用（wav，PowerShell System.Speech）
- 情感预设：edge-tts 免费接口不支持 SSML style，用语速+音调组合近似情感
"""
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import config as _cfg

_log = logging.getLogger("api_server")   # 复用主服务日志（wasily_log/api_server.log）


def _clean_env():
    """子进程环境：剔除非法 PYTHONHASHSEED（否则子解释器启动即 Fatal 崩溃）。"""
    env = dict(os.environ)
    hs = env.get("PYTHONHASHSEED")
    if hs is not None:
        try:
            if not (0 <= int(hs) <= 4294967295):
                env.pop("PYTHONHASHSEED", None)
        except (TypeError, ValueError):
            env.pop("PYTHONHASHSEED", None)
    return env

# ============ 音色目录 ============
VOICES = {
    "wxx_clone": {"name": "吴晓欣·本人克隆（首选）", "id": "wxx_clone"},
    "xiaoxiao": {"name": "晓晓（女·温柔）", "id": "zh-CN-XiaoxiaoNeural"},
    "yunxi":    {"name": "云希（男·阳光）", "id": "zh-CN-YunxiNeural"},
    "yunyang":  {"name": "云扬（男·新闻）", "id": "zh-CN-YunyangNeural"},
    "xiaoyi":   {"name": "晓伊（女·活泼）", "id": "zh-CN-XiaoyiNeural"},
    "yunjian":  {"name": "云健（男·浑厚）", "id": "zh-CN-YunjianNeural"},
    "xiaobei":  {"name": "晓北（女·东北）", "id": "zh-CN-liaoning-XiaobeiNeural"},
    "xiaoni":   {"name": "晓妮（女·陕川）", "id": "zh-CN-shaanxi-XiaoniNeural"},
    "yunye":    {"name": "云野（男· calm）", "id": "zh-CN-YunyeNeural"},
    # ===== 离线语音包（Kokoro 本地神经网络，断网可用、实时合成） =====
    "kokoro":         {"name": "离线·晓晓（女·温柔）", "id": "kokoro:zf_xiaoxiao"},
    "kokoro_xiaobei": {"name": "离线·晓北（女·清脆）", "id": "kokoro:zf_xiaobei"},
    "kokoro_xiaoni":  {"name": "离线·晓妮（女·甜美）", "id": "kokoro:zf_xiaoni"},
    "kokoro_xiaoyi":  {"name": "离线·晓伊（女·活泼）", "id": "kokoro:zf_xiaoyi"},
    "kokoro_yunxi":   {"name": "离线·云希（男·阳光）", "id": "kokoro:zm_yunxi"},
    "kokoro_yunyang": {"name": "离线·云扬（男·磁性）", "id": "kokoro:zm_yunyang"},
}

# Kokoro multi-lang v1.0 中文音色 ID（voices.bin 顺序，勿改）
KOKORO_SID = {
    "zf_xiaobei": 46, "zf_xiaoni": 47, "zf_xiaoxiao": 48, "zf_xiaoyi": 49,
    "zm_yunjian": 50, "zm_yunxi": 51, "zm_yunxia": 52, "zm_yunyang": 53,
}

# ============ 情感预设（rate: 语速, pitch: 音调） ============
EMOTIONS = {
    "normal":  {"label": "平静", "rate": "+0%",  "pitch": "+0Hz"},
    "happy":   {"label": "开心", "rate": "+15%", "pitch": "+18Hz"},
    "gentle":  {"label": "温柔", "rate": "-10%", "pitch": "+6Hz"},
    "serious": {"label": "严肃", "rate": "-5%",  "pitch": "-8Hz"},
    "excited": {"label": "兴奋", "rate": "+22%", "pitch": "+25Hz"},
    "sad":     {"label": "低落", "rate": "-18%", "pitch": "-12Hz"},
}


# 图标/emoji 不朗读：只保留文字（视频通话与网页对话共用）
_SPEECH_ICON_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002190-\U000021FF"
    "\U00002B00-\U00002BFF\U0000E000-\U0000F8FF\uFE0F\u20E3\u200D]")


def _clean_speech(text):
    t = _SPEECH_ICON_RE.sub("", str(text or ""))
    t = re.sub(r"[*_#>|`]+", " ", t)
    t = re.sub(r"https?://\S+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def list_voices():
    """返回音色目录与情感预设（供前端下拉）"""
    return {"voices": VOICES, "emotions": EMOTIONS}


def _edge_inproc(text, voice, rate, pitch, out_path):
    """进程内 edge_tts 合成（每次 asyncio.run 新建独立事件循环，避免长驻进程事件循环兼容问题）。
    省去 CLI 方式每句 ~2s 的 Python 解释器启动开销。"""
    import asyncio
    import edge_tts

    async def _go():
        kw = {}
        if voice:
            kw["voice"] = voice
        if rate and rate != "+0%":
            kw["rate"] = rate
        if pitch and pitch != "+0Hz":
            kw["pitch"] = pitch
        await edge_tts.Communicate(text.strip(), **kw).save(out_path)

    asyncio.run(_go())


def tts_edge(text, voice=None, rate="+0%", pitch="+0Hz"):
    """
    edge-tts 在线合成。优先进程内 API（快）；edge_tts 未安装时回退 CLI 子进程。
    """
    if not text or not text.strip():
        return None
    for attempt in range(2):   # 最多重试 2 次（应对网络瞬断）
        tmpdir = tempfile.mkdtemp(prefix="wasily_tts_")
        out_path = os.path.join(tmpdir, "tts.mp3")
        try:
            try:
                _edge_inproc(text, voice, rate, pitch, out_path)
            except ImportError:
                # 回退：CLI 子进程（独立网络栈，最兼容）
                cmd = [sys.executable, "-m", "edge_tts",
                       "--text", text.strip(),
                       "--write-media", out_path]
                if voice:
                    cmd += ["--voice", voice]
                if rate and rate != "+0%":
                    cmd += ["--rate", rate]
                if pitch and pitch != "+0Hz":
                    cmd += ["--pitch", pitch]
                r = subprocess.run(cmd, capture_output=True, timeout=30,
                                   env=_clean_env())
                if r.returncode != 0:
                    stderr = (r.stderr or b"").decode("utf-8", errors="replace")[:200]
                    raise RuntimeError(f"CLI rc={r.returncode} {stderr}")
            if os.path.exists(out_path):
                with open(out_path, "rb") as f:
                    data = f.read()
                if len(data) > 200:
                    return data
                _log.warning(f"[tts] edge-tts 产出空文件({len(data)}B)")
            else:
                _log.warning(f"[tts] edge-tts 第{attempt+1}次未产出文件")
                if attempt == 0:
                    time.sleep(1.5)
        except FileNotFoundError:
            return None   # edge-tts 未安装
        except Exception as e:
            _log.warning(f"[tts] edge-tts 异常: {type(e).__name__}: {str(e)[:150]}")
            if attempt == 0:
                time.sleep(1.5)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    return None


def tts_sapi(text, rate=0):
    """Windows SAPI 离线合成（PowerShell System.Speech），返回 wav 字节；失败返回 None"""
    if not text or not text.strip():
        return None
    tmpdir = tempfile.mkdtemp(prefix="wasily_tts_")
    txt_path = os.path.join(tmpdir, "in.txt")
    wav_path = os.path.join(tmpdir, "out.wav")
    ps1_path = os.path.join(tmpdir, "speak.ps1")
    try:
        with open(txt_path, "w", encoding="utf-8-sig") as f:
            f.write(text)
        rate = max(-10, min(10, int(rate)))
        with open(ps1_path, "w", encoding="utf-8-sig") as f:
            f.write(
                "Add-Type -AssemblyName System.Speech\n"
                "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
                f"$s.Rate = {rate}\n"
                "$t = Get-Content -Raw -Encoding UTF8 '" + txt_path + "'\n"
                "$s.SetOutputToWaveFile('" + wav_path + "')\n"
                "$s.Speak($t)\n"
                "$s.Dispose()\n")
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1_path],
            capture_output=True, timeout=60)
        if r.returncode == 0 and os.path.exists(wav_path):
            with open(wav_path, "rb") as f:
                data = f.read()
            if len(data) > 100:   # 46字节空wav(只有头)视为失败
                return data
            _log.warning(f"[tts] SAPI 产出空wav({len(data)}B)，视为失败 stderr={r.stderr[:150]!r}")
        else:
            _log.warning(f"[tts] SAPI 失败 rc={r.returncode} stderr={r.stderr[:150]!r}")
    except Exception:
        pass
    finally:
        try:
            for p in (txt_path, wav_path, ps1_path):
                if os.path.exists(p):
                    os.remove(p)
            os.rmdir(tmpdir)
        except OSError:
            pass
    return None


# ============ F5-TTS 声音克隆（吴晓欣真人音色，CPU 全离线） ============
_BASE = os.path.dirname(os.path.abspath(__file__))


# ============ Kokoro 离线语音包（本地神经网络，实时合成，断网可用） ============
_KOKORO_DIR = os.path.join(_BASE, "models", "kokoro-multi-lang-v1_0")
_KOKORO = None
_KOKORO_LOCK = threading.Lock()


def _get_kokoro():
    """懒加载 Kokoro TTS（首次 ~几秒，之后常驻内存，线程安全）"""
    global _KOKORO
    if _KOKORO is not None:
        return _KOKORO
    with _KOKORO_LOCK:
        if _KOKORO is not None:
            return _KOKORO
        import sherpa_onnx
        d = _KOKORO_DIR
        cfg = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                    model=os.path.join(d, "model.onnx"),
                    voices=os.path.join(d, "voices.bin"),
                    tokens=os.path.join(d, "tokens.txt"),
                    lexicon=os.path.join(d, "lexicon-us-en.txt") + "," +
                            os.path.join(d, "lexicon-zh.txt"),
                ),
                provider="cpu", num_threads=4, debug=False),
            max_num_sentences=2)
        if not cfg.validate():
            raise RuntimeError("Kokoro 配置校验失败（模型文件缺失？）")
        _KOKORO = sherpa_onnx.OfflineTts(cfg)
        _log.info(f"[tts] Kokoro 离线语音包加载完成，{_KOKORO.num_speakers} 个音色")
        return _KOKORO


def tts_kokoro(text, speaker="zf_xiaoxiao", speed=None):
    """Kokoro 离线合成。返回 (wav_bytes, 'audio/wav') 或 (None, err)"""
    try:
        tts = _get_kokoro()
    except Exception as e:
        return None, f"Kokoro 加载失败: {str(e)[:150]}"
    sid = KOKORO_SID.get(speaker, KOKORO_SID["zf_xiaoxiao"])
    if speed is None:
        try:
            speed = float(_cfg.get("tts.kokoro_speed", 1.1))
        except (TypeError, ValueError):
            speed = 1.1
    try:
        t0 = time.time()
        with _KOKORO_LOCK:   # 推理非线程安全
            audio = tts.generate(text.strip(), sid=sid, speed=speed)
        if not audio or not audio.samples:
            return None, "Kokoro 产出空音频"
        import numpy as np
        import wave
        import io
        samples = np.asarray(audio.samples, dtype=np.float32)
        pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(audio.sample_rate)
            w.writeframes(pcm.tobytes())
        _log.info(f"[tts] Kokoro合成 {len(text)}字 用时{time.time()-t0:.1f}s (sid={sid})")
        return buf.getvalue(), "audio/wav"
    except Exception as e:
        return None, f"Kokoro 合成失败: {str(e)[:150]}"



_F5_REF = os.path.join(_BASE, "_archive", "voice_samples", "ref_speech_10s.wav")
_F5_REF_TXT = _F5_REF + ".txt"
_F5_CKPT = r"D:\f5_tts\models\F5-TTS\F5TTS_v1_Base\model_1250000.safetensors"
_F5_VOCAB = r"D:\f5_tts\models\F5-TTS\F5TTS_v1_Base\vocab.txt"
_F5_VOCODER = r"D:\f5_tts\models\vocos"
_F5 = None
_F5_LOCK = threading.Lock()

# 克隆合成实时进度（训练页「测试克隆」显示步数/剩余时间用）
_CLONE_PROG = {"running": False, "step": 0, "total": 0, "text": "", "started": 0.0}


def clone_progress():
    """当前克隆合成进度：running/step/total/percent/elapsed/eta/text"""
    p = dict(_CLONE_PROG)
    if p.get("running") and p.get("started"):
        p["elapsed"] = max(0, int(time.time() - p["started"]))
        if p.get("step") and p.get("total"):
            p["percent"] = round(min(99.0, p["step"] / p["total"] * 100), 1)
            p["eta"] = int(p["elapsed"] / p["step"] * max(0, p["total"] - p["step"]))
    return p


# ============ 克隆合成缓存（同一音色卡+同一文本只合成一次，重复播放零延迟） ============
import hashlib
import json as _json

_CLONE_CACHE_DIR = os.path.join(_BASE, "avatars", "tts_cache")
_CLONE_CACHE_INDEX = os.path.join(_CLONE_CACHE_DIR, "clone_index.json")
_CLONE_CACHE_MAX = 500          # 最多缓存条数，超出按最旧淘汰
_CACHE_LOCK = threading.Lock()


def _clone_cache_key(text, ref, nfe, sway=-1.0, cfg_strength=2.0):
    """缓存键：文本 + 参考音(含修改时间) + 推理步数 + sway/cfg + 微调权重。
    参考音、微调模型或丝滑参数一变，旧缓存自动失效。"""
    try:
        ref_mt = os.path.getmtime(ref) if ref and os.path.isfile(ref) else 0
    except OSError:
        ref_mt = 0
    ckpt, ckpt_mt = "", 0
    try:
        ft = str(_cfg.get("voice.f5_ckpt") or "")
        ckpt = ft if ft and os.path.isfile(ft) else _F5_CKPT
        ckpt_mt = os.path.getmtime(ckpt) if os.path.isfile(ckpt) else 0
    except Exception:
        pass
    raw = (f"{text}|{os.path.basename(ref or '')}:{ref_mt}|{nfe}"
           f"|{sway}|{cfg_strength}"
           f"|{os.path.basename(ckpt)}:{ckpt_mt}")
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _clone_cache_index():
    try:
        with open(_CLONE_CACHE_INDEX, encoding="utf-8") as f:
            d = _json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _clone_cache_save(idx):
    try:
        os.makedirs(_CLONE_CACHE_DIR, exist_ok=True)
        with open(_CLONE_CACHE_INDEX, "w", encoding="utf-8") as f:
            _json.dump(idx, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def clone_cache_get(text, ref, nfe, sway=-1.0, cfg_strength=2.0):
    """命中返回 wav bytes，未命中返回 None"""
    key = _clone_cache_key(text, ref, nfe, sway, cfg_strength)
    with _CACHE_LOCK:
        idx = _clone_cache_index()
        ent = idx.get(key)
        if not ent:
            return None
        fp = os.path.join(_CLONE_CACHE_DIR, ent.get("file", ""))
        try:
            with open(fp, "rb") as f:
                data = f.read()
        except OSError:
            return None
        if len(data) <= 1000:
            return None
        ent["hits"] = int(ent.get("hits") or 0) + 1
        ent["last"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _clone_cache_save(idx)
    _log.info(f"[tts] 克隆缓存命中（第{ent['hits']}次）: {text[:16]}")
    return data


def clone_cache_put(text, ref, nfe, data, sway=-1.0, cfg_strength=2.0):
    """合成成功后写入缓存"""
    key = _clone_cache_key(text, ref, nfe, sway, cfg_strength)
    fn = f"c_{key}.wav"
    try:
        os.makedirs(_CLONE_CACHE_DIR, exist_ok=True)
        with open(os.path.join(_CLONE_CACHE_DIR, fn), "wb") as f:
            f.write(data)
        with _CACHE_LOCK:
            idx = _clone_cache_index()
            idx[key] = {"file": fn, "text": text[:80],
                        "sample": os.path.basename(ref or ""),
                        "nfe": nfe, "size": len(data), "hits": 0,
                        "created": time.strftime("%Y-%m-%d %H:%M:%S")}
            if len(idx) > _CLONE_CACHE_MAX:   # 容量淘汰：最旧的先删
                olds = sorted(idx.items(),
                              key=lambda kv: kv[1].get("created", ""))
                for k2, e2 in olds[:len(idx) - _CLONE_CACHE_MAX]:
                    try:
                        os.remove(os.path.join(_CLONE_CACHE_DIR,
                                               e2.get("file", "")))
                    except OSError:
                        pass
                    idx.pop(k2, None)
            _clone_cache_save(idx)
    except Exception as e:
        _log.warning(f"[tts] 克隆缓存写入失败: {e}")


def clone_cache_info():
    """缓存统计（训练页展示用）"""
    idx = _clone_cache_index()
    total_size = sum(int(e.get("size") or 0) for e in idx.values())
    return {"count": len(idx), "total_size": total_size}


def clone_cache_list(sample=None):
    """列出已合成缓存的句子（训练页逐句试听用）。sample 为参考音文件名，传入则只列该音色卡的。"""
    idx = _clone_cache_index()
    base = os.path.basename(sample) if sample else None
    out = []
    for e in idx.values():
        if base and e.get("sample") != base:
            continue
        out.append({"text": e.get("text", ""),
                    "size": int(e.get("size") or 0),
                    "nfe": e.get("nfe"),
                    "hits": int(e.get("hits") or 0),
                    "created": e.get("created", "")})
    out.sort(key=lambda x: x.get("created", ""), reverse=True)
    return out


def _f5_ref_text(ref_path=None):
    """参考音文本：缓存文件优先，否则用本地 faster-whisper 转写一次。
    缓存文件与参考音同名 .txt —— 换音色卡（不同 ref）互不污染。"""
    ref = ref_path or _F5_REF
    txt_fp = ref + ".txt"
    try:
        with open(txt_fp, encoding="utf-8") as f:
            t = f.read().strip()
            if t:
                return t
    except OSError:
        pass
    try:
        import silk_voice
        # 参考音转写用 small 模型：F5 对齐对文本准确度敏感，base 容易出乱码繁体
        t, err = silk_voice.stt_transcribe(ref, model_size="small")
        if t:
            with open(txt_fp, "w", encoding="utf-8") as f:
                f.write(t)
            return t
        _log.warning(f"[tts] 参考音转写为空: {err}")
    except Exception as e:
        _log.warning(f"[tts] 参考音转写失败: {e}")
    return ""


def _get_f5():
    """懒加载 F5-TTS（约16s，~1.3GB 模型常驻）。
    若训练中心已完成离线微调（voice.f5_ckpt），优先加载微调后的权重。"""
    global _F5
    if _F5 is None:
        with _F5_LOCK:
            if _F5 is None:
                ckpt = _F5_CKPT
                try:
                    ft = str(_cfg.get("voice.f5_ckpt") or "")
                    if ft and os.path.isfile(ft):
                        ckpt = ft
                        _log.info(f"[tts] 使用微调声音模型: {os.path.basename(os.path.dirname(ckpt))}")
                except Exception:
                    pass
                if not os.path.exists(ckpt):
                    raise RuntimeError(f"F5-TTS 模型缺失: {ckpt}")
                os.environ.setdefault("HF_HUB_OFFLINE", "1")
                os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
                # torchaudio.load 依赖 torchcodec（未装）；用 soundfile 补丁替代
                import soundfile as _sf
                import torch as _torch
                import torchaudio as _ta
                def _sf_load(path, **kw):
                    d, sr = _sf.read(path, dtype="float32", always_2d=True)
                    return _torch.from_numpy(d.T.copy()), sr
                _ta.load = _sf_load
                from f5_tts.api import F5TTS
                t0 = time.time()
                _F5 = F5TTS(model="F5TTS_v1_Base", ckpt_file=ckpt,
                            vocab_file=_F5_VOCAB, vocoder_local_path=_F5_VOCODER,
                            device="cpu")
                _log.info(f"[tts] F5-TTS 加载完成 {time.time()-t0:.1f}s")
                # 进度探针：euler 求解器每个推理步调用一次 fn，包一层记录实时步数
                try:
                    import f5_tts.model.cfm as _cfm_mod
                    if not getattr(_cfm_mod, "_wasily_probe", False):
                        _orig_odeint = _cfm_mod.odeint
                        def _odeint_probe(fn, y0, t, **kw):
                            _CLONE_PROG["total"] = max(1, len(t) - 1)
                            cnt = [0]
                            def fn2(tt, yy):
                                cnt[0] += 1
                                _CLONE_PROG["step"] = cnt[0]
                                return fn(tt, yy)
                            return _orig_odeint(fn2, y0, t, **kw)
                        _cfm_mod.odeint = _odeint_probe
                        _cfm_mod._wasily_probe = True
                except Exception:
                    pass
    return _F5


def _resolve_clone_ref(ref_path=None):
    """解析克隆参考音：显式指定 → 当前选中音色卡 → 默认 ref_speech_10s.wav"""
    ref = ref_path
    if not ref:
        cur = ""
        try:
            cur = _cfg.get("voice.clone_ref_cur") or ""
        except Exception:
            pass
        if cur:
            cur_clean = str(cur).replace("\\", "/").lstrip("/")
            cand = os.path.join(_BASE, "_archive", "voice_samples", cur_clean)
            if ".." not in cur_clean and os.path.isfile(cand):
                ref = cand
    return ref or _F5_REF


def _clone_params(nfe_step=None):
    """克隆推理参数：nfe 步数 / sway 丝滑 / cfg 强度（配置可覆盖）"""
    nfe = nfe_step or int(_cfg.get("tts.clone_nfe_step", 16) or 16)
    try:
        sway = float(_cfg.get("tts.clone_sway", -1))
    except (TypeError, ValueError):
        sway = -1.0
    try:
        cfg_strength = float(_cfg.get("tts.clone_cfg", 2.0))
    except (TypeError, ValueError):
        cfg_strength = 2.0
    return nfe, sway, cfg_strength


_CLAUSE_SPLIT_RE = re.compile(r"(?<=[，。！？；、…,.!?;~—])")
_CLAUSE_STRIP_RE = re.compile(r"[\s，。！？；、…,.!?;~—]")


def _split_clauses(text):
    """按标点拆子句；有效字≤2 的过短子句并入相邻句（F5 对极短文本合成质量差）。"""
    parts = [p for p in _CLAUSE_SPLIT_RE.split(text) if p and p.strip()]
    if not parts:
        return [text]
    out = []
    for p in parts:
        core = _CLAUSE_STRIP_RE.sub("", p)
        if out and 0 < len(core) <= 2:
            out[-1] += p            # 过短 → 并入前一句
        else:
            out.append(p)
    if len(out) > 1:                # 首句过短 → 并入后一句
        if 0 < len(_CLAUSE_STRIP_RE.sub("", out[0])) <= 2:
            out[1] = out[0] + out[1]
            out.pop(0)
    return [p.strip() for p in out if p.strip()] or [text]


def _concat_wavs(wav_list, gap_ms=80):
    """多段 wav bytes 拼接：段间插 gap_ms 静音。返回拼接后 wav bytes 或 None。"""
    import io as _io
    import numpy as _np
    import soundfile as _sf
    pcms, sr = [], None
    for b in wav_list:
        try:
            d, s = _sf.read(_io.BytesIO(b), dtype="float32")
        except Exception:
            continue
        if getattr(d, "ndim", 1) > 1:
            d = d[:, 0]
        if sr is None:
            sr = s
        if s != sr:
            continue
        pcms.append(d)
    if not pcms or not sr:
        return None
    gap = _np.zeros(int(sr * gap_ms / 1000), dtype="float32")
    segs = []
    for i, d in enumerate(pcms):
        if i:
            segs.append(gap)
        segs.append(d)
    buf = _io.BytesIO()
    _sf.write(buf, _np.concatenate(segs), sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def tts_clone_segmented(text, nfe_step=None, ref_path=None, gap_ms=80):
    """分句缓存克隆合成：整句缓存优先 → 拆子句各自缓存/合成 → PCM 拼接。
    重复子句跨对话复用（0s 秒播），长回复只需合成没见过的子句。
    返回 (bytes, 'audio/wav') 或 (None, err)，签名与 tts_clone 一致。"""
    if not text or not text.strip():
        return None, "空文本"
    clean = _clean_speech(text)
    if not clean:
        return None, "空文本"
    ref = _resolve_clone_ref(ref_path)
    if not os.path.exists(ref):
        return None, f"参考音缺失: {ref}"
    nfe, sway, cfg_strength = _clone_params(nfe_step)
    # 整句缓存：完全相同的回复直接秒播
    cached = clone_cache_get(clean, ref, nfe, sway, cfg_strength)
    if cached:
        return cached, "audio/wav"
    clauses = _split_clauses(clean)
    if len(clauses) <= 1:
        return tts_clone(clean, nfe_step=nfe_step, ref_path=ref_path)
    t0 = time.time()
    wavs = []
    for c in clauses:
        b, err = tts_clone(c, nfe_step=nfe_step, ref_path=ref_path)   # 子句各自走缓存
        if not b:
            return None, f"子句合成失败({c[:10]}): {err}"
        wavs.append(b)
    data = _concat_wavs(wavs, gap_ms)
    if not data:
        return None, "音频拼接失败"
    _log.info(f"[tts] 分句合成 {len(clauses)}段 总用时{time.time()-t0:.1f}s: {clean[:20]}")
    clone_cache_put(clean, ref, nfe, data, sway, cfg_strength)   # 整句也入缓存
    return data, "audio/wav"


def tts_clone(text, nfe_step=None, ref_path=None):
    """F5-TTS 声音克隆。ref_path 可指定音色卡参考音，默认 ref_speech_10s.wav。
    返回 (bytes, 'audio/wav') 或 (None, err)"""
    if not text or not text.strip():
        return None, "空文本"
    text = _clean_speech(text)
    if not text:
        return None, "空文本"
    ref = _resolve_clone_ref(ref_path)
    if not os.path.exists(ref):
        return None, f"参考音缺失: {ref}"
    nfe, sway, cfg_strength = _clone_params(nfe_step)
    # 缓存命中：同一音色卡+同一文本+同一丝滑参数直接返回，不再重复合成（秒播）
    cached = clone_cache_get(text, ref, nfe, sway, cfg_strength)
    if cached:
        return cached, "audio/wav"
    # 子进程隔离合成：F5 峰值内存数 GB，与 7B 主进程叠加曾致整机 OOM 杀掉服务
    tmp = os.path.join(tempfile.gettempdir(), f"wasily_clone_{int(time.time()*1000)}.wav")
    t0 = time.time()
    try:
        cli = os.path.join(_BASE, "clone_cli.py")
        _CLONE_PROG.update({"running": True, "step": 0, "total": nfe,
                            "text": text[:30], "started": time.time()})
        try:
            with _F5_LOCK:   # 串行化：多个并发请求各自拉数 GB 子进程会把内存打爆
                r = subprocess.run(
                    [sys.executable, cli, text.strip(), ref, tmp,
                     str(nfe), str(sway), str(cfg_strength)],
                    env=_clean_env(), capture_output=True, timeout=1800)
        finally:
            _CLONE_PROG["running"] = False
        if r.returncode != 0:
            err = (r.stderr or b"").decode("utf-8", errors="replace").strip()
            return None, f"克隆合成失败: {(err or '子进程异常退出')[-150:]}"
    except subprocess.TimeoutExpired:
        return None, "克隆合成超时（>30min）"
    except Exception as e:
        return None, f"克隆合成失败: {str(e)[:150]}"
    try:
        if os.path.exists(tmp) and os.path.getsize(tmp) > 1000:
            with open(tmp, "rb") as f:
                data = f.read()
            _log.info(f"[tts] F5克隆合成 {len(text)}字 用时{time.time()-t0:.1f}s "
                      f"(ref={os.path.basename(ref)} nfe={nfe} sway={sway})")
            clone_cache_put(text, ref, nfe, data, sway, cfg_strength)   # 写入缓存：下次同句秒播
            return data, "audio/wav"
        return None, "克隆合成产出空音频"
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def synthesize(text, voice=None, emotion="normal", rate=None, pitch=None):
    """统一入口。返回 (bytes, mime) 或 (None, err_msg)"""
    text = _clean_speech(text)
    if not text:
        return None, "空文本"
    voice_key = voice or _cfg.get("tts.voice", "zh-CN-XiaoxiaoNeural")
    # Kokoro 离线语音包：voice_key 可以是短名 "kokoro"/"kokoro_xiaoxi..."（经 VOICES 映射成
    # "kokoro:<speaker>"），也可以直接是 "kokoro:<speaker>"。断网可用、CPU 实时合成。
    if voice_key in VOICES and str(VOICES[voice_key]["id"]).startswith("kokoro:"):
        voice_key = VOICES[voice_key]["id"]
    if str(voice_key).startswith("kokoro:"):
        spk = str(voice_key)[7:]
        emo = EMOTIONS.get(emotion or "normal", EMOTIONS["normal"])
        # 语速百分比 → kokoro speed 倍率
        spd = None
        rs = rate if rate is not None else (emo["rate"] if emotion not in (None, "normal")
                                            else _cfg.get("tts.rate", "+0%"))
        try:
            pct = int(str(rs).replace("%", "").replace("+", ""))
            if pct:
                spd = max(0.6, min(1.8, 1.1 * (1 + pct / 100.0)))
        except (TypeError, ValueError):
            pass
        audio, mime = tts_kokoro(text, speaker=spk, speed=spd)
        if audio:
            return audio, mime
        _log.warning(f"[tts] Kokoro 合成失败回退 SAPI: {mime}")
        audio = tts_sapi(text, 0)
        if audio:
            return audio, "audio/wav"
        return None, f"Kokoro 与 SAPI 均失败: {mime}"
    # 本地离线合成（Windows SAPI 系统语音）：不联网、不依赖 edge-tts
    if voice_key in ("local", "sapi", "offline"):
        try:
            r10 = int(str(rate or _cfg.get("tts.rate", "+0%")).replace("%", "").replace("+", "")) // 5
        except (TypeError, ValueError):
            r10 = 0
        audio = tts_sapi(text, max(-10, min(10, r10)))
        if audio:
            return audio, "audio/wav"
        _log.warning("[tts] 本地离线合成失败，回退 edge-tts")
        voice_key = "zh-CN-XiaoxiaoNeural"
    # 音色卡：voice="clone:<样本名>" 指定 voice_samples 下的参考音做 F5 克隆
    if voice_key.startswith("clone:"):
        sample = voice_key[6:].replace("\\", "/").lstrip("/")
        cand = os.path.join(_BASE, "_archive", "voice_samples", sample) if ".." not in sample else ""
        if cand and not os.path.isfile(cand):
            # 宽容解析：clone:wxx → 找 wxx 开头的音频文件（如 wxx_ref_female.wav）
            try:
                import glob as _glob
                hits = [p for p in _glob.glob(cand + "*")
                        if os.path.splitext(p)[1].lower() in (".wav", ".mp3", ".m4a", ".flac", ".ogg")]
                if hits:
                    cand = sorted(hits)[0]
                    _log.info(f"[tts] 音色卡 {sample} 解析为 {os.path.basename(cand)}")
            except Exception:
                pass
        if cand and os.path.isfile(cand):
            audio, mime = tts_clone(text, ref_path=cand)
            if audio:
                return audio, mime
            _log.warning(f"[tts] 音色卡克隆失败回退: {mime}")
        else:
            _log.warning(f"[tts] 音色卡样本不存在: {sample}")
        voice_key = "wxx_clone"
    # 吴晓欣真人克隆音色：F5-TTS 本地离线合成，失败自动回退晓晓
    if voice_key == "wxx_clone":
        audio, mime = tts_clone(text)
        if audio:
            return audio, mime
        _log.warning(f"[tts] 克隆合成失败回退 edge-tts: {mime}")
        voice_key = "zh-CN-XiaoxiaoNeural"
    if voice_key in VOICES:                       # 支持短名或完整 id
        voice_id = VOICES[voice_key]["id"]
    else:
        voice_id = voice_key

    emo = EMOTIONS.get(emotion or "normal", EMOTIONS["normal"])
    rate = rate if rate is not None else _cfg.get("tts.rate", "+0%") if emotion in (None, "normal") else emo["rate"]
    pitch = pitch or emo["pitch"]

    audio = tts_edge(text, voice_id, rate, pitch)
    if audio:
        return audio, "audio/mpeg"
    if _cfg.get("tts.fallback_sapi", True):
        # 语速百分比 → SAPI -10~10
        try:
            r10 = int(str(rate).replace("%", "").replace("+", "")) // 5
        except (TypeError, ValueError):
            r10 = 0
        audio = tts_sapi(text, r10)
        if audio:
            return audio, "audio/wav"
    return None, "edge-tts 与 SAPI 合成均失败（检查网络或安装 edge-tts）"


if __name__ == "__main__":
    b, m = synthesize("你好，我是瓦西里，这是语音合成测试。", emotion="happy")
    out = os.path.join(tempfile.gettempdir(), f"wasily_tts_test_{int(time.time())}.mp3")
    if b:
        with open(out, "wb") as f:
            f.write(b)
        print("OK", m, out, len(b), "bytes")
    else:
        print("FAIL", m)
