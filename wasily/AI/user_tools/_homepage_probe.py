# -*- coding: utf-8 -*-
"""系统默认浏览器 / 主页配置探测（只读，不修改任何系统设置）。

数据来源（全部真读，无硬编码网址回退）：
- 默认浏览器 ProgId：
  HKCU\\Software\\Microsoft\\Windows\\Shell\\Associations\\UrlAssociations\\http\\UserChoice
- Chromium 系（Edge / Chrome / Brave / Vivaldi / Opera / 360 等）：
  User Data\\<Profile>\\Preferences 里的 homepage / homepage_is_newtabpage
- Firefox：profiles.ini -> <profile>\\prefs.js 的 browser.startup.homepage

读不到就返回 homepage_kind="none" 并说明原因，绝不编造网址。
"""

import configparser
import json
import os
import re
import subprocess

REG_HTTP = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice"
REG_HTTPS = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice"

# ProgId 前缀 -> (显示名, 家族)
_PROGID_MAP = (
    ("MSEdge", ("Microsoft Edge", "edge")),
    ("ChromeHTML", ("Google Chrome", "chrome")),
    ("BraveHTML", ("Brave", "brave")),
    ("VivaldiHTM", ("Vivaldi", "vivaldi")),
    ("OperaStable", ("Opera", "opera")),
    ("FirefoxURL", ("Mozilla Firefox", "firefox")),
    ("FirefoxHTML", ("Mozilla Firefox", "firefox")),
    ("IE.HTTP", ("Internet Explorer", "ie")),
    ("360SE", ("360 安全浏览器", "360")),
    ("360Chrome", ("360 极速浏览器", "360chrome")),
    ("SogouExplorer", ("搜狗浏览器", "sogou")),
    ("QQBrowser", ("QQ 浏览器", "qqbrowser")),
)

# Chromium 家族的新标签页占位值
_CHROMIUM_NTP = {
    "about:newtab", "about:blank",
    "chrome://newtab", "chrome://newtab/",
    "edge://newtab", "edge://newtab/",
}


def _reg_read(hive, path, name):
    """读注册表值；失败返回 None（不抛异常）"""
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(hive, path) as k:
            val, _ = winreg.QueryValueEx(k, name)
            return val
    except OSError:
        return None


def _reg_hives():
    try:
        import winreg
        return winreg.HKEY_CURRENT_USER, winreg.HKEY_CLASSES_ROOT
    except ImportError:
        return None, None


def read_default_progid():
    """返回 (progid, 注册表路径)；读不到返回 (None, 尝试过的路径)"""
    hkcu, _ = _reg_hives()
    if hkcu is None:
        return None, REG_HTTP
    for path in (REG_HTTP, REG_HTTPS):
        v = _reg_read(hkcu, path, "ProgId")
        if isinstance(v, str) and v.strip():
            return v.strip(), "HKCU\\" + path
    return None, "HKCU\\" + REG_HTTP


def _browser_from_progid(progid):
    up = (progid or "").upper()
    for prefix, (name, family) in _PROGID_MAP:
        if up.startswith(prefix.upper()):
            return name, family
    return (progid or "未知浏览器"), "other"


def _local(*parts):
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, *parts)


def _roaming(*parts):
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, *parts)


def _chromium_user_data(family):
    if family == "edge":
        return _local("Microsoft", "Edge", "User Data")
    if family == "chrome":
        return _local("Google", "Chrome", "User Data")
    if family == "brave":
        return _local("BraveSoftware", "Brave-Browser", "User Data")
    if family == "vivaldi":
        return _local("Vivaldi", "User Data")
    if family == "opera":
        return _roaming("Opera Software", "Opera Stable")
    if family == "360":
        return _roaming("360se6", "User Data")
    if family == "360chrome":
        return _roaming("360Chrome", "Chrome", "User Data")
    if family == "qqbrowser":
        return _local("Tencent", "QQBrowser", "User Data")
    if family == "sogou":
        return _roaming("SogouExplorer", "NewProfile")
    return ""


def _chromium_prefs_path(user_data):
    """定位 Preferences：优先 Local State 的 last_used，其次 Default / Profile N"""
    if not user_data or not os.path.isdir(user_data):
        return ""
    cands = []
    try:
        with open(os.path.join(user_data, "Local State"), encoding="utf-8") as f:
            st = json.load(f)
        last = str(((st.get("profile") or {}).get("last_used") or "")).strip()
        if last:
            cands.append(last)
    except Exception:
        pass
    cands.append("Default")
    try:
        for n in sorted(os.listdir(user_data)):
            if re.fullmatch(r"Profile \d+", n):
                cands.append(n)
    except OSError:
        pass
    for c in cands:
        p = os.path.join(user_data, c, "Preferences")
        if os.path.isfile(p):
            return p
    return ""


def _classify(homepage, is_newtab, has_key, family):
    """返回 (kind, url)；kind ∈ url / newtab / none"""
    hp = str(homepage or "").strip()
    if is_newtab is True:
        return "newtab", ""
    if not hp:
        return "none", ""
    if family == "firefox":
        # Firefox 的 about:home 是真实主页（Firefox 主页），about:newtab/blank 才是新标签页
        if hp in ("about:newtab", "about:blank"):
            return "newtab", ""
        return "url", hp
    if hp.lower() in _CHROMIUM_NTP:
        return "newtab", ""
    if not has_key:
        return "none", ""
    return "url", hp


def _read_chromium_prefs(path, family):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return None, f"读取 Preferences 失败：{e}"
    has_key = "homepage" in data
    kind, url = _classify(data.get("homepage"), data.get("homepage_is_newtabpage"),
                          has_key, family)
    return (kind, url), ""


def _firefox_prefs_path():
    ini = _roaming("Mozilla", "Firefox", "profiles.ini")
    if not os.path.isfile(ini):
        return ""
    cp = configparser.RawConfigParser(strict=False)
    try:
        cp.read(ini, encoding="utf-8")
    except Exception:
        return ""
    fallback = ""
    for sec in cp.sections():
        if not sec.lower().startswith("profile"):
            continue
        path = (cp.get(sec, "Path", fallback="") or "").strip()
        if not path:
            continue
        if cp.get(sec, "IsRelative", fallback="1").strip() == "1":
            full = _roaming("Mozilla", "Firefox", path.replace("/", os.sep))
        else:
            full = path.replace("/", os.sep)
        prefs = os.path.join(full, "prefs.js")
        if not fallback and os.path.isfile(prefs):
            fallback = prefs
        if cp.get(sec, "Default", fallback="0").strip() == "1" and os.path.isfile(prefs):
            return prefs
    return fallback


def _read_firefox_prefs(path):
    hp = None
    page = None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if hp is None:
                    m = re.match(r'\s*user_pref\("browser\.startup\.homepage",\s*"((?:[^"\\]|\\.)*)"\)', line)
                    if m:
                        hp = m.group(1).replace('\\"', '"').replace("\\\\", "\\")
                if page is None:
                    m2 = re.match(r'\s*user_pref\("browser\.startup\.page",\s*(\d+)\)', line)
                    if m2:
                        page = int(m2.group(1))
    except Exception as e:
        return None, f"读取 prefs.js 失败：{e}"
    if hp is None:
        return ("none", ""), ""
    kind, url = _classify(hp, False, True, "firefox")
    if kind == "url" and page == 0:
        # 启动页设为空白页，但主页本身仍配置了地址：如实按主页返回
        pass
    return (kind, url), ""


def _progid_exe(progid):
    """从 ProgId 的 shell\\open\\command 取浏览器可执行文件路径"""
    hkcu, hkcr = _reg_hives()
    cmd = ""
    for hive, path in ((hkcr, r"%s\shell\open\command" % progid),
                       (hkcu, r"Software\Classes\%s\shell\open\command" % progid)):
        if hive is None:
            continue
        v = _reg_read(hive, path, "")
        if isinstance(v, str) and v.strip():
            cmd = v.strip()
            break
    if not cmd:
        return ""
    m = re.match(r'\s*"([^"]+\.exe)"', cmd, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r'([A-Za-z]:\\[^"\s]+?\.exe)', cmd, re.IGNORECASE)
    return m.group(1) if m else ""


def resolve():
    """只读解析默认浏览器与主页，返回结构化 dict（ok 仅在有明确主页结论时为 True）"""
    progid, reg_path = read_default_progid()
    result = {
        "ok": False,
        "browser": "",
        "progid": progid or "",
        "homepage": "",
        "homepage_kind": "none",
        "source": {"registry": reg_path},
        "message": "",
    }
    if not progid:
        result["message"] = "未检测到系统默认浏览器（注册表 UserChoice 无 ProgId），请询问用户要打开哪个网址。"
        return result

    name, family = _browser_from_progid(progid)
    result["browser"] = name
    exe = _progid_exe(progid)
    if exe:
        result["source"]["exe"] = exe

    prefs = ""
    parsed = None
    err = ""
    if family == "firefox":
        prefs = _firefox_prefs_path()
        if prefs:
            parsed, err = _read_firefox_prefs(prefs)
    else:
        user_data = _chromium_user_data(family)
        prefs = _chromium_prefs_path(user_data)
        if prefs:
            parsed, err = _read_chromium_prefs(prefs, family)

    if prefs:
        result["source"]["profile"] = prefs
    if parsed is None:
        if not prefs:
            result["message"] = (
                "未检测到系统主页配置：%s 的配置文件不存在（%s），"
                "请询问用户要打开哪个网址。" % (name, err or "profile 路径未找到"))
        else:
            result["message"] = "未检测到系统主页配置：%s，请询问用户要打开哪个网址。" % (err or "解析失败")
        return result

    kind, url = parsed
    result["homepage_kind"] = kind
    result["homepage"] = url
    if kind == "url":
        result["ok"] = True
        result["message"] = "检测到 %s 的主页：%s（来源：%s）" % (name, url, prefs)
    elif kind == "newtab":
        result["ok"] = True
        result["message"] = (
            "%s 的主页设置为新标签页（未配置具体网址，来源：%s）" % (name, prefs))
    else:
        result["message"] = (
            "未检测到系统主页配置：%s 未设置主页（来源：%s），"
            "请询问用户要打开哪个网址。" % (name, prefs))
    return result


def _normalize_url(url):
    u = str(url or "").strip()
    if not u:
        return ""
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", u):
        return u
    if u.lower().startswith(("about:", "chrome://", "edge://", "file:")):
        return u
    if re.match(r"^[\w.-]+\.[a-zA-Z]{2,}(/|$)", u):
        return "https://" + u
    return u


def open_page(url=""):
    """打开用户给定网址（url 非空）或系统主页；返回结构化结果（含 opened）"""
    result = resolve()
    result["opened"] = False
    result["opened_url"] = ""
    user_url = _normalize_url(url)
    exe = result.get("source", {}).get("exe", "")

    def _launch(target):
        try:
            if target.lower().startswith(("about:", "chrome://", "edge://")):
                if exe:
                    subprocess.Popen([exe, target])
                else:
                    subprocess.Popen('start "" "%s"' % target, shell=True)
            else:
                os.startfile(target)
            return True, ""
        except Exception as e:
            return False, str(e)

    if user_url:
        ok, err = _launch(user_url)
        result["opened"] = ok
        result["opened_url"] = user_url
        result["ok"] = ok
        if ok:
            result["message"] = "已用默认浏览器打开用户指定的网址：%s" % user_url
        else:
            result["message"] = "打开网址失败：%s" % err
        return result

    kind = result.get("homepage_kind")
    if kind == "none":
        result["message"] = (result.get("message") or "未检测到系统主页配置") + \
            " 未打开任何网页。"
        return result

    if kind == "newtab":
        try:
            if exe:
                subprocess.Popen([exe])
            else:
                os.startfile("about:blank")
            result["opened"] = True
            result["ok"] = True
            result["message"] = (
                "%s 的主页是新标签页，已打开浏览器（未编造网址）" % result.get("browser", "默认浏览器"))
        except Exception as e:
            result["message"] = "打开浏览器失败：%s" % e
        return result

    target = result.get("homepage") or ""
    ok, err = _launch(target)
    result["opened"] = ok
    result["opened_url"] = target
    result["ok"] = ok
    if ok:
        result["message"] = "已用 %s 打开系统主页：%s" % (result.get("browser", "默认浏览器"), target)
    else:
        result["message"] = "打开主页失败：%s" % err
    return result
