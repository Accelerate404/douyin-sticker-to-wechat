"""
配置文件 - 路径、超时等参数
"""
import os
from pathlib import Path

# 项目根目录
PROJECT_DIR = Path(__file__).parent

# 下载的表情包保存目录
STICKER_DIR = PROJECT_DIR / "downloaded_stickers"
STICKER_DIR.mkdir(exist_ok=True)

# 抖音网页版 URL
DOUYIN_URL = "https://www.douyin.com"
DOUYIN_FAVORITE_EMOJI_URL = "https://www.douyin.com/user/self?showTab=favorite_emoji"

# Playwright 浏览器配置
BROWSER_HEADLESS = False  # 需要显示浏览器让用户扫码登录
BROWSER_SLOW_MO = 100     # 操作间隔(ms)，太快容易触发风控

# 登录等待超时(秒)
LOGIN_TIMEOUT = 120

# 微信 PC 版窗口标题关键词
WECHAT_WINDOW_TITLE = "微信"

# 微信自定义表情上限(每个表情包专辑)
WECHAT_STICKER_LIMIT = 999

# 请求头
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Referer": "https://www.douyin.com/",
}

# 日志级别
LOG_LEVEL = "INFO"
