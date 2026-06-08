"""
微信 PC 端自定义表情导入模块
通过聊天窗口发送图片，再手动/自动保存到「我的表情」

微信 PC 没有直接导入表情包的功能，
唯一途径是：发送图片到聊天 → 右键图片 → 添加到表情
"""
import ctypes
import logging
import subprocess
import time
from pathlib import Path

import pyautogui
import pyperclip
from PIL import Image

import config

logger = logging.getLogger(__name__)

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.3


class WeChatStickerImporter:
    """微信自定义表情导入器"""

    def __init__(self, on_progress=None):
        self.on_progress = on_progress or (lambda msg: logger.info(msg))
        self._pending_logs: list[str] = []

    def _emit(self, msg: str):
        self.on_progress(msg)

    def _log_later(self, msg: str):
        """延迟日志，不在操作过程中触发（避免抢焦点）"""
        self._pending_logs.append(msg)
        logger.info(msg)

    def _flush_logs(self):
        """一次性输出所有延迟日志"""
        for msg in self._pending_logs:
            self._emit(msg)
        self._pending_logs.clear()

    def run(self, sticker_paths: list[Path], auto_save: bool = True) -> int:
        """
        将表情包通过聊天窗口发送并保存到微信「我的表情」

        Args:
            sticker_paths: 表情包图片路径列表
            auto_save: 是否自动右键保存到表情

        Returns:
            成功处理的数量
        """
        if not sticker_paths:
            self._emit("没有需要导入的表情包")
            return 0

        valid_paths = [p for p in sticker_paths if p.exists() and p.stat().st_size > 0]
        if not valid_paths:
            self._emit("没有有效的表情包文件")
            return 0

        # 1. 转换 WebP 为 GIF（微信 PC 不支持动态 WebP）
        converted_paths = []
        for p in valid_paths:
            if p.suffix.lower() == '.webp':
                gif_path = self._convert_webp_to_gif(p)
                if gif_path:
                    converted_paths.append(gif_path)
                else:
                    converted_paths.append(p)
            else:
                converted_paths.append(p)

        valid_paths = converted_paths

        # 2. 压缩到微信表情包要求（≤500KB, ≤240x240）
        compressed_paths = []
        for p in valid_paths:
            cp = self._compress_for_wechat(p)
            compressed_paths.append(cp)

        valid_paths = compressed_paths
        self._emit(f"共 {len(valid_paths)} 个表情包待处理")

        try:
            if not self._focus_wechat():
                return 0

            self._emit("请确认已打开一个聊天窗口（给自己发消息的窗口最佳）")
            self._emit("3 秒后开始发送...")
            time.sleep(3)

            imported = 0
            for i, path in enumerate(valid_paths):
                # 每次发送前重新聚焦微信，防止焦点被 GUI 抢走
                self._refocus_wechat()

                ok = self._send_one(path, i + 1, len(valid_paths))
                if ok:
                    imported += 1

                # 日志在操作间隙安全输出
                self._flush_logs()
                time.sleep(0.5)

            self._flush_logs()
            self._emit(f"处理完成: {imported}/{len(valid_paths)}")
            return imported

        except pyautogui.FailSafeException:
            self._emit("操作被用户中断（鼠标移到屏幕左上角）")
            return 0
        except Exception as e:
            self._emit(f"导入出错: {e}")
            logger.exception("导入表情失败")
            return 0

    def _send_one(self, path: Path, current: int, total: int) -> bool:
        """发送单个图片到聊天窗口"""
        try:
            # 1. 复制图片到剪贴板
            self._copy_image_to_clipboard(path)
            time.sleep(0.3)

            # 2. 粘贴到输入框
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.5)

            # 3. 发送
            pyautogui.press("enter")
            time.sleep(1.0)

            self._log_later(f"[{current}/{total}] 已发送: {path.name}")
            return True

        except Exception as e:
            self._log_later(f"[{current}/{total}] 失败: {e}")
            return False

    def _send_file(self, path: Path, current: int, total: int) -> bool:
        """通过文件传输方式发送（保留 GIF 动画）"""
        try:
            ps_cmd = (
                f'Add-Type -AssemblyName System.Windows.Forms; '
                f'$collection = New-Object System.Collections.Specialized.StringCollection; '
                f'$collection.Add("{path.resolve()}"); '
                f'[System.Windows.Forms.Clipboard]::SetFileDropList($collection)'
            )
            subprocess.run(["powershell", "-Command", ps_cmd], capture_output=True, timeout=10)
            time.sleep(0.3)

            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.5)

            pyautogui.press("enter")
            time.sleep(1.0)

            self._log_later(f"[{current}/{total}] 已发送文件: {path.name}")
            return True

        except Exception as e:
            self._log_later(f"[{current}/{total}] 文件发送失败: {e}")
            return False

    def _convert_webp_to_gif(self, webp_path: Path) -> Path | None:
        """将动态 WebP 转换为 GIF（微信 PC 不支持动态 WebP）"""
        try:
            gif_path = webp_path.with_suffix('.gif')
            if gif_path.exists():
                return gif_path

            img = Image.open(webp_path)
            frames = []
            durations = []

            # 提取所有帧
            try:
                while True:
                    frame = img.copy().convert('RGBA')
                    frames.append(frame)
                    duration = img.info.get('duration', 100)
                    durations.append(max(duration, 20))  # 最小 20ms
                    img.seek(img.tell() + 1)
            except EOFError:
                pass

            if len(frames) <= 1:
                # 静态图片，转为 GIF（微信自定义表情只支持 GIF）
                gif_path = webp_path.with_suffix('.gif')
                img_rgb = frames[0].convert('RGB')
                img_rgb.save(gif_path, 'GIF', optimize=True)
                return gif_path

            # 动态图片，转 GIF
            frames[0].save(
                gif_path,
                save_all=True,
                append_images=frames[1:],
                duration=durations,
                loop=0,
                disposal=2,
            )
            self._emit(f"转换: {webp_path.name} -> {gif_path.name} ({len(frames)}帧)")
            return gif_path

        except Exception as e:
            self._emit(f"转换 WebP 失败: {e}")
            return None

    def _compress_for_wechat(self, path: Path) -> Path:
        """压缩图片到微信表情包要求: ≤500KB, ≤320x320, 必须是 GIF 格式"""
        MAX_SIZE = 500 * 1024  # 500KB
        MAX_DIM = 320

        ext = path.suffix.lower()
        is_gif = ext == '.gif'

        try:
            img = Image.open(path)

            if is_gif:
                return self._compress_gif(img, path, MAX_DIM, MAX_SIZE)
            else:
                # 静态图：转为 GIF 再压缩（微信自定义表情只支持 GIF）
                return self._static_to_gif(img, path, MAX_DIM, MAX_SIZE)

        except Exception as e:
            self._emit(f"压缩失败，使用原图: {e}")
            return path

    def _compress_gif(self, img, path: Path, max_dim: int, max_size: int) -> Path:
        """压缩 GIF 动图，保持播放速度"""
        # 提取所有帧和时长
        frames = []
        durations = []
        try:
            while True:
                frame = img.copy().convert('RGBA')
                frames.append(frame)
                durations.append(img.info.get('duration', 100))
                img.seek(img.tell() + 1)
        except EOFError:
            pass

        if not frames:
            return path

        # 缩小尺寸
        out_path = path.parent / f"wx_{path.name}.gif"
        w, h = frames[0].size
        if w > max_dim or h > max_dim:
            ratio = min(max_dim / w, max_dim / h)
            new_size = (int(w * ratio), int(h * ratio))
            frames = [f.resize(new_size, Image.LANCZOS) for f in frames]

        # 逐步降低质量直到文件大小合格（从高质量开始，尽量保持画质）
        for quality in [95, 85, 75, 65, 55]:
            frames[0].save(
                out_path,
                save_all=True,
                append_images=frames[1:],
                duration=durations,
                loop=0,
                disposal=2,
                optimize=True,
            )
            if out_path.stat().st_size <= max_size:
                self._emit(f"压缩 GIF: {path.name} -> {out_path.name} ({out_path.stat().st_size//1024}KB)")
                return out_path

        # 保底：轻微减少帧数，保持总时长不变（更保守，保留更多帧）
        step = 3
        while len(frames) > 3:
            new_frames = frames[::step]
            new_durations = []
            for i in range(0, len(durations), step):
                chunk = durations[i:i + step]
                new_durations.append(max(sum(chunk), 20))
            frames = new_frames
            durations = new_durations
            frames[0].save(
                out_path,
                save_all=True,
                append_images=frames[1:],
                duration=durations,
                loop=0,
                disposal=2,
                optimize=True,
            )
            if out_path.stat().st_size <= max_size:
                self._emit(f"压缩 GIF: {path.name} -> {out_path.name} ({out_path.stat().st_size//1024}KB, {len(frames)}帧)")
                return out_path
            step += 1

        self._emit(f"GIF 压缩后仍 {out_path.stat().st_size//1024}KB，尝试发送")
        return out_path

    def _static_to_gif(self, img, path: Path, max_dim: int, max_size: int) -> Path:
        """将静态图片转为 GIF（微信自定义表情只支持 GIF 格式）"""
        out_path = path.parent / f"wx_{path.name}.gif"

        # 缩小尺寸
        w, h = img.size
        if w > max_dim or h > max_dim:
            ratio = min(max_dim / w, max_dim / h)
            new_size = (int(w * ratio), int(h * ratio))
            img = img.resize(new_size, Image.LANCZOS)

        # 转为 RGB
        if img.mode in ('RGBA', 'P', 'LA', 'PA'):
            bg = Image.new('RGB', img.size, (255, 255, 255))
            if img.mode == 'P':
                img = img.convert('RGBA')
            if 'A' in img.mode or 'a' in img.mode:
                bg.paste(img, mask=img.split()[-1])
            else:
                bg.paste(img)
            img = bg
        elif img.mode != 'RGB':
            img = img.convert('RGB')

        # 保存为 GIF（静态 GIF，一帧）
        img.save(out_path, 'GIF', optimize=True)
        self._emit(f"静态转 GIF: {path.name} -> {out_path.name} ({out_path.stat().st_size//1024}KB)")
        return out_path

    def _refocus_wechat(self):
        """静默重新聚焦微信窗口（不输出日志，不抢焦点）"""
        try:
            hwnd = ctypes.windll.user32.FindWindowW(None, "微信")
            if hwnd:
                ctypes.windll.user32.SetForegroundWindow(hwnd)
                time.sleep(0.3)
        except Exception:
            pass

    def _focus_wechat(self) -> bool:
        """聚焦微信窗口（带日志输出）"""
        self._emit("正在查找微信窗口...")

        try:
            hwnd = ctypes.windll.user32.FindWindowW(None, "微信")
            if not hwnd:
                hwnd = self._find_window_by_title("微信")
            if hwnd:
                ctypes.windll.user32.SetForegroundWindow(hwnd)
                time.sleep(0.5)
                self._emit("已聚焦微信窗口")
                return True
        except Exception as e:
            logger.debug(f"Windows API 聚焦失败: {e}")

        try:
            import pygetwindow as gw
            windows = gw.getWindowsWithTitle("微信")
            if windows:
                windows[0].activate()
                time.sleep(0.5)
                self._emit("已聚焦微信窗口")
                return True
        except Exception:
            pass

        self._emit("未找到微信窗口，请确保微信已打开")
        return False

    def _find_window_by_title(self, title: str):
        result = []

        def callback(hwnd, _):
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
                if title in buf.value:
                    result.append(hwnd)
            return True

        ENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
        ctypes.windll.user32.EnumWindows(ENUMPROC(callback), 0)
        return result[0] if result else None

    @staticmethod
    def _copy_image_to_clipboard(image_path: Path):
        """将图片复制到 Windows 剪贴板（统一用文件方式，保留 GIF 动画）"""
        try:
            ps_cmd = (
                f'Add-Type -AssemblyName System.Windows.Forms; '
                f'$collection = New-Object System.Collections.Specialized.StringCollection; '
                f'$collection.Add("{image_path.resolve()}"); '
                f'[System.Windows.Forms.Clipboard]::SetFileDropList($collection)'
            )
            subprocess.run(
                ["powershell", "-Command", ps_cmd],
                capture_output=True, timeout=10
            )
        except Exception:
            pass


def import_to_wechat(
    sticker_paths: list[Path],
    on_progress=None,
) -> int:
    """便捷入口函数"""
    importer = WeChatStickerImporter(on_progress=on_progress)
    return importer.run(sticker_paths)
