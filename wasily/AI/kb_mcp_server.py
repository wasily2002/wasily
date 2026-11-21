# -*- coding: utf-8 -*-
"""知识库 MCP 能力总线（stdio，JSON-RPC 2.0）。

把存量知识库（3.2GB 索引）包装成标准 MCP 工具，供智能体、站点与第三方调用。
本进程只读：检索走 kb_search，正文读取走 kb_store.chunks_of_file，
未入库文件才回退 kb_parser；不写索引、不改表结构。
"""
import json
import sys
import traceback

import kb_apps
import kb_digest
import kb_files
import kb_sources
import train_data

PROTOCOL = "2024-11-05"
SERVER = {"name": "wasily-kb", "version": "1.0.0"}


def _schema(props, required=None):
    return {"type": "object", "properties": props,
            "required": required or []}


TOOLS = [
    {
        "name": "kb_search",
        "description": "全类型统一检索：文本/PDF/Office/图片走存量混合索引，"
                       "音视频与压缩包补文件名检索。返回匹配文件、片段与来源路径。",
        "inputSchema": _schema({
            "query": {"type": "string", "description": "关键词或自然语言问题"},
            "kind": {"type": "string",
                     "description": "可选：text/pdf/docx/xlsx/pptx/image/audio/video/archive，留空为全部"},
            "limit": {"type": "integer", "description": "返回条数，默认 8"},
        }, ["query"]),
    },
    {
        "name": "kb_file_info",
        "description": "查询单个文件是否已入库及其元数据（类型、大小、分块数、状态）。",
        "inputSchema": _schema({
            "path": {"type": "string", "description": "文件绝对路径"},
            "file_id": {"type": "integer", "description": "可选，索引内文件 id"},
        }),
    },
    {
        "name": "kb_read",
        "description": "读取文件正文。已入库文件直接复用索引分块，未入库文件只读解析。",
        "inputSchema": _schema({
            "path": {"type": "string"},
            "file_id": {"type": "integer"},
            "max_chars": {"type": "integer", "description": "默认 24000"},
        }),
    },
    {
        "name": "kb_summarize",
        "description": "对单个文件生成结构化摘要：概述、要点、关键词。"
                       "mode=extractive 离线快速；llm 调用本机模型增强；auto 自动选择。",
        "inputSchema": _schema({
            "path": {"type": "string"},
            "file_id": {"type": "integer"},
            "query": {"type": "string", "description": "用户查询上下文，用于贴合要点"},
            "mode": {"type": "string", "enum": ["auto", "extractive", "llm"]},
            "max_points": {"type": "integer", "description": "要点数量，默认 6"},
        }),
    },
    {
        "name": "kb_summarize_many",
        "description": "多文件跨内容聚合总结：共同主题、差异与逐文件摘要。"
                       "paths 为空时按 query 自动检索候选文件。",
        "inputSchema": _schema({
            "paths": {"type": "array", "items": {"type": "string"}},
            "query": {"type": "string"},
            "limit": {"type": "integer", "description": "默认 5"},
            "mode": {"type": "string", "enum": ["extractive", "llm"]},
        }),
    },
    {
        "name": "kb_report",
        "description": "生成知识库报告（Markdown 或 HTML），可落盘到 outputs/kb_reports。",
        "inputSchema": _schema({
            "topic": {"type": "string", "description": "报告主题"},
            "paths": {"type": "array", "items": {"type": "string"}},
            "query": {"type": "string"},
            "format": {"type": "string", "enum": ["markdown", "html"]},
            "mode": {"type": "string", "enum": ["extractive", "llm"]},
            "save": {"type": "boolean", "description": "默认 true"},
        }, ["topic"]),
    },
    {
        "name": "kb_name_search",
        "description": "仅按文件名搜索任意类型（含音视频、压缩包），适合找“某个文件”。",
        "inputSchema": _schema({
            "query": {"type": "string"},
            "kinds": {"type": "array", "items": {"type": "string"}},
            "limit": {"type": "integer", "description": "默认 20"},
        }, ["query"]),
    },
    {
        "name": "kb_stats",
        "description": "知识库统计：已索引文件、分块数、索引体积、支持的类型。",
        "inputSchema": _schema({}),
    },
    {
        "name": "kb_recent",
        "description": "最近入库的文件列表。",
        "inputSchema": _schema({
            "limit": {"type": "integer", "description": "默认 20"},
            "kind": {"type": "string"},
        }),
    },
    {
        "name": "kb_types",
        "description": "列出能力总线支持的文件类型与扩展名，供上层规划调用。",
        "inputSchema": _schema({}),
    },
    {
        "name": "kb_sources",
        "description": "列出已配置的统一存储（本地/WSL/SSH/WebDAV/S3），可选返回连通性状态。",
        "inputSchema": _schema({
            "include_status": {"type": "boolean", "description": "是否逐个自检，默认 false"},
        }),
    },
    {
        "name": "kb_source_manage",
        "description": "管理存储配置：action=add/remove/test。add 需传 source 对象"
                       "（id/type/name/options）；敏感凭据只写入本地 config.json。",
        "inputSchema": _schema({
            "action": {"type": "string", "enum": ["add", "remove", "test"]},
            "source": {"type": "object", "description": "add 时的完整配置"},
            "id": {"type": "string", "description": "remove/test 时的存储 id"},
        }, ["action"]),
    },
    {
        "name": "kb_source_list",
        "description": "列出指定存储某个目录下的文件与文件夹。",
        "inputSchema": _schema({
            "id": {"type": "string", "description": "存储 id"},
            "path": {"type": "string", "description": "目录路径，留空为根"},
        }, ["id"]),
    },
    {
        "name": "kb_source_read",
        "description": "读取远程/云盘文件内容（文本）。二进制文件请改用 kb_source_summarize。",
        "inputSchema": _schema({
            "id": {"type": "string"},
            "path": {"type": "string"},
            "max_bytes": {"type": "integer", "description": "默认 32MB"},
        }, ["id", "path"]),
    },
    {
        "name": "kb_source_search",
        "description": "在指定存储内按文件名检索；本地存储同时支持类型过滤。",
        "inputSchema": _schema({
            "id": {"type": "string"},
            "query": {"type": "string"},
            "path": {"type": "string", "description": "起始目录，留空为根"},
            "limit": {"type": "integer"},
            "kinds": {"type": "array", "items": {"type": "string"}},
        }, ["id", "query"]),
    },
    {
        "name": "kb_source_summarize",
        "description": "对远程/云盘文件生成结构化摘要：先落地临时文件，再复用知识库摘要链路，结束即清理。",
        "inputSchema": _schema({
            "id": {"type": "string"},
            "path": {"type": "string"},
            "query": {"type": "string"},
            "mode": {"type": "string", "enum": ["auto", "extractive", "llm"]},
            "max_points": {"type": "integer"},
        }, ["id", "path"]),
    },
    {
        "name": "app_office_read",
        "description": "读取内置 Office 文档（docx/xlsx/pptx）的正文，可指定工作表或幻灯片。",
        "inputSchema": _schema({
            "path": {"type": "string"},
            "sheet": {"type": "string", "description": "xlsx 工作表名"},
            "slide": {"type": "string", "description": "pptx 幻灯片序号（从 1 开始）"},
        }, ["path"]),
    },
    {
        "name": "app_office_edit",
        "description": "编辑 Office 文档。操作：docx 支持 replace_text/append_paragraph/set_paragraph；"
                       "xlsx 支持 set_cell/append_row/create_sheet；pptx 支持 add_slide/replace_text/add_textbox。"
                       "写入前自动备份。",
        "inputSchema": _schema({
            "path": {"type": "string"},
            "operations": {"type": "array", "items": {"type": "object"}},
            "dry_run": {"type": "boolean"},
        }, ["path", "operations"]),
    },
    {
        "name": "app_video_info",
        "description": "读取视频/音频元数据：时长、码率、编码、分辨率、声道。",
        "inputSchema": _schema({"path": {"type": "string"}}, ["path"]),
    },
    {
        "name": "app_video_frames",
        "description": "按时间均匀抽取视频关键帧，保存到 outputs/frames，返回图片路径供视觉分析。",
        "inputSchema": _schema({
            "path": {"type": "string"},
            "count": {"type": "integer", "description": "抽帧数量，默认 6，最多 24"},
            "width": {"type": "integer", "description": "缩放宽度，默认 640"},
        }, ["path"]),
    },
    {
        "name": "app_code_read",
        "description": "读取代码文件指定行范围，返回行号信息与语言类型。",
        "inputSchema": _schema({
            "path": {"type": "string"},
            "start": {"type": "integer", "description": "起始行，默认 1"},
            "end": {"type": "integer", "description": "结束行，0=到文件末尾"},
        }, ["path"]),
    },
    {
        "name": "app_code_edit",
        "description": "编辑代码文件。操作：replace/insert/append/write；写入前自动备份，支持 dry_run 预演。",
        "inputSchema": _schema({
            "path": {"type": "string"},
            "operations": {"type": "array", "items": {"type": "object"}},
            "dry_run": {"type": "boolean"},
        }, ["path", "operations"]),
    },
    {
        "name": "app_code_search",
        "description": "在工作区内按文件名或内容搜索代码，返回命中行与片段。",
        "inputSchema": _schema({
            "query": {"type": "string"},
            "root": {"type": "string", "description": "搜索根目录，留空为工作区"},
            "limit": {"type": "integer", "description": "默认 50"},
        }, ["query"]),
    },
    {
        "name": "train_stats",
        "description": "训练数据闭环统计：采集数、清洗数、标注分布、训练/验证集与反馈数。",
        "inputSchema": _schema({}),
    },
    {
        "name": "train_collect",
        "description": "从历史会话增量采集问答对，重复执行不会重复入库。",
        "inputSchema": _schema({
            "limit": {"type": "integer", "description": "单次最多新增条数，默认 2000"},
        }),
    },
    {
        "name": "train_clean",
        "description": "清洗 events：去空、去短、去重、脱敏，生成 dataset.jsonl。",
        "inputSchema": _schema({
            "min_chars": {"type": "integer"},
            "max_chars": {"type": "integer"},
        }),
    },
    {
        "name": "train_label",
        "description": "为数据集打标签：任务类型、成功与否、质量分。",
        "inputSchema": _schema({
            "mode": {"type": "string", "enum": ["heuristic"]},
        }),
    },
    {
        "name": "train_export",
        "description": "导出 SFT 训练/验证集（JSONL）并写 manifest。",
        "inputSchema": _schema({
            "val_ratio": {"type": "number", "description": "验证集比例，默认 0.1"},
        }),
    },
    {
        "name": "train_pipeline",
        "description": "一键跑完采集 → 清洗 → 标注 → 导出。",
        "inputSchema": _schema({}),
    },
    {
        "name": "train_feedback",
        "description": "回写用户反馈（0-5 分）到 feedback.jsonl，用于质量回流。",
        "inputSchema": _schema({
            "event_id": {"type": "string"},
            "rating": {"type": "integer"},
            "comment": {"type": "string"},
        }, ["event_id", "rating"]),
    },
    {
        "name": "train_append",
        "description": "实时采集一条交互（输入/输出/工具），供上层程序在对话完成后调用。",
        "inputSchema": _schema({
            "input": {"type": "string"},
            "output": {"type": "string"},
            "tools": {"type": "array", "items": {"type": "string"}},
            "session_id": {"type": "string"},
        }, ["input", "output"]),
    },
]


def _call(name, args):
    if name == "kb_search":
        return kb_digest.search_all(
            str(args.get("query") or ""), kind=str(args.get("kind") or ""),
            limit=int(args.get("limit") or 8))
    if name == "kb_file_info":
        return _file_info(args)
    if name == "kb_read":
        return kb_digest.read_document(
            path=args.get("path"), file_id=args.get("file_id"),
            max_chars=int(args.get("max_chars") or kb_digest.MAX_CONTENT_CHARS),
            allow_caption=bool(args.get("allow_caption")))
    if name == "kb_summarize":
        return kb_digest.summarize(
            path=args.get("path"), file_id=args.get("file_id"),
            query=str(args.get("query") or ""),
            mode=str(args.get("mode") or "auto"),
            max_points=int(args.get("max_points") or 6))
    if name == "kb_summarize_many":
        return kb_digest.summarize_many(
            paths=args.get("paths") or [], query=str(args.get("query") or ""),
            limit=int(args.get("limit") or 5),
            mode=str(args.get("mode") or "extractive"))
    if name == "kb_report":
        return kb_digest.build_report(
            str(args.get("topic") or "知识库报告"), paths=args.get("paths") or [],
            query=str(args.get("query") or ""),
            mode=str(args.get("mode") or "extractive"),
            fmt=str(args.get("format") or "markdown"),
            save=bool(args.get("save", True)))
    if name == "kb_name_search":
        return kb_files.search_names(
            str(args.get("query") or ""), kinds=args.get("kinds"),
            limit=int(args.get("limit") or 20))
    if name == "kb_stats":
        return kb_digest.stats()
    if name == "kb_recent":
        import kb_store
        rows = kb_store.get_store().list_recent(
            int(args.get("limit") or 20), kind=args.get("kind") or None)
        return {"ok": True, "files": rows}
    if name == "kb_types":
        return {"ok": True, "kinds": kb_files.supported_kinds(),
                "index_kinds": sorted(
                    {"text", "pdf", "docx", "xlsx", "pptx", "image"})}
    if name == "kb_sources":
        return kb_sources.list_sources(include_status=bool(args.get("include_status")))
    if name == "kb_source_manage":
        return _source_manage(args)
    if name == "kb_source_list":
        return kb_sources.source_list(str(args.get("id") or ""),
                                      str(args.get("path") or ""))
    if name == "kb_source_read":
        return kb_sources.source_read(
            str(args.get("id") or ""), str(args.get("path") or ""),
            int(args.get("max_bytes") or kb_sources.MAX_BYTES))
    if name == "kb_source_search":
        return kb_sources.source_search(
            str(args.get("id") or ""), str(args.get("query") or ""),
            path=str(args.get("path") or ""),
            limit=int(args.get("limit") or 20), kinds=args.get("kinds"))
    if name == "kb_source_summarize":
        return kb_sources.source_summarize(
            str(args.get("id") or ""), str(args.get("path") or ""),
            query=str(args.get("query") or ""),
            mode=str(args.get("mode") or "extractive"),
            max_points=int(args.get("max_points") or 6))
    if name == "app_office_read":
        return kb_apps.office_read(str(args.get("path") or ""),
                                   sheet=str(args.get("sheet") or ""),
                                   slide=str(args.get("slide") or ""))
    if name == "app_office_edit":
        return kb_apps.office_edit(str(args.get("path") or ""),
                                   args.get("operations") or [],
                                   dry_run=bool(args.get("dry_run")))
    if name == "app_video_info":
        return kb_apps.video_info(str(args.get("path") or ""))
    if name == "app_video_frames":
        return kb_apps.video_frames(str(args.get("path") or ""),
                                    count=int(args.get("count") or 6),
                                    width=int(args.get("width") or 640))
    if name == "app_code_read":
        return kb_apps.code_read(str(args.get("path") or ""),
                                 start=int(args.get("start") or 1),
                                 end=int(args.get("end") or 0))
    if name == "app_code_edit":
        return kb_apps.code_edit(str(args.get("path") or ""),
                                 args.get("operations") or [],
                                 dry_run=bool(args.get("dry_run")))
    if name == "app_code_search":
        return kb_apps.code_search(str(args.get("query") or ""),
                                   root=str(args.get("root") or ""),
                                   limit=int(args.get("limit") or 50))
    if name == "train_stats":
        return train_data.stats()
    if name == "train_collect":
        return train_data.collect(limit=int(args.get("limit") or 2000))
    if name == "train_clean":
        return train_data.clean(min_chars=args.get("min_chars"),
                                max_chars=args.get("max_chars"))
    if name == "train_label":
        return train_data.label(mode=str(args.get("mode") or "heuristic"))
    if name == "train_export":
        return train_data.export(val_ratio=args.get("val_ratio"))
    if name == "train_pipeline":
        return train_data.run_pipeline()
    if name == "train_feedback":
        return train_data.feedback(str(args.get("event_id") or ""),
                                   args.get("rating"),
                                   str(args.get("comment") or ""))
    if name == "train_append":
        return train_data.append_event(
            str(args.get("input") or ""), str(args.get("output") or ""),
            tools=args.get("tools"), session_id=str(args.get("session_id") or ""))
    raise KeyError("未知工具: %s" % name)


def _source_manage(args):
    action = str(args.get("action") or "").lower()
    if action == "add":
        return kb_sources.add_source(args.get("source") or {})
    if action == "remove":
        return kb_sources.remove_source(str(args.get("id") or ""))
    if action == "test":
        src = kb_sources.get_source(str(args.get("id") or ""))
        return {"ok": True, "id": src.id, "status": src.test()}
    raise ValueError("action 仅支持 add/remove/test")


def _file_info(args):
    import kb_store
    st = kb_store.get_store()
    rec = None
    if args.get("file_id") not in (None, ""):
        rec = st.get_file(int(args["file_id"]))
    elif args.get("path"):
        rec = st.get_file_by_path(str(args["path"]))
    if not rec:
        return {"ok": False, "error": "文件不在索引中"}
    return {"ok": True, "file": rec}


def _send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(mid, res):
    _send({"jsonrpc": "2.0", "id": mid, "result": res})


def _error(mid, code, msg):
    _send({"jsonrpc": "2.0", "id": mid,
           "error": {"code": code, "message": msg}})


def _text(payload):
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def handle(req):
    mid = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}
    if method == "initialize":
        _result(mid, {"protocolVersion": PROTOCOL,
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": SERVER})
    elif method in ("notifications/initialized", "notifications/cancelled"):
        return
    elif method == "ping":
        _result(mid, {})
    elif method == "tools/list":
        _result(mid, {"tools": TOOLS})
    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            out = _call(name, args)
            _result(mid, {"content": [{"type": "text", "text": _text(out)}],
                          "isError": not bool(out.get("ok", True))
                          if isinstance(out, dict) else False})
        except Exception as exc:  # noqa: BLE001
            _result(mid, {"content": [{"type": "text", "text": _text({
                "ok": False, "error": "%s: %s" % (type(exc).__name__, exc),
                "trace": traceback.format_exc(limit=3)})}],
                "isError": True})
    elif method is not None:
        _error(mid, -32601, "不支持的方法: %s" % method)


def main():
    try:
        train_data.start_auto_collect()
    except Exception:  # noqa: BLE001
        pass
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            handle(json.loads(line))
        except Exception as exc:  # noqa: BLE001
            _send({"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32700, "message": "解析失败: %s" % exc}})


if __name__ == "__main__":
    main()
