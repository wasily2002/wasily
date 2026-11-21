# -*- coding: utf-8 -*-
"""知识库解析层：全盘遍历 + 多格式正文提取 + 分块。

只读约束：这里所有文件都以 "rb" 打开，绝不写回源文件；
OCR 需要的临时图片写入 kb_data/tmp，用完即删。
"""
import os
import re
import tempfile
import time
import zipfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
TMP_DIR = Path(os.environ.get("WASILY_KB_DIR") or (BASE_DIR / "kb_data")) / "tmp"

TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json", ".jsonl",
    ".xml", ".html", ".htm", ".yaml", ".yml", ".ini", ".cfg", ".conf", ".toml",
    ".sql", ".py", ".js", ".ts", ".tsx", ".jsx", ".vue", ".java", ".kt", ".c",
    ".h", ".cpp", ".hpp", ".cs", ".go", ".rs", ".php", ".rb", ".swift", ".m",
    ".sh", ".bat", ".ps1", ".cmd", ".srt", ".vtt", ".tex", ".properties",
}
OFFICE_EXTS = {".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt"}
PDF_EXTS = {".pdf"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif", ".tif", ".tiff"}
ARCHIVE_EXTS = {".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz", ".apk", ".jar",
                ".exe", ".dll", ".msi", ".iso", ".bin", ".dat", ".db", ".sqlite",
                ".mp4", ".mkv", ".avi", ".mov", ".mp3", ".wav", ".flac", ".m4a"}

# 系统目录/缓存目录：扫描无检索价值，且体量巨大，直接排除
SKIP_DIR_NAMES = {
    "windows", "program files", "program files (x86)", "programdata",
    "$recycle.bin", "system volume information", "recovery", "perflogs",
    "node_modules", "__pycache__", ".git", ".svn", ".hg", ".venv", "venv",
    "site-packages", "dist-info", ".cache", "cache", "temp", "tmp",
    "_model_cache", "train_venv", "kb_data", ".pw-profile", ".playwright-mcp",
    ".idea", ".vscode", "appdata", "winsxs", "driverstore",
}
# 绝对路径前缀（小写、正斜杠）——AppData/Windows 等整棵跳过
SKIP_PREFIXES = (
    "c:/windows", "c:/program files", "c:/program files (x86)",
    "c:/programdata", "c:/$recycle.bin", "c:/system volume information",
    "c:/recovery", "c:/perflogs", "c:/virtualprinter",
    "d:/$recycle.bin", "d:/system volume information",
)
SKIP_FILE_NAMES = {"pagefile.sys", "hiberfil.sys", "swapfile.sys", "ntuser.dat"}

DEFAULT_LIMITS = {
    "max_file_mb": 20,
    "max_text_chars": 200000,
    "max_pdf_pages": 60,
    "max_sheet_rows": 2000,
    "max_chunk_chars": 700,
    "chunk_overlap": 100,
}


def classify(path):
    ext = Path(path).suffix.lower()
    if ext in TEXT_EXTS:
        return "text"
    if ext in PDF_EXTS:
        return "pdf"
    if ext in OFFICE_EXTS:
        return ext.lstrip(".")
    if ext in IMAGE_EXTS:
        return "image"
    if ext in ARCHIVE_EXTS:
        return "skip"
    return "skip"


def _skip_dir(name):
    return name.lower() in SKIP_DIR_NAMES or name.startswith(".")


def iter_files(roots, limits=None, exclude_prefixes=None):
    """递归遍历（不跟随符号链接），产出 (path, kind, size, mtime)。"""
    limits = dict(DEFAULT_LIMITS, **(limits or {}))
    max_bytes = int(limits["max_file_mb"]) * 1024 * 1024
    prefixes = tuple((exclude_prefixes or SKIP_PREFIXES))

    for root in roots:
        root = os.path.abspath(root)
        if not os.path.isdir(root):
            continue
        stack = [root]
        while stack:
            d = stack.pop()
            key = d.replace("\\", "/").lower()
            if any(key.startswith(p) for p in prefixes):
                continue
            try:
                entries = list(os.scandir(d))
            except (PermissionError, OSError):
                continue
            for e in entries:
                try:
                    if e.is_dir(follow_symlinks=False):
                        if not _skip_dir(e.name):
                            stack.append(e.path)
                        continue
                    if not e.is_file(follow_symlinks=False):
                        continue
                    name = e.name.lower()
                    if name in SKIP_FILE_NAMES:
                        continue
                    kind = classify(e.name)
                    if kind == "skip":
                        continue
                    st = e.stat(follow_symlinks=False)
                    if st.st_size <= 0 or st.st_size > max_bytes:
                        continue
                    yield e.path, kind, int(st.st_size), float(st.st_mtime)
                except (PermissionError, OSError):
                    continue


def _decode(data):
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def _read_text(path, limits):
    with open(path, "rb") as f:
        data = f.read(int(limits["max_text_chars"]) * 4 + 65536)
    if b"\x00" in data[:8192]:
        raise ValueError("二进制文件，跳过")
    text = _decode(data)
    return text[:int(limits["max_text_chars"])]


def _read_pdf(path, limits):
    import pymupdf
    parts = []
    with pymupdf.open(path) as doc:
        pages = min(doc.page_count, int(limits["max_pdf_pages"]))
        for i in range(pages):
            page = doc[i]
            txt = page.get_text("text") or ""
            # 扫描件正文极少：对前 3 页做 OCR，避免整本 PDF 都跑 OCR
            if len(txt.strip()) < 40 and i < 3:
                try:
                    pix = page.get_pixmap(dpi=150)
                    TMP_DIR.mkdir(parents=True, exist_ok=True)
                    tmp = TMP_DIR / ("pdf_%d_%d.png" % (os.getpid(), int(time.time() * 1000)))
                    pix.save(str(tmp))
                    try:
                        import kb_vlm
                        ocr_text, _err = kb_vlm.ocr(str(tmp), timeout=180)
                    finally:
                        try:
                            tmp.unlink()
                        except OSError:
                            pass
                    if ocr_text:
                        txt = txt + "\n" + ocr_text
                except Exception:  # noqa: BLE001 OCR 失败不阻断 PDF 正文
                    pass
            if txt.strip():
                parts.append(txt)
    return "\n\n".join(parts)


def _read_docx(path, limits):
    import docx
    d = docx.Document(path)
    parts = [p.text for p in d.paragraphs if p.text.strip()]
    for t in d.tables:
        for row in t.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)[:int(limits["max_text_chars"])]


def _read_xlsx(path, limits):
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    parts = []
    rows_limit = int(limits["max_sheet_rows"])
    try:
        for ws in wb.worksheets[:20]:
            parts.append("【工作表】%s" % ws.title)
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= rows_limit:
                    break
                cells = [str(c) for c in row if c is not None and str(c).strip()]
                if cells:
                    parts.append(" | ".join(cells))
    finally:
        wb.close()
    return "\n".join(parts)[:int(limits["max_text_chars"])]


def _read_pptx(path, limits):
    from pptx import Presentation
    prs = Presentation(path)
    parts = []
    for i, slide in enumerate(prs.slides, 1):
        texts = []
        for shape in slide.shapes:
            if hasattr(shape, "text") and shape.text and shape.text.strip():
                texts.append(shape.text.strip())
        if texts:
            parts.append("【幻灯片 %d】%s" % (i, "\n".join(texts)))
    return "\n\n".join(parts)[:int(limits["max_text_chars"])]


def extract(path, kind, limits=None, allow_caption=True):
    """返回 {"text": 正文, "caption": 图片描述或 "", "note": 附注}。"""
    limits = dict(DEFAULT_LIMITS, **(limits or {}))
    if kind == "text":
        return {"text": _read_text(path, limits), "caption": "", "note": ""}
    if kind == "pdf":
        return {"text": _read_pdf(path, limits), "caption": "", "note": ""}
    if kind == "docx":
        return {"text": _read_docx(path, limits), "caption": "", "note": ""}
    if kind == "xlsx":
        return {"text": _read_xlsx(path, limits), "caption": "", "note": ""}
    if kind == "pptx":
        return {"text": _read_pptx(path, limits), "caption": "", "note": ""}
    if kind in ("doc", "xls", "ppt"):
        raise ValueError("旧版二进制 Office 格式（%s）需要本机 Office 转换，已跳过" % kind)
    if kind == "image":
        import kb_vlm
        ocr_text, ocr_err = kb_vlm.ocr(path)
        caption, cap_err = ("", "未启用")
        if allow_caption:
            caption, cap_err = kb_vlm.caption(path)
        body = "\n".join(x for x in (caption, ocr_text) if x.strip())
        note = "; ".join(x for x in (ocr_err, cap_err) if x)
        return {"text": body, "caption": caption, "note": note}
    raise ValueError("不支持的类型: %s" % kind)


def chunk_text(text, max_chars=None, overlap=None):
    """按段落聚合切块，块间保留 overlap 字符，避免答案被硬切断。"""
    max_chars = int(max_chars or DEFAULT_LIMITS["max_chunk_chars"])
    overlap = int(overlap or DEFAULT_LIMITS["chunk_overlap"])
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return []
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, buf = [], ""
    for p in paras:
        if len(buf) + len(p) + 2 <= max_chars:
            buf = (buf + "\n\n" + p).strip()
            continue
        if buf:
            chunks.append(buf)
        if len(p) <= max_chars:
            buf = p
        else:
            start = 0
            while start < len(p):
                chunks.append(p[start:start + max_chars])
                start += max_chars - overlap
            buf = ""
    if buf:
        chunks.append(buf)
    # 短块合并，减少碎片（<80 字且能并进上一块）
    merged = []
    for c in chunks:
        if merged and len(c) < 80 and len(merged[-1]) + len(c) < max_chars:
            merged[-1] = merged[-1] + "\n" + c
        else:
            merged.append(c)
    return merged
