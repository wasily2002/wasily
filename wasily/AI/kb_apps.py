# -*- coding: utf-8 -*-
"""内置应用的 AI 可执行层：Office / 视频 / 代码。

设计原则：
- 只操作白名单根目录（config.json 的 apps.roots，默认当前工作区）；
- 任何写入前先备份到 outputs/app_backups，失败可回溯；
- Office 读取复用 readers.read_office，避免重复实现；
- 全部能力通过 MCP 暴露，自然语言指令 → 工具调用。
"""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import config as cfg

BASE_DIR = Path(__file__).resolve().parent
WORKSPACE = BASE_DIR.parents[2]
BACKUP_DIR = BASE_DIR / "outputs" / "app_backups"
FRAME_DIR = BASE_DIR / "outputs" / "frames"

SKIP_DIRS = {"node_modules", "__pycache__", ".git", ".svn", ".venv", "venv",
             "site-packages", "dist", "build", ".idea", ".vscode",
             "_model_cache", "train_venv", "kb_data", "_archive"}


class AppError(Exception):
    pass


# ---------------- 路径与备份 ----------------
def allowed_roots():
    roots = cfg.get("apps.roots", []) or []
    if isinstance(roots, str):
        roots = [roots]
    roots = [os.path.abspath(os.path.expanduser(r)) for r in roots if str(r).strip()]
    return roots or [str(WORKSPACE)]


def resolve_path(raw, must_exist=True, roots=None):
    if not raw:
        raise AppError("缺少文件路径")
    full = os.path.abspath(os.path.expanduser(str(raw)))
    roots = roots or allowed_roots()
    if not any(full == r or full.startswith(r + os.sep) for r in roots):
        raise AppError("路径不在允许的根目录内：%s" % full)
    if must_exist and not os.path.exists(full):
        raise AppError("文件不存在：%s" % full)
    return full


def _backup(path):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dst = BACKUP_DIR / ("%s-%s" % (time.strftime("%Y%m%d-%H%M%S"),
                                   os.path.basename(path)))
    shutil.copy2(path, dst)
    return str(dst)


# ---------------- Office ----------------
def office_read(path, sheet="", slide="", max_chars=200000):
    import readers
    full = resolve_path(path)
    try:
        payload = json.loads(readers.read_office(
            full, sheet=str(sheet or ""), slide=str(slide or ""),
            max_chars=int(max_chars or 200000), tool="app_office_read"))
    except Exception as exc:  # noqa: BLE001
        raise AppError("解析失败：%s" % exc)
    if not payload.get("ok"):
        return {"ok": False, "error": (payload.get("error") or {}).get("message", "读取失败"),
                "detail": payload.get("error")}
    data = payload.get("data") or {}
    return {"ok": True, "path": full, "format": data.get("format"),
            "text": data.get("text"), "meta": payload.get("meta") or {}}


def office_edit(path, operations, dry_run=False):
    full = resolve_path(path)
    ext = os.path.splitext(full)[1].lower()
    if ext not in (".docx", ".xlsx", ".pptx"):
        raise AppError("仅支持 docx/xlsx/pptx：%s" % ext)
    ops = list(operations or [])[:50]
    if not ops:
        raise AppError("operations 不能为空")
    backup = "" if dry_run else _backup(full)
    applied = []
    if ext == ".docx":
        applied = _edit_docx(full, ops)
    elif ext == ".xlsx":
        applied = _edit_xlsx(full, ops)
    else:
        applied = _edit_pptx(full, ops)
    return {"ok": True, "path": full, "backup": backup,
            "applied": applied, "dry_run": bool(dry_run)}


def _edit_docx(full, ops):
    import docx
    doc = docx.Document(full)
    applied = []
    for op in ops:
        kind = str(op.get("op") or "").lower()
        if kind == "replace_text":
            old, new = str(op.get("search") or ""), str(op.get("replace") or "")
            if not old:
                raise AppError("replace_text 缺少 search")
            hit = 0
            for p in doc.paragraphs:
                if old in p.text:
                    for run in p.runs:
                        if old in run.text:
                            run.text = run.text.replace(old, new)
                            hit += 1
                    if old in p.text and hit == 0:
                        p.text = p.text.replace(old, new)
                        hit += 1
            for table in doc.tables:
                for row in table.rows:
                    for cell in row.cells:
                        if old in cell.text:
                            cell.text = cell.text.replace(old, new)
                            hit += 1
            applied.append({"op": kind, "hits": hit})
        elif kind == "append_paragraph":
            text = str(op.get("text") or "")
            if not text:
                raise AppError("append_paragraph 缺少 text")
            doc.add_paragraph(text, style=op.get("style") or None)
            applied.append({"op": kind, "text": text})
        elif kind == "set_paragraph":
            idx = int(op.get("index") or 0)
            text = str(op.get("text") or "")
            if not (0 <= idx < len(doc.paragraphs)):
                raise AppError("段落序号越界：%s" % idx)
            doc.paragraphs[idx].text = text
            applied.append({"op": kind, "index": idx})
        else:
            raise AppError("docx 不支持的操作：%s" % kind)
    doc.save(full)
    return applied


def _edit_xlsx(full, ops):
    import openpyxl
    wb = openpyxl.load_workbook(full)
    applied = []
    for op in ops:
        kind = str(op.get("op") or "").lower()
        sheet = str(op.get("sheet") or "")
        if kind == "set_cell":
            ws = wb[sheet] if sheet else wb.active
            cell = str(op.get("cell") or "").strip()
            if not cell:
                raise AppError("set_cell 缺少 cell")
            ws[cell] = op.get("value")
            applied.append({"op": kind, "sheet": ws.title, "cell": cell})
        elif kind == "append_row":
            ws = wb[sheet] if sheet else wb.active
            values = op.get("values")
            if not isinstance(values, list):
                raise AppError("append_row 需要 values 数组")
            ws.append(values)
            applied.append({"op": kind, "sheet": ws.title,
                            "row": ws.max_row})
        elif kind == "create_sheet":
            if not sheet:
                raise AppError("create_sheet 缺少 sheet")
            wb.create_sheet(sheet)
            applied.append({"op": kind, "sheet": sheet})
        else:
            raise AppError("xlsx 不支持的操作：%s" % kind)
    wb.save(full)
    return applied


def _edit_pptx(full, ops):
    from pptx import Presentation
    from pptx.util import Inches
    prs = Presentation(full)
    applied = []
    for op in ops:
        kind = str(op.get("op") or "").lower()
        if kind == "add_slide":
            title = str(op.get("title") or "")
            bullets = op.get("bullets") or []
            layout = prs.slide_layouts[1] if len(prs.slide_layouts) > 1 else prs.slide_layouts[0]
            slide = prs.slides.add_slide(layout)
            if slide.shapes.title is not None:
                slide.shapes.title.text = title
            body = None
            for shape in slide.placeholders:
                if shape.placeholder_format.idx == 1:
                    body = shape
                    break
            if body is not None and bullets:
                tf = body.text_frame
                tf.text = str(bullets[0])
                for b in bullets[1:]:
                    tf.add_paragraph().text = str(b)
            applied.append({"op": kind, "title": title, "bullets": len(bullets)})
        elif kind == "replace_text":
            old, new = str(op.get("search") or ""), str(op.get("replace") or "")
            if not old:
                raise AppError("replace_text 缺少 search")
            hit = 0
            for slide in prs.slides:
                for shape in slide.shapes:
                    if shape.has_text_frame and old in shape.text_frame.text:
                        for p in shape.text_frame.paragraphs:
                            for r in p.runs:
                                if old in r.text:
                                    r.text = r.text.replace(old, new)
                                    hit += 1
            applied.append({"op": kind, "hits": hit})
        elif kind == "add_textbox":
            idx = int(op.get("slide") or 1) - 1
            if not (0 <= idx < len(prs.slides)):
                raise AppError("幻灯片序号越界：%s" % (idx + 1))
            box = prs.slides[idx].shapes.add_textbox(
                Inches(float(op.get("left") or 1)), Inches(float(op.get("top") or 1)),
                Inches(float(op.get("width") or 6)), Inches(float(op.get("height") or 2)))
            box.text_frame.text = str(op.get("text") or "")
            applied.append({"op": kind, "slide": idx + 1})
        else:
            raise AppError("pptx 不支持的操作：%s" % kind)
    prs.save(full)
    return applied


# ---------------- 视频 ----------------
def _run(cmd, timeout=60):
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


def video_info(path):
    full = resolve_path(path)
    info = {"ok": True, "path": full, "size": os.path.getsize(full),
            "format": os.path.splitext(full)[1].lstrip("."), "streams": [],
            "duration": 0, "bitrate": 0}
    if not shutil.which("ffprobe"):
        info["note"] = "未找到 ffprobe，仅返回文件元数据"
        return info
    r = _run(["ffprobe", "-v", "quiet", "-print_format", "json",
              "-show_format", "-show_streams", full], timeout=30)
    if r.returncode != 0:
        info["note"] = r.stderr.decode("utf-8", "ignore")[:200]
        return info
    data = json.loads(r.stdout.decode("utf-8", "ignore") or "{}")
    fmt = data.get("format") or {}
    info["duration"] = float(fmt.get("duration") or 0)
    info["bitrate"] = int(fmt.get("bit_rate") or 0)
    for s in data.get("streams") or []:
        item = {"type": s.get("codec_type"), "codec": s.get("codec_name")}
        if s.get("codec_type") == "video":
            item.update({"width": s.get("width"), "height": s.get("height"),
                         "fps": s.get("r_frame_rate")})
        elif s.get("codec_type") == "audio":
            item.update({"channels": s.get("channels"),
                         "sample_rate": s.get("sample_rate")})
        info["streams"].append(item)
    return info


def video_frames(path, count=6, width=640):
    full = resolve_path(path)
    if not shutil.which("ffmpeg"):
        raise AppError("未找到 ffmpeg，无法抽帧")
    meta = video_info(full)
    duration = float(meta.get("duration") or 0)
    count = max(1, min(int(count or 6), 24))
    FRAME_DIR.mkdir(parents=True, exist_ok=True)
    out_dir = FRAME_DIR / ("%s-%s" % (Path(full).stem,
                                      time.strftime("%Y%m%d-%H%M%S")))
    out_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for i in range(count):
        ts = (duration * (i + 0.5) / count) if duration else i
        out = out_dir / ("frame-%02d.jpg" % (i + 1))
        r = _run(["ffmpeg", "-y", "-ss", "%.2f" % ts, "-i", full,
                  "-frames:v", "1", "-vf", "scale=%d:-2" % int(width),
                  str(out)], timeout=60)
        if r.returncode == 0 and out.exists():
            frames.append({"path": str(out), "at": round(ts, 2),
                           "size": out.stat().st_size})
    return {"ok": True, "path": full, "duration": duration,
            "count": len(frames), "dir": str(out_dir), "frames": frames}


# ---------------- 代码编辑器 ----------------
def _is_text_file(path, sample=8192):
    try:
        with open(path, "rb") as f:
            data = f.read(sample)
        if b"\x00" in data:
            return False
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        try:
            data.decode("gb18030")
            return True
        except UnicodeDecodeError:
            return False
    except OSError:
        return False


def code_read(path, start=1, end=0, max_chars=120000):
    full = resolve_path(path)
    if not _is_text_file(full):
        raise AppError("不是文本文件：%s" % full)
    with open(full, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    total = len(lines)
    start = max(1, int(start or 1))
    end = int(end or 0) or total
    end = min(total, max(start, end))
    text = "".join(lines[start - 1:end])
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]
    return {"ok": True, "path": full, "total_lines": total,
            "start": start, "end": end, "truncated": truncated,
            "text": text, "language": os.path.splitext(full)[1].lstrip(".")}


def code_edit(path, operations, dry_run=False):
    full = resolve_path(path)
    if not _is_text_file(full):
        raise AppError("不是文本文件：%s" % full)
    ops = list(operations or [])[:100]
    if not ops:
        raise AppError("operations 不能为空")
    with open(full, "r", encoding="utf-8", errors="strict") as f:
        original = f.read()
    text = original
    applied = []
    for op in ops:
        kind = str(op.get("op") or "").lower()
        if kind == "replace":
            old = str(op.get("search") or "")
            new = str(op.get("replace") or "")
            if not old:
                raise AppError("replace 缺少 search")
            limit = int(op.get("count") or 0)
            hits = text.count(old)
            if not hits:
                raise AppError("未找到要替换的内容：%s" % old[:40])
            if limit > 0:
                text = text.replace(old, new, limit)
                applied.append({"op": kind, "hits": min(hits, limit)})
            else:
                text = text.replace(old, new)
                applied.append({"op": kind, "hits": hits})
        elif kind == "insert":
            line = max(1, int(op.get("line") or 1))
            body = str(op.get("text") or "")
            rows = text.splitlines(keepends=True)
            if line > len(rows) + 1:
                raise AppError("行号越界：%s" % line)
            rows.insert(line - 1, body if body.endswith("\n") else body + "\n")
            text = "".join(rows)
            applied.append({"op": kind, "line": line})
        elif kind == "append":
            body = str(op.get("text") or "")
            if text and not text.endswith("\n"):
                text += "\n"
            text += body + ("" if body.endswith("\n") else "\n")
            applied.append({"op": kind})
        elif kind == "write":
            text = str(op.get("content") or "")
            applied.append({"op": kind})
        else:
            raise AppError("代码编辑不支持的操作：%s" % kind)
    changed = text != original
    backup = "" if (dry_run or not changed) else _backup(full)
    if not dry_run and changed:
        with open(full, "w", encoding="utf-8", newline="") as f:
            f.write(text)
    return {"ok": True, "path": full, "backup": backup,
            "changed": changed, "dry_run": bool(dry_run), "applied": applied}


def code_search(query, root="", pattern="*", limit=50, max_file_kb=1024):
    q = str(query or "").strip()
    if not q:
        raise AppError("查询不能为空")
    base = resolve_path(root or str(WORKSPACE), must_exist=True)
    deadline = time.time() + 5
    limit = max(1, min(int(limit or 50), 200))
    results, scanned = [], 0
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS]
        if time.time() > deadline or len(results) >= limit:
            break
        for name in filenames:
            if time.time() > deadline or len(results) >= limit:
                break
            full = os.path.join(dirpath, name)
            scanned += 1
            name_hit = q.lower() in name.lower()
            line_hit = None
            if not name_hit and os.path.splitext(name)[1].lower() in _CODE_EXTS:
                try:
                    if os.path.getsize(full) <= max_file_kb * 1024:
                        with open(full, "r", encoding="utf-8", errors="ignore") as f:
                            for no, line in enumerate(f, 1):
                                if q.lower() in line.lower():
                                    line_hit = {"line": no, "text": line.strip()[:200]}
                                    break
                except OSError:
                    pass
            if name_hit or line_hit:
                results.append({"path": full, "name": name,
                                "match": "name" if name_hit else "content",
                                "line": (line_hit or {}).get("line"),
                                "snippet": (line_hit or {}).get("text")})
    return {"ok": True, "query": q, "root": base, "count": len(results),
            "scanned": scanned, "results": results, "partial": time.time() > deadline}


_CODE_EXTS = {".py", ".js", ".ts", ".tsx", ".jsx", ".vue", ".java", ".kt",
              ".c", ".h", ".cpp", ".hpp", ".cs", ".go", ".rs", ".php", ".rb",
              ".swift", ".m", ".sh", ".ps1", ".bat", ".sql", ".html", ".css",
              ".json", ".yaml", ".yml", ".toml", ".xml", ".md", ".txt"}
