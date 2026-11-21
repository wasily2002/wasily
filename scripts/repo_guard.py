"""提交前的仓库卫兵：扫描将要公开的内容，拦截密钥、超大文件与嵌套仓库。

默认用 .gitignore 缩小公开候选；显式 paths 是调用方确认的公开候选，即使被
ignore 也必须检查。运行目录与本机隐私数据仍硬跳过，避免把本机状态当源码。

用法：
    python scripts/repo_guard.py             # 人类可读摘要
    python scripts/repo_guard.py --json      # 机器可读，供 CI 与 IDE 调用
    python scripts/repo_guard.py --root DIR [paths ...]

退出码：0 = 无阻断项；1 = 存在 block；2 = 存在需要人工确认的 warn（仅 --strict）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

from dulwich.ignore import IgnoreFilterManager

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 整目录硬跳过：显式候选也不进入本机运行状态；build 不在此列，因为 VS Code
# 的 build/ 含必须公开的构建源码，只有其中被 .gitignore 排除的产物才跳过。
SKIP_DIRS = {
    "node_modules", "__pycache__", ".git", ".venv", "venv", "env",
    "train_venv", "triposr_venv", "models", "models_7b", "_model_cache",
    "vendor", "finetune", "sessions_store", "_evolve_sandbox",
    "undo_history", "uploads", "outputs", "training_data", "logs",
    "wasily_log", "data", "dist", "out", "coverage", "htmlcov",
}

# 按相对路径跳过的子树：IDE 运行分发包是部署目标，不进公开仓库。
SKIP_RELS = ("wasily/tools/wasily-ide",)

# 按后缀直接阻断：运行分发物与模型权重不应进入源码仓库。
BLOCK_EXT = {
    ".exe", ".dll", ".node", ".so", ".dylib", ".pdb", ".lib",
    ".asar", ".pak", ".gguf", ".ggml", ".safetensors", ".onnx",
    ".pt", ".pth", ".ckpt", ".h5", ".p12", ".pfx", ".pem", ".key",
}

# 按文件名阻断：明确的凭据与本机账户状态。
BLOCK_NAMES = {
    "key.bin", "account.json", "credentials.json", "secrets.json",
    "id_rsa", "id_ed25519", ".env",
}

SECRET_PATTERNS = [
    (re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}"), "GitHub Token"),
    (re.compile(rb"sk-[A-Za-z0-9]{20,}"), "API Key"),
    (re.compile(rb"AKIA[0-9A-Z]{16}"), "AWS Access Key"),
    (re.compile(
        rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----\r?\n"
        rb"(?:(?:Proc-Type|DEK-Info):[^\r\n]*\r?\n|\r?\n|"
        rb"[A-Za-z0-9+/=]{8,}\r?\n)+"
        rb"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"), "私钥"),
    (re.compile(rb"Authorization:\s*Bearer\s+[A-Za-z0-9._\-]{10,}"), "Bearer Token"),
]

SIZE_BLOCK = 50 * 1024 * 1024
SIZE_WARN = 10 * 1024 * 1024
SNIFF = 64 * 1024

TEXT_EXT = {
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".json", ".md",
    ".html", ".htm", ".css", ".scss", ".yml", ".yaml", ".toml", ".ini",
    ".cfg", ".bat", ".cmd", ".ps1", ".sh", ".txt", ".sql", ".xml", ".svg",
    ".pem", ".key", ".log", ".wxl", ".wxi", ".wxs", ".code-workspace",
}

UPSTREAM_ROOTS = (
    ("wasily", "tools", "wasily-ide-src"),
    ("wasily", "tools", "vscodium-build"),
)
FIXTURE_CERT_EXTS = {".pem", ".key", ".p12", ".pfx"}
FIXTURE_DIRS = {"fixture", "fixtures", "testdata", "test_data"}


def _is_skipped_dir(name: str) -> bool:
    return name in SKIP_DIRS or name.startswith(".cache")


def _is_skipped_rel(rel: str) -> bool:
    return any(rel == p or rel.startswith(p + "/") for p in SKIP_RELS)


def _upstream_parts(root: str, rel: str) -> tuple[str, ...] | None:
    full = os.path.join(root, rel).replace("\\", "/")
    parts = tuple(p.casefold() for p in full.split("/") if p and p != ".")
    found = None
    for marker in UPSTREAM_ROOTS:
        width = len(marker)
        for index in range(len(parts) - width + 1):
            if parts[index:index + width] == marker:
                found = parts[index + width:]
    return found


def _is_upstream_test_fixture(root: str, rel: str) -> bool:
    upstream_parts = _upstream_parts(root, rel)
    if upstream_parts is None:
        return False

    directories = upstream_parts[:-1]
    has_test = any(
        p in {"test", "tests"} or p.startswith(("test-", "test_"))
        or p.endswith(("-test", "-tests")) for p in directories)
    has_fixture = any(
        p in FIXTURE_DIRS or p.endswith("fixtures") for p in directories)
    # 上游测试证书是公开测试数据，不等同于本机凭据；同时限定上游树、测试目录
    # 和 fixture 目录，避免把任意 test/ 下的真实私钥或普通项目的密钥放进来。
    return has_test and has_fixture


def _is_upstream_test_source(root: str, rel: str) -> bool:
    parts = _upstream_parts(root, rel)
    if not parts:
        return False
    if parts[0] == "wasily":
        return False
    if parts[0] == "extensions" and len(parts) > 1:
        if parts[1].startswith("wasily-"):
            return False
    directories = parts[:-1]
    in_test_dir = any(
        p in {"test", "tests"} or p.startswith(("test-", "test_"))
        or p.endswith(("-test", "-tests")) for p in directories)
    return in_test_dir or parts[-1].endswith(
        (".test.ts", ".test.js", ".spec.ts", ".spec.js"))


def _is_upstream_test_placeholder(root: str, rel: str, label: str,
                                  value: bytes) -> bool:
    if not _is_upstream_test_source(root, rel):
        return False
    if label == "GitHub Token":
        body = value.split(b"_", 1)[1]
        return (
            len(set(body)) == 1
            or body == b"12345678901234567890"
            or body == b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef1234"
        )
    if label == "Bearer Token":
        token = re.split(rb"\s+", value)[-1]
        return token in {
            b"private-value",
            b"eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.payload.signature",
        }
    if label == "AWS Access Key":
        # AWS 文档固定的 EXAMPLE 值，不是可用账户凭据。
        return value[4:] == b"IOSFODNN7EXAMPLE"
    # 只放行明确不可用的测试占位值；不能在 test/ 下豁免随机形态的真实 token。
    return False


def _candidates(root: str, paths) -> list[tuple[str, bool]]:
    result = []
    for candidate in paths or []:
        raw = os.fspath(candidate)
        full = os.path.realpath(os.path.join(root, raw))
        try:
            inside = os.path.commonpath((root, full)) == root
        except ValueError:
            inside = False
        if not inside:
            raise ValueError("候选路径越出扫描根目录: %s" % raw)
        if not os.path.exists(full):
            raise ValueError("候选路径不存在: %s" % raw)
        rel = os.path.relpath(full, root)
        key = os.path.normcase(rel).replace("\\", "/")
        result.append((key, os.path.isdir(full)))
    return result


def _candidate_state(rel: str, candidates: list[tuple[str, bool]]):
    forced = False
    needed = False
    for candidate, is_dir in candidates:
        if candidate == "." and is_dir:
            forced = True
        elif rel == candidate:
            forced = True
        elif is_dir and rel.startswith(candidate + "/"):
            forced = True
        if rel == "." or candidate.startswith(rel + "/"):
            needed = True
    return forced, needed


def _manifest_rel(value: str) -> str:
    rel = os.fspath(value).replace("\\", "/").strip("/")
    if (not rel or os.path.isabs(value) or value.startswith(("/", "\\"))
            or ".." in rel.split("/")):
        raise ValueError("来源清单包含越界路径: %s" % value)
    return rel


def _load_source_manifest(root: str):
    path = os.path.join(root, "scripts", "upstream_sources.json")
    if not os.path.isfile(path):
        return [], {}
    with open(path, encoding="utf-8") as stream:
        manifest = json.load(stream)
    if manifest.get("version") != 1:
        raise ValueError("不支持的上游来源清单版本")

    include_dirs = [
        _manifest_rel(rel) for rel in manifest.get("include_dirs", [])]
    upstreams = {}
    for source in manifest.get("upstreams", []):
        prefix = _manifest_rel(source["prefix"])
        for name, (oid, mode) in source["files"].items():
            rel = prefix + "/" + _manifest_rel(name)
            key = os.path.normcase(rel).replace("\\", "/")
            upstreams[key] = (rel, oid, int(mode))
    return include_dirs, upstreams


def _blob_oid(path: str) -> str:
    if os.path.islink(path):
        data = os.fsencode(os.readlink(path))
    else:
        with open(path, "rb") as stream:
            data = stream.read()
    header = b"blob " + str(len(data)).encode("ascii") + b"\0"
    return hashlib.sha1(header + data).hexdigest()


def scan(root: str | None = None, paths=None,
         collect: set[str] | None = None) -> dict:
    root = os.path.realpath(os.path.abspath(os.fspath(root or ROOT)))
    if not os.path.isdir(root):
        raise ValueError("扫描根目录不存在或不是目录: %s" % root)
    candidates = _candidates(root, paths)
    include_dirs, upstreams = _load_source_manifest(root)
    # 直接实例化即可读取根及嵌套 .gitignore，且测试临时目录无需先初始化仓库。
    ignores = IgnoreFilterManager(root, [], ignorecase=os.name == "nt")
    findings: list[dict] = []
    processed = set()
    total_files = 0
    total_bytes = 0

    def add(level: str, rule: str, rel: str, size: int = 0,
            detail: str = "") -> None:
        findings.append({"level": level, "rule": rule, "path": rel,
                         "size": size, "detail": detail})

    def check_file(full: str, rel: str, upstream=None,
                   pin_required: bool = False) -> None:
        nonlocal total_files, total_bytes
        key = os.path.normcase(rel).replace("\\", "/")
        if key in processed:
            return
        processed.add(key)

        is_link = os.path.islink(full)
        if not is_link and not os.path.isfile(full):
            if upstream is not None:
                add("block", "missing_upstream_source", rel,
                    detail="上游来源清单中的文件在工作区缺失")
            return
        try:
            size = (len(os.fsencode(os.readlink(full))) if is_link
                    else os.path.getsize(full))
        except OSError:
            if upstream is not None:
                add("block", "missing_upstream_source", rel,
                    detail="上游来源清单中的文件无法读取")
            return

        total_files += 1
        total_bytes += size
        if collect is not None:
            collect.add(rel)
        name = os.path.basename(rel)
        ext = os.path.splitext(name)[1].lower()
        is_fixture = _is_upstream_test_fixture(root, rel)
        pinned = None

        def pinned_upstream() -> bool:
            nonlocal pinned
            if pinned is None:
                try:
                    pinned = bool(upstream) and _blob_oid(full) == upstream[1]
                except OSError:
                    pinned = False
            return pinned

        # 只有来源清单恢复的非文本文件需要固定 OID；普通 Wasily 图片与主题
        # 定制不因不同于上游而被误拦截。
        if (pin_required and upstream is not None
                and (is_link or ext not in TEXT_EXT) and not pinned_upstream()):
            add("block", "upstream_binary_changed", rel, size,
                "被忽略的上游非文本文件必须与来源清单一致")
            return

        if name.lower() in BLOCK_NAMES and not pinned_upstream():
            add("block", "credential_file", rel, size, "凭据/账户文件")
        elif (name.startswith(".env") and name != ".env.example"
                and not pinned_upstream()):
            add("block", "dotenv", rel, size, "环境变量文件")
        elif (ext in BLOCK_EXT and not pinned_upstream() and not (
                is_fixture and ext in FIXTURE_CERT_EXTS)):
            add("block", "binary_artifact", rel, size,
                "构建产物或模型权重")
        elif size >= SIZE_BLOCK:
            add("block", "oversize", rel, size, "超过 50MB")
        elif size >= SIZE_WARN:
            add("warn", "large", rel, size, "超过 10MB，需人工确认")

        if ext in TEXT_EXT and size <= 2 * 1024 * 1024:
            try:
                if is_link:
                    head = os.fsencode(os.readlink(full))[:SNIFF]
                else:
                    with open(full, "rb") as stream:
                        head = stream.read(SNIFF)
            except OSError:
                head = b""
            for pattern, label in SECRET_PATTERNS:
                blocked = False
                for match in pattern.finditer(head):
                    if label == "私钥" and is_fixture:
                        continue
                    if _is_upstream_test_placeholder(
                            root, rel, label, match.group(0)):
                        continue
                    add("block", "secret_pattern", rel, size, label)
                    blocked = True
                    break
                if blocked:
                    break

    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
        kept_dirs = []
        for name in dirnames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace("\\", "/")
            if name == ".git":
                if rel_dir != ".":
                    add("block", "nested_repository", rel,
                        detail="嵌套仓库目录，先移除或改为明确的源码依赖")
                continue
            if _is_skipped_dir(name) or _is_skipped_rel(rel):
                continue
            forced, needed = _candidate_state(
                os.path.normcase(rel).replace("\\", "/"), candidates)
            if not forced and not needed and ignores.may_prune_directory(
                    rel + "/"):
                continue
            kept_dirs.append(name)
        dirnames[:] = kept_dirs

        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace("\\", "/")
            if name == ".git":
                if rel_dir != ".":
                    try:
                        size = os.path.getsize(full)
                    except OSError:
                        size = 0
                    add("block", "nested_repository", rel, size,
                        "嵌套仓库指针文件，先移除或改为明确的源码依赖")
                continue
            key = os.path.normcase(rel).replace("\\", "/")
            forced, _ = _candidate_state(key, candidates)
            if not forced and ignores.is_ignored(rel) is True:
                continue
            check_file(full, rel, upstreams.get(key))

    # 这些目录是明确的源码，不是同名运行目录；只恢复源码，不放宽其他目录。
    for rel_dir in include_dirs:
        source_dir = os.path.join(root, *rel_dir.split("/"))
        if not os.path.isdir(source_dir):
            add("block", "missing_source_directory", rel_dir,
                detail="公开源码目录在工作区缺失")
            continue
        for dirpath, dirnames, filenames in os.walk(source_dir):
            kept_dirs = []
            for name in dirnames:
                if name == ".git":
                    rel = os.path.relpath(
                        os.path.join(dirpath, name), root).replace("\\", "/")
                    add("block", "nested_repository", rel,
                        detail="源码目录中不允许嵌套仓库")
                else:
                    kept_dirs.append(name)
            dirnames[:] = kept_dirs
            for name in filenames:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, root).replace("\\", "/")
                if name == ".git":
                    add("block", "nested_repository", rel,
                        detail="源码目录中不允许嵌套仓库指针")
                    continue
                check_file(full, rel)

    # 上游已跟踪文件不因本机通用 ignore 规则而消失；缺失文件也必须显式失败。
    for key, (rel, oid, mode) in upstreams.items():
        if key in processed:
            continue
        full = os.path.join(root, *rel.split("/"))
        check_file(full, rel, (rel, oid, mode), pin_required=True)

    blocks = [f for f in findings if f["level"] == "block"]
    warns = [f for f in findings if f["level"] == "warn"]
    return {
        "ok": not blocks,
        "root": root,
        "scanned_files": total_files,
        "scanned_bytes": total_bytes,
        "block": len(blocks),
        "warn": len(warns),
        "findings": findings[:200],
        "truncated": len(findings) > 200,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="扫描待公开内容")
    ap.add_argument("--root", default=ROOT, help="扫描根目录，默认是工程根")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--strict", action="store_true", help="存在 warn 也返回非零")
    ap.add_argument("paths", nargs="*", help="显式公开候选，忽略规则仍被绕过")
    args = ap.parse_args()
    result = scan(root=args.root, paths=args.paths)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("扫描目录: %s" % result["root"])
        print("文件数: %d，总大小: %.1f MB" % (
            result["scanned_files"], result["scanned_bytes"] / 1048576))
        print("阻断项: %d，警告项: %d" % (result["block"], result["warn"]))
        for f in result["findings"]:
            print("[%s] %-16s %s %s" % (
                f["level"], f["rule"], f["path"], f["detail"]))
        if result["truncated"]:
            print("… 结果已截断，使用 --json 查看完整清单")

    if not result["ok"]:
        return 1
    if args.strict and result["warn"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
