"""
表情包格式转换与压缩模块
WebP → GIF 转换、尺寸/大小压缩，满足微信自定义表情要求
"""
import logging
from pathlib import Path

from PIL import Image

logger = logging.getLogger(__name__)

MAX_FILE_SIZE = 500 * 1024  # 500KB
MAX_DIMENSION = 320


def convert_webp_to_gif(webp_path: Path) -> Path | None:
    """将 WebP 转换为 GIF"""
    try:
        gif_path = webp_path.with_suffix(".gif")
        if gif_path.exists():
            return gif_path

        img = Image.open(webp_path)
        frames = []
        durations = []

        try:
            while True:
                frame = img.copy().convert("RGBA")
                frames.append(frame)
                durations.append(max(img.info.get("duration", 100), 20))
                img.seek(img.tell() + 1)
        except EOFError:
            pass

        if len(frames) <= 1:
            frames[0].convert("RGB").save(gif_path, "GIF", optimize=True)
        else:
            frames[0].save(
                gif_path, save_all=True, append_images=frames[1:],
                duration=durations, loop=0, disposal=2,
            )

        logger.info(f"转换: {webp_path.name} -> {gif_path.name} ({len(frames)}帧)")
        return gif_path
    except Exception as e:
        logger.error(f"转换 WebP 失败 {webp_path.name}: {e}")
        return None


def compress_gif(img, out_path: Path) -> Path:
    """压缩 GIF 动图"""
    frames, durations = [], []
    try:
        while True:
            frames.append(img.copy().convert("RGBA"))
            durations.append(img.info.get("duration", 100))
            img.seek(img.tell() + 1)
    except EOFError:
        pass
    if not frames:
        return out_path

    w, h = frames[0].size
    if w > MAX_DIMENSION or h > MAX_DIMENSION:
        ratio = min(MAX_DIMENSION / w, MAX_DIMENSION / h)
        new_size = (int(w * ratio), int(h * ratio))
        frames = [f.resize(new_size, Image.LANCZOS) for f in frames]

    for q in [95, 85, 75, 65, 55]:
        frames[0].save(out_path, save_all=True, append_images=frames[1:],
                       duration=durations, loop=0, disposal=2, optimize=True)
        if out_path.stat().st_size <= MAX_FILE_SIZE:
            return out_path

    step = 3
    while len(frames) > 3:
        frames = frames[::step]
        durations = [max(sum(durations[i:i+step]), 20) for i in range(0, len(durations), step)]
        frames[0].save(out_path, save_all=True, append_images=frames[1:],
                       duration=durations, loop=0, disposal=2, optimize=True)
        if out_path.stat().st_size <= MAX_FILE_SIZE:
            return out_path
        step += 1
    return out_path


def static_to_gif(img, out_path: Path) -> Path:
    """静态图转 GIF"""
    w, h = img.size
    if w > MAX_DIMENSION or h > MAX_DIMENSION:
        ratio = min(MAX_DIMENSION / w, MAX_DIMENSION / h)
        img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)

    if img.mode != "RGB":
        if img.mode in ("RGBA", "P", "LA", "PA"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            if "A" in img.mode:
                bg.paste(img, mask=img.split()[-1])
            else:
                bg.paste(img)
            img = bg
        else:
            img = img.convert("RGB")

    img.save(out_path, "GIF", optimize=True)
    return out_path


def compress_for_wechat(path: Path, output_dir: Path | None = None) -> Path:
    """压缩图片到微信表情要求：≤500KB, ≤320×320, GIF"""
    out_path = (output_dir or path.parent) / f"wx_{path.stem}.gif"
    if out_path.exists():
        return out_path
    try:
        img = Image.open(path)
        if path.suffix.lower() == ".gif":
            return compress_gif(img, out_path)
        return static_to_gif(img, out_path)
    except Exception as e:
        logger.error(f"压缩失败 {path.name}: {e}")
        return path


def batch_convert(paths: list[Path], on_progress=None) -> list[Path]:
    """批量转换+压缩表情包"""
    emit = on_progress or (lambda msg: logger.info(msg))
    result = []
    for i, path in enumerate(paths):
        if not path.exists():
            continue
        work_path = path
        if path.suffix.lower() == ".webp":
            gif = convert_webp_to_gif(path)
            if gif:
                work_path = gif
        compressed = compress_for_wechat(work_path)
        result.append(compressed)
        emit(f"[{i+1}/{len(paths)}] 已处理: {compressed.name}")
    emit(f"转换完成: {len(result)}/{len(paths)} 个")
    return result
