"""
抖音表情包抓取模块
连接 Edge 浏览器（CDP），从聊天 emoji 面板中抓取喜欢的表情包

要求：调用 scrape 时用户已打开 emoji 面板并切到 ❤️ 标签
"""
import asyncio
import hashlib
import logging
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx
from playwright.async_api import async_playwright, Page, Browser

import config

logger = logging.getLogger(__name__)

CDP_PORT = 9222

# emoji 面板可能的容器选择器（按优先级）
EMOJI_PANEL_SELECTORS = [
    '[class*="emoji-panel"]',
    '[class*="EmojiPanel"]',
    '[class*="emoticon-panel"]',
    '[class*="sticker-panel"]',
    '[class*="StickerPanel"]',
    '[class*="expression-panel"]',
    '[class*="chat-emoji"]',
    '[class*="ChatEmoji"]',
    '[class*="im-emoji"]',
    '[class*="ImEmoji"]',
    '[data-e2e*="emoji"]',
    '[data-e2e*="sticker"]',
]


class DouyinStickerScraper:
    def __init__(self, on_progress=None, on_sticker_found=None):
        self.on_progress = on_progress or (lambda msg: logger.info(msg))
        self.on_sticker_found = on_sticker_found or (lambda url, path: None)
        self.sticker_urls: list[str] = []
        self.sticker_paths: list[Path] = []
        self._pw = None
        self._browser: Browser | None = None
        self._page: Page | None = None

    def _emit(self, msg: str):
        self.on_progress(msg)

    async def open_edge(self):
        import subprocess

        self._emit("正在启动 Edge 浏览器...")

        edge_paths = [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]
        edge_exe = None
        for p in edge_paths:
            if Path(p).exists():
                edge_exe = p
                break

        if not edge_exe:
            self._emit("未找到 Edge 浏览器")
            return False

        subprocess.Popen([
            edge_exe,
            f"--remote-debugging-port={CDP_PORT}",
            "https://www.douyin.com",
        ], creationflags=subprocess.DETACHED_PROCESS)
        self._emit("Edge 已启动，等待就绪...")
        await asyncio.sleep(3)

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.connect_over_cdp(
            f"http://127.0.0.1:{CDP_PORT}"
        )

        contexts = self._browser.contexts
        if contexts and contexts[0].pages:
            self._page = contexts[0].pages[0]
        else:
            ctx = await self._browser.new_context()
            self._page = await ctx.new_page()

        self._emit("已连接到 Edge")
        self._emit("请完成以下操作：")
        self._emit("  1. 在抖音中打开任意聊天窗口")
        self._emit("  2. 点击表情按钮，切到 ❤️ 标签")
        self._emit("  3. 准备好后点击「2. 开始抓取」")
        return True

    async def scrape_from_chat_panel(self) -> list[Path]:
        if not self._page:
            self._emit("未连接浏览器")
            return []

        self._emit("正在抓取聊天面板中的表情包...")

        result = await self._extract_sticker_urls()
        if not result:
            self._emit("未找到表情面板，请确保已打开 emoji 面板并切到 ❤️ 标签")
            return []

        urls, debug_info = result
        self.sticker_urls = urls
        self._emit(f"共发现 {len(self.sticker_urls)} 个表情包 URL")

        return self.sticker_paths

    async def _extract_sticker_urls(self) -> tuple[list[str], dict]:
        """从页面提取表情包 URL，返回 (urls, debug_info)"""
        try:
            result = await self._page.evaluate('''() => {
                // ===== 策略 1: 直接定位已知的 emoji 面板容器 =====
                // 抖音聊天表情面板的已知 class (从 DevTools 分析得到)
                var KNOWN_PANELS = [
                    '.l6PWGWJH',           // emoji 面板外层容器
                    '.rLB0rrZO',           // 表情包网格容器
                    '.XN0vSEQ6',           // 滚动区域
                ];

                for (var i = 0; i < KNOWN_PANELS.length; i++) {
                    var el = document.querySelector(KNOWN_PANELS[i]);
                    if (el) {
                        var rect = el.getBoundingClientRect();
                        if (rect.width > 50 && rect.height > 50) {
                            var imgs = el.querySelectorAll('img');
                            if (imgs.length >= 3) {
                                var urlList = [];
                                for (var j = 0; j < imgs.length; j++) {
                                    var img = imgs[j];
                                    var src = img.src || img.getAttribute('src') || '';
                                    if (src && src.startsWith('http')) {
                                        urlList.push({
                                            src: src,
                                            w: img.naturalWidth || img.width || 0,
                                            h: img.naturalHeight || img.height || 0
                                        });
                                    }
                                }
                                if (urlList.length >= 3) {
                                    return {
                                        urls: urlList,
                                        method: 'known_panel',
                                        selector: KNOWN_PANELS[i],
                                        imgCount: urlList.length
                                    };
                                }
                            }
                        }
                    }
                }

                // ===== 策略 2: 找高 z-index 的弹窗 (emoji 面板是弹窗) =====
                var allEls = document.querySelectorAll('div');
                var popupCandidates = [];

                for (var i = 0; i < allEls.length; i++) {
                    var el = allEls[i];
                    var style = window.getComputedStyle(el);
                    var zIndex = parseInt(style.zIndex) || 0;
                    var position = style.position;

                    // emoji 面板通常是 fixed/absolute 且 z-index > 50
                    if (zIndex < 50) continue;
                    if (position !== 'fixed' && position !== 'absolute') continue;

                    var rect = el.getBoundingClientRect();
                    if (rect.width < 100 || rect.height < 100) continue;
                    // 面板在右半边
                    if (rect.left < window.innerWidth * 0.5) continue;

                    var imgs = el.querySelectorAll('img');
                    if (imgs.length < 3) continue;

                    // 检查图片是否来自表情 CDN
                    var stickerCount = 0;
                    for (var j = 0; j < imgs.length; j++) {
                        var src = imgs[j].src || '';
                        if (src.indexOf('emoticon') >= 0 || src.indexOf('sticker') >= 0 ||
                            src.indexOf('byteimg.com') >= 0) {
                            stickerCount++;
                        }
                    }

                    popupCandidates.push({
                        el: el,
                        zIndex: zIndex,
                        imgCount: imgs.length,
                        stickerCount: stickerCount,
                        rect: { x: rect.x, y: rect.y, w: rect.width, h: rect.height }
                    });
                }

                // 按 sticker 数量排序
                popupCandidates.sort(function(a, b) { return b.stickerCount - a.stickerCount; });

                if (popupCandidates.length > 0 && popupCandidates[0].stickerCount >= 3) {
                    var best = popupCandidates[0];
                    var imgs = best.el.querySelectorAll('img');
                    var urlList = [];
                    for (var j = 0; j < imgs.length; j++) {
                        var src = imgs[j].src || '';
                        if (src && src.startsWith('http')) {
                            urlList.push({
                                src: src,
                                w: imgs[j].naturalWidth || imgs[j].width || 0,
                                h: imgs[j].naturalHeight || imgs[j].height || 0
                            });
                        }
                    }
                    return {
                        urls: urlList,
                        method: 'popup_analysis',
                        zIndex: best.zIndex,
                        stickerCount: best.stickerCount,
                        rect: best.rect
                    };
                }

                // ===== 策略 3: 查找包含大量表情 CDN URL 的区域 =====
                var allImgs = document.querySelectorAll('img');
                var stickerUrls = [];

                for (var i = 0; i < allImgs.length; i++) {
                    var img = allImgs[i];
                    var src = img.src || '';
                    if (!src || !src.startsWith('http')) continue;

                    // 表情包 CDN 特征
                    if (src.indexOf('emoticon') >= 0 || src.indexOf('im-emoticon') >= 0 ||
                        src.indexOf('sticker') >= 0) {
                        var w = img.naturalWidth || img.width || 0;
                        var h = img.naturalHeight || img.height || 0;
                        stickerUrls.push({ src: src, w: w, h: h });
                    }
                }

                if (stickerUrls.length >= 3) {
                    return {
                        urls: stickerUrls,
                        method: 'cdn_pattern',
                        count: stickerUrls.length
                    };
                }

                return null;
            }''')

            if not result:
                return None, {}

            urls = []
            debug_info = {"method": result.get("method")}

            for item in result.get("urls", []):
                src = item["src"]
                w = item.get("w", 0)
                h = item.get("h", 0)

                # 过滤太小的
                if w > 0 and (w < 30 or h < 30):
                    continue

                # 过滤明显的非表情 URL
                if not self._is_sticker_url(src):
                    continue

                normalized = self._normalize_url(src)
                if normalized not in urls:
                    urls.append(normalized)

            self._emit(f"检测方法: {result.get('method')}, 候选图片: {len(result.get('urls', []))}, 过滤后: {len(urls)}")

            if result.get("method") == "known_panel":
                self._emit(f"使用已知面板选择器: {result.get('selector')}")
            elif result.get("method") == "popup_analysis":
                self._emit(f"面板特征: z-index={result.get('zIndex')} 表情数={result.get('stickerCount')}")

            return urls, debug_info

        except Exception as e:
            self._emit(f"提取表情包出错: {e}")
            return None, {"error": str(e)}

    async def download_stickers(self) -> list[Path]:
        if not self.sticker_urls:
            return []

        self._emit(f"开始下载 {len(self.sticker_urls)} 个表情包...")
        self.sticker_paths = []

        async with httpx.AsyncClient(
            headers=config.HEADERS,
            follow_redirects=True,
            timeout=30,
        ) as client:
            for i, url in enumerate(self.sticker_urls):
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        ext = self._guess_ext(url, dict(resp.headers))
                        name = hashlib.md5(url.encode()).hexdigest()[:12] + ext
                        path = config.STICKER_DIR / name
                        path.write_bytes(resp.content)
                        self.sticker_paths.append(path)
                        self.on_sticker_found(url, path)
                        self._emit(f"下载 {i+1}/{len(self.sticker_urls)}: {name}")
                    else:
                        self._emit(f"下载失败 [{resp.status_code}]: {url[:60]}")
                except Exception as e:
                    self._emit(f"下载出错: {e}")

        self._emit(f"下载完成，共 {len(self.sticker_paths)} 个表情包")
        return self.sticker_paths

    def close(self):
        self._browser = None
        self._page = None

    @staticmethod
    def _is_sticker_url(url: str) -> bool:
        if not url or not url.startswith("http"):
            return False
        url_lower = url.lower()

        # 优先：来自表情 CDN 的 URL 直接通过
        if "im-emoticon" in url_lower or "emoticon-sign" in url_lower:
            return True

        # 检查图片扩展名 (包括 awebp 这种非标准格式)
        has_ext = any(ext in url_lower for ext in [
            ".gif", ".png", ".webp", ".jpg", ".jpeg",
            "awebp", "webp-resize", "webp-resi",
        ])
        if not has_ext:
            return False

        # 排除已知的非表情资源
        exclude = [
            "aweme-client-static-resource",  # UI 图标资源
            "chat_dialog", "chat_default", "edit_",
            "ai_mix", "specia", "im_gol",
            "avatar", "cover", "background", "banner",
            "logo", "icon", "poster", "thumb",
        ]
        return not any(kw in url_lower for kw in exclude)

    @staticmethod
    def _normalize_url(url: str) -> str:
        parsed = urlparse(url)
        if parsed.query:
            return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{parsed.query}"
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

    @staticmethod
    def _guess_ext(url: str, headers: dict) -> str:
        url_lower = url.lower()
        if ".gif" in url_lower: return ".gif"
        if ".png" in url_lower: return ".png"
        if ".webp" in url_lower: return ".webp"
        if ".jpg" in url_lower or ".jpeg" in url_lower: return ".jpg"
        ct = headers.get("content-type", "")
        if "gif" in ct: return ".gif"
        if "png" in ct: return ".png"
        if "webp" in ct: return ".webp"
        return ".png"
