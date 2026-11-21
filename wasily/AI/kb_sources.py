# -*- coding: utf-8 -*-
"""统一存储适配器（与 kb_store 完全解耦）。

职责只有四件事：列目录、读字节、下载到本地临时文件、连通性自检。
检索与摘要仍由 kb_search / kb_digest 负责，适配器不写任何索引。

支持：local / wsl / ssh / webdav / s3。配置存放在 config.json 的 sources 数组：
    {"id": "office-nas", "type": "webdav", "name": "团队云盘",
     "options": {"base_url": "https://dav.example.com/", "username": "...",
                 "password": "...", "root": "/"}}
凭据由用户自行填写，代码不做任何硬编码。
"""
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

import config as cfg

MAX_BYTES = 32 * 1024 * 1024


class SourceError(Exception):
    pass


# ---------------- 基类 ----------------
class BaseSource:
    type = "base"

    def __init__(self, spec):
        self.spec = spec or {}
        self.id = str(self.spec.get("id") or "")
        self.name = str(self.spec.get("name") or self.id)
        self.options = dict(self.spec.get("options") or {})

    def test(self):
        raise NotImplementedError

    def list(self, path=""):
        raise NotImplementedError

    def stat(self, path):
        raise NotImplementedError

    def read_bytes(self, path, max_bytes=MAX_BYTES):
        raise NotImplementedError

    def materialize(self, path):
        """返回本地可读路径与清理函数；本地/UNC 直接返回原路径。"""
        return path, (lambda: None)

    def describe(self):
        return {"id": self.id, "name": self.name, "type": self.type,
                "enabled": bool(self.spec.get("enabled", True))}


# ---------------- 本地 ----------------
class LocalSource(BaseSource):
    type = "local"

    def _roots(self):
        roots = self.options.get("roots") or []
        if isinstance(roots, str):
            roots = [roots]
        return [os.path.abspath(os.path.expanduser(r)) for r in roots if r]

    def _check(self, path):
        full = os.path.abspath(os.path.expanduser(path or "."))
        roots = self._roots()
        if roots and not any(full == r or full.startswith(r + os.sep) for r in roots):
            raise SourceError("路径不在该存储允许的根目录内：%s" % full)
        return full

    def test(self):
        roots = self._roots()
        if not roots:
            return {"ok": False, "message": "未配置 roots"}
        bad = [r for r in roots if not os.path.isdir(r)]
        if bad:
            return {"ok": False, "message": "目录不存在：%s" % "、".join(bad)}
        return {"ok": True, "message": "可访问 %d 个根目录" % len(roots)}

    def list(self, path=""):
        bases = [self._check(path)] if path else self._roots()
        entries = []
        for base in bases:
            try:
                with os.scandir(base) as it:
                    for e in it:
                        try:
                            st = e.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        entries.append({
                            "name": e.name, "path": e.path,
                            "is_dir": e.is_dir(follow_symlinks=False),
                            "size": int(st.st_size), "mtime": float(st.st_mtime),
                        })
            except (PermissionError, OSError) as exc:
                raise SourceError(str(exc))
        entries.sort(key=lambda x: (not x["is_dir"], -x["mtime"]))
        return {"ok": True, "path": path, "entries": entries[:2000]}

    def stat(self, path):
        full = self._check(path)
        st = os.stat(full)
        return {"ok": True, "path": full, "is_dir": os.path.isdir(full),
                "size": int(st.st_size), "mtime": float(st.st_mtime)}

    def read_bytes(self, path, max_bytes=MAX_BYTES):
        full = self._check(path)
        if not os.path.isfile(full):
            raise SourceError("文件不存在：%s" % full)
        with open(full, "rb") as f:
            return f.read(max_bytes)

    def search(self, query, path="", limit=20, kinds=None):
        import kb_files
        roots = [self._check(path)] if path else self._roots()
        return kb_files.search_names(query, kinds=kinds, limit=limit,
                                     roots=roots)


# ---------------- WSL ----------------
class WslSource(BaseSource):
    type = "wsl"

    def _unc_root(self):
        distro = str(self.options.get("distro") or "").strip()
        if not distro:
            return ""
        for prefix in ("\\\\wsl.localhost\\", "\\\\wsl$\\"):
            root = prefix + distro + "\\"
            if os.path.isdir(root):
                return root
        return ""

    def _local(self, path):
        root = self._unc_root()
        if not root:
            raise SourceError("WSL 发行版不可用或未配置 distro")
        rel = str(path or "/").replace("/", "\\").lstrip("\\")
        full = os.path.join(root, rel)
        if not os.path.abspath(full).startswith(os.path.abspath(root)):
            raise SourceError("路径越界")
        return full

    def test(self):
        try:
            root = self._unc_root()
        except SourceError as exc:
            return {"ok": False, "message": str(exc)}
        if not root:
            distros = _wsl_distros()
            hint = ("可用发行版：%s" % "、".join(distros)) if distros else "未检测到 WSL 发行版"
            return {"ok": False, "message": hint}
        return {"ok": True, "message": "已挂载 %s" % root}

    def list(self, path=""):
        full = self._local(path)
        out = []
        with os.scandir(full) as it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                out.append({"name": e.name, "path": str(e.path), "is_dir":
                            e.is_dir(follow_symlinks=False), "size":
                            int(st.st_size), "mtime": float(st.st_mtime)})
        out.sort(key=lambda x: (not x["is_dir"], -x["mtime"]))
        return {"ok": True, "path": path, "entries": out[:2000]}

    def stat(self, path):
        full = self._local(path)
        st = os.stat(full)
        return {"ok": True, "path": full, "is_dir": os.path.isdir(full),
                "size": int(st.st_size), "mtime": float(st.st_mtime)}

    def read_bytes(self, path, max_bytes=MAX_BYTES):
        full = self._local(path)
        with open(full, "rb") as f:
            return f.read(max_bytes)


def _wsl_distros():
    try:
        out = subprocess.run(["wsl.exe", "-l", "-q"], capture_output=True,
                             timeout=8).stdout.decode("utf-16-le", "ignore")
        return [x.strip() for x in out.replace("\x00", "").splitlines() if x.strip()]
    except Exception:  # noqa: BLE001
        return []


# ---------------- SSH ----------------
class SshSource(BaseSource):
    """不引入 paramiko：直接复用系统 OpenSSH 客户端，要求密钥登录。"""
    type = "ssh"

    def _target(self):
        user = str(self.options.get("user") or "").strip()
        host = str(self.options.get("host") or "").strip()
        if not host:
            raise SourceError("未配置 host")
        return ("%s@%s" % (user, host)) if user else host

    def _ssh(self, remote_cmd, timeout=20):
        args = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ConnectTimeout=8"]
        port = self.options.get("port")
        if port:
            args += ["-p", str(port)]
        key = self.options.get("key")
        if key:
            args += ["-i", os.path.expanduser(str(key))]
        args += [self._target(), remote_cmd]
        return subprocess.run(args, capture_output=True, timeout=timeout)

    def _remote_root(self):
        root = str(self.options.get("root") or "").strip()
        return root.rstrip("/") if root else ""

    def _full(self, path):
        root = self._remote_root()
        p = str(path or "")
        if root:
            if not p.startswith("/"):
                p = root + "/" + p.lstrip("/")
            elif p != root and not p.startswith(root + "/"):
                raise SourceError("路径不在该主机允许的根目录内")
        if not p:
            p = "/"
        return p

    def test(self):
        try:
            r = self._ssh("echo ok", timeout=15)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc)}
        if r.returncode == 0 and b"ok" in r.stdout:
            return {"ok": True, "message": "SSH 连接正常（密钥登录）"}
        msg = r.stderr.decode("utf-8", "ignore").strip()[:300]
        return {"ok": False, "message": msg or "SSH 认证失败，请配置密钥登录"}

    def list(self, path=""):
        target = self._full(path)
        cmd = ("find %s -maxdepth 1 -mindepth 1 -printf '%%y|%%s|%%T@|%%f\\n' "
               "2>/dev/null" % _shq(target))
        r = self._ssh(cmd)
        if r.returncode != 0:
            raise SourceError(r.stderr.decode("utf-8", "ignore")[:300] or "列目录失败")
        out = []
        for line in r.stdout.decode("utf-8", "ignore").splitlines():
            parts = line.split("|", 3)
            if len(parts) != 4:
                continue
            typ, size, mtime, name = parts
            out.append({"name": name, "path": (target.rstrip("/") + "/" + name),
                        "is_dir": typ == "d", "size": int(size or 0),
                        "mtime": float(mtime or 0)})
        out.sort(key=lambda x: (not x["is_dir"], -x["mtime"]))
        return {"ok": True, "path": target, "entries": out[:2000]}

    def stat(self, path):
        target = self._full(path)
        cmd = "stat -c '%%F|%%s|%%Y' %s 2>/dev/null" % _shq(target)
        r = self._ssh(cmd)
        if r.returncode != 0:
            raise SourceError("文件不存在或不可访问")
        parts = r.stdout.decode("utf-8", "ignore").strip().split("|")
        if len(parts) != 3:
            raise SourceError("无法解析 stat 结果")
        return {"ok": True, "path": target, "is_dir": "directory" in parts[0],
                "size": int(parts[1] or 0), "mtime": float(parts[2] or 0)}

    def read_bytes(self, path, max_bytes=MAX_BYTES):
        target = self._full(path)
        r = self._ssh("cat -- %s" % _shq(target), timeout=60)
        if r.returncode != 0:
            raise SourceError(r.stderr.decode("utf-8", "ignore")[:300] or "读取失败")
        return r.stdout[:max_bytes]

    def materialize(self, path):
        target = self._full(path)
        suffix = os.path.splitext(target)[1]
        fd, tmp = tempfile.mkstemp(prefix="wasily-src-", suffix=suffix)
        os.close(fd)
        args = ["scp", "-q", "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=accept-new"]
        port = self.options.get("port")
        if port:
            args += ["-P", str(port)]
        key = self.options.get("key")
        if key:
            args += ["-i", os.path.expanduser(str(key))]
        args += ["%s:%s" % (self._target(), target), tmp]
        r = subprocess.run(args, capture_output=True, timeout=120)
        if r.returncode != 0:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise SourceError(r.stderr.decode("utf-8", "ignore")[:300] or "下载失败")
        return tmp, (lambda: os.path.exists(tmp) and os.remove(tmp))

    def search(self, query, path="", limit=20, kinds=None):
        root = self._full(path)
        cmd = ("find %s -maxdepth 6 -mindepth 1 -iname %s -printf '%%y|%%s|%%T@|%%p\\n' "
               "2>/dev/null | head -n %d" % (_shq(root), _shq("*%s*" % query),
                                             max(1, int(limit))))
        r = self._ssh(cmd, timeout=30)
        out = []
        for line in r.stdout.decode("utf-8", "ignore").splitlines():
            parts = line.split("|", 3)
            if len(parts) != 4:
                continue
            typ, size, mtime, full = parts
            out.append({"name": os.path.basename(full), "path": full,
                        "is_dir": typ == "d", "size": int(size or 0),
                        "mtime": float(mtime or 0)})
        return {"ok": True, "query": query, "results": out, "scanned": len(out)}


def _shq(text):
    return "'" + str(text).replace("'", "'\"'\"'") + "'"


# ---------------- WebDAV ----------------
class WebdavSource(BaseSource):
    type = "webdav"

    def _base(self):
        base = str(self.options.get("base_url") or "").strip()
        if not base:
            raise SourceError("未配置 base_url")
        return base if base.endswith("/") else base + "/"

    def _url(self, path):
        root = str(self.options.get("root") or "").strip().strip("/")
        rel = str(path or "").strip("/")
        parts = [p for p in (root, rel) if p]
        return self._base() + "/".join(urllib.parse.quote(p) for p in parts)

    def _req(self, method, path, data=None, headers=None, timeout=20):
        req = urllib.request.Request(self._url(path), data=data, method=method)
        req.add_header("User-Agent", "Wasily-KB")
        user = self.options.get("username")
        if user:
            import base64
            token = base64.b64encode(("%s:%s" % (user, self.options.get("password") or ""))
                                     .encode("utf-8")).decode()
            req.add_header("Authorization", "Basic " + token)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        return urllib.request.urlopen(req, timeout=timeout)

    def test(self):
        try:
            with self._req("PROPFIND", "", data=b"", headers={"Depth": "0",
                                                              "Content-Type": "application/xml"}):
                return {"ok": True, "message": "WebDAV 可访问"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc)}

    def list(self, path=""):
        body = (b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:">'
                b'<d:prop><d:resourcetype/><d:getcontentlength/>'
                b'<d:getlastmodified/></d:prop></d:propfind>')
        with self._req("PROPFIND", path, data=body,
                       headers={"Depth": "1", "Content-Type": "application/xml"}) as resp:
            raw = resp.read()
        ns = {"d": "DAV:"}
        root = ET.fromstring(raw)
        base_path = urllib.parse.urlparse(self._url(path)).path.rstrip("/")
        entries = []
        for item in root.findall("d:response", ns):
            href = (item.findtext("d:href", default="", namespaces=ns) or "")
            item_path = urllib.parse.unquote(urllib.parse.urlparse(href).path).rstrip("/")
            if item_path == base_path:
                continue
            name = os.path.basename(item_path)
            prop = item.find("d:propstat/d:prop", ns)
            is_dir = prop is not None and prop.find("d:resourcetype/d:collection", ns) is not None
            size = int((prop.findtext("d:getcontentlength", default="0", namespaces=ns)
                        if prop is not None else "0") or 0)
            entries.append({"name": name, "path": item_path, "is_dir": is_dir,
                            "size": size, "mtime": 0})
        return {"ok": True, "path": path, "entries": entries[:2000]}

    def stat(self, path):
        with self._req("PROPFIND", path, data=b"",
                       headers={"Depth": "0", "Content-Type": "application/xml"}) as resp:
            raw = resp.read()
        ns = {"d": "DAV:"}
        root = ET.fromstring(raw)
        prop = root.find("d:response/d:propstat/d:prop", ns)
        is_dir = prop is not None and prop.find("d:resourcetype/d:collection", ns) is not None
        size = int((prop.findtext("d:getcontentlength", default="0", namespaces=ns)
                    if prop is not None else "0") or 0)
        return {"ok": True, "path": path, "is_dir": is_dir, "size": size, "mtime": 0}

    def read_bytes(self, path, max_bytes=MAX_BYTES):
        with self._req("GET", path, timeout=60) as resp:
            return resp.read(max_bytes)

    def materialize(self, path):
        suffix = os.path.splitext(str(path))[1]
        fd, tmp = tempfile.mkstemp(prefix="wasily-dav-", suffix=suffix)
        with os.fdopen(fd, "wb") as f:
            with self._req("GET", path, timeout=120) as resp:
                shutil.copyfileobj(resp, f)
        return tmp, (lambda: os.path.exists(tmp) and os.remove(tmp))

    def search(self, query, path="", limit=20, kinds=None):
        deadline = time.time() + 6
        queue, hits = [path or ""], []
        q = str(query or "").lower()
        while queue and time.time() < deadline and len(hits) < limit:
            cur = queue.pop(0)
            try:
                listing = self.list(cur)
            except Exception:  # noqa: BLE001
                continue
            for e in listing["entries"]:
                if e["is_dir"]:
                    if queue.__len__() < 200:
                        queue.append(e["path"])
                elif q in e["name"].lower():
                    hits.append(e)
        return {"ok": True, "query": query, "results": hits[:limit],
                "scanned": len(hits), "partial": bool(queue)}


# ---------------- S3 ----------------
class S3Source(BaseSource):
    type = "s3"

    def _client(self):
        try:
            import boto3
        except ImportError as exc:
            raise SourceError("缺少 boto3：%s" % exc)
        o = self.options
        return boto3.client(
            "s3", endpoint_url=o.get("endpoint_url") or None,
            aws_access_key_id=o.get("access_key") or None,
            aws_secret_access_key=o.get("secret_key") or None,
            region_name=o.get("region") or None)

    def _bucket(self):
        bucket = str(self.options.get("bucket") or "")
        if not bucket:
            raise SourceError("未配置 bucket")
        return bucket

    def _prefix(self, path):
        root = str(self.options.get("prefix") or "").strip("/")
        rel = str(path or "").strip("/")
        return "/".join(p for p in (root, rel) if p)

    def test(self):
        try:
            self._client().list_objects_v2(Bucket=self._bucket(), MaxKeys=1)
            return {"ok": True, "message": "S3 可访问"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc)}

    def list(self, path=""):
        prefix = self._prefix(path)
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        resp = self._client().list_objects_v2(Bucket=self._bucket(), Prefix=prefix,
                                              Delimiter="/", MaxKeys=1000)
        out = []
        for p in resp.get("CommonPrefixes") or []:
            name = p["Prefix"][len(prefix):].strip("/")
            out.append({"name": name, "path": p["Prefix"], "is_dir": True,
                        "size": 0, "mtime": 0})
        for o in resp.get("Contents") or []:
            if o["Key"] == prefix:
                continue
            out.append({"name": o["Key"][len(prefix):],
                        "path": o["Key"], "is_dir": False,
                        "size": int(o.get("Size") or 0),
                        "mtime": o["LastModified"].timestamp()})
        return {"ok": True, "path": prefix, "entries": out[:2000]}

    def stat(self, path):
        key = self._prefix(path)
        obj = self._client().head_object(Bucket=self._bucket(), Key=key)
        return {"ok": True, "path": key, "is_dir": False,
                "size": int(obj.get("ContentLength") or 0),
                "mtime": obj["LastModified"].timestamp()}

    def read_bytes(self, path, max_bytes=MAX_BYTES):
        obj = self._client().get_object(Bucket=self._bucket(), Key=self._prefix(path))
        return obj["Body"].read(max_bytes)

    def materialize(self, path):
        key = self._prefix(path)
        suffix = os.path.splitext(key)[1]
        fd, tmp = tempfile.mkstemp(prefix="wasily-s3-", suffix=suffix)
        with os.fdopen(fd, "wb") as f:
            self._client().download_fileobj(self._bucket(), key, f)
        return tmp, (lambda: os.path.exists(tmp) and os.remove(tmp))

    def search(self, query, path="", limit=20, kinds=None):
        prefix = self._prefix(path)
        q = str(query or "").lower()
        out = []
        token = None
        while len(out) < limit:
            kwargs = {"Bucket": self._bucket(), "Prefix": prefix, "MaxKeys": 500}
            if token:
                kwargs["ContinuationToken"] = token
            resp = self._client().list_objects_v2(**kwargs)
            for o in resp.get("Contents") or []:
                if q in o["Key"].lower():
                    out.append({"name": os.path.basename(o["Key"]), "path": o["Key"],
                                "is_dir": False, "size": int(o.get("Size") or 0),
                                "mtime": o["LastModified"].timestamp()})
                    if len(out) >= limit:
                        break
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
        return {"ok": True, "query": query, "results": out, "scanned": len(out)}


DRIVERS = {
    "local": LocalSource, "wsl": WslSource, "ssh": SshSource,
    "webdav": WebdavSource, "s3": S3Source,
}


# ---------------- 配置与注册表 ----------------
def _specs():
    rows = cfg.get("sources", []) or []
    return [r for r in rows if isinstance(r, dict) and r.get("id")]


def _save(rows):
    cfg.set_value("sources", rows)
    cfg.save()


def list_sources(include_status=False):
    out = []
    for spec in _specs():
        cls = DRIVERS.get(str(spec.get("type") or "").lower())
        if not cls:
            out.append({"id": spec.get("id"), "type": spec.get("type"),
                        "ok": False, "message": "未知类型"})
            continue
        src = cls(spec)
        item = src.describe()
        if include_status:
            try:
                item["status"] = src.test()
            except Exception as exc:  # noqa: BLE001
                item["status"] = {"ok": False, "message": str(exc)}
        out.append(item)
    return {"ok": True, "sources": out, "types": sorted(DRIVERS)}


def get_source(source_id):
    for spec in _specs():
        if str(spec.get("id")) == str(source_id):
            cls = DRIVERS.get(str(spec.get("type") or "").lower())
            if not cls:
                raise SourceError("未知存储类型：%s" % spec.get("type"))
            return cls(spec)
    raise SourceError("未找到存储：%s" % source_id)


def add_source(spec):
    if not isinstance(spec, dict) or not spec.get("id"):
        raise SourceError("缺少 id")
    if str(spec.get("type") or "").lower() not in DRIVERS:
        raise SourceError("type 仅支持：%s" % "、".join(sorted(DRIVERS)))
    rows = [r for r in _specs() if str(r.get("id")) != str(spec["id"])]
    rows.append(spec)
    _save(rows)
    return {"ok": True, "source": spec}


def remove_source(source_id):
    rows = [r for r in _specs() if str(r.get("id")) != str(source_id)]
    _save(rows)
    return {"ok": True, "removed": source_id}


def source_list(source_id, path=""):
    return get_source(source_id).list(path)


def source_stat(source_id, path):
    return get_source(source_id).stat(path)


def source_read(source_id, path, max_bytes=MAX_BYTES):
    src = get_source(source_id)
    data = src.read_bytes(path, max_bytes)
    return {"ok": True, "path": path, "bytes": len(data),
            "text": decode_bytes(data)}


def decode_bytes(data):
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "ignore")


def source_search(source_id, query, path="", limit=20, kinds=None):
    return get_source(source_id).search(query, path=path, limit=limit, kinds=kinds)


def source_summarize(source_id, path, query="", mode="extractive", max_points=6):
    """远程文件先落地到临时目录，再复用 kb_digest；结束立即清理。"""
    src = get_source(source_id)
    local, cleanup = src.materialize(path)
    try:
        import kb_digest
        res = kb_digest.summarize(path=local, query=query, mode=mode,
                                  max_points=max_points)
        res["source_id"] = source_id
        res["remote_path"] = path
        res["name"] = os.path.basename(str(path))
        return res
    finally:
        try:
            cleanup()
        except Exception:  # noqa: BLE001
            pass


def source_report(source_id, paths, topic, query="", mode="extractive",
                  fmt="markdown", save=True):
    tmp, cleanups = [], []
    try:
        for p in paths:
            local, cleanup = get_source(source_id).materialize(p)
            tmp.append(local)
            cleanups.append(cleanup)
        import kb_digest
        return kb_digest.build_report(topic, paths=tmp, query=query or topic,
                                      mode=mode, fmt=fmt, save=save)
    finally:
        for fn in cleanups:
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass
