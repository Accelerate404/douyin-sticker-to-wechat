"""
电脑版微信自动发送模块（纯 Windows API ctypes 实现，零额外依赖）
无需连接手机、无需手机权限，直接将转换好的 GIF 表情包粘贴发送到电脑版微信聊天窗口
用户在聊天窗口中右键表情即可「添加到表情」（支持停止按钮和 ESC 键随时中断）
"""
import ctypes
import logging
import struct
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Callable

import config

logger = logging.getLogger(__name__)

# Win32 常量
CF_HDROP = 15
GMEM_MOVEABLE = 0x0002
SW_RESTORE = 9
VK_CONTROL = 0x11
VK_V = 0x56
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B
KEYEVENTF_KEYUP = 0x0002

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# 显式声明 64 位安全的 Win32 函数签名，防止指针截断
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
kernel32.GlobalFree.restype = ctypes.c_void_p

user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = wintypes.BOOL
user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
user32.SetClipboardData.restype = ctypes.c_void_p
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wintypes.BOOL

user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]


def _get_window_text(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _get_class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def find_wechat_window(exclude_title: str = "抖音表情包") -> int | None:
    """查找可见的电脑版微信主窗口句柄"""
    candidates_exact: list[int] = []
    candidates_fuzzy: list[int] = []

    ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _get_window_text(hwnd)
        if not title or (exclude_title and exclude_title in title):
            return True
        cls = _get_class_name(hwnd)
        if cls in ("WeChatMainWndForPC", "Qt51514QWindowIcon") and ("微信" in title or "WeChat" in title):
            candidates_exact.append(hwnd)
        elif title == "微信" or title == "WeChat" or "微信" in title or "WeChat" in title:
            candidates_fuzzy.append(hwnd)
        return True

    user32.EnumWindows(ENUMPROC(_callback), 0)
    if candidates_exact:
        return candidates_exact[0]
    return candidates_fuzzy[0] if candidates_fuzzy else None


def focus_wechat_window(hwnd: int) -> bool:
    """还原并置顶激活电脑版微信窗口"""
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
            time.sleep(0.2)
        return bool(user32.SetForegroundWindow(hwnd))
    except Exception:
        return False


def _build_hdrop_bytes(file_path: Path) -> bytes:
    """构造 Win32 CF_HDROP (DROPFILES + UTF-16LE 双空字符结尾路径) 内存字节"""
    resolved = str(file_path.resolve())
    header = struct.pack("<IiiII", 20, 0, 0, 0, 1)
    payload = resolved.encode("utf-16-le") + b"\x00\x00\x00\x00"
    return header + payload


def copy_file_to_clipboard(file_path: Path) -> None:
    """通过 Win32 CF_HDROP 将本地文件写入系统剪贴板（完整保留 GIF 动画）"""
    data = _build_hdrop_bytes(file_path)
    h_global = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not h_global:
        raise RuntimeError("GlobalAlloc 分配剪贴板内存失败")

    ptr = kernel32.GlobalLock(h_global)
    if not ptr:
        kernel32.GlobalFree(h_global)
        raise RuntimeError("GlobalLock 锁定剪贴板内存失败")
    try:
        ctypes.memmove(ptr, data, len(data))
    finally:
        kernel32.GlobalUnlock(h_global)

    opened = False
    for _ in range(5):
        if user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(0.05)

    if not opened:
        kernel32.GlobalFree(h_global)
        raise RuntimeError("无法打开系统剪贴板（可能正被其他程序占用）")

    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_HDROP, h_global):
            kernel32.GlobalFree(h_global)
            raise RuntimeError("SetClipboardData 写入 CF_HDROP 失败")
    finally:
        user32.CloseClipboard()


def _press_ctrl_v() -> None:
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    time.sleep(0.04)
    user32.keybd_event(VK_V, 0, 0, 0)
    time.sleep(0.04)
    user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.04)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)


def _press_enter() -> None:
    user32.keybd_event(VK_RETURN, 0, 0, 0)
    time.sleep(0.04)
    user32.keybd_event(VK_RETURN, 0, KEYEVENTF_KEYUP, 0)


def _is_esc_pressed() -> bool:
    return bool(user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000)


def _sleep_interruptible(seconds: float, cancel_event: threading.Event | None) -> bool:
    """可随时被取消事件或 ESC 键中断的等待，返回 True 表示被中断"""
    end = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < end:
        if (cancel_event and cancel_event.is_set()) or _is_esc_pressed():
            return True
        time.sleep(min(0.1, max(0.01, end - time.monotonic())))
    return bool((cancel_event and cancel_event.is_set()) or _is_esc_pressed())


def send_stickers_to_wechat(
    paths: list[Path],
    skip_hashes: set[str] | None = None,
    send_delay: float = config.WECHAT_SEND_DELAY,
    cancel_event: threading.Event | None = None,
    on_progress: Callable[[str], None] | None = None,
    on_progress_pct: Callable[[int, int], None] | None = None,
    on_sent: Callable[[Path], None] | None = None,
) -> int:
    """
    依次将转换后的表情包粘贴发送到当前打开的电脑版微信聊天窗口。
    返回成功发送数量。
    """
    emit = on_progress or (lambda msg: logger.info(msg))
    valid_paths = [p for p in paths if p.exists() and p.stat().st_size > 0]
    if not valid_paths:
        emit("没有可发送的有效表情文件")
        return 0

    skipped = 0
    if skip_hashes:
        filtered: list[Path] = []
        for p in valid_paths:
            key = p.stem.removeprefix("wx_")
            if key in skip_hashes:
                skipped += 1
            else:
                filtered.append(p)
        valid_paths = filtered

    if skipped:
        emit(f"已跳过历史已发送表情 {skipped} 个，待发送 {len(valid_paths)} 个")

    if not valid_paths:
        emit("所有表情都已发送过，无需重复发送")
        return 0

    hwnd = find_wechat_window()
    if not hwnd:
        raise RuntimeError(
            "未找到电脑版微信窗口！请先打开电脑微信，并进入「文件传输助手」或任意聊天窗口。"
        )

    focus_wechat_window(hwnd)
    emit("已聚焦电脑版微信！请确保已打开目标聊天窗口（建议用「文件传输助手」），3 秒后开始发送...")
    emit("提示：发送期间可随时点击「停止」按钮或按键盘 ESC 键立即中断")

    if _sleep_interruptible(3.0, cancel_event):
        emit("发送已取消")
        return 0

    total = len(valid_paths)
    sent_count = 0

    for idx, path in enumerate(valid_paths, start=1):
        if (cancel_event and cancel_event.is_set()) or _is_esc_pressed():
            emit(f"发送已停止（已发送 {sent_count}/{total}）")
            break

        focus_wechat_window(hwnd)
        try:
            copy_file_to_clipboard(path)
            if _sleep_interruptible(0.2, cancel_event):
                emit(f"发送已停止（已发送 {sent_count}/{total}）")
                break

            _press_ctrl_v()
            if _sleep_interruptible(0.35, cancel_event):
                emit(f"发送已停止（已发送 {sent_count}/{total}）")
                break

            _press_enter()
            sent_count += 1
            if on_sent:
                on_sent(path)
            emit(f"[{idx}/{total}] 已发送: {path.name}")
        except Exception as e:
            emit(f"[{idx}/{total}] 发送失败 {path.name}: {e}")

        if on_progress_pct:
            on_progress_pct(idx, total)

        if idx < total and _sleep_interruptible(send_delay, cancel_event):
            emit(f"发送已停止（已发送 {sent_count}/{total}）")
            break

    if not ((cancel_event and cancel_event.is_set()) or _is_esc_pressed()):
        emit(f"发送完成: 共 {sent_count}/{total} 个表情包！请在电脑微信聊天窗口右键表情 →「添加到表情」")

    return sent_count
