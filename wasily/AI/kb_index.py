# -*- coding: utf-8 -*-
"""知识库索引器：全盘遍历 → 正文提取 → 分块 → 本地 BGE 嵌入 → 落库。

增量策略：以 (size, mtime) 判定变化；不变且已索引的文件直接跳过。
只读策略：源文件只以只读方式打开；所有写入都落在 kb_data/kb.db 与 kb_data/tmp。
"""
import hashlib
import os
import threading
import time
import traceback
from pathlib import Path

import config
import kb_parser
import kb_store
import kb_vlm

BASE_DIR = Path(__file__).resolve().parent
_MODEL_DIR = BASE_DIR / "_model_cache" / "bge-small-zh-v1.5"

_DEFAULTS = {
    "kb.roots": ["C:\\", "D:\\"],
    "kb.exclude": [],
    "kb.max_file_mb": 20,
    "kb.max_text_chars": 200000,
    "kb.max_pdf_pages": 60,
    "kb.max_sheet_rows": 2000,
    "kb.max_chunk_chars": 700,
    "kb.chunk_overlap": 100,
    "kb.image.caption": True,
    "kb.image.caption_max": 200,   # 单次索引最多做多少张图描述，防止 CPU 被图片吃满
    "kb.max_db_gb": 3.0,
    "kb.index.batch": 16,
    "kb.index.sleep_ms": 0,
}


def cfg(key, default=None):
    if default is None:
        default = _DEFAULTS.get(key)
    try:
        val = config.get(key, default)
    except Exception:  # noqa: BLE001
        val = default
    return default if val is None else val


def limits():
    return {
        "max_file_mb": int(cfg("kb.max_file_mb", 20)),
        "max_text_chars": int(cfg("kb.max_text_chars", 200000)),
        "max_pdf_pages": int(cfg("kb.max_pdf_pages", 60)),
        "max_sheet_rows": int(cfg("kb.max_sheet_rows", 2000)),
        "max_chunk_chars": int(cfg("kb.max_chunk_chars", 700)),
        "chunk_overlap": int(cfg("kb.chunk_overlap", 100)),
    }


def _encoder():
    """BGE 编码器复用 memory_vector 的子进程通道（torch 不能进主进程）。"""
    global _ENC
    with _ENC_LOCK:
        if _ENC is None:
            from memory_vector import VectorEncoder
            _ENC = VectorEncoder(model_name="BAAI/bge-small-zh-v1.5",
                                 model_dir=_MODEL_DIR)
        return _ENC


_ENC = None
_ENC_LOCK = threading.Lock()


class Indexer:
    def __init__(self):
        self.store = kb_store.get_store()
        self._lock = threading.RLock()
        self._thread = None
        self._stop = threading.Event()
        self.state = {
            "running": False, "phase": "idle", "current": "", "message": "",
            "found": 0, "indexed": 0, "skipped": 0, "errors": 0, "images": 0,
            "captioned": 0, "chars": 0, "started": 0.0, "elapsed": 0.0, "roots": [],
        }

    # ---------- 对外 ----------
    def status(self):
        with self._lock:
            st = dict(self.state)
        if st["running"] and st["started"]:
            st["elapsed"] = round(time.time() - st["started"], 1)
        st.update(self.store.status_counts())
        st["db_mb"] = round(self.store.db_size() / 1048576, 1)
        st["vision"] = kb_vlm.model_info()
        return st

    def start(self, roots=None, force=False):
        with self._lock:
            if self.state["running"]:
                return {"ok": False, "error": "索引任务已在运行", "status": self.status()}
            roots = roots or cfg("kb.roots") or ["C:\\", "D:\\"]
            if isinstance(roots, str):
                roots = [roots]
            roots = [os.path.abspath(r) for r in roots if os.path.isdir(r)]
            if not roots:
                return {"ok": False, "error": "没有可扫描的目录"}
            self._stop.clear()
            self.state.update({
                "running": True, "phase": "scan", "current": "", "message": "",
                "found": 0, "indexed": 0, "skipped": 0, "errors": 0, "images": 0,
                "captioned": 0, "chars": 0, "started": time.time(), "elapsed": 0.0,
                "roots": roots,
            })
            self._thread = threading.Thread(
                target=self._run, args=(roots, bool(force)),
                name="kb-indexer", daemon=True)
            self._thread.start()
        return {"ok": True, "status": self.status()}

    def stop(self):
        self._stop.set()
        with self._lock:
            self.state["message"] = "正在停止…"
        return {"ok": True, "status": self.status()}

    # ---------- 内部 ----------
    def _set(self, **kw):
        with self._lock:
            self.state.update(kw)

    def _run(self, roots, force):
        t0 = time.time()
        try:
            self._set(phase="scan")
            seen = 0
            exclude = tuple(str(x).replace("\\", "/").lower()
                            for x in (cfg("kb.exclude") or []))
            for path, kind, size, mtime in kb_parser.iter_files(
                    roots, limits(), exclude or None):
                if self._stop.is_set():
                    break
                seen += 1
                if seen % 200 == 0:
                    self._set(found=seen, current=path)
                try:
                    self.store.upsert_file(path, self._root_of(path, roots),
                                           kind, size, mtime)
                except Exception:  # noqa: BLE001 单文件登记失败不影响整体
                    self._set(errors=self.state["errors"] + 1)
                    continue
            if force:
                self.store.reset_statuses()
            self._set(found=seen, phase="index")
            for root in roots:
                try:
                    self.store.mark_missing_under_root(root)
                except Exception:  # noqa: BLE001
                    pass
            self._index_pending()
            if self._stop.is_set():
                self._set(message="已手动停止")
            self._set(phase="done")
        except Exception as exc:  # noqa: BLE001
            self._set(message="索引异常: %s" % exc,
                      detail=traceback.format_exc()[-1500:])
        finally:
            self._set(running=False, current="", elapsed=round(time.time() - t0, 1))
            self.store.meta_set("kb.last_run", {"time": time.time(),
                                                "elapsed": round(time.time() - t0, 1)})

    @staticmethod
    def _root_of(path, roots):
        p = os.path.abspath(path)
        best = ""
        for r in roots:
            r = os.path.abspath(r)
            if os.path.normcase(p).startswith(os.path.normcase(r.rstrip("\\/") + os.sep)):
                if len(r) > len(best):
                    best = r
        return best or os.path.abspath(roots[0])

    def _index_pending(self):
        lim = limits()
        batch_size = max(1, int(cfg("kb.index.batch", 16)))
        max_db = float(cfg("kb.max_db_gb", 3.0)) * (1024 ** 3)
        sleep_ms = int(cfg("kb.index.sleep_ms", 0))
        allow_caption = bool(cfg("kb.image.caption", True))
        caption_max = int(cfg("kb.image.caption_max", 500))
        encoder = None
        attempted = set()
        while not self._stop.is_set():
            ids = [i for i in self.store.pending_files(200) if i not in attempted]
            if not ids:
                break
            for fid in ids:
                attempted.add(fid)
                if self._stop.is_set():
                    return
                rec = self.store.get_file(fid)
                if not rec:
                    continue
                if self.store.db_size() > max_db:
                    self._set(message="索引库已达 %.1fGB 上限，自动停止；"
                                      "可调大 kb.max_db_gb 或缩小扫描范围"
                                      % (max_db / 1024 ** 3))
                    self._stop.set()
                    return
                self._set(current=rec["path"])
                try:
                    if encoder is None:
                        encoder = _encoder()
                    text, caption, note = "", "", ""
                    if rec["kind"] == "image":
                        allow = (allow_caption and kb_vlm.model_ready()
                                 and self.state["captioned"] < caption_max)
                        info = kb_parser.extract(rec["path"], rec["kind"], lim, allow)
                        text, caption, note = info["text"], info["caption"], info["note"]
                        if caption:
                            self._set(captioned=self.state["captioned"] + 1)
                        self._set(images=self.state["images"] + 1)
                    else:
                        info = kb_parser.extract(rec["path"], rec["kind"], lim)
                        text = info["text"]
                    chunks = kb_parser.chunk_text(text, lim["max_chunk_chars"],
                                                  lim["chunk_overlap"])
                    if not chunks or not any(c.strip() for c in chunks):
                        self.store.set_status(fid, "skipped",
                                              note or "无可索引的文本内容")
                        self._set(skipped=self.state["skipped"] + 1)
                        continue
                    vectors = []
                    for i in range(0, len(chunks), batch_size):
                        if self._stop.is_set():
                            return
                        vectors.extend(encoder.documents(chunks[i:i + batch_size]))
                    h = hashlib.sha1("\n".join(chunks).encode("utf-8", "ignore")).hexdigest()
                    self.store.replace_chunks(fid, chunks, vectors[:len(chunks)],
                                              text_hash=h,
                                              caption=caption if rec["kind"] == "image" else None)
                    self._set(indexed=self.state["indexed"] + 1,
                              chars=self.state["chars"] + sum(len(c) for c in chunks))
                except Exception as exc:  # noqa: BLE001
                    self.store.set_status(fid, "error", "%s" % exc)
                    self._set(errors=self.state["errors"] + 1)
                if sleep_ms:
                    time.sleep(sleep_ms / 1000.0)
        if encoder is not None:
            pass


_indexer = None
_indexer_lock = threading.Lock()


def get_indexer():
    global _indexer
    with _indexer_lock:
        if _indexer is None:
            _indexer = Indexer()
        return _indexer
