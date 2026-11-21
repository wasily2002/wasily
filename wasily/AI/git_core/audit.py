"""审计层：结构化迭代记录，带 hash chain 与脱敏。

为什么写到仓库外：审计包含本机路径、验证输出等运行状态，
公开仓库不应该收录；但迭代轨迹又必须可查，所以放用户数据目录。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from typing import Any

_SECRET_KEY = re.compile(r"(token|password|passwd|secret|authorization|cookie|api[_-]?key)", re.I)
_SECRET_VAL = [
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{10,}"),
    re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+"),
]


def audit_dir() -> str:
    env = os.environ.get("WASILY_AUDIT_DIR")
    if env:
        return env
    base = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "Wasily", "audit")


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            out[k] = "***" if _SECRET_KEY.search(str(k)) else _redact(v)
        return out
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, str):
        text = value
        for pat in _SECRET_VAL:
            text = pat.sub("***", text)
        return text[:4000]
    return value


def _last_hash(path: str) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 8192))
            tail = f.read().decode("utf-8", "replace")
        for line in reversed(tail.splitlines()):
            line = line.strip()
            if line:
                return json.loads(line).get("event_hash", "")
    except (OSError, ValueError):
        pass
    return ""


def append_event(repo_id: str, iteration_id: str, event: dict[str, Any]) -> dict[str, Any]:
    folder = audit_dir()
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, (repo_id or "repo") + ".jsonl")
    body = {
        "schema": 1,
        "event_id": uuid.uuid4().hex,
        "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "repo_id": repo_id,
        "iteration_id": iteration_id,
    }
    body.update(_redact(event or {}))
    body["prev_event_hash"] = _last_hash(path)
    payload = json.dumps(body, ensure_ascii=False, sort_keys=True)
    body["event_hash"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(body, ensure_ascii=False) + "\n")
    return body


def read_events(repo_id: str, limit: int = 50) -> list[dict[str, Any]]:
    path = os.path.join(audit_dir(), (repo_id or "repo") + ".jsonl")
    out: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    except (OSError, ValueError):
        return []
    return out[-max(1, limit):]
