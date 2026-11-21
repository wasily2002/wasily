@echo off
chcp 65001 >nul
rem ============ wasily 全部关闭 ============
rem 关闭：AI 服务 / 守护进程 / 桌面大脑(Electron+pywebview) / wasily IDE
rem 具体逻辑在 wasily_ctl.py 中实现（避免 bat 的引号与中文路径编码问题）
python "%~dp0wasily_ctl.py" stop
exit /b 0
