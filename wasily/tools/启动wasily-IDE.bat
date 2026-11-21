@echo off
chcp 65001 >nul
rem ============ wasily IDE 一键启动 ============
rem 1) 后台静默拉起守护进程（pythonw，无控制台窗口；已在运行则复用）
echo [wasily] 检查 AI 服务...
python "%~dp0wasily_ctl.py" watchdog

rem 2) 等待服务就绪（最多 60 秒）
set /a n=0
:wait
curl -s -o nul --max-time 2 http://127.0.0.1:8000/health && goto ready
set /a n+=1
if %n% geq 30 goto ready
timeout /t 2 /nobreak >nul
goto wait
:ready

rem 3) 启动 wasily IDE（中文界面 + AI 工作区）
start "" "d:\煮饭仔点餐\项目文件\项目介绍\wasily\tools\wasily-ide\wasily-ide.exe" --locale=zh-cn "d:\煮饭仔点餐\项目文件\项目介绍\wasily\AI"
exit
