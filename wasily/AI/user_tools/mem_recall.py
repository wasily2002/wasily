# -*- coding: utf-8 -*-
"""用户工具：多层记忆网络操作（一期动态注册机制的实战应用）"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SPEC = {
    "name": "mem_recall",
    "description": "从多层记忆(L1摘要/L2事实/L3认知)召回与查询相关的历史",
    "parameters": {"query": "要回忆的关键词或话题"},
}


def run(query=""):
    import memory_net
    rs = memory_net.recall(str(query), top=6)
    if not rs:
        return "记忆网络中没有与该话题相关的记忆"
    names = {1: "会话摘要", 2: "长期事实", 3: "核心认知"}
    return "\n".join(f"[{names.get(r.get('layer', 1))}/{r.get('topic', '')}] "
                     f"{r.get('text', '')[:200]}" for r in rs)
