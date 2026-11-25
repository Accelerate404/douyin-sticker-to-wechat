"""
抖音表情包 → 微信我的表情
主程序入口（GUI 模式）
"""
import asyncio
import logging
import os
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

import config

logging.basicConfig(
    level=config.LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(config.PROJECT_DIR / "app.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


class StickerTransferApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("抖音表情包 → 微信我的表情")
        self.root.geometry("830x680")
        self.root.resizable(True, True)

        self.sticker_paths: list[Path] = []
        self.is_running = False
        self.scraper = None
        self._cancel_event = threading.Event()

        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._show_welcome()

    def _show_welcome(self):
        """在日志区展示简洁引导、历史统计和本地已有表情统计"""
        self._log("欢迎使用「抖音表情包 → 微信我的表情」（电脑版微信直发，无需连接手机）")
        self._log("快速上手：1) 点「打开浏览器」扫码登录抖音 → 2) 打开聊天表情面板点「开始抓取」→ 3) 打开电脑微信聊天窗口点「发送到电脑微信」")
        hist = config.load_history()
        if hist["scraped"] or hist["sent"]:
            self._log(f"历史记录：已抓取 {len(hist['scraped'])} 个，已发送 {len(hist['sent'])} 个（默认自动跳过重复项）")
        if config.STICKER_DIR.exists():
            existing = [
                p for p in config.STICKER_DIR.iterdir()
                if p.is_file() and not p.name.startswith("wx_")
            ]
            if existing:
                self._log(f"检测到本地 downloaded_stickers/ 已有 {len(existing)} 个原始表情，可直接点「手动选择本地表情包」进行转换或发送。")

    def _open_dir(self, path: Path):
        """安全打开本地目录"""
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))
        except Exception as e:
            self._log(f"无法打开目录 {path}: {e}")

    def _ensure_loop(self):
        """确保后台事件循环在运行"""
        if self._loop and self._loop.is_running():
            return
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever, daemon=True
        )
        self._loop_thread.start()

    def _run_async(self, coro, timeout: float | None = None):
        """从工作线程提交协程到后台事件循环"""
        self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def _build_ui(self):
        tk.Label(
            self.root,
            text="抖音喜欢表情包 → 电脑版微信我的表情",
            font=("Microsoft YaHei", 14, "bold"),
        ).pack(pady=8)

        tk.Label(
            self.root,
            text="步骤：打开浏览器 → 开始抓取(可设数量上限/跳过已抓) → 转换并发送到电脑微信聊天窗口",
            font=("Microsoft YaHei", 9),
            fg="gray",
        ).pack(pady=(0, 5))

        btn_frame = tk.Frame(self.root)
        btn_frame.pack(pady=5, fill=tk.X, padx=20)

        self.btn_open = tk.Button(
            btn_frame,
            text="1. 打开浏览器",
            font=("Microsoft YaHei", 11),
            width=14,
            height=2,
            command=self._start_open_browser,
        )
        self.btn_open.pack(side=tk.LEFT, padx=3)

        self.btn_scrape = tk.Button(
            btn_frame,
            text="2. 开始抓取",
            font=("Microsoft YaHei", 11),
            width=14,
            height=2,
            command=self._start_scrape,
            state=tk.DISABLED,
        )
        self.btn_scrape.pack(side=tk.LEFT, padx=3)

        self.btn_convert = tk.Button(
            btn_frame,
            text="仅转换测试",
            font=("Microsoft YaHei", 11),
            width=12,
            height=2,
            command=self._start_convert_only,
            state=tk.DISABLED,
        )
        self.btn_convert.pack(side=tk.LEFT, padx=3)

        self.btn_send = tk.Button(
            btn_frame,
            text="3. 发送到电脑微信",
            font=("Microsoft YaHei", 11),
            width=16,
            height=2,
            command=self._start_send_wechat,
            state=tk.DISABLED,
        )
        self.btn_send.pack(side=tk.LEFT, padx=3)

        self.btn_stop = tk.Button(
            btn_frame,
            text="停止",
            font=("Microsoft YaHei", 11, "bold"),
            fg="red",
            width=8,
            height=2,
            command=self._request_stop,
            state=tk.DISABLED,
        )
        self.btn_stop.pack(side=tk.LEFT, padx=3)

        # 选项栏：画质预设、抓取上限、跳过历史、强制重转
        opt_frame = tk.Frame(self.root)
        opt_frame.pack(pady=4, fill=tk.X, padx=20)

        tk.Label(opt_frame, text="压缩预设:", font=("Microsoft YaHei", 9)).pack(side=tk.LEFT)
        self.preset_var = tk.StringVar(value=config.PRESET_HD["label"])
        self.preset_combo = ttk.Combobox(
            opt_frame,
            textvariable=self.preset_var,
            values=[config.PRESET_HD["label"], config.PRESET_COMPAT["label"]],
            state="readonly",
            width=22,
        )
        self.preset_combo.pack(side=tk.LEFT, padx=(4, 10))

        tk.Label(opt_frame, text="抓取上限(0=全部):", font=("Microsoft YaHei", 9)).pack(side=tk.LEFT)
        self.limit_var = tk.StringVar(value=str(config.DEFAULT_SCRAPE_LIMIT))
        self.limit_spin = tk.Spinbox(
            opt_frame,
            from_=0,
            to=999,
            width=5,
            textvariable=self.limit_var,
            font=("Microsoft YaHei", 9),
        )
        self.limit_spin.pack(side=tk.LEFT, padx=(4, 10))

        self.skip_history_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            opt_frame,
            text="跳过已抓取/已发送",
            variable=self.skip_history_var,
            font=("Microsoft YaHei", 9),
        ).pack(side=tk.LEFT, padx=4)

        self.force_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            opt_frame,
            text="强制重新转换",
            variable=self.force_var,
            font=("Microsoft YaHei", 9),
        ).pack(side=tk.LEFT, padx=4)

        debug_frame = tk.Frame(self.root)
        debug_frame.pack(pady=2, fill=tk.X, padx=20)
        self.btn_debug = tk.Button(
            debug_frame,
            text="页面诊断",
            font=("Microsoft YaHei", 9),
            width=12,
            command=self._start_debug,
            state=tk.DISABLED,
        )
        self.btn_debug.pack(side=tk.LEFT)
        tk.Label(
            debug_frame,
            text="(如自动抓取失败，点此 dump 页面结构；发送微信期间可按 ESC 随时中断)",
            font=("Microsoft YaHei", 8),
            fg="gray",
        ).pack(side=tk.LEFT, padx=5)

        ttk.Separator(self.root, orient="horizontal").pack(fill=tk.X, padx=20, pady=5)

        status_frame = tk.Frame(self.root)
        status_frame.pack(fill=tk.X, padx=20)
        self.status_var = tk.StringVar(value="就绪")
        tk.Label(status_frame, text="状态:", font=("Microsoft YaHei", 10)).pack(side=tk.LEFT)
        tk.Label(status_frame, textvariable=self.status_var, font=("Microsoft YaHei", 10)).pack(
            side=tk.LEFT, padx=5
        )
        self.count_var = tk.StringVar(value="0 个表情包")
        tk.Label(
            status_frame,
            textvariable=self.count_var,
            font=("Microsoft YaHei", 10, "bold"),
            fg="blue",
        ).pack(side=tk.RIGHT)

        self.progress = ttk.Progressbar(self.root, mode="indeterminate")
        self.progress.pack(fill=tk.X, padx=20, pady=5)

        log_frame = tk.LabelFrame(self.root, text="运行日志", font=("Microsoft YaHei", 10))
        log_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=5)
        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=14, font=("Consolas", 9), state=tk.DISABLED
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

        bottom = tk.Frame(self.root)
        bottom.pack(fill=tk.X, padx=20, pady=6)
        tk.Button(
            bottom,
            text="手动选择本地表情包",
            font=("Microsoft YaHei", 10),
            command=self._select_local,
        ).pack(side=tk.LEFT)
        tk.Button(
            bottom,
            text="打开下载目录",
            font=("Microsoft YaHei", 10),
            command=lambda: self._open_dir(config.STICKER_DIR),
        ).pack(side=tk.LEFT, padx=8)
        tk.Button(
            bottom,
            text="打开转换目录",
            font=("Microsoft YaHei", 10),
            command=lambda: self._open_dir(config.CONVERTED_DIR),
        ).pack(side=tk.LEFT, padx=2)
        tk.Button(
            bottom,
            text="断开浏览器",
            font=("Microsoft YaHei", 10),
            command=self._close_browser,
        ).pack(side=tk.LEFT, padx=6)
        tk.Button(
            bottom,
            text="清空下载与历史",
            font=("Microsoft YaHei", 10),
            command=self._clear_downloads,
        ).pack(side=tk.LEFT, padx=4)
        tk.Button(
            bottom,
            text="清空日志",
            font=("Microsoft YaHei", 10),
            command=self._clear_log,
        ).pack(side=tk.RIGHT)

    def _get_selected_preset(self) -> dict:
        if self.preset_var.get() == config.PRESET_COMPAT["label"]:
            return config.PRESET_COMPAT
        return config.PRESET_HD

    def _get_scrape_limit(self) -> int:
        try:
            val = int(self.limit_var.get().strip() or "0")
            return max(0, val)
        except ValueError:
            self.limit_var.set("0")
            return 0

    def _log(self, msg: str):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)
        logger.info(msg)

    def _clear_log(self):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _set_progress_pct(self, cur: int, total: int):
        self.progress.stop()
        self.progress.config(mode="determinate", maximum=max(1, total), value=cur)

    def _set_running(self, running: bool, determinate: bool = False):
        self.is_running = running
        self.btn_open.config(state=tk.NORMAL if (not running and not self.scraper) else tk.DISABLED)
        self.btn_scrape.config(state=tk.NORMAL if (not running and self.scraper) else tk.DISABLED)
        has_stickers = bool(self.sticker_paths)
        self.btn_convert.config(state=tk.NORMAL if (not running and has_stickers) else tk.DISABLED)
        self.btn_send.config(state=tk.NORMAL if (not running and has_stickers) else tk.DISABLED)
        self.btn_debug.config(state=tk.NORMAL if (not running and self.scraper) else tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL if running else tk.DISABLED)
        if running:
            if determinate:
                self.progress.stop()
                self.progress.config(mode="determinate", maximum=100, value=0)
            else:
                self.progress.config(mode="indeterminate")
                self.progress.start(10)
        else:
            self.progress.stop()

    def _request_stop(self):
        """请求停止当前运行中的抓取、下载、转换或发送"""
        if not self.is_running:
            return
        self._cancel_event.set()
        if self.scraper:
            self.scraper.cancel()
        self.status_var.set("正在停止...")
        self._log("已请求停止当前任务，请稍候...")

    # ========== 1. 打开浏览器 ==========
    def _start_open_browser(self):
        if self.is_running:
            return
        self._set_running(True, determinate=False)
        self.status_var.set("正在连接 Edge 浏览器...")

        def run():
            try:
                from douyin_scraper import DouyinStickerScraper

                self.scraper = DouyinStickerScraper(
                    on_progress=lambda msg: self.root.after(0, self._log, msg),
                )
                ok = self._run_async(self.scraper.open_edge())
                if not ok:
                    self.scraper = None
                    self.root.after(0, self.status_var.set, "连接失败")
                else:
                    self.root.after(0, self.status_var.set, "已连接 Edge，等待操作")
            except Exception as e:
                self.scraper = None
                self.root.after(0, self._log, f"错误: {e}")
                self.root.after(0, self.status_var.set, "连接失败")
            finally:
                self.root.after(0, self._set_running, False)

        threading.Thread(target=run, daemon=True).start()

    # ========== 2. 开始抓取 ==========
    def _start_scrape(self):
        if self.is_running or not self.scraper:
            return
        self._cancel_event.clear()
        self.scraper.reset_cancel()
        max_count = self._get_scrape_limit()

        skip_hashes: set[str] | None = None
        if self.skip_history_var.get():
            hist = config.load_history()
            existing_stems = (
                {
                    p.stem
                    for p in config.STICKER_DIR.iterdir()
                    if p.is_file() and not p.name.startswith("wx_")
                }
                if config.STICKER_DIR.exists()
                else set()
            )
            skip_hashes = set(hist.get("scraped", [])) | existing_stems

        self._set_running(True, determinate=False)
        self.status_var.set("正在抓取表情包...")

        def run():
            try:
                self._run_async(
                    self.scraper.scrape_from_chat_panel(
                        max_count=max_count,
                        skip_hashes=skip_hashes,
                    )
                )
                if self._cancel_event.is_set():
                    self.root.after(0, self.status_var.set, "抓取已停止")
                    return

                if not self.scraper.last_panel_detected:
                    self.root.after(0, self.status_var.set, "未检测到表情面板，请先打开表情面板")
                    self.root.after(
                        0,
                        lambda: messagebox.showwarning(
                            "未检测到表情面板",
                            "请先在浏览器中：\n"
                            "1. 打开任意聊天窗口 / 私信\n"
                            "2. 点击输入框旁的表情按钮\n"
                            "3. 切换到「红心 / 收藏」表情页\n\n"
                            "看到表情包后再点击「开始抓取」。",
                        ),
                    )
                    return

                if not self.scraper.sticker_urls:
                    self.root.after(0, self.status_var.set, "当前面板表情均已抓取过（未新增下载）")
                    return

                self.root.after(0, self.status_var.set, "正在下载表情包...")
                self._run_async(
                    self.scraper.download_stickers(
                        on_progress_pct=lambda c, t: self.root.after(0, self._set_progress_pct, c, t)
                    )
                )
                self.sticker_paths = self.scraper.sticker_paths

                if self._cancel_event.is_set():
                    self.root.after(0, self.status_var.set, f"已停止（已下载 {len(self.sticker_paths)} 个）")
                else:
                    self.root.after(0, self._log, f"抓取完成，共 {len(self.sticker_paths)} 个表情包")
                    self.root.after(0, self.status_var.set, "抓取完成")
                self.root.after(0, self._update_sticker_count)
            except Exception as e:
                self.root.after(0, self._log, f"错误: {e}")
                self.root.after(0, self.status_var.set, "抓取失败")
            finally:
                self.root.after(0, self._set_running, False)

        threading.Thread(target=run, daemon=True).start()

    def _update_sticker_count(self):
        self.count_var.set(f"{len(self.sticker_paths)} 个表情包")

    # ========== 仅转换测试 ==========
    def _start_convert_only(self):
        if self.is_running or not self.sticker_paths:
            return
        self._cancel_event.clear()
        preset = self._get_selected_preset()
        force = self.force_var.get()
        self._set_running(True, determinate=True)
        self.status_var.set("正在转换压缩表情包...")

        def run():
            try:
                from sticker_converter import batch_convert

                self.root.after(0, self._log, f"使用预设: {preset['label']}")
                converted, stats = batch_convert(
                    self.sticker_paths,
                    output_dir=config.CONVERTED_DIR,
                    max_size=preset["max_size"],
                    max_dim=preset["max_dim"],
                    force=force,
                    cancel_event=self._cancel_event,
                    on_progress=lambda msg: self.root.after(0, self._log, msg),
                    on_progress_pct=lambda c, t: self.root.after(0, self._set_progress_pct, c, t),
                )
                if stats.get("cancelled"):
                    self.root.after(0, self.status_var.set, f"转换已停止 ({len(converted)}/{stats['total']})")
                else:
                    self.root.after(
                        0,
                        self.status_var.set,
                        f"转换完成: {len(converted)}/{stats['total']} (输出目录: converted_stickers)",
                    )
            except Exception as e:
                self.root.after(0, self._log, f"错误: {e}")
                self.root.after(0, self.status_var.set, "转换失败")
            finally:
                self.root.after(0, self._set_running, False)

        threading.Thread(target=run, daemon=True).start()

    # ========== 3. 转换并发送到电脑版微信 ==========
    def _start_send_wechat(self):
        if self.is_running or not self.sticker_paths:
            return
        self._cancel_event.clear()
        preset = self._get_selected_preset()
        force = self.force_var.get()
        skip_hashes = (
            set(config.load_history().get("sent", []))
            if self.skip_history_var.get()
            else None
        )
        self._set_running(True, determinate=True)
        self.status_var.set("正在转换并发送到电脑微信...")

        def run():
            try:
                import wechat_sender
                from sticker_converter import batch_convert

                self.root.after(0, self._log, f"正在转换表情包格式（{preset['label']}）...")
                converted, stats = batch_convert(
                    self.sticker_paths,
                    output_dir=config.CONVERTED_DIR,
                    max_size=preset["max_size"],
                    max_dim=preset["max_dim"],
                    force=force,
                    cancel_event=self._cancel_event,
                    on_progress=lambda msg: self.root.after(0, self._log, msg),
                    on_progress_pct=lambda c, t: self.root.after(0, self._set_progress_pct, c, t),
                )
                if self._cancel_event.is_set() or stats.get("cancelled"):
                    self.root.after(0, self.status_var.set, "发送已停止")
                    return
                if not converted:
                    self.root.after(0, self._log, "没有可发送的转换文件")
                    self.root.after(0, self.status_var.set, "发送取消")
                    return

                self.root.after(0, self.status_var.set, "正在发送到电脑微信（按 ESC 或点停止可中断）...")
                self.root.after(0, self._set_progress_pct, 0, len(converted))
                count = wechat_sender.send_stickers_to_wechat(
                    converted,
                    skip_hashes=skip_hashes,
                    cancel_event=self._cancel_event,
                    on_progress=lambda msg: self.root.after(0, self._log, msg),
                    on_progress_pct=lambda c, t: self.root.after(0, self._set_progress_pct, c, t),
                    on_sent=lambda p: config.record_history("sent", [p.stem.removeprefix("wx_")]),
                )
                if self._cancel_event.is_set():
                    self.root.after(0, self.status_var.set, f"发送已停止（已发 {count} 个）")
                else:
                    self.root.after(0, self.status_var.set, f"发送完成（共 {count} 个）")
            except Exception as e:
                self.root.after(0, self._log, f"错误: {e}")
                self.root.after(0, self.status_var.set, "发送失败")
            finally:
                self.root.after(0, self._set_running, False)

        threading.Thread(target=run, daemon=True).start()

    # ========== 调试 ==========
    def _start_debug(self):
        if self.is_running or not self.scraper:
            return
        self._set_running(True, determinate=False)
        self.status_var.set("正在诊断页面...")

        def run():
            try:
                self._run_async(self.scraper.dump_page_structure())
                self.root.after(0, self.status_var.set, "诊断完成，结果保存在 page_dump.json")
            except Exception as e:
                self.root.after(0, self._log, f"诊断错误: {e}")
                self.root.after(0, self.status_var.set, "诊断失败")
            finally:
                self.root.after(0, self._set_running, False)

        threading.Thread(target=run, daemon=True).start()

    def _clear_downloads(self):
        """清空下载目录、转换目录和抓取/发送历史"""
        if self.is_running:
            messagebox.showinfo("提示", "请先点击「停止」再清空目录。")
            return
        if not messagebox.askyesno(
            "确认清空",
            "将删除 downloaded_stickers/、converted_stickers/ 中的图片，并重置已抓取/已发送历史记录。\n确定继续吗？",
        ):
            return

        removed = 0
        locked: list[str] = []
        for folder in (config.STICKER_DIR, config.CONVERTED_DIR):
            if not folder.exists():
                continue
            for item in folder.iterdir():
                if item.is_file():
                    try:
                        item.unlink()
                        removed += 1
                    except OSError:
                        locked.append(item.name)

        config.HISTORY_FILE.unlink(missing_ok=True)
        self.sticker_paths = []
        self._update_sticker_count()
        self._set_running(self.is_running)
        self._log(f"已清理 {removed} 个文件并重置历史记录")
        if locked:
            self._log(f"有 {len(locked)} 个文件正被系统预览占用（如资源管理器 COM Surrogate），请关闭文件夹预览窗格后重试")

    def _select_local(self):
        files = filedialog.askopenfilenames(
            title="选择表情包图片",
            filetypes=[("图片", "*.png *.jpg *.jpeg *.gif *.webp"), ("所有", "*.*")],
            initialdir=str(config.STICKER_DIR),
        )
        if files:
            self.sticker_paths = [Path(f) for f in files]
            self._update_sticker_count()
            self._set_running(self.is_running)
            self._log(f"已选择 {len(self.sticker_paths)} 个本地文件")

    def _close_browser(self):
        if not self.scraper:
            return
        scraper = self.scraper
        self.scraper = None
        self._set_running(self.is_running)

        def cleanup():
            try:
                self._run_async(scraper.aclose(), timeout=5)
            except Exception:
                scraper.close()
            self.root.after(0, self._log, "已断开浏览器连接")

        threading.Thread(target=cleanup, daemon=True).start()

    def _on_close(self):
        """窗口关闭时优雅清理 Playwright 与事件循环，避免异步任务未销毁警告"""
        if self.scraper and self._loop and self._loop.is_running():
            try:
                self._run_async(self.scraper.aclose(), timeout=5)
            except Exception:
                self.scraper.close()
            self.scraper = None

        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._loop_thread and self._loop_thread.is_alive():
            self._loop_thread.join(timeout=2)

        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    StickerTransferApp().run()
