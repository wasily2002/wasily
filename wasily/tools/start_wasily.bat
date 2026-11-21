@echo off
REM wasily IDE 启动脚本
REM 2026-10-30 修正：GPU 防崩溃参数（--disable-gpu/--in-process-gpu 等）在当前版本
REM 反而导致启动约 40 秒后整组进程崩溃退出，已移除；恢复为简洁启动方式。
set "WASILY_IDE=D:\煮饭仔点餐\项目文件\项目介绍\wasily\tools\wasily-ide\wasily-ide.exe"
set "WORKSPACE=D:\煮饭仔点餐\项目文件\项目介绍\wasily"

start "" "%WASILY_IDE%" --locale=zh-cn "%WORKSPACE%"
