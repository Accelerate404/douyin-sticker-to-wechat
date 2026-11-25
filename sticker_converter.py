"""
表情包格式转换与压缩模块
- 真实内容识别（不依赖文件扩展名，修复伪装扩展名导致的解码问题）
- 静态图保存为高清 PNG / JPG（保留透明通道，避免强转 256 色 GIF 变糊）
- 动图采用全局调色板 + 无复合掉帧的渐进压缩阶梯，优先保帧率与清晰度
"""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

from PIL import Image, ImageSequence

import config

logger = logging.getLogger(__name__)

DEFAULT_MAX_SIZE = config.PRESET_HD["max_size"]
DEFAULT_MAX_DIM = config.PRESET_HD["max_dim"]
MAX_INITIAL_FRAMES = 80


def _has_meaningful_alpha(img: Image.Image) -> bool:
    """检查图像是否包含实际使用的透明像素"""
    if img.mode in ("RGBA", "LA", "PA"):
        alpha = img.getchannel("A")
        return alpha.getextrema()[0] < 250
    if img.mode == "P" and "transparency" in img.info:
        rgba = img.convert("RGBA")
        return rgba.getchannel("A").getextrema()[0] < 250
    return False


def _frames_have_alpha(frames: list[Image.Image]) -> bool:
    """抽样检查动图帧是否包含透明通道"""
    if not frames:
        return False
    step = max(1, len(frames) // 10)
    for frame in frames[::step]:
        if frame.mode == "RGBA" and frame.getchannel("A").getextrema()[0] < 250:
            return True
    return False


def _resize_frames(frames: list[Image.Image], max_dim: int) -> list[Image.Image]:
    """按比例缩放帧列表，不超过 max_dim"""
    w, h = frames[0].size
    if max(w, h) <= max_dim:
        return frames
    ratio = min(max_dim / w, max_dim / h)
    new_size = (max(1, int(round(w * ratio))), max(1, int(round(h * ratio))))
    return [f.resize(new_size, Image.Resampling.LANCZOS) for f in frames]


def _resample_frames(
    orig_frames: list[Image.Image],
    orig_durations: list[int],
    target_count: int,
) -> tuple[list[Image.Image], list[int]]:
    """
    从原始帧序列均匀重采样至 target_count 帧，
    将被跳过帧的时长累加到保留帧上，严格保持总时长与播放速度不变。
    """
    n = len(orig_frames)
    if target_count >= n or target_count <= 0:
        return list(orig_frames), list(orig_durations)

    indices = sorted({int(i * (n - 1) / max(1, target_count - 1)) for i in range(target_count)})
    # 确保最后一帧索引不越界且覆盖尾部时长
    new_frames: list[Image.Image] = []
    new_durations: list[int] = []
    for k, idx in enumerate(indices):
        next_idx = indices[k + 1] if k + 1 < len(indices) else n
        dur_sum = sum(orig_durations[idx:next_idx])
        new_frames.append(orig_frames[idx])
        new_durations.append(max(20, dur_sum))

    return new_frames, new_durations


def _build_global_palette(
    rgb_frames: list[Image.Image],
    colors: int,
) -> Image.Image:
    """从多帧采样拼接马赛克生成全局共享调色板，消除帧间色偏闪烁"""
    sample_count = min(12, len(rgb_frames))
    step = max(1, len(rgb_frames) // sample_count)
    samples = rgb_frames[::step][:sample_count]

    w, h = samples[0].size
    mosaic = Image.new("RGB", (w * len(samples), h))
    for i, frame in enumerate(samples):
        mosaic.paste(frame, (i * w, 0))

    return mosaic.quantize(
        colors=colors,
        method=Image.Quantize.MEDIANCUT,
        dither=Image.Dither.NONE,
    )


def _encode_gif(
    frames: list[Image.Image],
    durations: list[int],
    out_path: Path,
    colors: int,
    has_alpha: bool,
    dither: Image.Dither,
) -> int:
    """将 RGBA 帧列表编码为优化的 GIF 文件，返回文件字节数"""
    if not has_alpha:
        rgb_frames = [f.convert("RGB") for f in frames]
        palette_img = _build_global_palette(rgb_frames, colors)
        p_frames = [f.quantize(palette=palette_img, dither=dither) for f in rgb_frames]
        if len(p_frames) == 1:
            p_frames[0].save(out_path, format="GIF", optimize=True)
        else:
            p_frames[0].save(
                out_path,
                format="GIF",
                save_all=True,
                append_images=p_frames[1:],
                duration=durations,
                loop=0,
                disposal=1,
                optimize=True,
            )
        return out_path.stat().st_size

    # 含透明背景：留出 1 个调色板索引作为透明色，半透明边缘先合成到白底避免黑边
    palette_colors = max(2, min(255, colors - 1))
    trans_idx = palette_colors

    rgb_frames: list[Image.Image] = []
    alphas: list[Image.Image] = []
    for f in frames:
        alpha = f.getchannel("A")
        bg = Image.new("RGB", f.size, (255, 255, 255))
        bg.paste(f, mask=alpha)
        rgb_frames.append(bg)
        alphas.append(alpha)

    palette_img = _build_global_palette(rgb_frames, palette_colors)
    raw_pal = list(palette_img.getpalette() or [])
    if len(raw_pal) < 768:
        raw_pal.extend([0] * (768 - len(raw_pal)))
    raw_pal[trans_idx * 3 : trans_idx * 3 + 3] = [255, 255, 255]

    p_frames: list[Image.Image] = []
    for rgb, alpha in zip(rgb_frames, alphas):
        q = rgb.quantize(palette=palette_img, dither=dither)
        q.putpalette(raw_pal)
        mask = alpha.point(lambda a: 255 if a < 128 else 0)
        q.paste(trans_idx, mask=mask)
        p_frames.append(q)

    if len(p_frames) == 1:
        p_frames[0].save(
            out_path,
            format="GIF",
            transparency=trans_idx,
            optimize=True,
        )
    else:
        p_frames[0].save(
            out_path,
            format="GIF",
            save_all=True,
            append_images=p_frames[1:],
            duration=durations,
            loop=0,
            disposal=2,
            transparency=trans_idx,
            optimize=True,
        )
    return out_path.stat().st_size


def _extract_animated_frames(img: Image.Image) -> tuple[list[Image.Image], list[int]]:
    """提取动图所有 RGBA 帧和安全的帧间隔(ms)"""
    frames: list[Image.Image] = []
    durations: list[int] = []
    for frame in ImageSequence.Iterator(img):
        frames.append(frame.convert("RGBA"))
        raw_dur = frame.info.get("duration", img.info.get("duration", 0))
        if not isinstance(raw_dur, (int, float)) or raw_dur <= 0:
            dur = 80
        else:
            dur = max(int(raw_dur), 20)
        durations.append(dur)
    return frames, durations


def _convert_static(
    img: Image.Image,
    stem: str,
    out_dir: Path,
    max_size: int,
    max_dim: int,
) -> Path:
    """将静态表情转换为高清透明/不透明单帧 GIF，确保电脑版微信右键可直接「添加到表情」"""
    static_dim = min(max(max_dim, 320), 400)
    rgba = img.convert("RGBA")
    w, h = rgba.size
    if max(w, h) > static_dim:
        ratio = min(static_dim / w, static_dim / h)
        rgba = rgba.resize(
            (max(1, int(round(w * ratio))), max(1, int(round(h * ratio)))),
            Image.Resampling.LANCZOS,
        )

    has_alpha = _has_meaningful_alpha(img)
    out_path = out_dir / f"wx_{stem}.gif"

    size = _encode_gif([rgba], [100], out_path, 256, has_alpha, Image.Dither.FLOYDSTEINBERG)
    if size <= max_size:
        return out_path

    size = _encode_gif([rgba], [100], out_path, 128, has_alpha, Image.Dither.NONE)
    if size <= max_size:
        return out_path

    cur = rgba
    while size > max_size and min(cur.size) > 120:
        nw, nh = max(64, int(cur.width * 0.85)), max(64, int(cur.height * 0.85))
        cur = cur.resize((nw, nh), Image.Resampling.LANCZOS)
        size = _encode_gif([cur], [100], out_path, 128, has_alpha, Image.Dither.NONE)
    return out_path


def _convert_animated(
    img: Image.Image,
    stem: str,
    out_dir: Path,
    max_size: int,
    max_dim: int,
) -> Path:
    """将动图按渐进策略压缩为高帧率清晰 GIF"""
    out_path = out_dir / f"wx_{stem}.gif"
    orig_frames, orig_durations = _extract_animated_frames(img)
    if not orig_frames:
        raise ValueError("未提取到任何有效帧")

    if len(orig_frames) == 1:
        return _convert_static(orig_frames[0], stem, out_dir, max_size, max_dim)

    has_alpha = _frames_have_alpha(orig_frames)
    orig_max = max(orig_frames[0].size)

    # 帧数极多（>80）先均匀压到 80 帧，避免微信解码卡顿或超限
    if len(orig_frames) > MAX_INITIAL_FRAMES:
        base_frames, base_durations = _resample_frames(
            orig_frames, orig_durations, MAX_INITIAL_FRAMES
        )
    else:
        base_frames, base_durations = orig_frames, orig_durations

    n_base = len(base_frames)
    resized_cache: dict[int, list[Image.Image]] = {}

    def get_resized(target_dim: int) -> list[Image.Image]:
        dim = min(orig_max, target_dim)
        if dim not in resized_cache:
            resized_cache[dim] = _resize_frames(base_frames, dim)
        return resized_cache[dim]

    # 1~3 阶段：保完整帧率，优先调色板与尺寸
    full_fps_stages = [
        (max_dim, 256, Image.Dither.FLOYDSTEINBERG),
        (max_dim, 128, Image.Dither.NONE),
        (min(max_dim, 260), 128, Image.Dither.NONE),
    ]
    for dim, colors, dither in full_fps_stages:
        frames = get_resized(dim)
        size = _encode_gif(frames, base_durations, out_path, colors, has_alpha, dither)
        if size <= max_size:
            return out_path

    # 4~5 阶段：始终基于 base_frames 按比例温和抽帧（绝不叠乘抽帧）
    min_keep = min(n_base, 8)
    tried_counts: set[int] = set()
    resample_configs = [
        (min(max_dim, 240), 96, [0.75, 0.60, 0.50, 0.40, 0.30]),
        (min(max_dim, 200), 64, [0.50, 0.35, 0.25]),
    ]
    for dim, colors, ratios in resample_configs:
        r_frames = get_resized(dim)
        for ratio in ratios:
            target = max(min_keep, int(round(n_base * ratio)))
            if target >= n_base:
                continue
            key = (dim, colors, target)
            if key in tried_counts:
                continue
            tried_counts.add(key)
            sub_frames, sub_durs = _resample_frames(r_frames, base_durations, target)
            size = _encode_gif(sub_frames, sub_durs, out_path, colors, has_alpha, Image.Dither.NONE)
            if size <= max_size:
                return out_path

    # 兜底压缩
    fallback_dim = min(orig_max, 160)
    fallback_target = max(min(n_base, 4), int(round(n_base * 0.2)))
    sub_frames, sub_durs = _resample_frames(
        get_resized(fallback_dim), base_durations, fallback_target
    )
    _encode_gif(sub_frames, sub_durs, out_path, 32, has_alpha, Image.Dither.NONE)
    return out_path


def _find_cached_output(stem: str, out_dir: Path, max_size: int) -> Path | None:
    """检查目标目录是否已有符合体积限制的 GIF 转换产物"""
    candidate = out_dir / f"wx_{stem}.gif"
    if candidate.exists() and 0 < candidate.stat().st_size <= max_size:
        return candidate
    return None


def _validate_image(path: Path) -> bool:
    """校验生成图片可正常打开且非空"""
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        with Image.open(path) as im:
            im.load()
            return im.size[0] > 0 and im.size[1] > 0
    except Exception:
        return False


def convert_for_wechat(
    path: Path,
    output_dir: Path | None = None,
    max_size: int = DEFAULT_MAX_SIZE,
    max_dim: int = DEFAULT_MAX_DIM,
    force: bool = False,
) -> tuple[Path | None, dict]:
    """
    将单个表情包转换为微信兼容格式。
    返回 (输出路径或 None, 元信息字典)。
    """
    out_dir = output_dir or config.CONVERTED_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = path.stem

    if not force:
        cached = _find_cached_output(stem, out_dir, max_size)
        if cached and _validate_image(cached):
            is_anim = cached.suffix.lower() == ".gif"
            return cached, {
                "cached": True,
                "animated": is_anim,
                "size": cached.stat().st_size,
                "oversized": False,
            }

    try:
        with Image.open(path) as img:
            n_frames = getattr(img, "n_frames", 1)
            if n_frames > 1:
                out_path = _convert_animated(img, stem, out_dir, max_size, max_dim)
                is_anim = out_path.suffix.lower() == ".gif"
            else:
                out_path = _convert_static(img, stem, out_dir, max_size, max_dim)
                is_anim = False

        if not _validate_image(out_path):
            logger.error(f"输出校验失败: {path.name}")
            return None, {"error": "output validation failed"}

        size = out_path.stat().st_size
        return out_path, {
            "cached": False,
            "animated": is_anim,
            "size": size,
            "oversized": size > max_size,
        }
    except Exception as e:
        logger.error(f"转换失败 {path.name}: {e}")
        return None, {"error": str(e)}


def batch_convert(
    paths: list[Path],
    output_dir: Path | None = None,
    max_size: int = DEFAULT_MAX_SIZE,
    max_dim: int = DEFAULT_MAX_DIM,
    force: bool = False,
    max_workers: int = 4,
    cancel_event: threading.Event | None = None,
    on_progress: Callable[[str], None] | None = None,
    on_progress_pct: Callable[[int, int], None] | None = None,
) -> tuple[list[Path], dict]:
    """
    并发批量转换表情包，返回 (转换成功路径列表, 统计信息)。
    """
    emit = on_progress or (lambda msg: logger.info(msg))
    out_dir = output_dir or config.CONVERTED_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    # 过滤无效路径并跳过已经是 wx_ 开头的输出文件（防止二次套娃）
    valid_paths = [
        p for p in paths
        if p.exists() and p.stat().st_size > 0 and not p.name.startswith("wx_")
    ]
    if not valid_paths:
        valid_paths = [p for p in paths if p.exists() and p.stat().st_size > 0]

    total = len(valid_paths)
    results_by_idx: dict[int, Path] = {}
    stats = {
        "total": total,
        "success": 0,
        "failed": 0,
        "oversized": 0,
        "static_count": 0,
        "animated_count": 0,
        "cancelled": False,
    }

    if total == 0:
        return [], stats

    def _convert_task(p: Path):
        if cancel_event and cancel_event.is_set():
            return None, {"cancelled": True}
        return convert_for_wechat(p, out_dir, max_size, max_dim, force)

    completed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_map = {
            pool.submit(_convert_task, p): idx
            for idx, p in enumerate(valid_paths)
        }
        for future in as_completed(future_map):
            if cancel_event and cancel_event.is_set():
                stats["cancelled"] = True
                for pending_f in future_map:
                    pending_f.cancel()
                break

            idx = future_map[future]
            src_path = valid_paths[idx]
            completed += 1
            try:
                out_path, meta = future.result()
            except Exception as e:
                out_path, meta = None, {"error": str(e)}

            if meta.get("cancelled"):
                stats["cancelled"] = True
                break

            if out_path is not None:
                results_by_idx[idx] = out_path
                stats["success"] += 1
                if meta.get("animated"):
                    stats["animated_count"] += 1
                else:
                    stats["static_count"] += 1
                if meta.get("oversized"):
                    stats["oversized"] += 1
                kb = meta.get("size", 0) // 1024
                tag = "缓存" if meta.get("cached") else "转换"
                emit(f"[{completed}/{total}] {tag}: {out_path.name} ({kb}KB)")
            else:
                stats["failed"] += 1
                emit(f"[{completed}/{total}] 失败: {src_path.name} ({meta.get('error', '未知错误')})")

            if on_progress_pct:
                on_progress_pct(completed, total)

    ordered_paths = [results_by_idx[i] for i in sorted(results_by_idx)]
    if stats["cancelled"]:
        emit(f"转换已停止（已完成 {stats['success']}/{total}）")
    else:
        emit(
            f"转换完成: 成功 {stats['success']}/{total} "
            f"(动图 {stats['animated_count']}, 静图 {stats['static_count']}, "
            f"失败 {stats['failed']}, 超限 {stats['oversized']})"
        )
    return ordered_paths, stats
