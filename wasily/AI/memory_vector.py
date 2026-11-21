# -*- coding: utf-8 -*-
"""本地加密向量索引：正文仍由 memory_net 的 EncLines 保管。"""
import atexit
import json
import queue
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

import config
from secure_store import get_fernet

_DIR = Path(__file__).resolve().parent
_QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："


class VectorEncoder:
    """中文 BGE 由 embed_worker.py 子进程执行。

    llama.cpp 与 torch 在同一个进程里会原生崩溃（进程直接消失、日志无回溯），
    所以主服务只做管道通信，绝不 import sentence_transformers。
    """

    def __init__(self, model_name=None, model_dir=None):
        self.model_name = model_name or config.get(
            "memory.vector.model", "BAAI/bge-small-zh-v1.5")
        self.model_dir = Path(model_dir) if model_dir else (
            _DIR / "_model_cache" / self.model_name.rsplit("/", 1)[-1])
        self.model_ver = self.model_name + "|memory-v1"
        self._proc = None
        self._queue = None
        self._dim = 0
        self._error = ""
        self._load_lock = threading.Lock()
        self._encode_lock = threading.Lock()
        atexit.register(self.close)

    # ---------- 子进程生命周期 ----------
    def _alive(self):
        return self._proc is not None and self._proc.poll() is None

    def _pump(self, proc, q):
        try:
            for line in proc.stdout:
                q.put(line)
        except Exception:
            pass
        q.put(None)

    def _recv(self, timeout):
        try:
            line = self._queue.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("嵌入进程响应超时")
        if line is None:
            raise RuntimeError(f"嵌入进程已退出(rc={self._proc.poll() if self._proc else '?'})")
        return json.loads(line)

    def _kill(self):
        proc = self._proc
        self._proc, self._queue = None, None
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass

    def _start(self):
        if not (self.model_dir / "model.safetensors").is_file():
            raise FileNotFoundError("中文嵌入模型未安装: " + str(self.model_dir))
        proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", str(_DIR / "embed_worker.py"),
             str(self.model_dir)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        q = queue.Queue()
        threading.Thread(target=self._pump, args=(proc, q), daemon=True).start()
        self._proc, self._queue = proc, q
        hello = self._recv(float(config.get("memory.vector.start_timeout", 240)))
        if not hello.get("ready"):
            raise RuntimeError(hello.get("error") or "嵌入进程未就绪")
        self._dim = int(hello.get("dim") or 0)

    def _ensure(self):
        if self._alive():
            return
        with self._load_lock:
            if self._alive():
                return
            self._kill()
            try:
                self._start()
                self._error = ""
            except Exception as exc:
                self._error = f"{type(exc).__name__}: {exc}"
                self._kill()
                raise

    # ---------- 编码 ----------
    def _encode(self, texts, batch_size=16):
        if not texts:
            return np.empty((0, 0), dtype=np.float32)
        self._ensure()
        payload = json.dumps({"texts": [str(t) for t in texts],
                              "batch_size": int(batch_size)}) + "\n"
        try:
            with self._encode_lock:
                self._proc.stdin.write(payload)
                self._proc.stdin.flush()
                resp = self._recv(float(config.get(
                    "memory.vector.encode_timeout", 300)))
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            self._kill()
            raise
        if resp.get("error"):
            raise RuntimeError(resp["error"])
        return np.asarray(resp["vectors"], dtype=np.float32)

    def documents(self, texts, batch_size=16):
        return self._encode(texts, batch_size=batch_size)

    def query(self, text):
        return self._encode([_QUERY_PREFIX + str(text or "")], batch_size=1)[0]

    def status(self):
        return {"model": self.model_name, "ready": self._alive(),
                "dim": self._dim, "error": self._error}

    def close(self):
        proc = self._proc
        self._proc, self._queue = None, None
        if proc is not None and proc.poll() is None:
            try:
                proc.stdin.close()
                proc.terminate()
            except Exception:
                pass


def _normalized(vector):
    vec = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(vec))
    if not vec.size or norm <= 0:
        raise ValueError("向量不能为空或零向量")
    return vec / norm


class MemoryVectorStore:
    """SQLite 保存加密向量；相似度计算在本地 NumPy 中完成。"""

    def __init__(self, path=None, encoder=None):
        self.path = str(path or (_DIR / "mem_vectors.db"))
        self.encoder = encoder or VectorEncoder()
        self._lock = threading.RLock()
        self._db = sqlite3.connect(
            self.path, check_same_thread=False, timeout=30)
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS memory_vectors ("
                "entry_id INTEGER PRIMARY KEY, model_ver TEXT NOT NULL, "
                "dim INTEGER NOT NULL, vec_enc BLOB NOT NULL, "
                "updated_at REAL NOT NULL)")
            self._db.commit()

    def upsert_many(self, items):
        """items 为 (entry_id, vector)，单事务覆盖当前模型的旧向量。"""
        now = time.time()
        rows = []
        fernet = get_fernet()
        for entry_id, vector in items:
            vec = _normalized(vector).astype("<f4", copy=False)
            rows.append((int(entry_id), self.encoder.model_ver, vec.size,
                         fernet.encrypt(vec.tobytes()), now))
        if not rows:
            return 0
        with self._lock:
            self._db.executemany(
                "INSERT INTO memory_vectors "
                "(entry_id, model_ver, dim, vec_enc, updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(entry_id) DO UPDATE SET model_ver=excluded.model_ver, "
                "dim=excluded.dim, vec_enc=excluded.vec_enc, "
                "updated_at=excluded.updated_at", rows)
            self._db.commit()
        return len(rows)

    def upsert(self, entry_id, vector):
        return self.upsert_many([(entry_id, vector)])

    def delete(self, entry_ids):
        ids = [int(v) for v in entry_ids]
        if not ids:
            return 0
        with self._lock:
            self._db.executemany(
                "DELETE FROM memory_vectors WHERE entry_id=?",
                [(entry_id,) for entry_id in ids])
            self._db.commit()
        return len(ids)

    def indexed_ids(self):
        with self._lock:
            rows = self._db.execute(
                "SELECT entry_id FROM memory_vectors WHERE model_ver=?",
                (self.encoder.model_ver,)).fetchall()
        return {int(row[0]) for row in rows}

    def prune(self, valid_ids):
        """清除已遗忘正文和旧模型留下的无效索引。"""
        valid = {int(v) for v in valid_ids}
        with self._lock:
            rows = self._db.execute(
                "SELECT entry_id, model_ver FROM memory_vectors").fetchall()
            doomed = [int(entry_id) for entry_id, model_ver in rows
                      if int(entry_id) not in valid or model_ver != self.encoder.model_ver]
        return self.delete(doomed)

    def reconcile(self, records, batch_size=16):
        """可重复执行的增量索引；重启只补缺失条目，不重复编码。"""
        current = [r for r in records if r.get("id") is not None]
        valid_ids = {int(r["id"]) for r in current}
        self.prune(valid_ids)
        indexed = self.indexed_ids()
        pending = [r for r in current if int(r["id"]) not in indexed]
        encoded = 0
        for offset in range(0, len(pending), batch_size):
            group = pending[offset:offset + batch_size]
            texts = [(r.get("topic", "") + " " + r.get("text", "")).strip()
                     for r in group]
            vecs = self.encoder.documents(texts, batch_size=batch_size)
            self.upsert_many([(int(r["id"]), vec) for r, vec in zip(group, vecs)])
            encoded += len(group)
        return {"encoded": encoded, "indexed": len(self.indexed_ids()),
                "pending": len(valid_ids - self.indexed_ids())}

    def search(self, query_vector, top=5, allowed_ids=None):
        """返回 [(entry_id, cosine_score)]；坏行跳过，由下次对账修复。"""
        query = _normalized(query_vector)
        allowed = {int(v) for v in allowed_ids} if allowed_ids is not None else None
        with self._lock:
            rows = self._db.execute(
                "SELECT entry_id, dim, vec_enc FROM memory_vectors WHERE model_ver=?",
                (self.encoder.model_ver,)).fetchall()
        fernet = get_fernet()
        scored = []
        for entry_id, dim, encrypted in rows:
            entry_id = int(entry_id)
            if allowed is not None and entry_id not in allowed:
                continue
            try:
                raw = fernet.decrypt(encrypted)
                vec = np.frombuffer(raw, dtype="<f4")
                if vec.size != int(dim) or vec.size != query.size:
                    continue
                scored.append((entry_id, float(query @ vec)))
            except Exception:
                continue
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:max(1, int(top))]

    def stats(self, valid_ids=None):
        with self._lock:
            total = self._db.execute(
                "SELECT COUNT(*) FROM memory_vectors").fetchone()[0]
        indexed = len(self.indexed_ids())
        result = {"total": int(total), "indexed": indexed}
        if valid_ids is not None:
            result["pending"] = len({int(v) for v in valid_ids} - self.indexed_ids())
        result.update(self.encoder.status())
        return result

    def close(self):
        with self._lock:
            self._db.close()
