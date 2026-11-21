# -*- coding: utf-8 -*-
"""本地知识库存储层（Khoj 同构：files / chunks / vectors / FTS）。

设计要点
- 只写 kb_data/kb.db，绝不写、移动、删除被索引的源文件（只读约束）。
- 元数据与正文分表：chunks 存原文，chunks_fts 用 trigram 外部内容索引，
  中文 3 字以上可直接短语匹配，避免依赖分词器；2 字查询回退 LIKE。
- 向量以 float32 BLOB 存 vecs 表，检索时按批流式扫描，内存占用与库大小无关。
- 单连接 + RLock：索引线程与 Web 检索并发时 SQLite(WAL) 读不阻塞写。
"""
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

import numpy as np

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("WASILY_KB_DIR") or (BASE_DIR / "kb_data"))
DB_PATH = DATA_DIR / "kb.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    root TEXT,
    name TEXT,
    ext TEXT,
    kind TEXT,
    size INTEGER,
    mtime REAL,
    status TEXT DEFAULT 'pending',
    error TEXT,
    chunks INTEGER DEFAULT 0,
    chars INTEGER DEFAULT 0,
    text_hash TEXT,
    caption TEXT,
    scan_ts REAL,
    indexed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_kind ON files(kind);
CREATE INDEX IF NOT EXISTS idx_files_root ON files(root);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL,
    ord INTEGER NOT NULL,
    kind TEXT,
    text TEXT NOT NULL,
    meta TEXT
);
CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_id);
CREATE TABLE IF NOT EXISTS vecs (
    chunk_id INTEGER PRIMARY KEY,
    dim INTEGER,
    v BLOB
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, content='chunks', content_rowid='id', tokenize='trigram'
);
"""


def norm_path(path):
    """统一路径键：绝对路径 + normcase，Windows 下大小写不同视为同一文件。"""
    return os.path.normcase(os.path.abspath(str(path)))


class KbStore:
    def __init__(self, db_path=None):
        self.db_path = Path(db_path or DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._c = None
        self._init()

    def _init(self):
        with self._lock:
            if self._c is None:
                self._c = sqlite3.connect(str(self.db_path), timeout=30,
                                          check_same_thread=False)
                self._c.execute("PRAGMA journal_mode=WAL")
                self._c.execute("PRAGMA synchronous=NORMAL")
                self._c.execute("PRAGMA busy_timeout=30000")
            self._c.executescript(_SCHEMA)
            self._c.commit()

    # ---------- meta ----------
    def meta_get(self, key, default=None):
        with self._lock:
            row = self._c.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row[0])
        except Exception:  # noqa: BLE001
            return row[0]

    def meta_set(self, key, value):
        with self._lock:
            self._c.execute("INSERT INTO meta(k,v) VALUES(?,?) "
                            "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                            (key, json.dumps(value, ensure_ascii=False)))
            self._c.commit()

    # ---------- files ----------
    def upsert_file(self, path, root, kind, size, mtime):
        """登记/更新文件元数据，返回 (file_id, 是否需要重建索引)。"""
        key = norm_path(path)
        p = Path(path)
        with self._lock:
            row = self._c.execute(
                "SELECT id,size,mtime,status FROM files WHERE path=?", (key,)).fetchone()
            now = time.time()
            if row:
                fid, old_size, old_mtime, status = row
                changed = (int(old_size or 0) != int(size)) or (
                    abs(float(old_mtime or 0) - float(mtime)) > 1e-6)
                self._c.execute(
                    "UPDATE files SET root=?,name=?,ext=?,kind=?,size=?,mtime=?,"
                    "scan_ts=? WHERE id=?",
                    (root, p.name, p.suffix.lower(), kind, size, mtime, now, fid))
                if changed:
                    self._c.execute("UPDATE files SET status='pending' WHERE id=?", (fid,))
                self._c.commit()
                return fid, changed
            cur = self._c.execute(
                "INSERT INTO files(path,root,name,ext,kind,size,mtime,status,scan_ts)"
                " VALUES(?,?,?,?,?,?,?, 'pending', ?)",
                (key, root, p.name, p.suffix.lower(), kind, size, mtime, now))
            self._c.commit()
            return cur.lastrowid, True

    def set_status(self, file_id, status, error="", caption=None):
        with self._lock:
            if caption is None:
                self._c.execute("UPDATE files SET status=?,error=?,indexed_at=? WHERE id=?",
                                (status, (error or "")[:500], time.time(), file_id))
            else:
                self._c.execute("UPDATE files SET status=?,error=?,caption=?,indexed_at=?"
                                " WHERE id=?",
                                (status, (error or "")[:500], caption[:1000],
                                 time.time(), file_id))
            self._c.commit()

    def get_file(self, file_id):
        with self._lock:
            row = self._c.execute(
                "SELECT id,path,root,name,ext,kind,size,mtime,status,error,chunks,"
                "chars,text_hash,caption,scan_ts,indexed_at FROM files WHERE id=?",
                (file_id,)).fetchone()
        return self._row_file(row)

    def get_file_by_path(self, path):
        with self._lock:
            row = self._c.execute(
                "SELECT id,path,root,name,ext,kind,size,mtime,status,error,chunks,"
                "chars,text_hash,caption,scan_ts,indexed_at FROM files WHERE path=?",
                (norm_path(path),)).fetchone()
        return self._row_file(row)

    @staticmethod
    def _row_file(row):
        if not row:
            return None
        keys = ("id", "path", "root", "name", "ext", "kind", "size", "mtime",
                "status", "error", "chunks", "chars", "text_hash", "caption",
                "scan_ts", "indexed_at")
        return dict(zip(keys, row))

    def reset_statuses(self):
        """强制重建：把所有已处理状态改回 pending（不动任何源文件）。"""
        with self._lock:
            self._c.execute(
                "UPDATE files SET status='pending' "
                "WHERE status IN ('indexed','skipped','error','missing')")
            self._c.commit()

    def files_by_ids(self, ids):
        if not ids:
            return {}
        qs = ",".join("?" * len(set(ids)))
        with self._lock:
            rows = self._c.execute(
                "SELECT id,path,root,name,ext,kind,size,mtime,status,error,chunks,"
                "chars,text_hash,caption,scan_ts,indexed_at FROM files "
                f"WHERE id IN ({qs})", tuple(set(ids))).fetchall()
        return {r[0]: self._row_file(r) for r in rows}

    def pending_files(self, limit=200):
        with self._lock:
            rows = self._c.execute(
                "SELECT id FROM files WHERE status IN ('pending','error') "
                "ORDER BY status DESC, id LIMIT ?", (int(limit),)).fetchall()
        return [r[0] for r in rows]

    def list_recent(self, limit=30, kind=None):
        """最近入索引的文件，供 Agent 知识库页展示（只读查询）。"""
        sql = ("SELECT id,path,name,kind,size,mtime,caption,indexed_at FROM files "
               "WHERE status='indexed'")
        args = []
        if kind and kind != "all":
            sql += " AND kind=?"
            args.append(kind)
        sql += " ORDER BY indexed_at DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._c.execute(sql, args).fetchall()
        return [{"file_id": r[0], "path": r[1], "name": r[2], "kind": r[3],
                 "size": r[4], "mtime": r[5], "caption": r[6],
                 "indexed_at": r[7]} for r in rows]

    def status_counts(self):
        with self._lock:
            rows = self._c.execute(
                "SELECT status, COUNT(*) FROM files GROUP BY status").fetchall()
            kinds = self._c.execute(
                "SELECT kind, COUNT(*) FROM files WHERE status='indexed' "
                "GROUP BY kind").fetchall()
            tot = self._c.execute(
                "SELECT COUNT(*), COALESCE(SUM(chunks),0), COALESCE(SUM(chars),0),"
                " COALESCE(SUM(size),0) FROM files WHERE status='indexed'").fetchone()
        return {"by_status": dict(rows), "by_kind": dict(kinds),
                "indexed_files": int(tot[0] or 0), "chunks": int(tot[1] or 0),
                "chars": int(tot[2] or 0), "source_bytes": int(tot[3] or 0)}

    def mark_missing_under_root(self, root):
        """源文件消失时只更新数据库状态，不动磁盘。返回受影响条数。"""
        root_key = norm_path(root)
        prefix = root_key.rstrip("\\/") + os.sep
        with self._lock:
            rows = self._c.execute(
                "SELECT id,path FROM files WHERE path LIKE ? AND status!='missing'",
                (prefix.replace("%", r"\%").replace("_", r"\_") + "%",)).fetchall()
            gone = [{**{"id": i, "path": p}} for i, p in rows
                    if not os.path.exists(p)]
            if gone:
                self._c.executemany("UPDATE files SET status='missing' WHERE id=?",
                                    [(g["id"],) for g in gone])
                self._c.commit()
        return len(gone)

    # ---------- chunks ----------
    def replace_chunks(self, file_id, chunks, vectors, text_hash="", caption=None):
        """整文件替换：先删旧块与向量，再写入新块并同步 FTS。"""
        assert len(chunks) == len(vectors), "chunks 与 vectors 数量必须一致"
        now = time.time()
        with self._lock:
            old = [r[0] for r in self._c.execute(
                "SELECT id FROM chunks WHERE file_id=?", (file_id,)).fetchall()]
            if old:
                qs = ",".join("?" * len(old))
                self._c.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({qs})", old)
                self._c.execute(f"DELETE FROM vecs WHERE chunk_id IN ({qs})", old)
            self._c.execute("DELETE FROM chunks WHERE file_id=?", (file_id,))
            total_chars = 0
            for i, (text, vec) in enumerate(zip(chunks, vectors)):
                cur = self._c.execute(
                    "INSERT INTO chunks(file_id,ord,kind,text,meta) VALUES(?,?,?,?,?)",
                    (file_id, i, "text", text, "{}"))
                cid = cur.lastrowid
                arr = np.asarray(vec, dtype=np.float32)
                self._c.execute("INSERT INTO vecs(chunk_id,dim,v) VALUES(?,?,?)",
                                (cid, int(arr.shape[0]), arr.tobytes()))
                self._c.execute("INSERT INTO chunks_fts(rowid,text) VALUES(?,?)",
                                (cid, text))
                total_chars += len(text)
            if caption is None:
                self._c.execute(
                    "UPDATE files SET status='indexed',error='',chunks=?,chars=?,"
                    "text_hash=?,indexed_at=? WHERE id=?",
                    (len(chunks), total_chars, text_hash, now, file_id))
            else:
                self._c.execute(
                    "UPDATE files SET status='indexed',error='',chunks=?,chars=?,"
                    "text_hash=?,caption=?,indexed_at=? WHERE id=?",
                    (len(chunks), total_chars, text_hash, caption[:1000], now, file_id))
            self._c.commit()
        invalidate_vector_cache()   # 向量变了，下次检索重建内存矩阵

    def chunks_by_ids(self, ids):
        if not ids:
            return {}
        qs = ",".join("?" * len(ids))
        with self._lock:
            rows = self._c.execute(
                f"SELECT id,file_id,ord,text FROM chunks WHERE id IN ({qs})", ids
            ).fetchall()
        return {r[0]: {"id": r[0], "file_id": r[1], "ord": r[2], "text": r[3]}
                for r in rows}

    def chunks_of_file(self, file_id):
        with self._lock:
            rows = self._c.execute(
                "SELECT id,ord,text FROM chunks WHERE file_id=? ORDER BY ord",
                (file_id,)).fetchall()
        return [{"id": r[0], "ord": r[1], "text": r[2]} for r in rows]

    # ---------- 检索 ----------
    def _vec_key(self):
        # 不能用 COUNT(*)：2.9GB 库上是全表扫描，单次 30s+。
        # 用写入时自增的版本号 + MAX(chunk_id)（走主键索引，O(log n)）判断缓存是否过期。
        row = self._c.execute("SELECT COALESCE(MAX(chunk_id),0) FROM vecs").fetchone()
        return int(self.meta_get("kb.vec_epoch", 0) or 0), int(row[0] or 0)

    def _build_vec_cache(self):
        """把整库向量规整成 int8 矩阵常驻内存：960MB 的 float32 压到 ~240MB，
        查询从「每次读 2.9GB 磁盘」变成「一次加载 + 内存矩阵点积」。
        不预分配（COUNT(*) 在大库上极慢），按批追加后一次拼接。"""
        global _VEC_CACHE
        key = self._vec_key()
        dim = 0
        ids_parts = []
        mats = []
        # 用独立只读连接：整库扫描要 1~2 分钟，不能占住主连接的锁，
        # 否则索引状态/检索接口会被一直阻塞。
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        try:
            cur = conn.execute("SELECT chunk_id,dim,v FROM vecs")
            while True:
                rows = cur.fetchmany(4000)
                if not rows:
                    break
                ids = []
                arrs = []
                for cid, d, blob in rows:
                    v = np.frombuffer(blob, dtype=np.float32)
                    if not dim:
                        dim = int(d)
                    if v.shape[0] != dim:
                        continue
                    nv = float(np.linalg.norm(v)) or 1.0
                    arrs.append(np.clip(np.rint(v / nv * 127.0), -127, 127)
                                .astype(np.int8))
                    ids.append(cid)
                if arrs:
                    ids_parts.append(np.asarray(ids, dtype=np.int32))
                    mats.append(np.vstack(arrs))
        finally:
            conn.close()
        if mats:
            cache = {"key": key, "ids": np.concatenate(ids_parts),
                     "mat": np.vstack(mats), "dim": dim}
        else:
            cache = {"key": key, "ids": np.empty(0, np.int32), "mat": None, "dim": 0}
        with _VEC_LOCK:
            _VEC_CACHE = cache
        return cache

    def vector_cache_ready(self):
        cache = _VEC_CACHE
        try:
            return bool(cache) and cache["key"] == self._vec_key()
        except Exception:  # noqa: BLE001
            return False

    def vector_cache_nowait(self):
        """非阻塞读取向量缓存：已就绪则返回，否则立即返回 None（绝不触发构建）。
        检索路径必须用它——否则缓存未建时会在大库上同步扫表数分钟，把接口拖死。"""
        cache = _VEC_CACHE
        try:
            if cache and cache["key"] == self._vec_key():
                return cache
        except Exception:  # noqa: BLE001
            pass
        return None

    def vector_cache(self):
        global _VEC_BUILDING
        cache = _VEC_CACHE
        if cache and cache["key"] == self._vec_key():
            return cache
        with _VEC_LOCK:
            cache = _VEC_CACHE
            if cache and cache["key"] == self._vec_key():
                return cache
            if _VEC_BUILDING:
                return cache or {"key": None, "ids": np.empty(0, np.int32),
                                 "mat": None, "dim": 0}
            _VEC_BUILDING = True
        try:
            return self._build_vec_cache()
        finally:
            _VEC_BUILDING = False

    def knn(self, query_vec, limit=60, batch=4000):
        """余弦近邻：优先走内存 int8 矩阵，失败再回退逐批扫表。"""
        q = np.asarray(query_vec, dtype=np.float32).ravel()
        cache = self.vector_cache_nowait()
        if cache is None or cache.get("key") is None:
            # 向量缓存未就绪：本轮跳过向量路，由 search() 降级为纯全文（FTS）。
            # 绝不在这里触发构建或持锁扫表——那会让单次检索变成分钟级并卡死 KB 子系统。
            return []
        if cache.get("mat") is not None and cache["mat"].shape[0]:
            qn = float(np.linalg.norm(q)) or 1.0
            qi = np.clip(np.rint(q / qn * 127.0), -127, 127).astype(np.float32)
            mat, ids = cache["mat"], cache["ids"]
            if mat.shape[1] == qi.shape[0]:
                scores = np.empty(mat.shape[0], dtype=np.float32)
                for start in range(0, mat.shape[0], 50000):
                    end = min(start + 50000, mat.shape[0])
                    scores[start:end] = mat[start:end].astype(np.float32) @ qi
                k = min(int(limit), scores.shape[0])
                idx = np.argpartition(-scores, k - 1)[:k] if k < scores.shape[0] \
                    else np.arange(scores.shape[0])
                out = [(float(scores[i]), int(ids[i])) for i in idx]
                out.sort(key=lambda x: -x[0])
                return out
        norm = float(np.linalg.norm(q)) or 1.0
        q = q / norm
        scored = []
        with self._lock:
            cur = self._c.execute("SELECT chunk_id,dim,v FROM vecs")
            while True:
                rows = cur.fetchmany(batch)
                if not rows:
                    break
                ids = []
                mats = []
                for cid, dim, blob in rows:
                    v = np.frombuffer(blob, dtype=np.float32)
                    if v.shape[0] != q.shape[0]:
                        continue
                    ids.append(cid)
                    mats.append(v)
                if not mats:
                    continue
                mat = np.vstack(mats)
                scores = mat @ q
                k = min(limit, scores.shape[0])
                idx = np.argpartition(-scores, k - 1)[:k] if k < scores.shape[0] else \
                    np.arange(scores.shape[0])
                for i in idx:
                    scored.append((float(scores[i]), ids[i]))
        scored.sort(key=lambda x: -x[0])
        return scored[:limit]

    def fts(self, query, limit=60):
        """全文检索：≥3 字走 trigram 短语，短词回退 LIKE。返回 [(chunk_id, bm25)]。"""
        q = (query or "").strip()
        if not q:
            return []
        terms = [t for t in re.split(r"\s+", q) if t]
        if terms and all(len(t) >= 3 for t in terms):
            match = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
            try:
                with self._lock:
                    rows = self._c.execute(
                        "SELECT rowid, bm25(chunks_fts) FROM chunks_fts "
                        "WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT ?",
                        (match, int(limit))).fetchall()
                return [(int(r[0]), float(r[1])) for r in rows]
            except sqlite3.OperationalError:
                pass
        with self._lock:
            rows = self._c.execute(
                "SELECT id FROM chunks WHERE text LIKE ? LIMIT ?",
                ("%" + q.replace("%", r"\%").replace("_", r"\_") + "%", int(limit))
            ).fetchall()
        return [(int(r[0]), 0.0) for r in rows]

    def db_size(self):
        try:
            return self.db_path.stat().st_size
        except OSError:
            return 0


_store = None
_store_lock = threading.Lock()
# 向量内存缓存（进程级）：int8 矩阵 + chunk_id 数组
_VEC_CACHE = None
_VEC_LOCK = threading.Lock()
_VEC_BUILDING = False


def invalidate_vector_cache():
    """索引写入新向量后调用，下次查询重建缓存。"""
    global _VEC_CACHE
    with _VEC_LOCK:
        _VEC_CACHE = None


def get_store():
    global _store
    with _store_lock:
        if _store is None:
            _store = KbStore()
        return _store
