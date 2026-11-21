@echo off
chcp 936 >nul
setlocal EnableExtensions
title Wasily 一键启动

set "ROOT=%~dp0"
set "AI_DIR=%ROOT%AI"
set "IDE_EXE=%ROOT%tools\wasily-ide\wasily-ide.exe"
set "PY=C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"

echo ==========================================
echo   Wasily 一键启动：后端 + MCP + IDE
echo ==========================================

if not exist "%AI_DIR%\api_server.py" (
  echo [错误] 未找到 %AI_DIR%\api_server.py
  pause
  exit /b 1
)

echo [1/3] 检查 AI 后端 :8000
powershell -NoProfile -Command "try{(Invoke-WebRequest -Uri 'http://127.0.0.1:8000/health' -UseBasicParsing -TimeoutSec 2)|Out-Null;exit 0}catch{exit 1}"
if not errorlevel 1 (
  echo       已在运行，跳过
  goto CHECK_IDE
)

echo       启动中（首次加载本地模型，通常 10-60 秒）...
powershell -NoProfile -WindowStyle Hidden -Command "Start-Process -FilePath '%PY%' -ArgumentList '-X','utf8','api_server.py' -WorkingDirectory '%AI_DIR%' -WindowStyle Hidden -RedirectStandardOutput '%AI_DIR%\api_run.log' -RedirectStandardError '%AI_DIR%\api_err.log'"
powershell -NoProfile -Command "for($i=0;$i -lt 80;$i++){try{if((Invoke-RestMethod -Uri 'http://127.0.0.1:8000/health' -TimeoutSec 3).status -eq 'ok'){exit 0}}catch{};Start-Sleep -Milliseconds 1500};exit 1"
if errorlevel 1 (
  echo       [警告] 后端未就绪，请查看 %AI_DIR%\api_run.log
) else (
  echo       后端已就绪，remote MCP 已随后端自动拉起 :9000
)

:CHECK_IDE
echo [2/3] 检查 IDE
tasklist /FI "IMAGENAME eq wasily-ide.exe" 2>nul | find /I "wasily-ide.exe" >nul
if not errorlevel 1 (
  echo       已在运行，跳过
  goto DONE
)
if not exist "%IDE_EXE%" (
  echo       [错误] 未找到 %IDE_EXE%
  pause
  exit /b 1
)
echo       启动中...
start "" "%IDE_EXE%"

:DONE
echo [3/3] 汇总
powershell -NoProfile -Command "Start-Sleep -Seconds 2; try{$m=Invoke-RestMethod -Uri 'http://127.0.0.1:8000/v1/mcp/servers' -TimeoutSec 8; $m.servers | ForEach-Object{ '       MCP ' + $_.name.PadRight(11) + ' connected=' + $_.connected + '  tools=' + $_.tools.Count }}catch{'       MCP 状态查询失败'}"
echo.
echo 完成。本窗口 5 秒后自动关闭。
timeout /t 5 >nul 2>&1
exit /b 0
