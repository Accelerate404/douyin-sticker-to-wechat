"""
抖音表情包抓取模块
- 使用独立 Edge 调试目录启动，不杀用户日常浏览器并持久化登录态
- 支持网络 JSON 响应拦截 + 面板自动滚动 DOM 提取双通道
- 并发下载、已存在文件跳过、基于文件魔数的准确扩展名识别
"""
import asyncio
import hashlib
import io
import json
import logging
import subprocess
import weakref
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx
from PIL import Image
from playwright.async_api import Browser, Page, Response, async_playwright

import config

logger = logging.getLogger(__name__)

CDP_PORT = 9222


def _detect_image_ext(data: bytes, url: str, headers: dict) -> str:
    """优先根据文件魔数判断真实图片扩展名，避免保存错误后缀"""
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"

    url_lower = url.lower()
    for ext in (".gif", ".webp", ".png", ".jpg"):
        if ext in url_lower:
            return ext
    if ".jpeg" in url_lower:
        return ".jpg"

    ct = headers.get("content-type", "").lower()
    if "webp" in ct:
        return ".webp"
    if "gif" in ct:
        return ".gif"
    if "jpeg" in ct or "jpg" in ct:
        return ".jpg"
    if "png" in ct:
        return ".png"
    return ".png"


class DouyinStickerScraper:
    def __init__(self, on_progress=None, on_sticker_found=None):
        self.on_progress = on_progress or (lambda msg: logger.info(msg))
        self.on_sticker_found = on_sticker_found or (lambda url, path: None)
        self.sticker_urls: list[str] = []
        self.sticker_paths: list[Path] = []
        self._network_url_by_key: dict[str, str] = {}
        self._pw = None
        self._browser: Browser | None = None
        self._page: Page | None = None
        self._attached_pages: weakref.WeakSet[Page] = weakref.WeakSet()
        self._attached_contexts: weakref.WeakSet[Any] = weakref.WeakSet()
        self._cancelled = False
        self.last_panel_detected = False

    def cancel(self):
        """请求取消正在进行的抓取或下载"""
        self._cancelled = True

    def reset_cancel(self):
        """重置取消状态"""
        self._cancelled = False

    def _emit(self, msg: str):
        self.on_progress(msg)

    async def _is_cdp_ready(self) -> bool:
        """检查本地 CDP 端口是否已就绪"""
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"http://127.0.0.1:{CDP_PORT}/json/version")
                return resp.status_code == 200
        except Exception:
            return False

    async def open_edge(self) -> bool:
        """连接已有 CDP 调试端口，或使用独立 Profile 启动 Edge"""
        if await self._is_cdp_ready():
            self._emit("检测到已开启的 Edge 调试端口，正在连接...")
        else:
            edge_paths = [
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            ]
            edge_exe = next((p for p in edge_paths if Path(p).exists()), None)
            if not edge_exe:
                self._emit("未找到 Edge 浏览器")
                return False

            config.EDGE_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            self._emit("正在启动独立 Edge 调试实例（不影响日常浏览器）...")

            subprocess.Popen(
                [
                    edge_exe,
                    f"--remote-debugging-port={CDP_PORT}",
                    f"--user-data-dir={config.EDGE_PROFILE_DIR}",
                    "--disable-sync",
                    "--disable-extensions",
                    "--disable-default-apps",
                    "--disable-background-networking",
                    "--disable-component-extensions-with-background-pages",
                    "--disable-features=msImplicitSignIn,msAccountConsistency,msEdgeAccountConsistency,msEdgeFRE,msEdgeOnRamp",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--no-service-autorun",
                    config.DOUYIN_URL,
                ],
                creationflags=subprocess.DETACHED_PROCESS,
            )

            ready = False
            for _ in range(30):
                await asyncio.sleep(0.5)
                if await self._is_cdp_ready():
                    ready = True
                    break

            if not ready:
                self._emit("Edge 调试端口启动超时，请检查是否已有占用了 9222 端口的进程")
                return False

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.connect_over_cdp(
            f"http://127.0.0.1:{CDP_PORT}"
        )

        contexts = self._browser.contexts
        for ctx in contexts:
            self._attach_context_listener(ctx)

        page = await self._get_active_douyin_page()
        if not page:
            if contexts and contexts[0].pages:
                page = contexts[0].pages[0]
            else:
                ctx = await self._browser.new_context()
                self._attach_context_listener(ctx)
                page = await ctx.new_page()

        self._page = page
        self._attach_network_listener(page)
        self._emit("已连接到 Edge（网络拦截器已就绪）")
        return True

    def _attach_context_listener(self, ctx: Any):
        """监听浏览器上下文新建标签页，自动挂载网络拦截器"""
        if ctx not in self._attached_contexts:
            self._attached_contexts.add(ctx)
            ctx.on("page", lambda p: self._attach_network_listener(p))

    def _attach_network_listener(self, page: Page | None = None):
        """挂载网络响应监听，拦截表情列表 API 返回的高清 URL"""
        target = page or self._page
        if target and target not in self._attached_pages:
            target.on("response", self._handle_response)
            self._attached_pages.add(target)

    async def _get_active_douyin_page(self) -> Page | None:
        """动态查找当前正在前台使用的抖音标签页"""
        if not self._browser:
            return self._page

        douyin_pages: list[Page] = []
        fallback_page: Page | None = None
        for ctx in self._browser.contexts:
            self._attach_context_listener(ctx)
            for p in ctx.pages:
                if p.is_closed():
                    continue
                self._attach_network_listener(p)
                if "douyin.com" in (p.url or ""):
                    douyin_pages.append(p)
                elif fallback_page is None:
                    fallback_page = p

        for p in douyin_pages:
            try:
                if await p.evaluate("document.visibilityState === 'visible'"):
                    self._page = p
                    return p
            except Exception:
                continue

        if douyin_pages:
            self._page = douyin_pages[-1]
            return self._page
        if fallback_page:
            self._page = fallback_page
        return self._page

    @staticmethod
    def _is_better_sticker_url(candidate: str, current: str) -> bool:
        """优先选择无 resize/thumb 缩略参数的原图或动图 URL"""
        def _score(u: str) -> tuple[int, int]:
            ul = u.lower()
            no_resize = 1 if ("resize" not in ul and "thumb" not in ul) else 0
            is_ori = 1 if ("ori" in ul or "animate" in ul or ".gif" in ul) else 0
            return (no_resize, is_ori)

        return _score(candidate) > _score(current)

    async def _handle_response(self, response: Response):
        """缓存接口中出现的表情高清 URL，仅用于对当前面板抓取结果升级原图，不混入未打开的官方包"""
        try:
            url_lower = response.url.lower()
            if not any(
                kw in url_lower
                for kw in ("emoticon", "sticker", "favorite_emoji", "emoji_list", "im/emoji")
            ):
                return
            if response.status != 200:
                return
            ct = response.headers.get("content-type", "").lower()
            if "json" not in ct and "text" not in ct:
                return
            data = await response.json()
            found: list[str] = []
            self._collect_urls_from_json(data, found)
            added = 0
            for raw_url in found:
                if self._is_strict_emoticon_url(raw_url):
                    norm = self._normalize_url(raw_url)
                    key = self._sticker_dedup_key(norm)
                    if key not in self._network_url_by_key:
                        self._network_url_by_key[key] = norm
                        added += 1
                    elif self._is_better_sticker_url(norm, self._network_url_by_key[key]):
                        self._network_url_by_key[key] = norm
            if added:
                logger.info(f"网络缓存新增 {added} 个表情原图索引（累计 {len(self._network_url_by_key)}）")
        except Exception:
            pass

    @classmethod
    def _collect_urls_from_json(cls, node: Any, out: list[str]):
        """递归扫描 JSON 中的 http(s) 图片 URL"""
        if isinstance(node, str):
            if node.startswith("http"):
                out.append(node)
        elif isinstance(node, dict):
            for val in node.values():
                cls._collect_urls_from_json(val, out)
        elif isinstance(node, list):
            for item in node:
                cls._collect_urls_from_json(item, out)

    async def scrape_from_chat_panel(
        self,
        max_count: int = 0,
        skip_hashes: set[str] | None = None,
    ) -> list[str]:
        """抓取流程：检查登录 → 校验表情面板打开 → 按上限增量抓取（自动跳过历史已抓）"""
        self.sticker_urls = []
        self.sticker_paths = []
        self.last_panel_detected = False

        page = await self._get_active_douyin_page()
        if not page:
            self._emit("未连接浏览器")
            return []

        if "douyin.com" not in page.url:
            self._emit("正在打开抖音...")
            await page.goto(config.DOUYIN_URL, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(3)

        if self._cancelled:
            self._emit("抓取已停止")
            return []

        self._emit("检查登录状态...")
        if not await self._is_logged_in(page):
            self._emit("请在浏览器中扫码登录抖音，登录后会自动继续...")
            if not await self._wait_for_login(page, timeout=180):
                if self._cancelled:
                    self._emit("抓取已停止")
                else:
                    self._emit("登录超时，请重新点击「开始抓取」")
                return []
            self._emit("登录成功!")
            await asyncio.sleep(2)

        if self._cancelled:
            self._emit("抓取已停止")
            return []

        dom_urls, _debug_info, skipped_count, panel_detected = await self._extract_with_scroll(
            max_count=max_count,
            skip_hashes=skip_hashes,
        )
        if self._cancelled:
            self._emit("抓取已停止")
            return []

        # 严格门禁：仅当页面上真正检测到表情面板时才允许合并抓取结果，绝不误抓视频封面
        if not panel_detected:
            self._emit("========================================")
            self._emit("未检测到打开的表情面板！请先在抖音中完成以下操作：")
            self._emit("  1. 打开任意聊天窗口（或私信）")
            self._emit("  2. 点击输入框旁的表情按钮 → 切换到红心/收藏标签")
            self._emit("  3. 看到表情包后，再次点击「开始抓取」")
            self._emit("========================================")
            self._emit("（如确认已打开仍无法识别，可点击「页面诊断」保存 page_dump.json）")
            return []

        self.last_panel_detected = True
        merged: list[str] = []
        seen_keys: set[str] = set()
        upgraded = 0
        for u in dom_urls:
            key = self._sticker_dedup_key(u)
            if key in seen_keys:
                continue
            seen_keys.add(key)

            best_u = u
            net_u = self._network_url_by_key.get(key)
            if net_u and self._is_better_sticker_url(net_u, best_u):
                best_u = net_u
                upgraded += 1

            if skip_hashes and self._url_hash(best_u) in skip_hashes:
                skipped_count += 1
                continue

            merged.append(best_u)
            if max_count > 0 and len(merged) >= max_count:
                break

        if not merged:
            if skipped_count > 0:
                self._emit(
                    f"当前面板中的表情包均已抓取过（已跳过 {skipped_count} 个），"
                    "如需重新抓取请取消勾选「跳过已抓取/已发送」"
                )
            else:
                self._emit("当前表情面板中未发现新的表情包")
            return []

        self.sticker_urls = merged
        skip_msg = f"，跳过已抓取 {skipped_count} 个" if skipped_count else ""
        limit_msg = f"（上限 {max_count}）" if max_count > 0 else ""
        upgrade_msg = f"，高清升级 {upgraded} 个" if upgraded else ""
        self._emit(f"共发现 {len(merged)} 个不重复新表情包{limit_msg}{skip_msg}{upgrade_msg}")
        return self.sticker_urls

    async def _is_logged_in(self, page: Page) -> bool:
        """检查是否已登录"""
        try:
            has_login_modal = await page.evaluate('''() => {
                const els = document.querySelectorAll('[class*="login"], [class*="Login"], [class*="qrcode"], [class*="QRCode"]');
                for (const el of els) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width > 100 && rect.height > 100) return true;
                }
                return false;
            }''')
            if has_login_modal:
                return False

            if "login" in page.url or "passport" in page.url:
                return False

            has_user_ui = await page.evaluate('''() => {
                const avatar = document.querySelector('[class*="avatar"], [class*="Avatar"], [class*="user-info"]');
                if (avatar) {
                    const rect = avatar.getBoundingClientRect();
                    return rect.width > 10 && rect.height > 10;
                }
                return false;
            }''')
            return has_user_ui
        except Exception:
            return False

    async def _wait_for_login(self, page: Page, timeout: int = 180) -> bool:
        """等待用户扫码登录（支持取消）"""
        import time

        start = time.time()
        while time.time() - start < timeout:
            if self._cancelled:
                return False
            if await self._is_logged_in(page):
                return True
            await asyncio.sleep(2)
        return False

    async def dump_page_structure(self) -> dict:
        """dump 当前页面的 DOM 结构信息，用于调试选择器"""
        page = await self._get_active_douyin_page()
        if not page:
            self._emit("未连接浏览器")
            return {}

        self._emit("正在分析页面结构...")

        try:
            result = await page.evaluate(r'''() => {
                const info = {
                    url: location.href,
                    title: document.title,
                    viewport: { w: innerWidth, h: innerHeight },
                    keywordElements: [],
                    popups: [],
                    buttons: [],
                    images: [],
                    inputs: [],
                    tabs: [],
                };

                const keywords = ['emoji', 'sticker', 'emoticon', 'expression', 'emotion',
                    'favorite', 'collect', 'heart', 'like', 'chat', 'message',
                    'panel', 'dialog', 'popup', 'modal'];

                const all = document.querySelectorAll('*');
                for (let i = 0; i < all.length; i++) {
                    const el = all[i];
                    const cls = el.className || '';
                    const id = el.id || '';
                    const clsStr = typeof cls === 'string' ? cls : cls.baseVal || '';
                    const combined = (clsStr + ' ' + id).toLowerCase();

                    for (const kw of keywords) {
                        if (combined.includes(kw)) {
                            const rect = el.getBoundingClientRect();
                            info.keywordElements.push({
                                tag: el.tagName.toLowerCase(),
                                class: clsStr.slice(0, 100),
                                id: id,
                                keyword: kw,
                                visible: rect.width > 0 && rect.height > 0,
                                rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) },
                                text: (el.textContent || '').trim().slice(0, 50),
                            });
                            break;
                        }
                    }

                    const style = getComputedStyle(el);
                    const z = parseInt(style.zIndex) || 0;
                    if (z > 50 && (style.position === 'fixed' || style.position === 'absolute')) {
                        const rect = el.getBoundingClientRect();
                        if (rect.width > 80 && rect.height > 80) {
                            info.popups.push({
                                tag: el.tagName.toLowerCase(),
                                class: (clsStr || '').slice(0, 100),
                                z: z,
                                rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) },
                                childImgCount: el.querySelectorAll('img').length,
                            });
                        }
                    }

                    if (el.tagName === 'BUTTON' || el.getAttribute('role') === 'button' || el.onclick) {
                        const rect = el.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0) {
                            info.buttons.push({
                                tag: el.tagName.toLowerCase(),
                                class: (clsStr || '').slice(0, 80),
                                ariaLabel: el.getAttribute('aria-label') || '',
                                text: (el.textContent || '').trim().slice(0, 30),
                                rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) },
                            });
                        }
                    }

                    if (el.tagName === 'IMG' && info.images.length < 50) {
                        const src = el.src || el.getAttribute('src') || '';
                        if (src.startsWith('http')) {
                            const rect = el.getBoundingClientRect();
                            info.images.push({
                                src: src.slice(0, 200),
                                w: el.naturalWidth || el.width || 0,
                                h: el.naturalHeight || el.height || 0,
                                rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) },
                            });
                        }
                    }

                    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.getAttribute('contenteditable') === 'true') {
                        const rect = el.getBoundingClientRect();
                        if (rect.width > 0) {
                            info.inputs.push({
                                tag: el.tagName.toLowerCase(),
                                class: (clsStr || '').slice(0, 80),
                                placeholder: el.getAttribute('placeholder') || '',
                                contentEditable: el.getAttribute('contenteditable'),
                                rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) },
                            });
                        }
                    }

                    if (el.getAttribute('role') === 'tab' || (clsStr || '').toLowerCase().includes('tab')) {
                        const rect = el.getBoundingClientRect();
                        if (rect.width > 0) {
                            info.tabs.push({
                                tag: el.tagName.toLowerCase(),
                                class: (clsStr || '').slice(0, 80),
                                text: (el.textContent || '').trim().slice(0, 30),
                                ariaSelected: el.getAttribute('aria-selected'),
                                rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) },
                            });
                        }
                    }
                }

                return info;
            }''')

            dump_path = config.PROJECT_DIR / "page_dump.json"
            with open(dump_path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)

            self._emit(f"页面: {result.get('title', '')}")
            self._emit(f"URL: {result.get('url', '')}")
            self._emit(f"关键词元素: {len(result.get('keywordElements', []))} 个")
            self._emit(f"弹窗: {len(result.get('popups', []))} 个 | 图片: {len(result.get('images', []))} 个")
            self._emit(f"诊断结果已保存: {dump_path}")
            return result

        except Exception as e:
            self._emit(f"诊断出错: {e}")
            return {}

    async def _scroll_panel_step(self) -> bool:
        """优先滚动已识别的表情面板容器，避免误滚聊天记录或视频页"""
        if not self._page:
            return False
        try:
            return await self._page.evaluate('''() => {
                const candidates = [];
                const tagged = document.querySelector('[data-sticker-panel="1"]');
                if (tagged) {
                    candidates.push(tagged);
                    for (const child of tagged.querySelectorAll('div')) {
                        if (child.scrollHeight > child.clientHeight + 20) {
                            candidates.push(child);
                        }
                    }
                } else {
                    for (const sel of ['.XN0vSEQ6', '.rLB0rrZO', '.l6PWGWJH']) {
                        const el = document.querySelector(sel);
                        if (el) candidates.push(el);
                    }
                }
                for (const el of candidates) {
                    const prev = el.scrollTop;
                    const maxScroll = el.scrollHeight - el.clientHeight;
                    if (maxScroll > 10 && prev < maxScroll - 2) {
                        el.scrollTop = Math.min(maxScroll, prev + Math.max(160, Math.floor(el.clientHeight * 0.8)));
                        return el.scrollTop > prev;
                    }
                }
                return false;
            }''')
        except Exception:
            return False

    async def _extract_with_scroll(
        self,
        max_count: int = 0,
        skip_hashes: set[str] | None = None,
    ) -> tuple[list[str], dict, int, bool]:
        """自动滚动表情面板并按上限收集去重后的新表情 URL，返回 (urls, debug_info, skipped_count, panel_detected)"""
        collected: list[str] = []
        seen_keys: set[str] = set()
        last_debug: dict = {}
        skipped_count = 0
        panel_detected = False

        for step in range(15):
            if self._cancelled:
                break
            urls, debug_info = await self._extract_sticker_urls(quiet=(step > 0))
            if debug_info:
                last_debug = debug_info
            if urls is None:
                break
            if len(urls) > 0:
                panel_detected = True
            for u in urls:
                dedup_key = self._sticker_dedup_key(u)
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)
                if skip_hashes and self._url_hash(u) in skip_hashes:
                    skipped_count += 1
                    continue
                collected.append(u)
                if max_count > 0 and len(collected) >= max_count:
                    break

            if max_count > 0 and len(collected) >= max_count:
                break

            moved = await self._scroll_panel_step()
            if not moved or self._cancelled:
                break
            await asyncio.sleep(0.4)

        return collected, last_debug, skipped_count, panel_detected

    async def _extract_sticker_urls(self, quiet: bool = False) -> tuple[list[str] | None, dict]:
        """从页面严格提取表情包面板内的 URL，返回 (urls, debug_info)"""
        try:
            result = await self._page.evaluate('''() => {
                var KNOWN_PANELS = ['.l6PWGWJH', '.rLB0rrZO', '.XN0vSEQ6'];

                var oldTagged = document.querySelectorAll('[data-sticker-panel]');
                for (var t = 0; t < oldTagged.length; t++) {
                    oldTagged[t].removeAttribute('data-sticker-panel');
                }

                function isCircularAvatar(img, ir) {
                    if (!ir || ir.width <= 0 || ir.height <= 0) return false;
                    if (Math.abs(ir.width - ir.height) > 2) return false;
                    var br = (window.getComputedStyle(img).borderRadius || '').trim();
                    if (br.indexOf('50%') >= 0) return true;
                    var px = parseFloat(br);
                    return !isNaN(px) && px >= ir.width * 0.45;
                }

                function collectPanelImgs(container) {
                    var imgs = container.querySelectorAll('img');
                    var list = [];
                    for (var j = 0; j < imgs.length; j++) {
                        var img = imgs[j];
                        var src = img.src || img.getAttribute('src') || '';
                        if (!src || !src.startsWith('http')) continue;
                        var nw = img.naturalWidth || img.width || 0;
                        var nh = img.naturalHeight || img.height || 0;
                        if (nw > 1000 || nh > 1000) continue;
                        var ir = img.getBoundingClientRect();
                        if (ir.width > 0 && (ir.width < 20 || ir.width > 220)) continue;
                        if (ir.height > 0 && (ir.height < 20 || ir.height > 220)) continue;
                        if (isCircularAvatar(img, ir)) continue;
                        list.push({ src: src, w: nw, h: nh, rw: Math.round(ir.width), rh: Math.round(ir.height) });
                    }
                    return list;
                }

                for (var i = 0; i < KNOWN_PANELS.length; i++) {
                    var el = document.querySelector(KNOWN_PANELS[i]);
                    if (el) {
                        var rect = el.getBoundingClientRect();
                        if (rect.width >= 150 && rect.width <= 750 && rect.height >= 150 && rect.height <= 750) {
                            var urlList = collectPanelImgs(el);
                            if (urlList.length >= 3) {
                                el.setAttribute('data-sticker-panel', '1');
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

                var allEls = document.querySelectorAll('div');
                var popupCandidates = [];

                for (var i = 0; i < allEls.length; i++) {
                    var el = allEls[i];
                    var style = window.getComputedStyle(el);
                    var zIndex = parseInt(style.zIndex) || 0;
                    var position = style.position;

                    if (zIndex < 50) continue;
                    if (position !== 'fixed' && position !== 'absolute') continue;

                    var rect = el.getBoundingClientRect();
                    if (rect.width < 150 || rect.width > 750 || rect.height < 150 || rect.height > 750) continue;
                    if (rect.left < window.innerWidth * 0.45) continue;

                    var imgs = el.querySelectorAll('img');
                    if (imgs.length < 3) continue;

                    var cdnStickerCount = 0;
                    var gridStickerCount = 0;
                    var gridPoints = [];
                    for (var j = 0; j < imgs.length; j++) {
                        var img = imgs[j];
                        var src = img.src || '';
                        var nw = img.naturalWidth || img.width || 0;
                        var nh = img.naturalHeight || img.height || 0;
                        if (nw > 1000 || nh > 1000) continue;
                        var ir = img.getBoundingClientRect();
                        if (isCircularAvatar(img, ir)) continue;
                        var isStickerCdn = (src.indexOf('emoticon') >= 0 || src.indexOf('im-emoticon') >= 0 || src.indexOf('sticker') >= 0);
                        if (isStickerCdn) cdnStickerCount++;
                        if (ir.width >= 24 && ir.width <= 180 && ir.height >= 24 && ir.height <= 180) {
                            gridStickerCount++;
                            gridPoints.push({ x: Math.round(ir.left), y: Math.round(ir.top) });
                        }
                    }

                    // 区分二维表情网格与单列好友头像列表：同一行需至少有 3 个不同横坐标的非圆图片
                    var gridRowMax = 0;
                    for (var p1 = 0; p1 < gridPoints.length; p1++) {
                        var rowXs = {};
                        var rowCount = 0;
                        for (var p2 = 0; p2 < gridPoints.length; p2++) {
                            if (Math.abs(gridPoints[p1].y - gridPoints[p2].y) <= 12) {
                                var bucketX = Math.round(gridPoints[p2].x / 20);
                                if (!rowXs[bucketX]) {
                                    rowXs[bucketX] = true;
                                    rowCount++;
                                }
                            }
                        }
                        if (rowCount > gridRowMax) gridRowMax = rowCount;
                    }

                    if (cdnStickerCount >= 3 || (gridRowMax >= 3 && gridStickerCount >= 4)) {
                        popupCandidates.push({
                            el: el,
                            zIndex: zIndex,
                            stickerCount: cdnStickerCount * 10 + gridStickerCount,
                            rect: { x: rect.x, y: rect.y, w: rect.width, h: rect.height }
                        });
                    }
                }

                popupCandidates.sort(function(a, b) { return b.stickerCount - a.stickerCount; });

                if (popupCandidates.length > 0) {
                    var best = popupCandidates[0];
                    var urlList = collectPanelImgs(best.el);
                    if (urlList.length >= 3) {
                        best.el.setAttribute('data-sticker-panel', '1');
                        return {
                            urls: urlList,
                            method: 'popup_analysis',
                            zIndex: best.zIndex,
                            stickerCount: best.stickerCount,
                            rect: best.rect
                        };
                    }
                }

                var allImgs = document.querySelectorAll('img');
                var stickerUrls = [];

                for (var i = 0; i < allImgs.length; i++) {
                    var img = allImgs[i];
                    var src = img.src || '';
                    if (!src || !src.startsWith('http')) continue;

                    if (src.indexOf('emoticon') >= 0 || src.indexOf('im-emoticon') >= 0 ||
                        src.indexOf('sticker') >= 0) {
                        var nw = img.naturalWidth || img.width || 0;
                        var nh = img.naturalHeight || img.height || 0;
                        var ir = img.getBoundingClientRect();
                        if (nw > 1000 || nh > 1000) continue;
                        if (ir.width > 0 && (ir.width < 24 || ir.width > 200 || ir.height < 24 || ir.height > 200)) continue;
                        if (isCircularAvatar(img, ir)) continue;
                        stickerUrls.push({ src: src, w: nw, h: nh, rw: Math.round(ir.width), rh: Math.round(ir.height) });
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

            urls: list[str] = []
            debug_info = {"method": result.get("method")}

            for item in result.get("urls", []):
                src = item["src"]
                w = item.get("w", 0)
                h = item.get("h", 0)

                if w > 1000 or h > 1000:
                    continue
                if w > 0 and (w < 30 or h < 30):
                    continue
                if not self._is_sticker_url(src):
                    continue

                normalized = self._normalize_url(src)
                if all(self._sticker_dedup_key(normalized) != self._sticker_dedup_key(u) for u in urls):
                    urls.append(normalized)

            if not quiet:
                self._emit(
                    f"检测方法: {result.get('method')}, 候选图片: {len(result.get('urls', []))}, 过滤后: {len(urls)}"
                )
            return urls, debug_info

        except Exception as e:
            self._emit(f"提取表情包出错: {e}")
            return None, {"error": str(e)}

    @staticmethod
    def _find_existing_sticker(url_hash: str) -> Path | None:
        """查找本地是否已下载过同哈希表情文件"""
        for candidate in config.STICKER_DIR.glob(f"{url_hash}.*"):
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
        return None

    async def download_stickers(
        self,
        on_progress_pct: Callable[[int, int], None] | None = None,
    ) -> list[Path]:
        """并发下载表情包（稳定哈希去重、跳过已下载、图片内容 MD5 去重、校验尺寸）"""
        if not self.sticker_urls:
            return []

        total = len(self.sticker_urls)
        self._emit(f"开始下载 {total} 个表情包（并发 8 线程）...")
        self.sticker_paths = []

        sem = asyncio.Semaphore(8)
        results: dict[int, Path] = {}
        seen_content_md5: set[str] = set()
        completed = 0
        skipped = 0
        rejected_big = 0
        dup_content = 0

        async with httpx.AsyncClient(
            headers=config.HEADERS,
            follow_redirects=True,
            timeout=30,
        ) as client:
            async def fetch_one(idx: int, url: str):
                nonlocal completed, skipped, rejected_big, dup_content
                if self._cancelled:
                    return

                url_hash = self._url_hash(url)
                existing = self._find_existing_sticker(url_hash)
                if existing is not None:
                    try:
                        c_md5 = hashlib.md5(existing.read_bytes()).hexdigest()
                        if c_md5 in seen_content_md5:
                            dup_content += 1
                            completed += 1
                            if on_progress_pct:
                                on_progress_pct(completed, total)
                            return
                        seen_content_md5.add(c_md5)
                    except OSError:
                        pass
                    results[idx] = existing
                    self.on_sticker_found(url, existing)
                    skipped += 1
                    completed += 1
                    if on_progress_pct:
                        on_progress_pct(completed, total)
                    return

                async with sem:
                    if self._cancelled:
                        return
                    try:
                        resp = await client.get(url)
                        if self._cancelled:
                            return
                        if resp.status_code == 200 and resp.content:
                            # 尺寸门禁：写入磁盘前校验图片分辨率，绝不保存 4K 视频封面
                            try:
                                with Image.open(io.BytesIO(resp.content)) as im:
                                    w, h = im.size
                                if max(w, h) > 1200 or min(w, h) < 24:
                                    rejected_big += 1
                                    logger.info(f"跳过非表情尺寸图片 ({w}x{h}): {url[:80]}")
                                    return
                            except Exception:
                                return

                            # 图片字节内容 MD5 去重，防止同图不同 URL 重复保存
                            content_md5 = hashlib.md5(resp.content).hexdigest()
                            if content_md5 in seen_content_md5:
                                dup_content += 1
                                return
                            seen_content_md5.add(content_md5)

                            ext = _detect_image_ext(resp.content, url, dict(resp.headers))
                            name = f"{url_hash}{ext}"
                            path = config.STICKER_DIR / name
                            path.write_bytes(resp.content)
                            results[idx] = path
                            self.on_sticker_found(url, path)
                        else:
                            self._emit(f"下载失败 [{resp.status_code}]: {url[:60]}")
                    except Exception as e:
                        self._emit(f"下载出错: {e}")
                    finally:
                        completed += 1
                        if completed % 10 == 0 or completed == total:
                            self._emit(f"下载进度: {completed}/{total}")
                        if on_progress_pct:
                            on_progress_pct(completed, total)

            await asyncio.gather(*(fetch_one(i, u) for i, u in enumerate(self.sticker_urls)))

        self.sticker_paths = [results[i] for i in sorted(results)]
        if self.sticker_paths:
            config.record_history("scraped", [p.stem for p in self.sticker_paths])
        if self._cancelled:
            self._emit(f"下载已停止（已完成 {len(self.sticker_paths)} 个）")
        else:
            extra_parts = []
            if rejected_big:
                extra_parts.append(f"过滤非表情大图 {rejected_big} 个")
            if dup_content:
                extra_parts.append(f"去重同图 {dup_content} 个")
            extra = "，" + "，".join(extra_parts) if extra_parts else ""
            self._emit(
                f"下载完成，共 {len(self.sticker_paths)} 个不重复表情包（本地复用 {skipped} 个{extra}）"
            )
        return self.sticker_paths

    async def aclose(self):
        """异步优雅关闭浏览器连接与 Playwright 资源"""
        try:
            for p in list(self._attached_pages):
                try:
                    p.remove_listener("response", self._handle_response)
                except Exception:
                    pass
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()
        except Exception as e:
            logger.debug(f"关闭浏览器连接时忽略异常: {e}")
        finally:
            self._page = None
            self._browser = None
            self._pw = None
            self._attached_pages.clear()
            self._attached_contexts.clear()

    def close(self):
        self._page = None
        self._browser = None
        self._pw = None
        self._attached_pages.clear()
        self._attached_contexts.clear()

    @classmethod
    def _is_strict_emoticon_url(cls, url: str) -> bool:
        """用于网络 JSON 响应拦截的严格表情 URL 校验，避免抓到视频封面/推荐图"""
        if not url or not url.startswith("http"):
            return False
        url_lower = url.lower()
        if not any(kw in url_lower for kw in ("emoticon", "im-emoticon", "emoticon-sign", "sticker")):
            return False
        return cls._is_sticker_url(url)

    @staticmethod
    def _is_sticker_url(url: str) -> bool:
        if not url or not url.startswith("http"):
            return False
        url_lower = url.lower()

        if "im-emoticon" in url_lower or "emoticon-sign" in url_lower or "/emoticon" in url_lower:
            return True

        has_ext = any(
            ext in url_lower
            for ext in (".gif", ".png", ".webp", ".jpg", ".jpeg", "awebp", "webp-resize", "webp-resi")
        )
        if not has_ext:
            return False

        exclude = (
            "aweme-client-static-resource",
            "chat_dialog",
            "chat_default",
            "edit_",
            "ai_mix",
            "specia",
            "im_gol",
            "avatar",
            "circle-head",
            "head-img",
            "user-avatar",
            "aweme-avatar",
            "emblem",
            "badge",
            "medal",
            "gift",
            "cover",
            "background",
            "banner",
            "logo",
            "/icon",
            "_icon",
            "icon_",
            "icon.",
            "poster",
            "thumb",
        )
        return not any(kw in url_lower for kw in exclude)

    @staticmethod
    def _sticker_dedup_key(url: str) -> str:
        """提取稳定的资源去重键：剥离 CDN 镜像主机、query 签名、~tplv 模板后缀与扩展名"""
        parsed = urlparse(url)
        path = parsed.path or ""
        if "~" in path:
            path = path.split("~", 1)[0]
        lower_path = path.lower().rstrip("/")
        for ext in (".awebp", ".webp", ".jpeg", ".jpg", ".png", ".gif"):
            if lower_path.endswith(ext):
                lower_path = lower_path[: -len(ext)]
                break
        return lower_path or f"{parsed.netloc.lower()}{path.lower()}"

    @classmethod
    def _url_hash(cls, url: str) -> str:
        """基于稳定去重键生成 12 位文件名/历史哈希，不受过期签名与镜像域名影响"""
        return hashlib.md5(cls._sticker_dedup_key(url).encode()).hexdigest()[:12]

    @staticmethod
    def _normalize_url(url: str) -> str:
        parsed = urlparse(url)
        if parsed.query:
            return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{parsed.query}"
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
