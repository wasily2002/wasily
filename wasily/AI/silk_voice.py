"""
wasily 语音识别与微信 silk 编解码
- STT：faster-whisper 本地离线识别（tiny 模型 int8，本地目录离线加载）
- silk：pilk 库实现微信 silk v3 ↔ wav 双向转换（语音条互通）
依赖缺失时返回明确提示文本，可通过对话"安装工具"能力自动安装
"""
import os
import struct
import subprocess
import tempfile
import wave

# 兼容补丁：av>=19 移除了 av.open 的 metadata_errors 参数，
# 而 faster-whisper(1.1.x) 仍会传它 —— 这里剥掉该参数（av14 无 py312 轮子，无法降级）
try:
    import av as _av
    _av_open_orig = _av.open
    def _av_open_compat(*args, **kwargs):
        kwargs.pop("metadata_errors", None)
        return _av_open_orig(*args, **kwargs)
    _av.open = _av_open_compat
except ImportError:
    pass

# faster-whisper 模型缓存（进程内单例）
_stt_model = None
_stt_size = None


def deps_ok():
    """检查依赖状态"""
    out = {}
    try:
        import faster_whisper  # noqa
        out["stt"] = True
    except ImportError:
        out["stt"] = False
    try:
        import pilk  # noqa
        out["silk"] = True
    except ImportError:
        out["silk"] = False
    return out


def _get_stt_model(size="base"):
    """懒加载 whisper 模型（int8 CPU，节省内存）
    默认 base：本地目录 D:\\f5_tts\\models\\fw-base（由 whisper-base 离线转换为
    CTranslate2 int8），中文短句识别率显著高于 tiny，CPU 上 3.5s 音频约 1s"""
    global _stt_model, _stt_size
    if _stt_model is not None and _stt_size == size:
        return _stt_model
    from faster_whisper import WhisperModel
    local = {"tiny": r"D:\f5_tts\models\fw-tiny", "base": r"D:\f5_tts\models\fw-base",
             "small": r"D:\f5_tts\models\fw-hub\models--Systran--faster-whisper-small\snapshots\536b0662742c02347bc0e980a01041f333bce120"}
    src = local.get(size, size)
    if not (isinstance(src, str) and os.path.isdir(src)):
        src = size   # 本地目录不存在则按模型名在线下载
    _stt_model = WhisperModel(src, device="cpu", compute_type="int8")
    _stt_size = size
    return _stt_model


def stt_transcribe(audio_path, language="zh", model_size="base"):
    """语音转文字。支持 webm/mp3/wav 等容器（faster-whisper 内建 PyAV 解码）
    返回 (text, err)；成功 err=None"""
    try:
        model = _get_stt_model(model_size)
    except ImportError:
        return None, "faster-whisper 未安装。可对我说：安装 faster-whisper"
    except Exception as e:
        return None, f"识别模型加载失败: {str(e)[:150]}"
    try:
        segments, info = model.transcribe(audio_path, language=language,
                                          vad_filter=True,
                                          initial_prompt="以下是简体中文普通话日常对话。")
        text = "".join(seg.text for seg in segments).strip()
        return text, None
    except Exception as e:
        return None, f"识别失败: {str(e)[:150]}"


def silk_decode_to_wav(silk_path, sample_rate=24000):
    """微信 silk → wav。返回 wav 路径，失败返回 None"""
    try:
        import pilk
    except ImportError:
        return None
    pcm_path = silk_path + ".pcm"
    wav_path = os.path.splitext(silk_path)[0] + ".wav"
    try:
        pilk.decode(silk_path, pcm_path, sample_rate)
        with open(pcm_path, "rb") as pf:
            pcm = pf.read()
        with wave.open(wav_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm)
        return wav_path
    except Exception:
        return None
    finally:
        try:
            if os.path.exists(pcm_path):
                os.remove(pcm_path)
        except OSError:
            pass


def silk_encode_from_wav(wav_path, silk_path=None):
    """wav → 微信 silk v3（发微信语音条用）。返回 silk 路径，失败返回 None"""
    try:
        import pilk
    except ImportError:
        return None
    silk_path = silk_path or (os.path.splitext(wav_path)[0] + ".silk")
    try:
        pilk.encode(wav_path, silk_path, pcm_rate=24000)
        return silk_path
    except Exception:
        return None


def stt_from_silk(silk_path, language="zh"):
    """微信语音 silk → 文字（先解码 wav 再 whisper）"""
    wav = silk_decode_to_wav(silk_path)
    if not wav:
        return None, "silk 解码失败（需安装 pilk）"
    return stt_transcribe(wav, language)


if __name__ == "__main__":
    print("依赖状态:", deps_ok())
