# -*- coding: utf-8 -*-
"""Obsidian 知识库：把 vault 里的 Markdown 向量化，供语义检索。

设计：
- vault 默认指向 D:\\煮饭仔点餐（用户已把整个项目开成 Obsidian 库）。
- 只索引 .md，跳过 .git / node_modules / .obsidian / .trae / 产物目录。
- 按标题分块，单块超过 600 字再切，块间留 100 字重叠。
- 向量用 memory_net 现成的 BGE 中文编码器，存到独立的 obsidian_kb_vectors.db，
  不污染对话记忆的向量库。
- 元数据（块文本 + 来源路径）存 obsidian_kb_chunks.jsonl，检索时按 entry_id 反查。
- 增量索引：按文件 mtime 判断，没变就跳过。
"""
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

import config
from memory_vector import MemoryVectorStore, VectorEncoder

_DIR = Path(__file__).resolve().parent
_VAULT = Path(config.get("kb.vault", "D:\\煮饭仔点餐"))
_CHUNK_FILE = _DIR / "obsidian_kb_chunks.jsonl"
_VECTOR_DB = _DIR / "obsidian_kb_vectors.db"
_SKIP_DIRS = {".git", ".obsidian", ".trae", ".wasily", "node_modules",
              "dist", "build", "__pycache__", ".venv", "venv", ".idea", ".vscode",
              "miniapp", "node", "_model_cache", ".pw-profile", ".playwright-mcp"}
_MAX_CHUNK = 600
_OVERLAP = 100

_encoder = VectorEncoder()
_store = MemoryVectorStore(path=str(_VECTOR_DB), encoder=_encoder)
_lock = threading.RLock()
_indexing = {"running": False, "last": 0.0, "docs": 0, "chunks": 0}


def _vault():
    p = Path(os.environ.get("OBSIDIAN_VAULT", str(_VAULT)))
    return p


def _iter_md(vault):
    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if f.lower().endswith(".md"):
                yield Path(root) / f


def _split_md(text):
    """按标题切分；超长段落再按字数切。"""
    # 统一换行
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # 按 # 标题切
    parts = re.split(r'(?m)^(#{1,6}\s+.+)$', text)
    blocks = []
    cur_title = ""
    cur_body = ""
    for i, part in enumerate(parts):
        if i % 2 == 1:   # 标题行
            if cur_body.strip():
                blocks.append((cur_title, cur_body))
            cur_title = part.strip()
            cur_body = ""
        else:
            cur_body += part
    if cur_body.strip():
        blocks.append((cur_title, cur_body))

    out = []
    for title, body in blocks:
        body = body.strip()
        if not body:
            continue
        # 超长再切
        if len(body) <= _MAX_CHUNK:
            out.append((title, body))
        else:
            step = _MAX_CHUNK - _OVERLAP
            for s in range(0, len(body), step):
                piece = body[s:s + _MAX_CHUNK].strip()
                if len(piece) >= 40:
                    out.append((title, piece))
    return out


def _entry_id(path, idx):
    """稳定的整数 id：path 的 md5 前 8 位 + 块序号。"""
    h = hashlib.md5(str(path).encode("utf-8")).hexdigest()[:8]
    return int(h, 16) * 10000 + idx


def _load_chunks():
    rows = []
    if _CHUNK_FILE.is_file():
        with open(_CHUNK_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
    return rows


def _save_chunks(rows):
    tmp = str(_CHUNK_FILE) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, _CHUNK_FILE)


def index(force=False):
    """扫描 vault，向量化新/变的 md。返回统计。"""
    if _indexing["running"]:
        return {"running": True, "docs": _indexing["docs"],
                "chunks": _indexing["chunks"]}
    vault = _vault()
    if not vault.is_dir():
        return {"error": "vault 不存在: %s" % vault}
    _indexing["running"] = True
    t0 = time.time()
    try:
        old = {r["path"]: r for r in _load_chunks()}
        seen = set()
        rows = []
        docs = 0
        chunks = 0
        # 收集当前所有 md 的 (path, mtime)
        current = {}
        for p in _iter_md(vault):
            try:
                mt = p.stat().st_mtime
                current[str(p)] = mt
            except Exception:
                pass
        # 先决定哪些要重切
        to_encode = []   # (entry_id, text)
        new_rows = []
        for path_str, mt in current.items():
            seen.add(path_str)
            prev = old.get(path_str)
            if not force and prev and abs(prev.get("mtime", 0) - mt) < 1e-3:
                # 没变，原样保留（向量也还在库里）
                rows.extend(prev.get("chunks", []))
                docs += 1
                chunks += len(prev.get("chunks", []))
                continue
            # 变了或新文件
            try:
                text = Path(path_str).read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            rel = str(Path(path_str).relative_to(vault))
            blks = _split_md(text)
            file_rows = []
            for idx, (title, body) in enumerate(blks):
                eid = _entry_id(path_str, idx)
                # 标题 + 正文一起编码，提升标题命中
                enc_text = (title + "\n" + body).strip() if title else body
                to_encode.append((eid, enc_text))
                file_rows.append({"id": eid, "path": path_str, "rel": rel,
                                  "title": title, "text": body[:1200], "mtime": mt})
            rows.append({"path": path_str, "mtime": mt, "chunks": file_rows})
            docs += 1
            chunks += len(file_rows)
            new_rows.append(file_rows)

        # 删掉已不存在的文件
        for path_str in list(old.keys()):
            if path_str not in seen:
                eids = [c["id"] for c in old[path_str].get("chunks", [])]
                if eids:
                    try:
                        _store.delete(eids)
                    except Exception:
                        pass

        # 批量编码
        if to_encode:
            batch = max(1, int(config.get("memory.vector.batch_size", 16)))
            for off in range(0, len(to_encode), batch):
                group = to_encode[off:off + batch]
                texts = [t for _, t in group]
                try:
                    vecs = _encoder.documents(texts, batch_size=batch)
                    _store.upsert_many([(eid, v) for (eid, _), v in zip(group, vecs)])
                except Exception as e:
                    print("[obsidian_kb] encode fail: %s" % e, flush=True)

        _save_chunks(rows)
        _indexing["docs"] = docs
        _indexing["chunks"] = chunks
        _indexing["last"] = time.time()
        return {"docs": docs, "chunks": chunks, "elapsed": round(time.time() - t0, 1),
                "vault": str(vault)}
    finally:
        _indexing["running"] = False


def search(query, top_k=5):
    """语义检索，返回 [{path, title, text, score}]。"""
    if not query:
        return []
    try:
        qv = _encoder.query(query)
    except Exception as e:
        return [{"error": "向量编码失败: %s" % e}]
    hits = _store.search(qv, top=max(1, int(top_k)))
    by_id = {}
    for r in _load_chunks():
        for c in r.get("chunks", []):
            by_id[c["id"]] = c
    out = []
    for eid, score in hits:
        c = by_id.get(eid)
        if c:
            out.append({"path": c.get("rel") or c.get("path", ""),
                        "title": c.get("title", ""),
                        "text": c.get("text", ""),
                        "score": round(float(score), 4)})
    return out


def status():
    rows = _load_chunks()
    docs = len(rows)
    chunks = sum(len(r.get("chunks", [])) for r in rows)
    return {
        "vault": str(_vault()),
        "vault_exists": _vault().is_dir(),
        "docs": docs,
        "chunks": chunks,
        "indexing": _indexing["running"],
        "last_index": _indexing["last"],
        "vector_model": _encoder.model_name,
        "vector_ready": _encoder._alive() if hasattr(_encoder, "_alive") else True,
    }


def docs_list():
    """列出已索引的文档（相对路径）。"""
    rows = _load_chunks()
    return [{"rel": r["chunks"][0].get("rel") if r.get("chunks") else r.get("path", ""),
             "chunks": len(r.get("chunks", []))}
            for r in rows if r.get("chunks")]
