"""自动更新服务。

更新源优先级：update.manifest_url（http 或本地路径）→ 安装目录 updates/latest.json。
没有清单或版本不高于当前版本时，统一返回「当前没有可用的更新」，与 Trae 的表现一致。

清单格式（updates/latest.json）：
    {
      "version": "0.3.1",
      "published": "2026-11-20",
      "channel": "stable",
      "notes": "修复若干问题",
      "min_version": "0.2.0",
      "package": {"url": "http://192.168.1.10/wasily-0.3.1.zip",
                  "sha256": "……", "size": 12345678}
    }
package.url 支持 http(s) 与本地绝对路径；download 只下载到用户数据目录，
apply 只解压到 staging 并写入 pending 标记，真正替换由重启器在退出后执行。
"""
import hashlib
import json
import os
import re
import time
import urllib.request
import zipfile

import config as cfg

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VERSION_FILE = os.path.join(BASE_DIR, "version.json")
FALLBACK_VERSION = "0.3.0"

_CACHE = {"ts": 0, "data": None}
_STATE = {"busy": False, "phase": "idle", "message": "", "progress": 0}


def _data_dir():
    appdata = os.environ.get("APPDATA")
    base = os.path.join(appdata, "Wasily") if appdata else os.path.join(
        os.path.expanduser("~"), ".wasily")
    return base


def app_dir():
    path = os.path.join(_data_dir(), "updates")
    os.makedirs(path, exist_ok=True)
    return path


def current():
    """读取当前版本信息；version.json 由构建流程写入。"""
    if os.path.isfile(VERSION_FILE):
        try:
            with open(VERSION_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            data.setdefault("version", FALLBACK_VERSION)
            return data
        except (OSError, ValueError):
            pass
    return {"product": "Wasily", "version": FALLBACK_VERSION,
            "channel": str(cfg.get("update.channel", "stable") or "stable"),
            "build": ""}


def _ver_tuple(text):
    nums = re.findall(r"\d+", str(text or ""))
    return tuple(int(n) for n in nums[:4]) or (0,)


def _manifest_location():
    raw = str(cfg.get("update.manifest_url", "") or "").strip()
    if raw:
        return raw
    return os.path.join(BASE_DIR, "updates", "latest.json")


def _read_manifest():
    loc = _manifest_location()
    if re.match(r"^https?://", loc, re.I):
        req = urllib.request.Request(loc, headers={"User-Agent": "Wasily-Updater"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    if os.path.isfile(loc):
        with open(loc, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def check(force=False):
    """检查更新；结果缓存 30 秒，避免前端轮询反复读盘。"""
    now = time.time()
    if not force and _CACHE["data"] and now - _CACHE["ts"] < 30:
        return _CACHE["data"]
    cur = current()
    cur_ver = str(cur.get("version") or FALLBACK_VERSION)
    result = {
        "ok": True,
        "current": cur_ver,
        "latest": cur_ver,
        "has_update": False,
        "message": "当前没有可用的更新",
        "channel": str(cfg.get("update.channel", "stable") or "stable"),
        "checked_at": now,
    }
    try:
        manifest = _read_manifest()
    except Exception as exc:  # noqa: BLE001 —— 网络/格式错误都要回到「无更新」而不是崩
        result.update(ok=False, message="检查更新失败：%s" % exc)
        manifest = None
    if manifest:
        latest = str(manifest.get("version") or "")
        result["latest"] = latest or cur_ver
        if latest and _ver_tuple(latest) > _ver_tuple(cur_ver):
            pkg = manifest.get("package") or {}
            result.update(
                has_update=True,
                message="发现新版本 %s" % latest,
                notes=manifest.get("notes") or "",
                published=manifest.get("published") or "",
                min_version=manifest.get("min_version") or "",
                package={"url": pkg.get("url") or "", "sha256": pkg.get("sha256") or "",
                         "size": pkg.get("size") or 0},
            )
        else:
            result["message"] = "当前没有可用的更新"
    cfg.set_value("update.last_check", int(now))
    cfg.set_value("update.last_version", result.get("latest") or cur_ver)
    cfg.save()
    _CACHE.update(ts=now, data=result)
    return result


def status():
    cur = current()
    pending = os.path.join(app_dir(), "pending.json")
    pending_info = None
    if os.path.isfile(pending):
        try:
            with open(pending, "r", encoding="utf-8") as f:
                pending_info = json.load(f)
        except (OSError, ValueError):
            pending_info = None
    return {"current": cur, "last_check": cfg.get("update.last_check", 0),
            "channel": cfg.get("update.channel", "stable"),
            "busy": _STATE["busy"], "phase": _STATE["phase"],
            "message": _STATE["message"], "progress": _STATE["progress"],
            "pending": pending_info}


def _download_to(url, target):
    if re.match(r"^https?://", url, re.I):
        req = urllib.request.Request(url, headers={"User-Agent": "Wasily-Updater"})
        with urllib.request.urlopen(req, timeout=60) as resp, \
                open(target, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if total:
                    _STATE.update(progress=min(99, int(done * 100 / total)),
                                  message="下载中 %d/%d" % (done, total))
    else:
        src = os.path.abspath(os.path.expanduser(url))
        if not os.path.isfile(src):
            raise ValueError("安装包不存在：%s" % src)
        with open(src, "rb") as i, open(target, "wb") as o:
            while True:
                chunk = i.read(65536)
                if not chunk:
                    break
                o.write(chunk)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(package):
    """下载并校验安装包，返回落盘路径。"""
    if not isinstance(package, dict) or not package.get("url"):
        raise ValueError("缺少安装包地址")
    url = str(package["url"])
    name = os.path.basename(url.split("?")[0]) or "wasily-update.zip"
    target = os.path.join(app_dir(), name)
    _STATE.update(busy=True, phase="download", message="准备下载", progress=0)
    try:
        _download_to(url, target)
        want = str(package.get("sha256") or "").strip().lower()
        if want:
            _STATE.update(phase="verify", message="校验安装包", progress=99)
            got = _sha256(target).lower()
            if got != want:
                os.remove(target)
                raise ValueError("安装包校验失败（sha256 不一致）")
        _STATE.update(busy=False, phase="done", message="下载完成", progress=100)
        _CACHE.update(ts=0, data=None)
        return {"path": target, "size": os.path.getsize(target),
                "sha256": _sha256(target)}
    except Exception:
        _STATE.update(busy=False, phase="error", message="下载失败")
        raise


def apply(package_path, version=""):
    """解压到 staging 并写 pending 标记；不直接覆盖正在运行的文件。"""
    if not package_path or not os.path.isfile(package_path):
        raise ValueError("安装包不存在")
    ver = version or str(check().get("latest") or "next")
    stage = os.path.join(app_dir(), "staging", re.sub(r"[^\w.\-]", "_", ver))
    os.makedirs(stage, exist_ok=True)
    if zipfile.is_zipfile(package_path):
        with zipfile.ZipFile(package_path) as zf:
            for member in zf.namelist():
                target = os.path.realpath(os.path.join(stage, member))
                if not target.startswith(os.path.realpath(stage) + os.sep):
                    raise ValueError("安装包包含非法路径：%s" % member)
            zf.extractall(stage)
    else:
        raise ValueError("仅支持 zip 安装包")
    pending = {"version": ver, "stage": stage, "package": package_path,
               "created": time.time(), "requires_restart": True}
    with open(os.path.join(app_dir(), "pending.json"), "w", encoding="utf-8") as f:
        json.dump(pending, f, ensure_ascii=False, indent=2)
    return pending
