"""
核心模块自动化单元测试（覆盖历史记录、数量上限与跳过、下载尺寸门禁、动静 GIF 转换、电脑微信 Win32 发送逻辑）
"""
import asyncio
import hashlib
import io
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image, ImageDraw

import config
import wechat_sender
from douyin_scraper import DouyinStickerScraper
from sticker_converter import batch_convert, convert_for_wechat


def _make_image_bytes(size: tuple[int, int], fmt: str, animated: bool = False, frames: int = 1) -> bytes:
    buf = io.BytesIO()
    if not animated:
        im = Image.new("RGBA", size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(im)
        draw.ellipse((4, 4, size[0] - 4, size[1] - 4), fill=(255, 80, 120, 255))
        if fmt == "JPEG":
            im = im.convert("RGB")
        im.save(buf, format=fmt)
        return buf.getvalue()

    seq = []
    for i in range(frames):
        im = Image.new("RGBA", size, (240, 240, 240, 255))
        draw = ImageDraw.Draw(im)
        x = int((size[0] - 30) * (i / max(1, frames - 1)))
        draw.rectangle((x, 20, x + 28, size[1] - 20), fill=((i * 37) % 255, 100, 200, 255))
        seq.append(im)
    seq[0].save(
        buf,
        format=fmt,
        save_all=True,
        append_images=seq[1:],
        duration=40 if fmt == "GIF" else 0,
        loop=0,
    )
    return buf.getvalue()


class TestHistoryConfig(unittest.TestCase):
    def test_history_load_save_and_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            hist_file = Path(tmp) / "sticker_history.json"
            with patch.object(config, "HISTORY_FILE", hist_file):
                self.assertEqual(config.load_history(), {"scraped": [], "sent": []})
                config.record_history("scraped", ["aaa", "bbb", "aaa"])
                config.record_history("sent", ["aaa"])
                data = config.load_history()
                self.assertEqual(data["scraped"], ["aaa", "bbb"])
                self.assertEqual(data["sent"], ["aaa"])


class TestScraperFilteringLimitsAndGuards(unittest.TestCase):
    def test_strict_url_filtering(self):
        self.assertFalse(
            DouyinStickerScraper._is_strict_emoticon_url(
                "https://p3-pc-sign.douyinpic.com/tos-cn-i-0813/video_cover.webp"
            )
        )
        self.assertFalse(
            DouyinStickerScraper._is_strict_emoticon_url(
                "https://p3-aweme-im-img.byteimg.com/tos-cn-i-7lppr0tkux/018fca01~tplv-circle-head-img-web.webp"
            )
        )
        self.assertFalse(
            DouyinStickerScraper._is_sticker_url(
                "https://p3-pc-weboff.byteimg.com/tos-cn-i-9r5gewecjs/emblem.png"
            )
        )
        self.assertTrue(
            DouyinStickerScraper._is_strict_emoticon_url(
                "https://p3-aweme-im-img.byteimg.com/tos-cn-i-7lppr0tkux/emoticon_123.webp"
            )
        )
        self.assertTrue(
            DouyinStickerScraper._is_strict_emoticon_url(
                "https://p9-im-emoticon.byteimg.com/tos-cn-i-123/sticker.awebp"
            )
        )

    def test_strict_panel_gate_blocks_stale_network_urls(self):
        scraper = DouyinStickerScraper(on_progress=lambda _: None)
        scraper._page = MagicMock()
        scraper._page.url = "https://www.douyin.com/"
        scraper._network_sticker_urls = [
            "https://p9-im-emoticon.byteimg.com/tos-cn-i-123/emoticon_cached.webp"
        ]

        with (
            patch.object(scraper, "_get_active_douyin_page", new=AsyncMock(return_value=scraper._page)),
            patch.object(scraper, "_is_logged_in", new=AsyncMock(return_value=True)),
            patch.object(scraper, "_extract_with_scroll", new=AsyncMock(return_value=([], {}, 0, False))),
        ):
            urls = asyncio.run(scraper.scrape_from_chat_panel())

        self.assertEqual(urls, [])
        self.assertFalse(scraper.last_panel_detected)

    def test_scrape_limit_and_skip_hashes(self):
        scraper = DouyinStickerScraper(on_progress=lambda _: None)
        scraper._page = MagicMock()
        scraper._page.url = "https://www.douyin.com/"

        u1 = "https://p9-im-emoticon.byteimg.com/s1.webp"
        u2 = "https://p9-im-emoticon.byteimg.com/s2.webp"
        u3 = "https://p9-im-emoticon.byteimg.com/s3.webp"
        u4 = "https://p9-im-emoticon.byteimg.com/s4.webp"
        h1 = DouyinStickerScraper._url_hash(u1)

        with (
            patch.object(scraper, "_get_active_douyin_page", new=AsyncMock(return_value=scraper._page)),
            patch.object(scraper, "_is_logged_in", new=AsyncMock(return_value=True)),
            patch.object(scraper, "_extract_sticker_urls", new=AsyncMock(return_value=([u1, u2, u3, u4], {"method": "known_panel"}))),
            patch.object(scraper, "_scroll_panel_step", new=AsyncMock(return_value=False)),
        ):
            urls = asyncio.run(scraper.scrape_from_chat_panel(max_count=2, skip_hashes={h1}))

        self.assertTrue(scraper.last_panel_detected)
        self.assertEqual(urls, [u2, u3])

    def test_mirror_signature_dedup_and_no_unrelated_network_merge(self):
        m1 = "https://p3-im-emoticon.byteimg.com/tos-cn-i-abc/id1~tplv-resize:100:100.webp?x-signature=AAA"
        m2 = "https://p9-im-emoticon.byteimg.com/tos-cn-i-abc/id1~tplv-ori.gif?x-signature=BBB"
        m3 = "https://p6-im-emoticon.byteimg.com/tos-cn-i-abc/id1.awebp?x-expires=999"

        self.assertEqual(
            DouyinStickerScraper._sticker_dedup_key(m1),
            DouyinStickerScraper._sticker_dedup_key(m2),
        )
        self.assertEqual(
            DouyinStickerScraper._url_hash(m1),
            DouyinStickerScraper._url_hash(m2),
        )

        scraper = DouyinStickerScraper(on_progress=lambda _: None)
        scraper._page = MagicMock()
        scraper._page.url = "https://www.douyin.com/"
        # 模拟网络拦截到了 50 个不相关的官方表情包 + 1 个当前面板表情的高清版本
        for i in range(50):
            u_other = f"https://p9-im-emoticon.byteimg.com/tos-cn-i-other/pack_{i}.webp"
            scraper._network_url_by_key[DouyinStickerScraper._sticker_dedup_key(u_other)] = u_other
        scraper._network_url_by_key[DouyinStickerScraper._sticker_dedup_key(m2)] = m2

        with (
            patch.object(scraper, "_get_active_douyin_page", new=AsyncMock(return_value=scraper._page)),
            patch.object(scraper, "_is_logged_in", new=AsyncMock(return_value=True)),
            patch.object(scraper, "_extract_sticker_urls", new=AsyncMock(return_value=([m1, m3], {"method": "known_panel"}))),
            patch.object(scraper, "_scroll_panel_step", new=AsyncMock(return_value=False)),
        ):
            urls = asyncio.run(scraper.scrape_from_chat_panel())

        # 3 个镜像/签名变体合并为 1 个，且自动升级为无 resize 的高清版本 m2；不混入 50 个无关包
        self.assertEqual(urls, [m2])

    def test_download_content_md5_dedup(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            hist_file = tmp_dir / "history.json"
            same_bytes = _make_image_bytes((150, 150), "WEBP")

            async def fake_get(_url: str):
                resp = MagicMock()
                resp.status_code = 200
                resp.headers = {"content-type": "image/webp"}
                resp.content = same_bytes
                return resp

            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = AsyncMock(side_effect=fake_get)

            scraper = DouyinStickerScraper(on_progress=lambda _: None)
            scraper.sticker_urls = [
                "https://p9-im-emoticon.byteimg.com/tos-cn-i-a/sticker_one.webp",
                "https://p9-im-emoticon.byteimg.com/tos-cn-i-b/sticker_two.webp",
            ]

            with (
                patch.object(config, "STICKER_DIR", tmp_dir),
                patch.object(config, "HISTORY_FILE", hist_file),
                patch("douyin_scraper.httpx.AsyncClient", return_value=mock_client),
            ):
                paths = asyncio.run(scraper.download_stickers())

            self.assertEqual(len(paths), 1)
            self.assertEqual(len(list(tmp_dir.glob("*.webp"))), 1)

    def test_download_dimension_guard_and_cancel(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            hist_file = tmp_dir / "history.json"
            big_webp = _make_image_bytes((2160, 3840), "WEBP")
            ok_webp = _make_image_bytes((180, 180), "WEBP")

            async def fake_get(url: str):
                resp = MagicMock()
                resp.status_code = 200
                resp.headers = {"content-type": "image/webp"}
                resp.content = big_webp if "big_cover" in url else ok_webp
                return resp

            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = AsyncMock(side_effect=fake_get)

            scraper = DouyinStickerScraper(on_progress=lambda _: None)
            scraper.sticker_urls = [
                "https://p9-im-emoticon.byteimg.com/big_cover.webp",
                "https://p9-im-emoticon.byteimg.com/valid_sticker.webp",
            ]

            with (
                patch.object(config, "STICKER_DIR", tmp_dir),
                patch.object(config, "HISTORY_FILE", hist_file),
                patch("douyin_scraper.httpx.AsyncClient", return_value=mock_client),
            ):
                paths = asyncio.run(scraper.download_stickers())
                saved_hist = config.load_history()

            self.assertEqual(len(paths), 1)
            self.assertTrue(paths[0].exists())
            self.assertEqual(paths[0].suffix, ".webp")
            self.assertEqual(saved_hist["scraped"], [paths[0].stem])

            scraper.cancel()
            scraper.sticker_urls = ["https://p9-im-emoticon.byteimg.com/another.webp"]
            with (
                patch.object(config, "STICKER_DIR", tmp_dir),
                patch.object(config, "HISTORY_FILE", hist_file),
                patch("douyin_scraper.httpx.AsyncClient", return_value=mock_client),
            ):
                cancelled_paths = asyncio.run(scraper.download_stickers())
            self.assertEqual(cancelled_paths, [])


class TestStickerConverter(unittest.TestCase):
    def test_static_transparent_gif_and_animated_conversion_and_cancel(self):
        with tempfile.TemporaryDirectory() as tmp:
            src_dir = Path(tmp) / "src"
            out_dir = Path(tmp) / "out"
            src_dir.mkdir()

            static_png = src_dir / "static_alpha.png"
            static_png.write_bytes(_make_image_bytes((200, 200), "PNG"))

            fake_gif = src_dir / "disguised.gif"
            fake_gif.write_bytes(_make_image_bytes((160, 160), "WEBP"))

            anim_webp = src_dir / "anim40.webp"
            anim_webp.write_bytes(_make_image_bytes((240, 240), "WEBP", animated=True, frames=40))

            p1, m1 = convert_for_wechat(static_png, out_dir, force=True)
            self.assertIsNotNone(p1)
            self.assertEqual(p1.suffix, ".gif")
            with Image.open(p1) as im1:
                self.assertEqual(im1.format, "GIF")
                self.assertIn("transparency", im1.info)

            p2, m2 = convert_for_wechat(fake_gif, out_dir, force=True)
            self.assertIsNotNone(p2)
            self.assertEqual(p2.suffix, ".gif")

            p3, m3 = convert_for_wechat(
                anim_webp,
                out_dir,
                max_size=config.PRESET_COMPAT["max_size"],
                max_dim=config.PRESET_COMPAT["max_dim"],
                force=True,
            )
            self.assertIsNotNone(p3)
            self.assertEqual(p3.suffix, ".gif")
            self.assertTrue(m3["animated"])
            self.assertLessEqual(p3.stat().st_size, config.PRESET_COMPAT["max_size"])
            with Image.open(p3) as im:
                self.assertGreaterEqual(getattr(im, "n_frames", 1), 15)
                im.seek(0)
                self.assertGreaterEqual(im.info.get("duration", 0), 20)

            ev = threading.Event()
            ev.set()
            paths, stats = batch_convert(
                [static_png, anim_webp],
                output_dir=out_dir,
                force=True,
                cancel_event=ev,
                on_progress=lambda _: None,
            )
            self.assertTrue(stats["cancelled"])
            self.assertEqual(paths, [])


class TestWeChatSender(unittest.TestCase):
    def test_build_hdrop_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "中文表情.gif"
            p.write_bytes(b"GIF89a")
            raw = wechat_sender._build_hdrop_bytes(p)
            p_files, x, y, f_nc, f_wide = struct.unpack("<IiiII", raw[:20])
            self.assertEqual((p_files, x, y, f_nc, f_wide), (20, 0, 0, 0, 1))
            self.assertTrue(raw.endswith(b"\x00\x00\x00\x00"))
            decoded = raw[20:-4].decode("utf-16-le")
            self.assertEqual(decoded, str(p.resolve()))

    def test_send_skip_sent_and_cancel(self):
        with tempfile.TemporaryDirectory() as tmp:
            f1 = Path(tmp) / "wx_hash1.gif"
            f2 = Path(tmp) / "wx_hash2.gif"
            f3 = Path(tmp) / "wx_hash3.gif"
            for f in (f1, f2, f3):
                f.write_bytes(b"GIF89a")

            sent_recorded: list[str] = []
            cancel_ev = threading.Event()

            with (
                patch("wechat_sender.find_wechat_window", return_value=12345),
                patch("wechat_sender.focus_wechat_window", return_value=True),
                patch("wechat_sender.copy_file_to_clipboard") as mock_clip,
                patch("wechat_sender._press_ctrl_v"),
                patch("wechat_sender._press_enter"),
                patch("wechat_sender._sleep_interruptible", return_value=False),
            ):
                count = wechat_sender.send_stickers_to_wechat(
                    [f1, f2, f3],
                    skip_hashes={"hash1"},
                    send_delay=0.0,
                    cancel_event=cancel_ev,
                    on_progress=lambda _: None,
                    on_sent=lambda p: sent_recorded.append(p.stem),
                )

            self.assertEqual(count, 2)
            self.assertEqual(mock_clip.call_count, 2)
            self.assertEqual(sent_recorded, ["wx_hash2", "wx_hash3"])

            # 测试取消立即生效
            cancel_ev.set()
            with (
                patch("wechat_sender.find_wechat_window", return_value=12345),
                patch("wechat_sender.focus_wechat_window", return_value=True),
                patch("wechat_sender.copy_file_to_clipboard") as mock_clip2,
            ):
                count2 = wechat_sender.send_stickers_to_wechat(
                    [f2],
                    cancel_event=cancel_ev,
                    on_progress=lambda _: None,
                )
            self.assertEqual(count2, 0)
            self.assertEqual(mock_clip2.call_count, 0)


if __name__ == "__main__":
    unittest.main()
