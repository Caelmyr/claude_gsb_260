"""GIF 动图引擎：多图合成、动图拆帧、逐帧处理后重新合帧。

围绕用户最关心的三个「不能错」设计：

帧顺序不能乱
    拆帧严格按 GIF 的帧索引 0..n-1 落盘（文件名带零填充序号），合帧按索引升序
    读回；会话元数据里保存 frames 有序列表，任何接口都不依赖目录列举顺序。

帧间时长要准确
    GIF89a 的帧时长单位是 1/100 秒（centisecond），只能表达 10ms 的整数倍，
    且规范上 0 会被多数播放器解释为默认值。normalize_durations_ms() 在入口统一
    归一化（钳制上下限并对齐到 10ms），并把「请求值 / 实际值」一并返回，
    前端可以如实展示。拆帧读到的时长本身就是合法 centisecond，原样写回即零损失。

拆帧再合帧节奏不变
    Pillow 的 GIF 解码器在 seek() 时已经按照 disposal 方法
    （1 保留 / 2 恢复背景 / 3 恢复上一帧）和帧的子矩形偏移做了完整画布合成，
    因此 extract_frames() 每帧拿到的都是「该时刻应显示的完整画面」；
    重新保存时固定 disposal=2（每帧独立、恢复背景），并用 optimize=False ——
    Pillow 的 GIF 优化会把内容相同的相邻帧合并（帧数减少、时长被累加），
    与逐帧处理的语义冲突，必须关掉。
"""
import io

from PIL import Image

from . import config
from .algorithms import util

# ---------------------------------------------------------------------------
# 时长
# ---------------------------------------------------------------------------
def normalize_duration_ms(value, default=None):
    """把单个时长（毫秒）归一化为 GIF 可表达的合法值：10ms 的整数倍，带上下限。

    返回 (requested_ms, actual_ms)。非法/缺省输入用 default（再缺省用全局默认）。
    """
    lo, hi = config.GIF_MIN_DURATION_MS, config.GIF_MAX_DURATION_MS
    fallback = config.GIF_DEFAULT_DURATION_MS if default is None else default
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = float(fallback)
    if v != v or v <= 0:  # NaN 或非正数：缺省/0 都按默认值处理（0 在播放器里语义不定）
        v = float(fallback)
    v = max(lo, min(hi, v))
    return int(round(v)), int(round(v / 10.0)) * 10


def normalize_durations_ms(values, count):
    """批量归一化。values 可为标量（应用到全部）或列表（逐帧，缺项用默认值）。

    返回 (requested_list, actual_list)，长度均为 count，单位毫秒。
    """
    default = config.GIF_DEFAULT_DURATION_MS
    if values is None or isinstance(values, (int, float)):
        seq = [values if values is not None else default] * count
    else:
        seq = list(values)
        if len(seq) < count:
            seq = seq + [default] * (count - len(seq))
    requested, actual = [], []
    for v in seq[:count]:
        r, a = normalize_duration_ms(v, default=default)
        requested.append(r)
        actual.append(a)
    return requested, actual


# ---------------------------------------------------------------------------
# 尺寸
# ---------------------------------------------------------------------------
def normalize_size(frames, max_width=None):
    """把所有帧统一到同一尺寸（GIF 要求等大），必要时等比缩放。

    - 以第一帧尺寸为基准（尺寸压缩时基准整体等比缩小）；
    - 其余帧若比例/尺寸不同，按「contain」等比缩放到能放进基准画布，
      再居中贴到白底基准画布上（留白不变形）——多张比例不同的照片合成时
      不会被拉伸；相同比例的帧（含拆帧所得帧、压缩输出）精确填满、无留白。
    """
    if not frames:
        return frames
    base_w, base_h = frames[0].size
    if max_width:
        max_width = max(16, int(max_width))
        if max(base_w, base_h) > max_width:
            scale = max_width / float(max(base_w, base_h))
            base_w = max(1, int(round(base_w * scale)))
            base_h = max(1, int(round(base_h * scale)))

    out = []
    for fr in frames:
        fr = util.ensure_rgb(fr)
        if fr.size == (base_w, base_h):
            out.append(fr)
            continue
        w, h = fr.size
        scale = min(base_w / float(w), base_h / float(h))
        nw = max(1, int(round(w * scale)))
        nh = max(1, int(round(h * scale)))
        fitted = fr.resize((nw, nh), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (base_w, base_h), (255, 255, 255))
        canvas.paste(fitted, ((base_w - nw) // 2, (base_h - nh) // 2))
        out.append(canvas)
    return out


# ---------------------------------------------------------------------------
# 拆帧
# ---------------------------------------------------------------------------
def extract_frames(gif_bytes, max_frames=None):
    """把一张 GIF 拆成有序帧。

    返回 dict：
      frames   - list[PIL.Image RGB]，第 i 项即播放到第 i 帧时的完整画面
      durations_ms - list[int]，每帧时长（毫秒，已是合法 centisecond 值）
      width/height/loop - 原动图信息（loop: 0=无限循环，>0=播放次数）
    异常：ValueError（非 GIF / 无多帧 / 超过帧数上限）。
    """
    limit = max_frames or config.GIF_MAX_FRAMES
    try:
        im = Image.open(io.BytesIO(gif_bytes))
    except Exception as exc:  # noqa: BLE001
        raise ValueError("无法识别的图像格式") from exc

    if (im.format or "").upper() != "GIF":
        raise ValueError("仅支持 GIF 动图拆帧")
    n = getattr(im, "n_frames", 1)
    if n <= 1:
        raise ValueError("该文件不是多帧动图")
    if n > limit:
        raise ValueError(f"帧数 {n} 超过上限 {limit}")

    frames, durations = [], []
    for i in range(n):
        im.seek(i)
        # seek 后解码器已按 disposal + 偏移完成画布合成；取 RGB 完整画面
        frames.append(util.ensure_rgb(im.copy()))
        _, actual = normalize_duration_ms(im.info.get("duration"),
                                          default=config.GIF_DEFAULT_DURATION_MS)
        durations.append(actual)

    loop = im.info.get("loop")
    loop = 0 if loop is None else max(0, int(loop))
    return {
        "frames": frames,
        "durations_ms": durations,
        "width": im.size[0],
        "height": im.size[1],
        "loop": loop,
    }


# ---------------------------------------------------------------------------
# 合帧
# ---------------------------------------------------------------------------
def compose_gif(frames, durations_ms=None, loop=0, max_width=None):
    """把有序静态帧合成为 GIF 字节流。

    frames       - list[PIL.Image]（RGB/RGBA/P 均可），顺序即播放顺序
    durations_ms - 标量或逐帧列表（毫秒）；缺省用全局默认。统一走归一化
    loop         - 0 无限循环（默认），n>0 播放 n 遍后停止
    max_width    - 尺寸压缩：输出最长边上限（None 不压缩）

    返回 (gif_bytes, meta)，meta 含帧数、实际逐帧时长、总时长、输出尺寸。
    """
    if not frames:
        raise ValueError("没有可合成的帧")
    if len(frames) > config.GIF_MAX_FRAMES:
        raise ValueError(f"帧数 {len(frames)} 超过上限 {config.GIF_MAX_FRAMES}")

    requested, actual = normalize_durations_ms(durations_ms, len(frames))
    try:
        loop = int(loop)
    except (TypeError, ValueError):
        loop = 0
    loop = max(0, loop)

    images = normalize_size(frames, max_width=max_width)

    buf = io.BytesIO()
    images[0].save(
        buf, format="GIF", save_all=True, append_images=images[1:],
        duration=actual, loop=loop, disposal=2, optimize=False,
    )
    data = buf.getvalue()
    meta = {
        "frame_count": len(images),
        "durations_ms": actual,
        "requested_durations_ms": requested,
        "total_duration_ms": sum(actual),
        "loop": loop,
        "width": images[0].size[0],
        "height": images[0].size[1],
        "size_bytes": len(data),
    }
    return data, meta
