"""生成可分发源码包，使用白名单排除个人配置、密钥、数据库和虚拟环境。"""
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
output = root / "dist" / "补货分析演示源码.zip"
output.parent.mkdir(exist_ok=True)
files = ["README.md", "requirements.txt", "pytest.ini", "config.example.toml", ".env.example", ".gitignore", ".dockerignore", "Dockerfile", "compose.yaml", "start.bat", "start.sh"]
for directory in ["app", "tests", "scripts"]:
    files.extend(str(p.relative_to(root)) for p in (root / directory).rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc")
with ZipFile(output, "w", ZIP_DEFLATED) as archive:
    for filename in sorted(files):
        archive.write(root / filename, filename)
print(f"Created {output.name}: {len(files)} source files, {output.stat().st_size} bytes")
