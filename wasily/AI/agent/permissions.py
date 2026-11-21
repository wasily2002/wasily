# -*- coding: utf-8 -*-
"""agent 权限控制。

三级能力：read（只读工具/查询）/ write（改文件、改配置）/ exec（跑命令、桌面操作）。
按角色配置允许的能力，并对文件类工具做根目录白名单校验。
"""
from __future__ import annotations

import os

from . import errors

ROLE_CAPS = {
    "viewer": {"read"},
    "editor": {"read", "write"},
    "operator": {"read", "write", "exec"},
}

# 需要 exec 能力的工具/前缀
EXEC_TOOLS = {"shell_exec", "run_python", "ide_run_terminal", "open_app",
              "mouse_click", "key_type", "screenshot", "window_activate",
              "window_close", "clipboard_read", "clipboard_write",
              "browser_eval", "spawn_agent"}

# 需要 write 能力的工具
WRITE_TOOLS = {"file_write", "apply_patch", "file_delete", "file_move",
               "ide_edit_file", "save_output", "doc_write", "app_office_edit",
               "app_code_edit", "undo_step", "undo_to", "undo_clear",
               "kb_source_manage", "train_append", "train_feedback"}

# 路径参数名（用于白名单校验）
PATH_KEYS = ("path", "file", "target", "src", "dst", "root", "dir")


class PermissionPolicy:
    def __init__(self, role="operator", roots=None, allow_mcp_write=False):
        self.role = role if role in ROLE_CAPS else "viewer"
        # realpath 解析 Windows 8.3 短名与符号链接，避免 C:\Users\ADMINI~1 与长名不匹配
        self.roots = [os.path.realpath(r) for r in (roots or [])]
        self.allow_mcp_write = bool(allow_mcp_write)

    @property
    def caps(self):
        return ROLE_CAPS[self.role]

    def required_cap(self, name, readonly=True):
        if name in EXEC_TOOLS or name.startswith(("mouse_", "key_", "window_")):
            return "exec"
        if name in WRITE_TOOLS or not readonly:
            return "write"
        return "read"

    def check(self, name, readonly=True):
        cap = self.required_cap(name, readonly)
        if cap not in self.caps:
            raise errors.PermissionDenied(
                "角色 %s 无权执行 %s（需要 %s 能力）" % (self.role, name, cap),
                {"tool": name, "required": cap, "role": self.role})
        return cap

    def check_path(self, value):
        """根目录白名单：只在配置了 roots 时生效。"""
        if not self.roots or not isinstance(value, str) or not value.strip():
            return
        p = os.path.realpath(os.path.expanduser(value))
        for r in self.roots:
            try:
                if os.path.commonpath([p, r]) == r:
                    return
            except ValueError:
                continue
        raise errors.PermissionDenied(
            "路径不在允许范围内: %s" % p,
            {"path": p, "roots": self.roots})

    def check_args(self, args):
        if not isinstance(args, dict):
            return
        for k in PATH_KEYS:
            if k in args:
                self.check_path(args[k])

    def to_dict(self):
        return {"role": self.role, "caps": sorted(self.caps),
                "roots": self.roots, "allow_mcp_write": self.allow_mcp_write}
