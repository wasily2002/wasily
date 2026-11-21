# -*- coding: utf-8 -*-
"""用户工具：启动/停止/查看本地知识库的索引进度。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SPEC = {
    "name": "kb_index",
    "description": "管理本地知识库索引：start=开始全盘只读扫描并索引；stop=停止；status=查看进度。",
    "parameters": {
        "action": "start / stop / status，默认 status",
        "roots": "可选：要扫描的根目录，多个用英文逗号分隔；留空用配置（默认 C:\\,D:\\）",
        "force": "可选：true 表示全部重建，忽略增量",
    },
}


def run(action="status", roots="", force=False):
    import kb_index
    ix = kb_index.get_indexer()
    act = str(action or "status").lower()
    if act == "start":
        rs = [r.strip() for r in str(roots or "").split(",") if r.strip()] or None
        res = ix.start(roots=rs, force=bool(force))
        if not res.get("ok"):
            return "启动失败：%s" % res.get("error")
        st = res["status"]
        return "索引已启动，扫描根目录：%s。首次全盘耗时较长，可用 status 查看进度。" % \
               "、".join(st.get("roots") or [])
    if act == "stop":
        ix.stop()
        return "已请求停止索引。"
    st = ix.status()
    return ("阶段：%s\n运行中：%s\n已发现：%s\n已索引：%s\n跳过：%s\n错误：%s\n"
            "图片：%s\n库大小：%sMB\n当前：%s") % (
        st.get("phase"), "是" if st.get("running") else "否", st.get("found"),
        st.get("indexed"), st.get("skipped"), st.get("errors"), st.get("images"),
        st.get("db_mb"), st.get("current") or "-")
