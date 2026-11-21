@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title 项目文档独立版 · 启动器

echo ============================================================
echo   项目文档独立版（wasily AI 文档 + 供销社便利店文档）
echo ============================================================

REM [1/4] 检查 Python
set "PY=python"
where python >nul 2>nul
if errorlevel 1 (
    where py >nul 2>nul
    if errorlevel 1 (
        echo [ERROR] 未找到 Python，请安装 Python 3 并勾选加入 PATH
        pause
        exit /b 1
    )
    set "PY=py -3"
)

REM [2/4] 自动挑选空闲端口（8001 起，最多试到 8030）
set "PORT=8001"
:find_port
set "BUSY="
for /f "delims=" %%a in ('netstat -ano ^| findstr /c:":%PORT% "') do set "BUSY=1"
if defined BUSY (
    set /a PORT+=1
    if !PORT! leq 8030 goto find_port
    echo [ERROR] 8001-8030 端口全部被占用，请先关闭占用程序
    pause
    exit /b 1
)
set "DOCS_PORT=%PORT%"

REM [3/4] 拉起文档服务（独立最小化窗口，关闭窗口即停止服务）
echo [1/3] 启动文档服务，端口 %PORT% ...
start "docs-server-%PORT%" /min /d "%~dp0" cmd /k %PY% server.py

REM 等待服务就绪（最多约 20 秒）
set /a tries=0
:wait_ready
ping -n 2 127.0.0.1 >nul 2>&1
curl -s -m 2 "http://127.0.0.1:%PORT%/api/projects" >nul 2>&1
if not errorlevel 1 goto ready
set /a tries+=1
if !tries! lss 20 goto wait_ready
echo       [WARN] 等待超时，仍尝试打开页面
goto open

:ready
echo [2/3] 服务已就绪

:open
REM [4/4] 打开浏览器
echo [3/3] 打开浏览器 ...
start "" "http://127.0.0.1:%PORT%/"
echo ============================================================
echo   项目文档中心:  http://127.0.0.1:%PORT%/
echo   wasily 文档:   http://127.0.0.1:%PORT%/docs?project=wasily
echo   供销社文档:    http://127.0.0.1:%PORT%/docs?project=gxs
echo ------------------------------------------------------------
echo   文档编辑保存会写入本文件夹 projects 下的 docs.json
echo   停止服务：关闭任务栏最小化的 "docs-server-%PORT%" 窗口
echo ============================================================
ping -n 4 127.0.0.1 >nul 2>&1
exit /b 0
