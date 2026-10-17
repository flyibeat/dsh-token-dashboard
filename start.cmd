@echo off
REM DeepSeek Harness 词元用量看板 —— 一键启动（Windows）
REM 双击本文件即可；关闭窗口即停止服务。
setlocal
cd /d "%~dp0"

REM 解释器优先级：DSH_PYTHON 环境变量 → PATH 上的 python → DSH 托管的 Python。
REM 不写死用户名/盘符：DSH 托管的运行时路径由 DSH_HOME 推出来。
set "PY="
if defined DSH_PYTHON set "PY=%DSH_PYTHON%"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  for /d %%D in ("%DSH_HOME%\dsh-runtimes\*") do (
    if exist "%%D\dependencies\python\python.exe" set "PY=%%D\dependencies\python\python.exe"
  )
)
if not defined PY (
  for /d %%D in ("%USERPROFILE%\.dsh\dsh-runtimes\*") do (
    if exist "%%D\dependencies\python\python.exe" set "PY=%%D\dependencies\python\python.exe"
  )
)
if not defined PY (
  echo 找不到 Python。请安装 Python 3.10+ 并加入 PATH，
  echo 或设 DSH_PYTHON 指向 python.exe。
  pause
  exit /b 1
)

echo 使用解释器：%PY%
"%PY%" usage_server.py --open %*
if errorlevel 1 pause
