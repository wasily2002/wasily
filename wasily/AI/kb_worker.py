# -*- coding: utf-8 -*-
"""知识库 OCR / 视觉描述子进程。

为什么必须是子进程：torch 与 llama.cpp 同进程会原生崩溃（无 Python 回溯），
主服务只通过 stdin/stdout 的 JSON 行协议调用；模型失败只影响本条请求。
"""
import json
import os
import sys

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
# 国内网络直连 huggingface 常超时，统一走镜像（用户已装本地权重时不会联网）
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

_OCR = None
_VLM = None
_VLM_FAILED = ""

CAPTION_PROMPT = "用中文描述这张图片的主要内容，包括画面主体、场景和图中可见的文字。"


def _get_ocr():
    global _OCR
    if _OCR is None:
        import easyocr
        # gpu=False：本机 MX450 显存不足，且索引要长时间跑，CPU 更稳
        _OCR = easyocr.Reader(["ch_sim", "en"], gpu=False, verbose=False)
    return _OCR


def _load_vlm(model_dir):
    global _VLM, _VLM_FAILED
    if _VLM is not None:
        return _VLM
    if _VLM_FAILED:
        raise RuntimeError(_VLM_FAILED)
    import torch
    from transformers import AutoProcessor
    try:
        from transformers import AutoModelForVision2Seq as _AutoModel
    except ImportError:  # transformers 新版把视觉语言模型改到 ImageTextToText
        from transformers import AutoModelForImageTextToText as _AutoModel
    processor = AutoProcessor.from_pretrained(model_dir, local_files_only=True)
    model = _AutoModel.from_pretrained(model_dir, torch_dtype=torch.float32,
                                       local_files_only=True)
    model.eval()
    _VLM = (model, processor)
    return _VLM


def do_ocr(path):
    reader = _get_ocr()
    lines = reader.readtext(path, detail=0, paragraph=True)
    return {"text": "\n".join(str(x) for x in lines)}


def do_caption(path, model_dir):
    import torch
    from PIL import Image
    model, processor = _load_vlm(model_dir)
    img = Image.open(path)
    if img.mode != "RGB":
        img = img.convert("RGB")
    # 长边压到 512：500M 模型再多像素也只增加耗时，文字细节交给 OCR
    img.thumbnail((512, 512))
    messages = [{"role": "user", "content": [
        {"type": "image"},
        {"type": "text", "text": CAPTION_PROMPT},
    ]}]
    try:
        prompt = processor.apply_chat_template(messages, add_generation_prompt=True)
        inputs = processor(text=prompt, images=[img], return_tensors="pt")
    except Exception:  # noqa: BLE001 老版本 processor 不支持 chat template
        inputs = processor(images=img, text=CAPTION_PROMPT, return_tensors="pt")
    with torch.no_grad():
        out = model.generate(**inputs, max_new_tokens=64, do_sample=False,
                             num_beams=1, repetition_penalty=1.15)
    # 只解码新生成部分，否则会把输入 prompt 原样回显
    prompt_len = inputs["input_ids"].shape[-1]
    text = processor.batch_decode(out[:, prompt_len:], skip_special_tokens=True)[0]
    if CAPTION_PROMPT in text:
        text = text.split(CAPTION_PROMPT, 1)[-1]
    for tag in ("Assistant:", "assistant:", "User:", "user:"):
        if text.strip().startswith(tag):
            text = text.strip()[len(tag):].strip()
    return {"text": text.strip()}


def main():
    model_dir = sys.argv[1] if len(sys.argv) > 1 else ""
    out = sys.stdout
    out.write(json.dumps({"ready": True, "pid": os.getpid()}) + "\n")
    out.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            cmd = req.get("cmd")
            if cmd == "ping":
                msg = {"ok": True}
            elif cmd == "ocr":
                msg = dict(ok=True, **do_ocr(req["path"]))
            elif cmd == "caption":
                msg = dict(ok=True, **do_caption(req["path"], model_dir))
            else:
                msg = {"ok": False, "error": "未知命令: %s" % cmd}
        except Exception as exc:  # noqa: BLE001 单条失败不能拖垮管道
            msg = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        out.write(json.dumps(msg, ensure_ascii=False) + "\n")
        out.flush()


if __name__ == "__main__":
    main()
