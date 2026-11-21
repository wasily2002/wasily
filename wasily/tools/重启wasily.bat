@echo off
chcp 65001 >nul
rem ============ wasily 一键重启 ============
rem 1) 关闭全部组件  2) 启动 AI 服务并等待就绪
rem 3) 拉起桌面悬浮大脑  4) 启动 wasily IDE
rem 具体逻辑在 wasily_ctl.py 中实现（避免 bat 的引号与中文路径编码问题）
python "%~dp0wasily_ctl.py" restart
exit /b 0
