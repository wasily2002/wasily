# -*- coding: utf-8 -*-
"""全类型文件元数据检索（文件名级）。

存量索引只收录可解析正文的格式（文本/PDF/Office/图片），音视频与压缩包
不入库。这里用「文件名搜索」补齐：只读遍历、时间预算内返回，不写任何索引，
因此不会影响 3.2GB 存量库的查询路径。
"""
import os
import time

import kb_parser

AUDIO_EXTS = {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma", ".ape"}
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ts"}
ARCHIVE_EXTS = {".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz", ".iso",
                ".apk", ".jar", ".msi", ".cab"}
CODE_EXTS = {".py", ".js", ".ts", ".tsx", ".jsx", ".vue", ".java", ".go", ".rs",
             ".c", ".h", ".cpp", ".hpp", ".cs", ".php", ".rb", ".swift", ".kt",
             ".sh", ".ps1", ".bat", ".sql", ".html", ".css", ".json", ".yaml",
             ".yml", ".toml", ".xml", ".md"}
TABLE_EXTS = {".xlsx", ".xls", ".csv", ".tsv"}
DOC_EXTS = {".docx", ".doc", ".pdf", ".pptx", ".ppt", ".txt", ".rtf"}

KIND_EXTS = {
    "audio": AUDIO_EXTS,
    "video": VIDEO_EXTS,
    "archive": ARCHIVE_EXTS,
    "code": CODE_EXTS,
    "table": TABLE_EXTS,
    "doc": DOC_EXTS,
}
KIND_LABEL = {
    "audio": "音频", "video": "视频", "archive": "压缩包",
    "code": "代码", "table": "表格", "doc": "文档",
    "image": "图片", "text": "文本", "pdf": "PDF",
    "docx": "Word", "xlsx": "Excel", "pptx": "PPT", "skip": "其他",
}


def classify_any(path):
    """比 kb_parser.classify 更细：把音视频/压缩包也识别出来。"""
    ext = os.path.splitext(path)[1].lower()
    base = kb_parser.classify(path)
    if base != "skip":
        return base
    for kind, exts in KIND_EXTS.items():
        if ext in exts:
            return kind
    return "skip"


def supported_kinds():
    return [{"kind": k, "label": KIND_LABEL.get(k, k),
             "exts": sorted(v)} for k, v in KIND_EXTS.items()]


def _match(name, terms):
    low = name.lower()
    return all(t in low for t in terms)


def search_names(query, kinds=None, limit=20, roots=None, max_files=40000,
                 time_budget=3.0):
    """按文件名搜索任意类型文件。

    kinds: 可传 ["video","archive"] 或 "video"，留空表示全部类型。
    只读遍历，达到时间预算即返回 partial=True，避免拖住调用方。
    """
    q = (query or "").strip().lower()
    if not q:
        return {"ok": False, "error": "查询不能为空"}
    terms = [t for t in q.replace("，", " ").split() if t]
    if not terms:
        terms = [q]
    if isinstance(kinds, str):
        kinds = [kinds] if kinds else []
    kinds = [k for k in (kinds or []) if k and k != "all"]
    wanted_exts = set()
    for k in kinds:
        if k in KIND_EXTS:
            wanted_exts |= KIND_EXTS[k]
        elif k in ("image", "text", "pdf", "docx", "xlsx", "pptx"):
            wanted_exts |= {e for e, kind in _PARSER_KIND_ITEMS if kind == k}

    roots = roots or (config_roots())
    t0 = time.time()
    hits, scanned, partial = [], 0, False

    for root in roots:
        root = os.path.abspath(root)
        if not os.path.isdir(root):
            continue
        stack = [root]
        while stack:
            if time.time() - t0 > time_budget or scanned >= max_files:
                partial = True
                break
            d = stack.pop()
            key = d.replace("\\", "/").lower()
            if any(key.startswith(p) for p in kb_parser.SKIP_PREFIXES):
                continue
            try:
                entries = list(os.scandir(d))
            except (PermissionError, OSError):
                continue
            for e in entries:
                scanned += 1
                try:
                    if e.is_dir(follow_symlinks=False):
                        if not kb_parser._skip_dir(e.name):  # noqa: SLF001
                            stack.append(e.path)
                        continue
                    if not e.is_file(follow_symlinks=False):
                        continue
                    name = e.name
                    if name.lower() in kb_parser.SKIP_FILE_NAMES:
                        continue
                    ext = os.path.splitext(name)[1].lower()
                    if wanted_exts and ext not in wanted_exts:
                        continue
                    if not _match(name, terms):
                        continue
                    st = e.stat(follow_symlinks=False)
                    hits.append({
                        "name": name, "path": e.path,
                        "kind": classify_any(name), "ext": ext,
                        "size": int(st.st_size), "mtime": float(st.st_mtime),
                    })
                    if len(hits) >= limit:
                        return {"ok": True, "query": query, "results": hits,
                                "scanned": scanned, "partial": partial,
                                "took_ms": int((time.time() - t0) * 1000)}
                except (PermissionError, OSError):
                    continue
        if partial:
            break
    hits.sort(key=lambda r: r["mtime"], reverse=True)
    return {"ok": True, "query": query, "results": hits[:limit],
            "scanned": scanned, "partial": partial,
            "took_ms": int((time.time() - t0) * 1000)}


_PARSER_KIND_ITEMS = []
for _ext in kb_parser.TEXT_EXTS:
    _PARSER_KIND_ITEMS.append((_ext, "text"))
for _ext in kb_parser.PDF_EXTS:
    _PARSER_KIND_ITEMS.append((_ext, "pdf"))
for _ext in kb_parser.OFFICE_EXTS:
    _PARSER_KIND_ITEMS.append((_ext, _ext.lstrip(".")))
for _ext in kb_parser.IMAGE_EXTS:
    _PARSER_KIND_ITEMS.append((_ext, "image"))


def config_roots():
    try:
        import config as cfg
        roots = cfg.get("kb.roots") or []
        return [r for r in roots if isinstance(r, str) and r.strip()]
    except Exception:  # noqa: BLE001
        return []
