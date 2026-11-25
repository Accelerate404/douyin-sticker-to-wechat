"""
一键打包脚本：生成单文件 Windows 可执行程序 dist/DouyinStickerToWeChat.exe
依赖：pip install pyinstaller
"""
import subprocess
import sys
from pathlib import Path

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
    print("正在打包:", " ".join(cmd))
    subprocess.run(cmd, cwd=str(ROOT), check=True)
    exe_path = ROOT / "dist" / "DouyinStickerToWeChat.exe"
    if exe_path.exists():
        size_mb = exe_path.stat().st_size / (1024 * 1024)
        print(f"\n打包成功: {exe_path} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    build()
