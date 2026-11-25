@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist "config.local.toml" copy "config.example.toml" "config.local.toml" >nul
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 goto failed
)
if not exist ".venv\ready.flag" (
    .venv\Scripts\python.exe -m pip install -r requirements.txt --index-url https://pypi.org/simple
    if errorlevel 1 goto failed
    echo ready> .venv\ready.flag
)
echo 请用浏览器打开 http://127.0.0.1:8000 ，按 Ctrl+C 停止服务。
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
if errorlevel 1 goto failed
exit /b 0
:failed
echo 启动失败，请检查是否安装 Python 3.11 或更新版本，以及网络是否正常。
pause
exit /b 1
