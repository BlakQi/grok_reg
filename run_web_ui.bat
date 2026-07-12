@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
title Grok 注册机管理控制台
cd /d "%~dp0"

echo ===================================================
echo           Grok 注册机网页控制台启动器
echo ===================================================
echo.
echo [*] 关闭本窗口将同时停止网页服务
echo.

:: 补齐常见 PATH，避免双击时找不到 uv
set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"

:: 优先使用项目虚拟环境，更稳
set "PY="
if exist "%~dp0.venv\Scripts\python.exe" (
  set "PY=%~dp0.venv\Scripts\python.exe"
) else (
  where uv >nul 2>&1
  if not errorlevel 1 (
    set "PY=uv run python"
  ) else (
    where python >nul 2>&1
    if not errorlevel 1 (
      set "PY=python"
    )
  )
)

if not defined PY (
  echo [错误] 未找到 Python 运行环境。
  echo        请先在本目录执行: uv sync
  echo.
  pause
  exit /b 1
)

if not exist "%~dp0web_app.py" (
  echo [错误] 未找到 web_app.py，请确认 bat 位于项目根目录。
  echo 当前目录: %CD%
  echo.
  pause
  exit /b 1
)

echo [*] 正在清理端口 5000 的旧进程...
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":5000" ^| findstr "LISTENING"') do (
  echo [!] 结束旧进程 PID=%%a
  taskkill /F /PID %%a >nul 2>&1
)

timeout /t 1 /nobreak >nul

echo.
echo [*] 使用解释器: !PY!
echo [*] 地址: http://127.0.0.1:5000
echo [*] 请保持本窗口打开；关闭窗口即停止服务
echo.

start "" "http://127.0.0.1:5000"

:: 前台运行，窗口关闭则服务停止
if /I "!PY!"=="uv run python" (
  uv run python web_app.py
) else (
  "!PY!" web_app.py
)
set "ERR=!ERRORLEVEL!"

echo.
if not "!ERR!"=="0" (
  echo [错误] 服务异常退出，错误码=!ERR!
) else (
  echo [*] 服务已正常停止
)

echo [*] 退出清理端口 5000...
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":5000" ^| findstr "LISTENING"') do (
  taskkill /F /PID %%a >nul 2>&1
)

echo.
pause
endlocal
