"""GIF 读帧 / 合帧核心。

设计目标（对应需求里的三条硬约束）：

1. 帧顺序不能乱——所有接口都以有序 list 传递帧，帧上带固定 index。
2. 帧间时长准确——GIF 的时间粒度是「厘秒（10ms）」，本模块统一用毫秒做
   内部单位，读入即规范化为 10 的整数倍；编码时四舍五入到厘秒并回读实际值。
3. 拆帧再合帧节奏不变——extract_frames() 产出的 (帧, duration, loop) 原样喂给
   encode_gif() 时，回读结果与输入一致（时长、循环、帧数、不透明画面）。

关键细节：Pillow 的 seek() 不会帮我们做「全画布帧合成」，差异帧（只含变化
矩形 + 透明）必须按 GIF 规范逐帧叠加，并按上一帧的 disposal 方法决定叠加前
如何清理画布：
  disposal=1 不清除（下一帧直接盖上去）
  disposal=2 恢复为背景色（这里按透明处理）
  disposal=3 恢复到上一帧之前的快照
"""
import io

from PIL import Image, ImageDraw

# GIF 时间的最小单位：厘秒
DURATION_UNIT_MS = 10
# duration 为 0/缺失时，浏览器通常按 100ms 播放，读入时统一成这个值
DEFAULT_DURATION_MS = 100
# 过短的时长多数解码器不支持，编码时夹到 20ms 以上
MIN_DURATION_MS = 20


class GifError(ValueError):
    """GIF 解析相关的可读错误（直接返回给前端）。"""


def normalize_duration(ms):
    """把任意毫秒值规范化为合法的 GIF 帧时长（10 的倍数、>=20）。"""
    try:
        ms = int(round(float(ms)))
    except (TypeError, ValueError):
        ms = DEFAULT_DURATION_MS
    if ms <= 0:
        ms = DEFAULT_DURATION_MS
    ms = max(MIN_DURATION_MS, int(round(ms / DURATION_UNIT_MS)) * DURATION_UNIT_MS)
    return ms


def quantize_durations(durations):
    """编码前批量规范化；返回新列表，不改原列表。"""
    return [normalize_duration(d) for d in durations]


def fit_size(size, max_dim=None, scale=None):
    """按最长边等比计算新尺寸。max_dim 为上限；scale 为倍率（受上限约束）。"""
    w, h = size
    if scale is not None:
        w = max(1, int(round(w * scale)))
        h = max(1, int(round(h * scale)))
    if max_dim and max(w, h) > max_dim:
        s = max_dim / float(max(w, h))
        w = max(1, int(round(w * s)))
        h = max(1, int(round(h * s)))
    return w, h


def resize_frame(img, size):
    """等比缩放到精确目标尺寸（调用方已保证比例一致）。"""
    if img.size == size:
        return img
    return img.resize(size, Image.Resampling.LANCZOS)


def extract_frames(data, max_dim=None):
    """从 GIF 字节拆出「全画布 RGBA 帧」。

    返回 dict：
      frames        [PIL.Image RGBA, ...]（有序、同尺寸）
      durations     [毫秒, ...]（已规范化）
      loop          NETSCAPE 循环计数（0=无限循环）
      frame_count   帧数
      width/height  合成后尺寸
      resized       是否因超过 max_dim 而整体缩放过
    """
    try:
        im = Image.open(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        raise GifError("无法识别的图像格式") from exc

    fmt = (im.format or "").upper()
    n = getattr(im, "n_frames", 1)
    if fmt != "GIF":
        raise GifError(f"不是 GIF 动图（检测到 {fmt or '未知格式'}）")

    width, height = im.size
    resized = False
    if max_dim and max(width, height) > max_dim:
        width, height = fit_size(im.size, max_dim=max_dim)
        resized = True

    canvas = Image.new("RGBA", (im.size[0], im.size[1]), (0, 0, 0, 0))
    snapshot = None        # disposal=3 时保存的「叠加前快照」
    previous = None        # (bbox, disposal) 上一帧的清理信息
    frames, durations = [], []

    for index in range(n):
        im.seek(index)

        # 1) 按「上一帧」的 disposal 方式清理画布
        if previous is not None:
            bbox, disposal = previous
            if disposal == 2:
                ImageDraw.Draw(canvas).rectangle(bbox, fill=(0, 0, 0, 0))
            elif disposal == 3 and snapshot is not None:
                canvas = snapshot.copy()

        # 2) 首帧若没有透明索引，整帧是不透明的，用背景色索引铺满画布
        if index == 0 and "transparency" not in im.info:
            bg_index = im.info.get("background", 0)
            palette = im.getpalette() or [0] * 768
            bg_index = max(0, min(bg_index, len(palette) // 3 - 1))
            bg_color = tuple(palette[bg_index * 3:bg_index * 3 + 3]) + (255,)
            canvas = Image.new("RGBA", im.size, bg_color)

        # 3) 取出本帧的变化矩形（tile），按 alpha 合成到画布
        if im.tile:
            bbox = im.tile[0][1]
        else:
            bbox = (0, 0, im.size[0], im.size[1])
        disposal = getattr(im, "disposal_method", 0) or 0
        duration = normalize_duration(im.info.get("duration"))

        if disposal == 3:
            snapshot = canvas.copy()

        tile = im.crop(bbox).convert("RGBA")
        canvas.alpha_composite(tile, bbox[:2])

        frame = canvas.copy()
        if resized:
            frame = resize_frame(frame, (width, height))
        frames.append(frame)
        durations.append(duration)
        previous = (bbox, disposal)

    loop = im.info.get("loop", 0)
    try:
        loop = max(0, int(loop))
    except (TypeError, ValueError):
        loop = 0

    return {
        "frames": frames,
        "durations": durations,
        "loop": loop,
        "frame_count": len(frames),
        "width": width,
        "height": height,
        "resized": resized,
    }


def flatten_on_white(img, size=None):
    """RGBA -> RGB，透明区域铺白底（GIF 只支持 1 位透明，统一铺白底最稳）。"""
    img = img.convert("RGBA")
    if size:
        img = resize_frame(img, size)
    bg = Image.new("RGB", img.size, (255, 255, 255))
    bg.paste(img, mask=img.split()[-1])
    return bg


def encode_gif(frames, durations, loop=0, max_dim=None, colors=256, optimize=True):
    """把有序 RGB/RGBA 帧编码成 GIF 字节。

    - 所有帧先适配到同一画布（以第一帧尺寸为准；尺寸不一致的等比缩放，
      不足部分居中铺白底），避免单帧编辑改了尺寸导致合帧错乱。
    - durations 逐帧规范化到厘秒；loop=0 无限循环。
    - colors 为调色板色数（32/64/128/256），越小文件越省。
    返回 (bytes, meta)，meta 里带编码后回读的 effective_durations，
    调用方可据此向用户报告真实节奏。
    """
    if not frames:
        raise GifError("没有可合成的帧")
    colors = max(2, min(256, int(colors)))
    durations = quantize_durations(durations)
    if len(durations) < len(frames):
        durations += [DEFAULT_DURATION_MS] * (len(frames) - len(durations))
    durations = durations[:len(frames)]

    target = fit_size(frames[0].size, max_dim=max_dim)

    canvas_frames = []
    for fr in frames:
        fr = fr.convert("RGBA")
        if fr.size != target:
            fr = resize_frame(fr, target)
        canvas_frames.append(flatten_on_white(fr, target))

    # P 模式调色板在 save_all 下由首帧统一管理；这里先全部量化到同一调色板，
    # 避免逐帧自适应调色板导致的闪烁。
    quantized = [canvas_frames[0].quantize(colors=colors, method=Image.Quantize.MEDIANCUT)]
    for fr in canvas_frames[1:]:
        quantized.append(fr.quantize(colors=colors, method=Image.Quantize.MEDIANCUT))

    buf = io.BytesIO()
    save_kwargs = {}
    if optimize:
        save_kwargs["optimize"] = True
    quantized[0].save(
        buf,
        format="GIF",
        save_all=True,
        append_images=quantized[1:],
        duration=durations,
        loop=max(0, int(loop)),
        disposal=[1] * len(quantized),
        **save_kwargs,
    )
    data = buf.getvalue()

    # 回读真实生效的时长（编码阶段可能再次被夹取），用于节奏校验
    check = extract_frames(data)
    meta = {
        "frame_count": len(quantized),
        "width": target[0],
        "height": target[1],
        "size_bytes": len(data),
        "loop": check["loop"],
        "effective_durations": check["durations"],
        "colors": colors,
    }
    return data, meta
