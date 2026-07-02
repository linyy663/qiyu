@echo off
chcp 65001 >nul
title 七鱼客服统计工具 - 本地服务

echo ============================================
echo   网易七鱼客服工单统计与质检工具
echo   正在启动本地服务...
echo ============================================

:: 切换到脚本所在目录
cd /d "%~dp0"

:: 检查端口是否被占用
netstat -ano 2>nul | findstr ":5890" >nul
if %errorlevel% equ 0 (
    echo [提示] 端口 5890 已被占用，正在释放...
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5890"') do (
        taskkill /F /PID %%a 2>nul
    )
    timeout /t 2 /nobreak >nul
)

:: 启动守护进程
echo [启动] 正在启动 Flask 后端服务...
start "七鱼客服服务" /MIN cmd /c "C:\Users\linyy\.workbuddy\binaries\python\versions\3.13.12\python.exe" start_server.py

:: 等待服务启动
echo [等待] 等待服务就绪...
timeout /t 4 /nobreak >nul

:: 检查服务是否已启动
curl -s http://localhost:5890/api/config >nul 2>&1
if %errorlevel% equ 0 (
    echo [成功] 服务已启动！
    echo [地址] http://localhost:5890
    start http://localhost:5890
) else (
    echo [提示] 服务可能还在启动中，请稍后访问 http://localhost:5890
)

echo.
echo 按任意键关闭此窗口（不会停止服务）...
pause >nul
