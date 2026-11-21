"""微调能力评估。

只做只读探测：CPU/内存/显卡/磁盘、模型格式、现有 LoRA 环境与 adapter。
结论口径：
- 当前推理栈是 llama.cpp + GGUF，不支持反向传播，因此「实时微调」不成立；
- 增量训练用离线 LoRA/QLoRA（现有 lora_lab 已具备），训练后导出 adapter 再热切换；
- 无 GPU 或显存不足时，用记忆/知识注入（RAG）达到近似「大脑训练」的即时效果。
"""
import json
import os
import shutil
import subprocess
import sys

import config as cfg

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LAB = os.path.join(BASE_DIR, "finetune")


def _ram_gb():
    try:
        import psutil
        return round(psutil.virtual_memory().total / 1073741824, 1)
    except Exception:  # noqa: BLE001
        try:
            import ctypes
            class _Mem(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = _Mem()
            m.dwLength = ctypes.sizeof(_Mem)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return round(m.ullTotalPhys / 1073741824, 1)
        except Exception:  # noqa: BLE001
            return 0


def _gpus():
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,memory.total,driver_version",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=6)
        rows = []
        for line in (out.stdout or "").splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2:
                rows.append({"name": parts[0], "vram_gb": round(float(parts[1]) / 1024, 1),
                             "driver": parts[2] if len(parts) > 2 else ""})
        return rows
    except Exception:  # noqa: BLE001
        return []


def _torch_info():
    try:
        import importlib.util
        if importlib.util.find_spec("torch") is None:
            return {"installed": False, "cuda": False, "version": ""}
        import torch  # noqa: PLC0415 —— 仅评估时导入，避免拖慢服务启动
        return {"installed": True, "cuda": bool(torch.cuda.is_available()),
                "version": str(torch.__version__), "cuda_version": str(torch.version.cuda or "")}
    except Exception as exc:  # noqa: BLE001
        return {"installed": False, "cuda": False, "version": "", "error": str(exc)[:120]}


def _model_info():
    path = str(cfg.get("server.model_path", "") or "")
    info = {"path": path, "exists": bool(path and os.path.isfile(path)),
            "format": os.path.splitext(path)[1].lstrip(".").lower() or "", "size_gb": 0}
    if info["exists"]:
        info["size_gb"] = round(os.path.getsize(path) / 1073741824, 2)
    return info


def _disk_free_gb():
    try:
        return round(shutil.disk_usage(BASE_DIR).free / 1073741824, 1)
    except OSError:
        return 0


def _lab_state():
    out = {"adapters": [], "state": {}, "dataset_lines": 0}
    try:
        if os.path.isdir(LAB):
            out["adapters"] = [d for d in os.listdir(LAB)
                               if os.path.isdir(os.path.join(LAB, d)) and d.startswith("adapter")]
        fp = os.path.join(LAB, "state.json")
        if os.path.isfile(fp):
            with open(fp, "r", encoding="utf-8") as f:
                out["state"] = json.load(f)
        ds = os.path.join(LAB, "dataset.jsonl")
        if os.path.isfile(ds):
            with open(ds, "r", encoding="utf-8", errors="ignore") as f:
                out["dataset_lines"] = sum(1 for _ in f)
    except Exception:  # noqa: BLE001
        pass
    return out


def assess():
    gpus = _gpus()
    vram = max([g["vram_gb"] for g in gpus], default=0)
    torch_info = _torch_info()
    model = _model_info()
    lab = _lab_state()
    disk = _disk_free_gb()
    env = {
        "platform": sys.platform,
        "cpu_count": os.cpu_count() or 0,
        "ram_gb": _ram_gb(),
        "gpus": gpus,
        "max_vram_gb": vram,
        "torch": torch_info,
        "disk_free_gb": disk,
        "model": model,
        "lab": lab,
    }
    # 实时微调判断
    realtime = {
        "supported": False,
        "reason": "当前推理栈为 llama.cpp + GGUF（%s），只做前向推理，不支持反向传播与在线权重更新；"
                  "且实时微调会与生成抢占同一份显存/算力，导致对话阻塞。" % (model.get("format") or "GGUF"),
    }
    # 增量 LoRA 可行性：显存、CUDA 版 PyTorch、磁盘三个条件都要满足
    reasons = []
    if vram < 8:
        reasons.append("显存不足（检测到 %.1f GB，建议 ≥8 GB）" % vram)
    if not torch_info.get("cuda"):
        reasons.append("当前 PyTorch 为 CPU 版或未启用 CUDA（%s）" % (
            torch_info.get("version") or "未安装"))
    if disk < 20:
        reasons.append("磁盘可用空间不足（%.1f GB，训练至少需要 20 GB）" % disk)
    feasible = not reasons
    if feasible:
        level = "推荐" if vram >= 12 else "可行（需降配）"
        detail = ("显存 %.1f GB、CUDA 可用、磁盘 %.1f GB，"
                  "可对 7B 模型做 QLoRA 增量训练（4bit + LoRA，rank 8-16）。"
                  % (vram, disk))
    elif vram >= 8:
        level = "条件不满足"
        detail = "显存达标，但仍有阻塞项：" + "；".join(reasons) + "。"
    else:
        level = "不建议本机训练"
        detail = ("未满足本机增量训练条件：" + "；".join(reasons) +
                  "。CPU 训练 7B 模型单轮需数小时以上，不满足交互式使用。")
    incremental = {
        "supported": feasible,
        "level": level,
        "detail": detail,
        "blockers": reasons,
        "method": "QLoRA（4bit 量化 + LoRA adapter）",
        "engine": "finetune/qlora_train.py（已存在）→ 导出 adapter → 转换 GGUF 后由 lora_lab.apply 热挂载",
        "requirements": {
            "vram_gb": ">=8（推荐 12+）",
            "ram_gb": ">=16",
            "disk_gb": ">=20（基座模型 + 数据集 + 输出）",
            "software": "PyTorch+CUDA、transformers、peft、bitsandbytes、llama.cpp（转换工具）",
        },
        "current": {
            "torch_installed": torch_info.get("installed"),
            "cuda_available": torch_info.get("cuda"),
            "disk_free_gb": disk,
            "existing_adapters": len(lab["adapters"]),
            "dataset_samples": lab["dataset_lines"],
        },
        "expected": ("领域术语、表达风格与任务格式的贴合度提升；7B QLoRA 通常 1-3 个 epoch 可观察到效果。"
                     "不会注入最新事实，时效信息仍需依赖联网检索。"),
    }
    immediate = {
        "supported": True,
        "name": "记忆 + 知识注入（无需训练，立即生效）",
        "detail": "把纠错样本写入记忆库/知识库，生成时按向量召回注入；"
                  "效果接近「教会模型新事实」，且不会破坏原有能力。",
        "paths": ["memory_net（对话记忆与召回）", "kb_*（知识库检索）",
                  "finetune/feedback.jsonl（纠错样本池，可作为后续训练集）"],
        "expected": "下一轮对话即可生效；适合事实、偏好与专有资料，不适合改变模型的推理能力。",
    }
    plan = [
        {"step": 1, "title": "积累高质量样本",
         "detail": "把纠错、偏好、领域问答写入 finetune/feedback.jsonl 与 knowledge.jsonl，"
                   "建议 >=500 条再训练。"},
        {"step": 2, "title": "评估与清洗",
         "detail": "用 lora_lab.dataset_list 审核样本，剔除重复与低质；保留 10% 作为验证集。"},
        {"step": 3, "title": "离线 QLoRA 训练",
         "detail": "在 GPU 机器上运行 qlora_train.py（rank 8-16、3-5 epoch），"
                   "训练期间不要同时跑对话推理。"},
        {"step": 4, "title": "导出与热切换",
         "detail": "adapter 转 GGUF 后放入 finetune/，调用 lora_lab.apply 挂载，重启后生效；"
                   "保留旧 adapter 以便回滚。"},
        {"step": 5, "title": "评测与回归",
         "detail": "用固定问题集对比训练前后，确认没有能力退化；不达标则回滚。"},
    ]
    recommended = ("incremental" if feasible else "immediate")
    return {
        "ok": True,
        "env": env,
        "realtime": realtime,
        "incremental": incremental,
        "immediate": immediate,
        "plan": plan,
        "recommended": recommended,
        "summary": ("实时微调不可行；" +
                    ("可在本机做离线 QLoRA 增量训练。" if feasible
                     else "建议先用记忆/知识注入实现即时「大脑训练」，训练放到带 GPU 的机器上。")),
    }
