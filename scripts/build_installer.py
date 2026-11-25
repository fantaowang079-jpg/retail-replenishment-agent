"""生成给业务人员使用的一键安装包，不包含测试、个人配置和运行数据。"""
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
output = root / "dist" / "补货建议安装包.zip"
output.parent.mkdir(exist_ok=True)

install_bat = r'''@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo 未找到 Python 3.11 或更新版本，请先安装 Python。
  pause
  exit /b 1
)
if not exist "config.local.toml" copy "config.example.toml" "config.local.toml" >nul
if not exist ".venv\Scripts\python.exe" (
  echo 正在创建本地运行环境...
  python -m venv .venv
  if errorlevel 1 goto failed
)
if not exist ".venv\ready.flag" (
  echo 正在安装依赖，首次安装需要联网...
  .venv\Scripts\python.exe -m pip install -r requirements.txt --index-url https://pypi.org/simple
  if errorlevel 1 goto failed
  echo ready> .venv\ready.flag
)
echo 安装完成，正在启动服务...
start "补货建议服务" /min cmd /c call "%~dp0启动补货建议.bat"
timeout /t 3 /nobreak >nul
start "" "http://127.0.0.1:8000/"
exit /b 0
:failed
echo 安装失败，请检查网络和 Python 环境后重新运行。
pause
exit /b 1
'''

start_bat = r'''@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 尚未安装，请先双击“安装并启动补货建议.bat”。
  pause
  exit /b 1
)
echo 请用浏览器打开 http://127.0.0.1:8000 ，按 Ctrl+C 停止服务。
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
'''

instructions = '''补货建议安装包

一、安装
1. 解压整个文件夹，不要只运行压缩包里的文件。
2. 双击“安装并启动补货建议.bat”。
3. 首次安装需要 Python 3.11 或更新版本，并需要联网下载依赖。
4. 浏览器会打开 http://127.0.0.1:8000 。

二、配置自己的模型密钥
安装完成后，用记事本打开同目录的 config.local.toml，在 [deepseek] 下填写 api_key，重启服务即可。没有密钥也能使用内置业务数据和程序计算。

三、停止和再次启动
关闭启动服务的命令窗口即可停止；再次双击“启动补货建议.bat”即可启动，不会重复安装依赖。

四、这份包和源码包的区别
这份是给业务人员直接使用的精简安装包，去掉了测试和开发说明，但 Python 文件仍在包内，具备开发能力的人可以修改。需要完整测试、架构说明和所有开发文件，请使用“补货分析演示源码.zip”。
'''

files = ["requirements.txt", "config.example.toml"]
count = 3
with ZipFile(output, "w", ZIP_DEFLATED) as archive:
    archive.writestr("安装并启动补货建议.bat", install_bat.encode("utf-8-sig"))
    archive.writestr("启动补货建议.bat", start_bat.encode("utf-8-sig"))
    archive.writestr("安装说明.txt", instructions.encode("utf-8"))
    for filename in files:
        archive.write(root / filename, filename)
        count += 1
    for path in (root / "app").rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            archive.write(path, str(path.relative_to(root)))
            count += 1
print(f"Created {output.name}: {count} files, {output.stat().st_size} bytes")
