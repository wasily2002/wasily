# -*- coding: utf-8 -*-
"""clone_cli.py — F5-TTS 克隆合成子进程

为什么独立进程：F5 推理峰值内存数 GB，与 7B 主服务进程（~6GB）叠加曾多次把
16GB 整机内存打爆、主服务被系统杀掉。子进程隔离后，即使 OOM 也只死自己，
且合成完内存立刻释放。

用法: python clone_cli.py <text> <ref_wav> <out_wav> [nfe] [sway] [cfg_strength]
退出码 0=成功；失败信息写 stderr。
"""
import os
import sys

_BASE = os.path.dirname(os.path.abspath(__file__))
_F5_CKPT = r"D:\f5_tts\models\F5-TTS\F5TTS_v1_Base\model_1250000.safetensors"
_F5_VOCAB = r"D:\f5_tts\models\F5-TTS\F5TTS_v1_Base\vocab.txt"
_F5_VOCODER = r"D:\f5_tts\models\vocos"


def main():
    text, ref, out = sys.argv[1], sys.argv[2], sys.argv[3]
    nfe = int(sys.argv[4]) if len(sys.argv) > 4 else 16
    sway = float(sys.argv[5]) if len(sys.argv) > 5 else -1.0
    cfg_strength = float(sys.argv[6]) if len(sys.argv) > 6 else 2.0
    sys.path.insert(0, _BASE)
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

    import config as _cfg
    _cfg.load()
    ckpt = str(_cfg.get("voice.f5_ckpt") or "")
    if not (ckpt and os.path.isfile(ckpt)):
        ckpt = _F5_CKPT
    print(f"[clone] ckpt={os.path.basename(ckpt)}", flush=True)

    # 参考音文本：同名 .txt 优先，否则 faster-whisper small 转写并缓存
    ref_text = ""
    try:
        with open(ref + ".txt", encoding="utf-8") as f:
            ref_text = f.read().strip()
    except OSError:
        pass
    if not ref_text:
        import silk_voice
        ref_text, err = silk_voice.stt_transcribe(ref, model_size="small")
        if not ref_text:
            print(f"参考音转写失败: {err}", file=sys.stderr)
            return 2
        try:
            with open(ref + ".txt", "w", encoding="utf-8") as f:
                f.write(ref_text.strip())
        except OSError:
            pass

    # torchaudio.load 依赖 torchcodec（未装）；用 soundfile 补丁替代
    import soundfile as sf
    import torch
    import torchaudio
    def _sf_load(path, **kw):
        d, sr = sf.read(path, dtype="float32", always_2d=True)
        return torch.from_numpy(d.T.copy()), sr
    torchaudio.load = _sf_load
    torch.set_num_threads(max(2, (os.cpu_count() or 4) - 2))

    from f5_tts.api import F5TTS
    f5 = F5TTS(model="F5TTS_v1_Base", ckpt_file=ckpt,
               vocab_file=_F5_VOCAB, vocoder_local_path=_F5_VOCODER, device="cpu")
    kw = dict(ref_file=ref, ref_text=ref_text, gen_text=text.strip(),
              nfe_step=nfe, cfg_strength=cfg_strength,
              file_wave=out, show_info=lambda *a: None, progress=None)
    if sway >= 0:
        kw["sway_sampling_coef"] = sway
    f5.infer(**kw)
    print("[clone] done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
