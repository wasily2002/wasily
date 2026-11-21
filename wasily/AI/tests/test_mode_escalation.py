# -*- coding: utf-8 -*-
"""模式自动升级 / 模式校验 / 工具集完备性测试。

覆盖三类回归：
1. solo 启动时工具集必须包含完整 Agent 能力（写文件、跑终端、桌面操作）；
2. IDE 下 Ask/Plan 遇到动作型任务要自动升级到 solo（复杂任务触发）；
3. 模式层不再直接拒绝写工具，工具集任何模式下都不为空（工具调用全流程前提）。
"""
import os
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import ide_agent  # noqa: E402
import tools  # noqa: E402


class TestLooksLikeAction(unittest.TestCase):
    def test_action(self):
        for q in ("帮我修改 login.html 的按钮样式", "创建项目骨架", "运行测试并修复报错",
                  "把 a.py 重命名为 b.py", "截图看看当前窗口", "安装这个依赖"):
            self.assertTrue(ide_agent.looks_like_action(q), q)

    def test_question(self):
        for q in ("什么是闭包", "怎么创建虚拟环境", "为什么这段代码会崩溃",
                  "如何理解事件循环？", "Python 和 Go 有什么区别"):
            self.assertFalse(ide_agent.looks_like_action(q), q)

    def test_empty(self):
        self.assertFalse(ide_agent.looks_like_action(""))
        self.assertFalse(ide_agent.looks_like_action(None))


class TestResolveMode(unittest.TestCase):
    def test_ide_ask_escalates(self):
        mode, escalated = ide_agent.resolve_mode("ide", "ask", "帮我修复这个 bug")
        self.assertEqual((mode, escalated), ("solo", True))

    def test_ide_plan_escalates(self):
        mode, escalated = ide_agent.resolve_mode("ide", "plan", "重构整个模块")
        self.assertEqual((mode, escalated), ("solo", True))

    def test_ide_question_stays(self):
        mode, escalated = ide_agent.resolve_mode("ide", "ask", "什么是依赖注入？")
        self.assertEqual((mode, escalated), ("ask", False))

    def test_ide_solo_unchanged(self):
        mode, escalated = ide_agent.resolve_mode("ide", "solo", "帮我修复这个 bug")
        self.assertEqual((mode, escalated), ("solo", False))

    def test_ide_empty_mode_is_solo(self):
        mode, escalated = ide_agent.resolve_mode("ide", None, "创建 index.html", current="ask")
        self.assertEqual(mode, "solo")

    def test_non_ide_no_escalation(self):
        mode, escalated = ide_agent.resolve_mode("web", "ask", "帮我修改文件")
        self.assertEqual((mode, escalated), ("ask", False))


class TestValidateMode(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(ide_agent.validate_mode("agent"), "solo")
        self.assertEqual(ide_agent.validate_mode("IDE"), "solo")
        self.assertEqual(ide_agent.validate_mode("ASK"), "ask")
        self.assertEqual(ide_agent.validate_mode("plan"), "plan")
        self.assertEqual(ide_agent.validate_mode(""), "solo")
        self.assertEqual(ide_agent.validate_mode(None, fallback="ask"), "ask")
        self.assertEqual(ide_agent.validate_mode("garbage"), "solo")


class TestToolSets(unittest.TestCase):
    def test_non_empty_every_mode(self):
        for mode in ("ask", "plan", "solo", "", "garbage"):
            names = tools.enabled_tool_names("随便看看", mode, 0)
            self.assertTrue(names, "模式 %r 下工具集为空" % mode)

    def test_solo_full_capability(self):
        names = tools.enabled_tool_names("帮我改代码", "solo", 0)
        for name in ("file_read", "file_write", "ide_edit_file",
                     "ide_run_terminal", "shell_exec", "screenshot"):
            self.assertIn(name, names)

    def test_pure_read_hides_write(self):
        names = tools.enabled_tool_names("读一下 a.py", "ask", 0, pure_read=True)
        self.assertIn("file_read", names)
        self.assertNotIn("file_write", names)

    def test_write_not_blocked_by_mode(self):
        # 模式层不再直接拒绝写工具：应进入参数校验（参数错误而非只读拒绝）
        out = tools.execute_tool("file_write", {}, mode="ask")
        self.assertNotIn("对话模式下不会自动修改文件", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
