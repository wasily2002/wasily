"""远程操作：clone / fetch / pull / push / remotes。

不依赖系统 git 或 gh。凭据只在内存里交给传输层——
绝不写进 remote URL：那会落在 .git/config 里，并被日志与截图带出去。
"""
from __future__ import annotations

import os
import re
from typing import Any

import dulwich.porcelain

from .refs import current_branch
from .repository import GitError, RepoHandle, lock_for
from .snapshot import head_sha

_USERINFO = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*)://([^/@]+)@")


def guard_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        raise GitError("bad_url", "远程地址不能为空")
    if _USERINFO.match(raw):
        raise GitError("url_has_credentials",
                       "远程地址不能内嵌凭据，请改用 username/password 参数")
    return raw


def _creds(username: str, password: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if username:
        out["username"] = username
    if password:
        out["password"] = password
    return out


def _sha_hex(value: Any) -> str:
    """把 Dulwich 的 ref 值归一化成 40 位十六进制。

    不同版本/传输方式返回 20 字节原始 sha 或 40 字节 hex 字符串，
    如果统一 .hex() 会把后者二次编码，导致远端对账永远不相等。
    """
    if isinstance(value, bytes):
        if len(value) == 20:
            return value.hex()
        text = value.decode("ascii", "ignore").strip()
        return text if re.fullmatch(r"[0-9a-f]{40}", text) else value.hex()
    return str(value)


def clone(url: str, target: str, branch: str = "", depth: int | None = None,
          username: str = "", password: str = "") -> dict[str, Any]:
    src = guard_url(url)
    dest = os.path.abspath(target)
    if os.path.exists(dest) and os.listdir(dest):
        raise GitError("target_not_empty", "目标目录非空: %s" % dest)
    os.makedirs(dest, exist_ok=True)
    try:
        repo = dulwich.porcelain.clone(
            src, dest, branch=(branch or None), depth=depth,
            **_creds(username, password))
    except Exception as exc:  # noqa: BLE001
        raise GitError("clone_failed", "克隆失败: %s" % exc) from exc
    try:
        # 克隆时 dulwich 可能沿用全局 core.autocrlf 检出，导致工作区与索引
        # 内容不一致（状态恒为脏、pull 直接被拒）。这里关掉转换并重检出一份，
        # 保证「索引 = 工作区 = 提交内容」三者字节一致。
        from .repository import disable_autocrlf
        disable_autocrlf(repo, dest)
        dulwich.porcelain.reset(repo, "hard")
        head = head_sha(repo)
        return {"ok": True, "root": dest, "branch": branch,
                "head": head.hex() if head else ""}
    finally:
        repo.close()


def remotes(handle: RepoHandle) -> dict[str, Any]:
    out: dict[str, str] = {}
    try:
        cfg = handle.repo.get_config()
        for section in cfg.sections():
            if section[0] == b"remote" and len(section) > 1:
                try:
                    out[section[1].decode("utf-8")] = cfg.get(section, b"url").decode("utf-8")
                except KeyError:
                    out[section[1].decode("utf-8")] = ""
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "remotes": out}


def fetch(handle: RepoHandle, remote: str = "origin", prune: bool = False,
          username: str = "", password: str = "") -> dict[str, Any]:
    with lock_for("remote:" + handle.id):
        try:
            res = dulwich.porcelain.fetch(
                handle.repo, remote, prune=prune, quiet=True,
                **_creds(username, password))
        except Exception as exc:  # noqa: BLE001
            raise GitError("fetch_failed", "拉取失败: %s" % exc) from exc
    refs: dict[str, str] = {}
    for name, sha in (getattr(res, "refs", {}) or {}).items():
        key = name.decode("utf-8", "replace") if isinstance(name, bytes) else str(name)
        refs[key] = _sha_hex(sha)
    return {"ok": True, "remote": remote, "refs": refs}


def pull(handle: RepoHandle, remote: str = "origin", branch: str = "",
         ff_only: bool = True, username: str = "", password: str = "") -> dict[str, Any]:
    """默认只允许快进；分叉时明确报错，交给人工合并，不自动制造合并提交。"""
    target = branch or current_branch(handle) or "master"
    before = head_sha(handle.repo)
    with lock_for("remote:" + handle.id), handle.lock:
        try:
            dulwich.porcelain.pull(
                handle.repo, remote, refspecs=target,
                fast_forward=True, ff_only=ff_only,
                **_creds(username, password))
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
            if "Diverge" in type(exc).__name__ or "diverge" in text.lower() \
                    or "not fast-forward" in text.lower():
                raise GitError("diverged", "本地与远端已分叉，需要人工合并") from exc
            raise GitError("pull_failed", "拉取失败: %s" % text) from exc
    after = head_sha(handle.repo)
    return {"ok": True, "branch": target,
            "before": before.hex() if before else "",
            "after": after.hex() if after else "",
            "changed": before != after}


def push(handle: RepoHandle, branch: str = "", remote: str = "origin",
         set_upstream: bool = True, username: str = "",
         password: str = "") -> dict[str, Any]:
    """只推明确的那一个分支，禁止 force；推送后回读远端 ref 做对账。"""
    target = branch or current_branch(handle)
    if not target:
        raise GitError("detached_head", "当前不在分支上，无法推送")
    local = head_sha(handle.repo)
    refspec = "refs/heads/%s:refs/heads/%s" % (target, target)
    with lock_for("remote:" + handle.id):
        try:
            res = dulwich.porcelain.push(
                handle.repo, remote, refspecs=[refspec], force=False,
                set_upstream=set_upstream, **_creds(username, password))
        except Exception as exc:  # noqa: BLE001
            raise GitError("push_failed",
                           "推送被拒绝（可能非快进或无权限）: %s" % exc) from exc
    status: dict[str, Any] = {}
    for ref, val in (getattr(res, "ref_status", {}) or {}).items():
        key = ref.decode("utf-8", "replace") if isinstance(ref, bytes) else str(ref)
        status[key] = val.decode("utf-8", "replace") if isinstance(val, bytes) else val
    failed = {k: v for k, v in status.items() if v and str(v).lower() != "ok"}
    if failed:
        raise GitError("push_rejected", "远端拒绝了推送", ref_status=failed)

    verified = None
    try:
        info = fetch(handle, remote, username=username, password=password)
        remote_sha = (info.get("refs") or {}).get("refs/heads/" + target)
        # 以远端实际广播的 ref 为准；本地 remote-tracking ref 只是缓存，不能作为依据。
        if remote_sha and local:
            verified = remote_sha == local.hex()
    except Exception:  # noqa: BLE001
        verified = None
    return {"ok": True, "branch": target, "head": local.hex() if local else "",
            "ref_status": status, "verified": verified}
