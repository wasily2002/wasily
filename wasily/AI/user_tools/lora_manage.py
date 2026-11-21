# -*- coding: utf-8 -*-
"""用户工具：LoRA 微调管理（数据集导出/启动训练/状态/停止/挂载）"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SPEC = {
    "name": "lora_manage",
    "description": "LoRA 微调管理：action=export 导出训练集 / train 启动训练 / status 查看状态 / stop 停止 / apply 挂载适配器",
    "parameters": {
        "action": "export|train|status|stop|apply",
        "adapter": "apply 时的适配器文件名（如 adapter_1024_1530.gguf）",
    },
}


def run(action="status", adapter=""):
    import lora_lab
    action = (action or "status").lower()
    if action == "export":
        r = lora_lab.export_dataset()
        return f"已导出 {r['samples']} 条训练样本"
    if action == "train":
        r = lora_lab.train_start()
        return r.get("message") or f"训练已启动: {r.get('adapter')}"
    if action == "stop":
        return str(lora_lab.train_stop())
    if action == "apply":
        return lora_lab.apply(adapter).get("message", "")
    st = lora_lab.status()
    return (f"状态: {st.get('status')} | 样本: {st.get('samples', 0)} | "
            f"引擎: {st.get('engine', '-')} | 说明: {st.get('message', '-')}")
