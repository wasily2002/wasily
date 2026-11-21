# -*- coding: utf-8 -*-
"""chat_engine.py — 轻量对话引擎封装（供 lora_lab 对比测试等内部模块调用）

运行策略：
- 在 api_server 进程内：直接复用已加载的全局 LLM（零额外内存，推荐路径）。
  注意：此时 LoRA 适配器状态取决于服务启动时的 config，无法在单次调用中临时卸载。
- 独立运行（脚本/子进程）：按 config.json 自行加载模型，遵循 lora.path 配置，
  因此 lora_lab._ask_llm 的临时禁用 adapter 逻辑在独立模式下生效。
"""
import os
import threading

import config as _cfg


class ChatEngine:
    def __init__(self):
        _cfg.load()
        self._llm = None
        self._lock = threading.Lock()
        self.shared = False          # True = 复用 api_server 的全局模型
        try:
            import api_server
            if getattr(api_server, "llm", None) is not None:
                self._llm = api_server.llm
                self._lock = api_server.GENERATE_LOCK
                self.shared = True
        except Exception:
            pass                      # 不在 api_server 进程内 → 懒加载自己的模型

    def _ensure_loaded(self):
        if self._llm is not None:
            return
        from llama_cpp import Llama
        base = os.path.dirname(os.path.abspath(__file__))
        model = _cfg.get("server.model_path") or ""
        if not os.path.isabs(model):
            model = os.path.join(base, model)
        lora = _cfg.get("lora.path", "") or ""
        if lora and not os.path.exists(lora):
            lora = ""
        self._llm = Llama(
            model_path=model,
            n_ctx=int(_cfg.get("server.n_ctx", 8192)),
            n_threads=int(_cfg.get("server.n_threads", 0) or (os.cpu_count() or 4)),
            n_gpu_layers=0,          # 独立模式保守纯 CPU，避免与主服务抢显存
            lora_path=lora or None,
            verbose=False,
        )

    def chat(self, question, session_id="default"):
        """单轮问答。返回 {'reply': str}"""
        self._ensure_loaded()
        ai_name = _cfg.get("ai_name", "wasily")
        msgs = [
            {"role": "system", "content": f"你是{ai_name}，用中文自然回答。"},
            {"role": "user", "content": str(question)},
        ]
        max_tokens = int(_cfg.get("server.max_generate_tokens", 512))
        with self._lock:             # llama.cpp 非线程安全
            r = self._llm.create_chat_completion(
                messages=msgs, max_tokens=max_tokens, temperature=0.7)
        text = r["choices"][0]["message"]["content"] if r.get("choices") else ""
        return {"reply": text}
