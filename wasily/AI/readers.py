# -*- coding: utf-8 -*-
"""wasily 统一读取层（工具契约 v2）。

设计目标：
- 所有读取类工具返回同一种 JSON 字符串，模型/前端都能稳定解析；
- 二进制文件先探测类型再路由，绝不把 PDF/Office/图片当 UTF-8 文本读；
- 大文件按行/页/工作表分段，返回 next_offset / next_page 让模型续读；
- 失败返回固定错误码 + 可执行建议，而不是一句“读取错误”。

成功：
{"ok":true,"tool":"read_pdf","kind":"pdf","data":{...},"meta":{...}}
失败：
{"ok":false,"tool":"read_pdf","error":{"code":"...","message":"...",
 "suggestion":"...","candidates":[...]}}
"""
import base64
import hashlib
import io
import json
import mimetypes
import os
import re
import time
import zipfile
from pathlib import Path
from urllib.parse import urljoin, urlparse

AI_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = AI_DIR / "uploads"

TEXT_MAX = 30000
PDF_MAX = 30000
OFFICE_MAX = 30000
IMAGE_MAX = 20000
WEB_MAX = 20000
MAX_BYTES_TEXT = 32 * 1024 * 1024

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".ico", ".heic"}
PDF_EXTS = {".pdf"}
OFFICE_EXTS = {".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt"}
OLE_EXTS = {".doc", ".xls", ".ppt"}
ARCHIVE_EXTS = {".zip", ".7z", ".rar", ".gz", ".tar", ".tgz", ".bz2", ".xz", ".jar", ".apk"}

_LOCAL_VISION = AI_DIR / "_model_cache" / "llava-phi-3-mini-hf"
_OCR = {"reader": None, "attempted": False}


def _dumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def ok(tool, kind="", data=None, meta=None):
    out = {"ok": True, "tool": tool}
    if kind:
        out["kind"] = kind
    out["data"] = data or {}
    out["meta"] = meta or {}
    return _dumps(out)


def fail(tool, code, message, suggestion="", **extra):
    err = {"code": code, "message": message}
    if suggestion:
        err["suggestion"] = suggestion
    err.update(extra)
    return _dumps({"ok": False, "tool": tool, "error": err})


def retag(result, tool, extra_meta=None):
    """把子解析器的返回改成调用方工具名，并补充 meta（用于 file_read 自动路由）。"""
    try:
        obj = json.loads(result)
    except Exception:
        return result
    if not isinstance(obj, dict):
        return result
    obj["tool"] = tool
    if extra_meta:
        obj.setdefault("meta", {})
        if isinstance(obj["meta"], dict):
            obj["meta"].update(extra_meta)
    return _dumps(obj)


# ============ 路径与类型探测 ============

def resolve_path(raw):
    """返回 (Path, 提示文本)。路径不存在时按文件名在常见根目录浅层查找。"""
    s = str(raw or "").strip().strip('"').strip("'")
    if not s:
        return None, "路径为空"
    p = Path(os.path.expanduser(s))
    if p.exists():
        return p.resolve(), ""
    if p.is_absolute():
        return p, f"文件不存在: {s}"
    roots = [Path.cwd(), AI_DIR, AI_DIR.parent]
    for root in roots:
        cand = root / p
        if cand.exists():
            return cand.resolve(), f"[路径纠正] 相对路径已解析为 {cand.resolve()}"
    name = p.name
    hits = []
    for root in roots:
        cand = root / name
        if cand.is_file() and str(cand).lower() not in {str(x).lower() for x in hits}:
            hits.append(cand)
    if len(hits) == 1:
        return hits[0], f"[路径纠正] 未找到 {s}，已按文件名定位到 {hits[0]}"
    if len(hits) > 1:
        return p, "文件不存在: %s\n找到多个同名文件，请用完整路径重试：\n%s" % (
            s, "\n".join(str(x) for x in hits[:5]))
    near = []
    try:
        stem = p.stem.lower()
        for root in roots[:1]:
            for dirpath, dirnames, filenames in os.walk(root):
                if dirpath[len(str(root)):].count(os.sep) >= 2:
                    dirnames[:] = []
                    continue
                for fn in filenames:
                    if stem and stem in fn.lower():
                        near.append(os.path.join(dirpath, fn))
                        if len(near) >= 5:
                            break
                if len(near) >= 5:
                    break
    except Exception:
        pass
    msg = f"文件不存在: {s}"
    if near:
        msg += "\n相近文件：\n" + "\n".join(near[:5])
    return p, msg


def _read_head(path, size=16):
    try:
        with open(path, "rb") as f:
            return f.read(size)
    except Exception:
        return b""


def _looks_text(sample):
    if not sample:
        return True
    if b"\x00" in sample:
        return False
    try:
        text, _ = _decode_bytes(sample)
    except Exception:
        return False
    ctrl = sum(1 for ch in text if ord(ch) < 32 and ch not in "\n\r\t")
    return ctrl / max(1, len(text)) < 0.02


def sniff_kind(path):
    """返回 text/image/pdf/office/ole/archive/zip/binary。"""
    p = Path(path)
    ext = p.suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in PDF_EXTS:
        return "pdf"
    if ext in (".docx", ".xlsx", ".pptx"):
        return "office"
    if ext in OLE_EXTS:
        return "ole"
    if ext in ARCHIVE_EXTS:
        return "archive"
    head = _read_head(p)
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        return "office" if ext in OFFICE_EXTS else "zip"
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "ole"
    if head[:3] == b"\xff\xd8\xff" or head.startswith(b"\x89PNG") or head.startswith(b"GIF8"):
        return "image"
    sample = b""
    try:
        with open(p, "rb") as f:
            sample = f.read(4096)
    except Exception:
        return "binary"
    return "text" if _looks_text(sample) else "binary"


def _decode_bytes(data):
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8(replace)"


def _clip(text, max_chars):
    if max_chars and len(text) > int(max_chars):
        return text[:int(max_chars)], True
    return text, False


def _human_size(n):
    n = float(n or 0)
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f}MB"
    if n >= 1024:
        return f"{n / 1024:.1f}KB"
    return f"{int(n)}B"


# ============ 文本 ============

def read_text(path, offset=0, limit=0, max_chars=TEXT_MAX, tool="file_read"):
    p = Path(path)
    try:
        size = p.stat().st_size
    except Exception as e:
        return fail(tool, "FILE_NOT_FOUND", f"无法读取文件信息: {e}",
                    "先用 file_list 确认路径")
    if size > MAX_BYTES_TEXT:
        return fail(tool, "FILE_TOO_LARGE",
                    f"文本文件过大（{_human_size(size)}），单次不宜整读",
                    "用 offset/limit 分段读取，例如 offset=0,limit=2000；或先 file_search 定位")
    try:
        raw = p.read_bytes()
    except Exception as e:
        return fail(tool, "PERMISSION_DENIED", f"读取失败: {e}", "检查文件是否被占用或无权限")
    text, enc = _decode_bytes(raw)
    lines = text.splitlines()
    total = len(lines)
    try:
        start = max(0, int(offset or 0))
    except Exception:
        start = 0
    try:
        lim = int(limit or 0)
    except Exception:
        lim = 0
    end = min(total, start + lim) if lim > 0 else total
    body = "\n".join(lines[start:end])
    truncated = False
    next_offset = None
    if len(body) > int(max_chars):
        body = body[:int(max_chars)]
        truncated = True
        used = body.count("\n") + 1 if body else 0
        next_offset = start + max(1, used)
    elif end < total:
        truncated = True
        next_offset = end
    return ok(tool, "text", {
        "path": str(p),
        "encoding": enc,
        "start_line": start + 1,
        "text": body,
    }, {
        "bytes": size,
        "total_lines": total,
        "lines_returned": (body.count("\n") + 1) if body else 0,
        "truncated": truncated,
        "next_offset": next_offset,
        "max_chars": int(max_chars),
    })


def read_binary_info(path, tool="file_read"):
    p = Path(path)
    try:
        size = p.stat().st_size
    except Exception as e:
        return fail(tool, "FILE_NOT_FOUND", f"无法读取文件信息: {e}", "先用 file_list 确认路径")
    head = _read_head(p, 16)
    mime = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
    try:
        sha = hashlib.sha256(p.read_bytes()[:1024 * 1024]).hexdigest()[:16]
    except Exception:
        sha = ""
    return ok(tool, "binary", {
        "path": str(p),
        "bytes": size,
        "bytes_human": _human_size(size),
        "mime": mime,
        "magic_hex": head.hex(),
        "sha256_first1mb": sha,
        "readable_as_text": False,
        "message": "这是二进制文件，未返回内容（避免污染模型上下文）",
        "suggestion": "按类型改用具名工具：PDF→read_pdf，Office→read_office，图片→read_image",
    })


# ============ OCR / 视觉 ============

def _prepare_image(img, max_side=2000):
    try:
        from PIL import Image
        w, h = img.size
        if max(w, h) > max_side:
            scale = max_side / float(max(w, h))
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale)))
                             , Image.LANCZOS)
        return img
    except Exception:
        return img


def _ocr_text(img):
    global _OCR
    if not _OCR["attempted"]:
        _OCR["attempted"] = True
        import easyocr
        _OCR["reader"] = easyocr.Reader(["ch_sim", "en"], gpu=False, verbose=False)
    import numpy as np
    arr = np.array(_prepare_image(img).convert("RGB"))
    result = _OCR["reader"].readtext(arr)
    lines = [str(r[1]).strip() for r in result if len(r) > 1 and str(r[1]).strip()]
    return "\n".join(lines)


def _vision_enabled_by_config():
    """视觉模型默认关闭（CPU 单图 5–8 分钟，且只在需要画面语义时才值得等），
    由 config vision.enabled 打开；工具调用时用 mode=describe 手动触发。"""
    try:
        import config
        return bool(config.get("vision.enabled", False))
    except Exception:
        return False


def _vision_describe(img, path=""):
    """本地视觉 LLM（moondream2 / llama.cpp），未安装时立即返回空，不加载模型。"""
    try:
        import vision
    except Exception as e:
        return None, "", {"error": "vision 模块不可用: %s" % e, "installed": False}
    st = vision.status()
    if not st.get("installed"):
        return None, "", {"installed": False, "error": "未安装视觉模型",
                          "install_hint": st.get("install_hint")}
    res = vision.describe(path)
    if not res.get("ok"):
        return None, "", {"installed": True, "error": res.get("error"),
                          "device": res.get("device")}
    return (res.get("text") or None), (res.get("model") or ""), res


def read_image(path, mode="auto", max_chars=IMAGE_MAX, tool="read_image"):
    p = Path(path)
    try:
        from PIL import Image
        img = Image.open(str(p))
        img.load()
    except Exception as e:
        return fail(tool, "UNSUPPORTED_IMAGE", f"无法打开图片: {e}",
                    "确认文件是 png/jpg/webp/bmp；损坏文件先用系统图片查看器另存")
    t0 = time.time()
    m = str(mode or "auto").lower()
    use_vlm = m in ("describe", "vision", "vlm")
    if m in ("auto",) and _vision_enabled_by_config():
        use_vlm = True
    desc, vision_model, vres = (None, "", None)
    if use_vlm:
        desc, vision_model, vres = _vision_describe(img, str(p))
    ocr_text, ocr_engine, ocr_error = "", "", ""
    if m in ("auto", "describe", "ocr") and not (m == "describe" and desc):
        try:
            ocr_text = _ocr_text(img)
            ocr_engine = "easyocr"
        except Exception as e:
            ocr_error = str(e)[:120]
    clip_text, truncated = _clip(ocr_text, int(max_chars or IMAGE_MAX))
    note = ""
    if desc:
        note = "已用本地视觉模型解析画面语义（mode=%s）。" % m
    elif m == "auto":
        note = ("默认只用 OCR 文字；视觉描述默认关闭（Qwen2-VL-2B 纯 CPU 单图约 5–8 分钟），"
                "需要画面语义时传 mode=describe 手动触发。")
    elif ocr_text:
        note = "视觉模型不可用或未启用，仅返回 OCR 文字；无法判断画面语义/布局。"
    elif ocr_error:
        note = "OCR 不可用：" + ocr_error
    else:
        note = "未检测到文字；视觉模型不可用，无法描述画面内容。"
    sections = {}
    if desc:
        try:
            import vision as _v
            sections = _v.parse_sections(desc)
        except Exception:
            sections = {"overview": desc, "key_elements": "", "anomalies": ""}
    data = {
        "path": str(p),
        "width": img.width,
        "height": img.height,
        "format": img.format or p.suffix.lstrip("."),
        "visual_description": desc,
        "visual_understanding": bool(desc),
        "visual_sections": sections,
        "ocr_text": clip_text,
        "has_text": bool(ocr_text.strip()),
        "ocr_error": ocr_error or None,
    }
    meta = {
        "ocr_engine": ocr_engine or None,
        "vision_model": vision_model or None,
        "vision_device": (vres or {}).get("device") if desc else None,
        "vision_elapsed_s": (vres or {}).get("elapsed_s") if desc else None,
        "vision_error": None if desc else ((vres or {}).get("error") if vres else None),
        "elapsed_s": round(time.time() - t0, 2),
        "truncated": truncated,
        "note": note,
    }
    return ok(tool, "image", data, meta)


# ============ PDF ============

def _parse_pages(spec, total, max_pages):
    if not spec or str(spec).strip().lower() in ("all", "全部"):
        sel = list(range(min(total, max_pages)))
        return sel, total > len(sel)
    ids = set()
    for part in re.split(r"[,，]", str(spec)):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"(\d+)\s*[-~]\s*(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            ids.update(range(min(a, b) - 1, max(a, b)))
        elif part.isdigit():
            ids.add(int(part) - 1)
    valid = sorted(i for i in ids if 0 <= i < total)
    sel = valid[:max_pages]
    return sel, len(valid) > len(sel)


def read_pdf(path, pages="", ocr="auto", max_chars=PDF_MAX, max_pages=20, tool="read_pdf"):
    p = Path(path)
    if not p.exists():
        return fail(tool, "FILE_NOT_FOUND", f"PDF 不存在: {p}",
                    "先用 file_search 按文件名查找，或让用户提供完整路径")
    try:
        import fitz  # PyMuPDF
    except Exception:
        return fail(tool, "DEPENDENCY_MISSING", "缺少 PyMuPDF，无法解析 PDF",
                    "安装：pip install pymupdf")
    t0 = time.time()
    try:
        doc = fitz.open(str(p))
    except Exception as e:
        return fail(tool, "PDF_PARSE_FAILED", f"PDF 打开失败: {e}",
                    "确认文件未损坏；必要时用系统阅读器另存后再试")
    try:
        if getattr(doc, "needs_pass", False):
            return fail(tool, "PDF_ENCRYPTED", "PDF 已加密，无法读取文本",
                        "先解密或另存为未加密 PDF")
        total = doc.page_count
        sel, pages_more = _parse_pages(pages, total, int(max_pages or 20))
        if not sel:
            return fail(tool, "INVALID_ARG", f"页码超出范围（共 {total} 页）",
                        "pages 用 1-3 或 1,2,5 形式，或传 all")
        chunks = []
        chars = 0
        used_pages = []
        next_page = None
        ocr_pages = []
        ocr_skipped = []
        no_text_pages = []
        truncated = False
        for pno in sel:
            page = doc[pno]
            try:
                text = page.get_text("text") or ""
            except Exception:
                text = ""
            if len(text.strip()) < 50 and str(ocr).lower() in ("auto", "on"):
                if len(ocr_pages) < 3:
                    try:
                        pix = page.get_pixmap(dpi=180)
                        from PIL import Image
                        img = Image.open(io.BytesIO(pix.tobytes("png")))
                        text = _ocr_text(img)
                        ocr_pages.append(pno + 1)
                    except Exception as e:
                        ocr_skipped.append({"page": pno + 1, "error": str(e)[:80]})
                else:
                    ocr_skipped.append({"page": pno + 1, "error": "单次最多 OCR 3 页"})
            if not text.strip():
                no_text_pages.append(pno + 1)
            block = f"[第 {pno + 1} 页]\n{text.strip()}"
            remain = int(max_chars or PDF_MAX) - chars
            if remain <= 0:
                next_page = pno + 1
                truncated = True
                break
            if len(block) > remain:
                chunks.append(block[:remain])
                chars = int(max_chars or PDF_MAX)
                next_page = pno + 1
                truncated = True
                break
            chunks.append(block)
            chars += len(block)
            used_pages.append(pno + 1)
        if pages_more and next_page is None and sel:
            next_page = max(sel) + 2 if max(sel) + 1 < total else None
        body = "\n\n".join(chunks)
        if not body.strip():
            return fail(tool, "NO_TEXT_LAYER",
                        "PDF 没有可提取文本（可能是扫描件/图片型 PDF）",
                        "传 ocr=on 重试；或安装 easyocr；单次最多 OCR 3 页，可分页多次调用",
                        total_pages=total, pages_tried=used_pages[:10])
        return ok(tool, "pdf", {
            "path": str(p),
            "total_pages": total,
            "pages_read": used_pages,
            "text": body,
            "ocr_pages": ocr_pages,
            "no_text_pages": no_text_pages,
        }, {
            "bytes": p.stat().st_size,
            "truncated": truncated,
            "next_page": next_page,
            "ocr_skipped": ocr_skipped,
            "elapsed_s": round(time.time() - t0, 2),
        })
    finally:
        try:
            doc.close()
        except Exception:
            pass


# ============ Office ============

def _local(tag):
    return str(tag).split("}")[-1]


def _docx_text(p):
    import docx
    d = docx.Document(str(p))
    parts = []
    for para in d.paragraphs:
        t = (para.text or "").strip()
        if t:
            parts.append(t)
    for i, table in enumerate(d.tables[:12]):
        rows = []
        for row in table.rows[:60]:
            cells = [(c.text or "").strip().replace("\n", " ") for c in row.cells]
            rows.append(" | ".join(cells))
        parts.append(f"[表格 {i + 1}]\n" + "\n".join(rows))
    return "\n".join(parts)


def _xlsx_text(p, sheet=""):
    try:
        import openpyxl
    except Exception:
        openpyxl = None
    if openpyxl is not None:
        wb = openpyxl.load_workbook(str(p), read_only=True, data_only=True)
        out = []
        try:
            sheets = wb.worksheets
            if sheet:
                sheets = [ws for ws in sheets if ws.title == str(sheet)] or sheets[:1]
            for ws in sheets[:10]:
                rows = []
                for r in ws.iter_rows(values_only=True):
                    vals = ["" if v is None else str(v) for v in r]
                    if any(vals):
                        rows.append(" | ".join(vals))
                    if len(rows) >= 200:
                        break
                out.append(f"[工作表 {ws.title}]\n" + "\n".join(rows))
        finally:
            wb.close()
        return "\n".join(out)
    # 无 openpyxl 时直接解析 xlsx 的 zip/XML，不引入新依赖
    import xml.etree.ElementTree as ET
    out = []
    with zipfile.ZipFile(str(p)) as z:
        names = z.namelist()
        shared = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.iter():
                if _local(si.tag) == "si":
                    texts = [t.text or "" for t in si.iter() if _local(t.tag) == "t"]
                    shared.append("".join(texts))
        sheet_map = {}
        try:
            wroot = ET.fromstring(z.read("xl/workbook.xml"))
            rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
            rid_target = {}
            for rel in rels.iter():
                if _local(rel.tag) == "Relationship":
                    rid_target[rel.attrib.get("Id", "")] = rel.attrib.get("Target", "")
            for sh in wroot.iter():
                if _local(sh.tag) == "sheet":
                    rid = next((v for k, v in sh.attrib.items() if _local(k) == "id"), "")
                    tgt = rid_target.get(rid, "")
                    if tgt:
                        tgt = tgt.lstrip("/")
                        if not tgt.startswith("xl/"):
                            tgt = "xl/" + tgt
                        sheet_map[tgt] = sh.attrib.get("name", "")
        except Exception:
            pass
        sheet_files = sorted(
            [n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n)],
            key=lambda n: int(re.search(r"(\d+)", n).group(1)))
        for idx, sf in enumerate(sheet_files[:10], 1):
            name = sheet_map.get(sf) or f"Sheet{idx}"
            if sheet and name != str(sheet):
                continue
            root = ET.fromstring(z.read(sf))
            rows = []
            for row in root.iter():
                if _local(row.tag) != "row":
                    continue
                vals = []
                for c in list(row):
                    if _local(c.tag) != "c":
                        continue
                    t = c.attrib.get("t")
                    v = ""
                    for ch in c:
                        if _local(ch.tag) == "v":
                            v = ch.text or ""
                        elif _local(ch.tag) == "is":
                            v = "".join(x.text or "" for x in ch.iter() if _local(x.tag) == "t")
                    if t == "s":
                        try:
                            v = shared[int(v)]
                        except Exception:
                            pass
                    vals.append(v)
                if any(vals):
                    rows.append(" | ".join(vals))
                if len(rows) >= 200:
                    break
            out.append(f"[工作表 {name}]\n" + "\n".join(rows))
    return "\n".join(out)


def _pptx_text(p, slide=""):
    import xml.etree.ElementTree as ET
    out = []
    with zipfile.ZipFile(str(p)) as z:
        slides = sorted(
            [n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)],
            key=lambda n: int(re.search(r"(\d+)", n).group(1)))
        if slide:
            want = set()
            for part in re.split(r"[,，]", str(slide)):
                m = re.match(r"(\d+)\s*[-~]\s*(\d+)", part.strip())
                if m:
                    a, b = int(m.group(1)), int(m.group(2))
                    want.update(range(min(a, b), max(a, b) + 1))
                elif part.strip().isdigit():
                    want.add(int(part.strip()))
            if want:
                slides = [n for n in slides
                          if int(re.search(r"(\d+)", n).group(1)) in want]
        for n in slides[:30]:
            root = ET.fromstring(z.read(n))
            texts = [t.text or "" for t in root.iter() if _local(t.tag) == "t"]
            body = "\n".join(x for x in texts if x.strip())
            num = int(re.search(r"(\d+)", n).group(1))
            out.append(f"[第 {num} 张幻灯片]\n{body}")
    return "\n".join(out)


def read_office(path, sheet="", slide="", max_chars=OFFICE_MAX, tool="read_office"):
    p = Path(path)
    if not p.exists():
        return fail(tool, "FILE_NOT_FOUND", f"文件不存在: {p}",
                    "先用 file_search 按文件名查找")
    ext = p.suffix.lower()
    t0 = time.time()
    try:
        if ext == ".docx":
            text = _docx_text(p)
        elif ext == ".xlsx":
            text = _xlsx_text(p, sheet=sheet)
        elif ext == ".pptx":
            text = _pptx_text(p, slide=slide)
        elif ext in OLE_EXTS:
            return fail(tool, "UNSUPPORTED_FORMAT",
                        f"旧版 Office 二进制格式（{ext}）暂不支持解析",
                        "用 Office/WPS 另存为 docx/xlsx/pptx 后再读")
        else:
            return fail(tool, "UNSUPPORTED_FORMAT", f"不是可解析的 Office 文档: {ext}",
                        "支持 docx/xlsx/pptx；旧版 doc/xls/ppt 需先转换")
        text, truncated = _clip(text, int(max_chars or OFFICE_MAX))
        if not text.strip():
            if sheet or slide:
                return fail(tool, "INVALID_ARG",
                            "指定的工作表/幻灯片不存在或没有文本",
                            "不传 sheet/slide 可读取全部内容，再确认名称/页码")
            return fail(tool, "PARSE_FAILED", "文档没有可提取文本（可能是空文档或全图片）",
                        "确认文档内容非空；图片型文档请先转成文字")
        meta = {
            "bytes": p.stat().st_size,
            "format": ext.lstrip("."),
            "truncated": truncated,
            "elapsed_s": round(time.time() - t0, 2),
        }
        if truncated:
            meta["suggestion"] = "内容已截断：加大 max_chars，或对 xlsx 指定 sheet、pptx 指定 slide 分段读取"
        return ok(tool, "office", {
            "path": str(p),
            "format": ext.lstrip("."),
            "text": text,
        }, meta)
    except Exception as e:
        return fail(tool, "PARSE_FAILED", f"解析失败: {e}",
                    "确认文件完整；docx/xlsx/pptx 可先用 Office 打开另存一次")


# ============ 网页 ============

class _HTMLExtractor:
    SKIP = {"script", "style", "noscript", "template", "svg"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
             "section", "article", "header", "footer", "table", "ul", "ol", "pre"}

    def __init__(self, base_url=""):
        from html.parser import HTMLParser
        self.base_url = base_url
        self.title = ""
        self.parts = []
        self.links = []
        self.skip = 0
        self.in_title = False
        ext = self

        class _P(HTMLParser):
            def handle_starttag(_self, tag, attrs):
                tag = tag.lower()
                a = dict(attrs)
                if tag in ext.SKIP:
                    ext.skip += 1
                    return
                if tag == "title":
                    ext.in_title = True
                if tag == "a" and a.get("href"):
                    try:
                        ext.links.append(urljoin(ext.base_url, a["href"]))
                    except Exception:
                        pass
                if tag in ext.BLOCK:
                    ext.parts.append("\n")

            def handle_startendtag(_self, tag, attrs):
                if tag.lower() == "br":
                    ext.parts.append("\n")

            def handle_endtag(_self, tag):
                tag = tag.lower()
                if tag in ext.SKIP and ext.skip > 0:
                    ext.skip -= 1
                    return
                if tag == "title":
                    ext.in_title = False
                if tag in ext.BLOCK:
                    ext.parts.append("\n")

            def handle_data(_self, data):
                if ext.skip:
                    return
                if ext.in_title:
                    ext.title += data
                    return
                ext.parts.append(data)

        self._p = _P()

    def feed(self, html):
        self._p.feed(html)

    def text(self):
        raw = "".join(self.parts)
        raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
        raw = re.sub(r"\n\s*\n+", "\n", raw)
        return raw.strip()


def _download(url, timeout=25, max_bytes=10 * 1024 * 1024):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf;q=0.8,*/*;q=0.5",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "identity",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return {
            "status": getattr(r, "status", 200),
            "headers": dict(r.headers.items()),
            "final_url": r.geturl(),
            "body": r.read(max_bytes),
        }


def fetch_web(url, max_chars=WEB_MAX, include_links=True, tool="fetch_web"):
    s = str(url or "").strip()
    u = urlparse(s)
    if u.scheme not in ("http", "https") or not u.netloc:
        return fail(tool, "INVALID_URL", f"不是有效的 http/https 网址: {s}",
                    "传完整 URL，例如 https://example.com/article")
    t0 = time.time()
    try:
        resp = _download(s)
    except Exception as e:
        name = type(e).__name__
        code = "HTTP_ERROR" if "HTTP" in name else "NETWORK_ERROR"
        return fail(tool, code, f"抓取失败: {e}",
                    "确认网络/代理可用；先 network_ping 测连通性，或改用 network_request 看原始响应")
    ctype = (resp["headers"].get("Content-Type") or "").lower()
    raw = resp["body"]
    if "application/pdf" in ctype or raw[:5] == b"%PDF-":
        UPLOAD_DIR.joinpath("web").mkdir(parents=True, exist_ok=True)
        name = hashlib.sha256(s.encode()).hexdigest()[:10] + ".pdf"
        fp = UPLOAD_DIR / "web" / name
        fp.write_bytes(raw)
        return retag(read_pdf(fp, max_chars=int(max_chars or WEB_MAX), tool="fetch_web"),
                     "fetch_web", {"source_url": s, "status": resp["status"], "content_type": ctype})
    if any(x in ctype for x in ("image/", "application/octet-stream")) and "text" not in ctype:
        UPLOAD_DIR.joinpath("web").mkdir(parents=True, exist_ok=True)
        ext = ".png" if "png" in ctype else ".jpg"
        fp = UPLOAD_DIR / "web" / (hashlib.sha256(s.encode()).hexdigest()[:10] + ext)
        fp.write_bytes(raw)
        return retag(read_image(fp, tool="fetch_web"), "fetch_web",
                     {"source_url": s, "status": resp["status"], "content_type": ctype})
    if "html" not in ctype and "text" not in ctype and "xml" not in ctype and "json" not in ctype:
        return fail(tool, "CONTENT_UNSUPPORTED",
                    f"该链接返回不可读类型: {ctype or 'unknown'}",
                    "若是下载链接，用 network_download 保存后按文件类型读")
    charset = "utf-8"
    m = re.search(r"charset=([\w\-]+)", ctype)
    if m:
        charset = m.group(1)
    try:
        html = raw.decode(charset, errors="replace")
    except Exception:
        html = raw.decode("utf-8", errors="replace")
    ext = _HTMLExtractor(resp["final_url"])
    try:
        ext.feed(html)
    except Exception:
        pass
    text = ext.text()
    text, truncated = _clip(text, int(max_chars or WEB_MAX))
    title = re.sub(r"\s+", " ", ext.title).strip()
    links = [x for x in ext.links if x.startswith("http")][:20] if include_links else []
    if not text:
        return fail(tool, "PARSE_FAILED", "网页已下载但没有提取到正文（可能全靠 JS 渲染）",
                    "该页面需要浏览器执行 JS；可改用 IDE 内置浏览器打开，或提供页面正文")
    return ok(tool, "web", {
        "url": resp["final_url"],
        "title": title,
        "text": text,
        "links": links,
    }, {
        "status": resp["status"],
        "content_type": ctype,
        "bytes": len(raw),
        "truncated": truncated,
        "elapsed_s": round(time.time() - t0, 2),
        "js_rendered": False,
        "note": "直接抓取 HTML 正文，不执行 JavaScript；纯前端渲染页面可能取不到内容。",
    })
