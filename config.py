"""
配置文件 - 路径、超时、历史记录与微信发送参数
"""
import json
import sys
import threading
from pathlib import Path

# 应用根目录（兼容 PyInstaller 打包后的 .exe 同级目录）
APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
PROJECT_DIR = APP_DIR

# 下载的原始表情包保存目录
STICKER_DIR = PROJECT_DIR / "downloaded_stickers"
STICKER_DIR.mkdir(exist_ok=True)

# 转换压缩后的微信表情包输出目录
CONVERTED_DIR = PROJECT_DIR / "converted_stickers"
CONVERTED_DIR.mkdir(exist_ok=True)

# 独立 Edge 调试配置目录（避免关闭用户日常浏览器，同时持久化登录态）
EDGE_PROFILE_DIR = PROJECT_DIR / "edge_profile"

# 增量历史记录文件（记录已抓取与已发送的表情哈希）
HISTORY_FILE = PROJECT_DIR / "sticker_history.json"
_HISTORY_LOCK = threading.Lock()

# 抖音网页版 URL
DOUYIN_URL = "https://www.douyin.com"
DOUYIN_FAVORITE_EMOJI_URL = "https://www.douyin.com/user/self?showTab=favorite_emoji"

# Playwright 浏览器配置
BROWSER_HEADLESS = False  # 需要显示浏览器让用户扫码登录
BROWSER_SLOW_MO = 100     # 操作间隔(ms)，太快容易触发风控

# 登录等待超时(秒)
LOGIN_TIMEOUT = 120

# 抓取默认上限（0 表示不限制）
DEFAULT_SCRAPE_LIMIT = 0

# 微信自定义表情上限(每个表情包专辑)
WECHAT_STICKER_LIMIT = 999

# 电脑版微信连续发送间隔(秒)
WECHAT_SEND_DELAY = 0.8

# 压缩预设
PRESET_HD = {
    "label": "高清模式 (≤950KB, 推荐)",
    "max_size": 950 * 1024,
    "max_dim": 360,
}
PRESET_COMPAT = {
    "label": "兼容模式 (≤500KB)",
    "max_size": 490 * 1024,
    "max_dim": 300,
}

# 请求头
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Referer": "https://www.douyin.com/",
}

# 日志级别
LOG_LEVEL = "INFO"


def load_history() -> dict[str, list[str]]:
    """读取已抓取/已发送的表情哈希历史"""
    with _HISTORY_LOCK:
        if not HISTORY_FILE.exists():
            return {"scraped": [], "sent": []}
        try:
            data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {
                    "scraped": [str(x) for x in data.get("scraped", []) if isinstance(x, str)],
                    "sent": [str(x) for x in data.get("sent", []) if isinstance(x, str)],
                }
        except Exception:
            pass
        return {"scraped": [], "sent": []}


def save_history(history: dict[str, list[str]]) -> None:
    """原子写入表情哈希历史"""
    with _HISTORY_LOCK:
        clean = {
            "scraped": list(dict.fromkeys(str(x) for x in history.get("scraped", []))),
            "sent": list(dict.fromkeys(str(x) for x in history.get("sent", []))),
        }
        tmp = HISTORY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(HISTORY_FILE)


def record_history(kind: str, hashes: list[str]) -> None:
    """增量追加指定类型的哈希记录（scraped 或 sent）"""
    if kind not in ("scraped", "sent") or not hashes:
        return
    with _HISTORY_LOCK:
        if HISTORY_FILE.exists():
            try:
                data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        else:
            data = {}
        existing = [str(x) for x in data.get(kind, []) if isinstance(x, str)]
        merged = list(dict.fromkeys([*existing, *(str(h) for h in hashes if h)]))
        clean = {
            "scraped": merged if kind == "scraped" else [str(x) for x in data.get("scraped", []) if isinstance(x, str)],
            "sent": merged if kind == "sent" else [str(x) for x in data.get("sent", []) if isinstance(x, str)],
        }
        tmp = HISTORY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(HISTORY_FILE)
