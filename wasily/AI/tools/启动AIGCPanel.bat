@echo off
chcp 65001 >nul
title 启动 AIGCPanel
set "EXE=%LOCALAPPDATA%\Programs\aigcpanel\AigcPanel.exe"
if not exist "%EXE%" (
  echo 未找到 AIGCPanel，请先安装到默认目录。
  pause
  exit /b 1
)
start "" "%EXE%"
echo AIGCPanel 已启动。
echo 本地接口地址：http://127.0.0.1:63933   （API 文档：http://127.0.0.1:63933/doc）
timeout /t 5 >nul
