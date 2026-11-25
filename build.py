"""
一键打包脚本：生成单文件 Windows 可执行程序 dist/DouyinStickerToWeChat.exe
依赖：pip install pyinstaller
"""
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent


def build():
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name",
        "DouyinStickerToWeChat",
        "--collect-data",
        "playwright",
        "--hidden-import",
        "playwright.async_api",
        str(ROOT / "main.py"),
    ]
    print("Building EXE:", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=str(ROOT), check=True)
    exe_path = ROOT / "dist" / "DouyinStickerToWeChat.exe"
    if exe_path.exists():
        size_mb = exe_path.stat().st_size / (1024 * 1024)
        print(f"\nBuild succeeded: {exe_path} ({size_mb:.1f} MB)", flush=True)


if __name__ == "__main__":
    build()
