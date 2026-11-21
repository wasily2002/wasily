@echo off
chcp 65001 >nul
rem ============ 打开 wasily AI 对话窗口 ============
rem 服务未运行时自动拉起并等待就绪，然后在默认浏览器打开对话页
python "%~dp0wasily_ctl.py" chat
exit /b 0
