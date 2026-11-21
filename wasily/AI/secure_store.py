"""
加密安全存储（Fernet + Windows DPAPI 护钥）
  - 密钥生成后经 CryptProtectData（当前Windows用户绑定）加密存 key.bin，
    换机/换用户/直接拷走文件都无法解密
  - EncLines：按行独立加密的 JSONL 存储，支持追加/全量读取/条件删除(原子重写)
  - 首次使用自动把旧明文 .jsonl 迁移为 .enc，原文件改名 .bak 保留
"""
import ctypes
import json
import os
import threading

from cryptography.fernet import Fernet, InvalidToken

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KEY_PATH = os.path.join(BASE_DIR, "key.bin")
_key_lock = threading.Lock()
_fernet = None


# ---------------- DPAPI 护钥 ----------------
# 注意：pbData 必须声明 c_void_p 并显式声明 argtypes/restype，
# 旧实现把 .raw 临时缓冲直接塞给 c_char_p，指针悬空导致 CryptProtectData
# 写越界 → 整个进程堆损坏崩溃（0xC0000374），密钥一直没能落盘
class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint), ("pbData", ctypes.c_void_p)]


_crypt32 = ctypes.windll.crypt32
_kernel32 = ctypes.windll.kernel32
for _fn in (_crypt32.CryptProtectData, _crypt32.CryptUnprotectData):
    _fn.argtypes = [ctypes.POINTER(_DATA_BLOB), ctypes.c_wchar_p,
                    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                    ctypes.c_uint, ctypes.POINTER(_DATA_BLOB)]
    _fn.restype = ctypes.c_bool
_kernel32.LocalFree.argtypes = [ctypes.c_void_p]
_kernel32.LocalFree.restype = ctypes.c_void_p


def _dpapi_call(fn, data: bytes, err_msg: str) -> bytes:
    if not data:
        raise ValueError("DPAPI 输入为空")
    buf = ctypes.create_string_buffer(data, len(data))   # 保持在作用域内，防指针悬空
    bin_ = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.c_void_p))
    bout = _DATA_BLOB()
    if not fn(ctypes.byref(bin_), None, None, None, None, 0, ctypes.byref(bout)):
        raise OSError(err_msg)
    out = ctypes.string_at(bout.pbData, bout.cbData)
    _kernel32.LocalFree(bout.pbData)
    return out


def _dpapi_protect(data: bytes) -> bytes:
    return _dpapi_call(_crypt32.CryptProtectData, data, "CryptProtectData 失败")


def _dpapi_unprotect(data: bytes) -> bytes:
    return _dpapi_call(_crypt32.CryptUnprotectData, data,
                       "CryptUnprotectData 失败（密钥文件不属于当前用户？）")


def get_fernet() -> Fernet:
    """单例 Fernet；密钥经 DPAPI 保护存储"""
    global _fernet
    with _key_lock:
        if _fernet is not None:
            return _fernet
        if os.path.exists(KEY_PATH):
            try:
                with open(KEY_PATH, "rb") as f:
                    raw = _dpapi_unprotect(f.read())
                _fernet = Fernet(raw)
            except Exception:
                # 密钥文件为空/损坏（如 0 字节）：重新生成并覆盖。
                # 旧记录此前已无法解密，重新生成不会造成额外损失
                raw = Fernet.generate_key()
                with open(KEY_PATH, "wb") as f:
                    f.write(_dpapi_protect(raw))
                _fernet = Fernet(raw)
        else:
            raw = Fernet.generate_key()
            with open(KEY_PATH, "wb") as f:
                f.write(_dpapi_protect(raw))
            _fernet = Fernet(raw)
        return _fernet


# ---------------- 按行加密 JSONL ----------------
class EncLines:
    """每行独立 token 的加密 JSONL：{id,...} 记录集"""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()

    # -- 基础读写 --
    def append(self, rec: dict):
        token = get_fernet().encrypt(
            json.dumps(rec, ensure_ascii=False).encode("utf-8")).decode()
        with self.lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(token + "\n")

    def load_all(self):
        if not os.path.exists(self.path):
            return []
        f = get_fernet()
        out = []
        with open(self.path, "r", encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(f.decrypt(line.encode()).decode("utf-8")))
                except (InvalidToken, Exception):
                    continue  # 篡改/损坏行跳过
        return out

    def rewrite(self, recs):
        """原子重写全部记录"""
        f = get_fernet()
        tmp = self.path + ".tmp"
        with self.lock:
            with open(tmp, "w", encoding="utf-8") as fp:
                for r in recs:
                    fp.write(f.encrypt(
                        json.dumps(r, ensure_ascii=False).encode("utf-8")).decode() + "\n")
            os.replace(tmp, self.path)

    # -- 常用操作 --
    def delete_ids(self, ids):
        """按 id 列表删除，返回删除数。ids=None+all=True 清空"""
        with self.lock:
            recs = self._load_unlocked()
        if ids is None:
            kept = []
        else:
            ids = set(ids)
            kept = [r for r in recs if r.get("id") not in ids]
        removed = len(recs) - len(kept)
        if removed:
            self.rewrite(kept)
        return removed

    def delete_where(self, pred):
        """按谓词删除，返回删除数"""
        with self.lock:
            recs = self._load_unlocked()
        kept = [r for r in recs if not pred(r)]
        removed = len(recs) - len(kept)
        if removed:
            self.rewrite(kept)
        return removed

    def _load_unlocked(self):
        # 内部：已在锁内时解密全量
        if not os.path.exists(self.path):
            return []
        f = get_fernet()
        out = []
        with open(self.path, "r", encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(f.decrypt(line.encode()).decode("utf-8")))
                    except Exception:
                        pass
        return out

    def next_id(self, recs=None):
        recs = recs if recs is not None else self.load_all()
        return max((r.get("id", 0) for r in recs), default=0) + 1


def migrate_plaintext(jsonl_path: str, enc: EncLines):
    """把旧明文 jsonl 迁移到加密存储；成功后原文件改名 .bak"""
    if not os.path.exists(jsonl_path):
        return 0
    if os.path.exists(enc.path):        # 已迁移过
        os.replace(jsonl_path, jsonl_path + ".bak")
        return 0
    n = 0
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                enc.append(json.loads(line))
                n += 1
            except Exception:
                pass
    os.replace(jsonl_path, jsonl_path + ".bak")
    if n:
        print(f"[secure] 已加密迁移 {n} 条记录: {os.path.basename(jsonl_path)} → "
              f"{os.path.basename(enc.path)}", flush=True)
    return n


# ---------------- 全局实例（历史 / 审计 / 记忆） ----------------
HISTORY_ENC = EncLines(os.path.join(BASE_DIR, "history.enc"))
AUDIT_ENC = EncLines(os.path.join(BASE_DIR, "audit.enc"))
MEMORY_ENC = EncLines(os.path.join(BASE_DIR, "learnings.enc"))


def init_all_migrations():
    migrate_plaintext(os.path.join(BASE_DIR, "history.jsonl"), HISTORY_ENC)
    migrate_plaintext(os.path.join(BASE_DIR, "self_train.jsonl"), HISTORY_ENC)
    migrate_plaintext(os.path.join(BASE_DIR, "learnings.jsonl"), MEMORY_ENC)
