"""GIF 动图能力冒烟：拆帧 / 帧编辑 / 重编码节奏保持 / 压缩 / 多图合成。

运行：python tests/test_gif.py

覆盖需求的三条硬约束：
  1. 帧顺序不乱——帧数、每帧索引在拆解/编辑/合回后保持；
  2. 帧间时长准确——不等时长原样回读；非厘秒倍数按四舍五入量化；
  3. 拆帧再合帧节奏不变——extract -> encode -> extract 时长/循环一致；
另覆盖 disposal=1/2/3 三种 GIF 还原方式与尺寸压缩。
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw  # noqa: E402

from server import config, gif as gif_mod, pipeline as pe  # noqa: E402
from server.gif_sessions import GifSessionStore  # noqa: E402
from server.image_store import ImageStore  # noqa: E402

PASS, FAIL = "✔", "✘"
failures = []


def check(name, cond, detail=""):
    print(f"  {PASS if cond else FAIL} {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


def pal_image(color_boxes, transparent_bg=True, size=(80, 60)):
    """造一张 P 模式局部帧：color_boxes=[(box, palette_index), ...]。"""
    im = Image.new("P", size, 0)
    # 0=黑(透明) 1=红 2=绿 3=蓝
    im.putpalette([0, 0, 0, 255, 0, 0, 0, 255, 0, 0, 0, 255] + [0] * (768 - 12))
    d = ImageDraw.Draw(im)
    for box, idx in color_boxes:
        d.rectangle(box, fill=idx)
    if transparent_bg:
        im.info["transparency"] = 0
    return im


def save_gif(frames, durations, disposal, loop=0, transparency=0):
    buf = io.BytesIO()
    frames[0].save(
        buf, format="GIF", save_all=True, append_images=frames[1:],
        duration=durations, loop=loop, disposal=disposal,
        transparency=transparency,
    )
    return buf.getvalue()


def main():
    config.ensure_dirs()
    store = ImageStore()
    sessions = GifSessionStore(config.GIF_SESSIONS_DIR)

    print("== 1. 三种 disposal 方式的正确合成 ==")
    # 三帧分别在不同位置画色块；逐帧检查「自己的块在不在」「上一帧的块还在不在」
    boxes = [(5, 5, 20, 20), (40, 5, 55, 20), (5, 35, 20, 50)]
    centers = [(12, 12), (47, 12), (12, 42)]
    for disp in (1, 2, 3):
        data = save_gif(
            [pal_image([(boxes[0], 1)]), pal_image([(boxes[1], 2)]), pal_image([(boxes[2], 3)])],
            durations=[100, 100, 100], disposal=[disp] * 3)
        ext = gif_mod.extract_frames(data)
        for fi in range(3):
            x, y = centers[fi]
            check(f"disposal={disp} 帧{fi}自己的块不透明",
                  ext["frames"][fi].getpixel((x, y))[3] == 255)
        # 帧2里「帧0块」的留存情况：disposal=1 累积保留；2/3 被清除
        prev_kept = ext["frames"][2].getpixel(centers[0])[3] == 255
        if disp == 1:
            check("disposal=1 旧块累积保留", prev_kept)
        else:
            check(f"disposal={disp} 旧块被清除", not prev_kept)

    print("== 2. 首帧不透明（无 transparency）= 不透明背景铺满 ==")
    bg = Image.new("RGB", (40, 30), (10, 20, 30))
    buf = io.BytesIO()
    bg.save(buf, format="GIF")
    ext = gif_mod.extract_frames(buf.getvalue())
    px = ext["frames"][0].getpixel((0, 0))
    check("首帧不透明且保留背景色", px == (10, 20, 30, 255), str(px))

    print("== 3. 时长/循环读取与规范化 ==")
    data = save_gif(
        [pal_image([((5, 5, 20, 20), 1)]), pal_image([((40, 5, 55, 20), 2)])],
        durations=[120, 270], disposal=[2, 2], loop=2)
    ext = gif_mod.extract_frames(data)
    check("不等时长精确回读", ext["durations"] == [120, 270], str(ext["durations"]))
    check("loop=2 回读", ext["loop"] == 2)
    check("时长 0/缺失 -> 100ms", gif_mod.normalize_duration(0) == 100)
    check("非厘秒倍数四舍五入", gif_mod.normalize_duration(33) == 30
          and gif_mod.normalize_duration(36) == 40, str(gif_mod.normalize_duration(33)))
    check("过短时长夹到 20ms", gif_mod.normalize_duration(5) == 20)

    print("== 4. 拆 -> 合 -> 拆：节奏/帧数/画面不变 ==")
    rec_gif = store.save_upload(data, "rhythm.gif")
    with open(store.file_path(rec_gif["id"]), "rb") as f:
        ext = gif_mod.extract_frames(f.read(), max_dim=config.GIF_DEFAULT_MAX_DIM)
    sess_rec = sessions.create(ext, source={"image_id": rec_gif["id"], "filename": "rhythm.gif"})
    sid = sess_rec["id"]
    sv = sessions.view(sess_rec)
    check("会话帧数=2", sv["frame_count"] == 2)
    check("会话总时长=390ms", sv["total_duration_ms"] == 390, str(sv["total_duration_ms"]))
    check("帧 URL 按序排列", [fr["index"] for fr in sv["frames"]] == [0, 1])

    images = sessions.list_frame_images(sid)
    out, meta = gif_mod.encode_gif(
        images, [fr["duration_ms"] for fr in sess_rec["frames"]], loop=sess_rec["loop"])
    again = gif_mod.extract_frames(out)
    check("重编码帧数一致", again["frame_count"] == 2)
    check("重编码时长一致", again["durations"] == [120, 270], str(again["durations"]))
    check("重编码循环一致", again["loop"] == 2)
    # 不透明像素画面一致（透明区重编码后铺白底，属预期取舍）
    mismatch = 0
    for a, b in zip(ext["frames"], again["frames"]):
        for pa, pb in zip(a.getdata(), b.getdata()):
            if pa[3] != 0 and abs(pa[0] - pb[0]) + abs(pa[1] - pb[1]) + abs(pa[2] - pb[2]) > 24:
                mismatch += 1
    check("不透明区域像素保持一致", mismatch == 0, f"{mismatch} 个失配")
    check("回读节奏标记一致", meta["effective_durations"] == [120, 270])

    print("== 5. 逐帧滤镜不影响时长与帧序 ==")
    img0 = sessions.frame_image(sid, 0).convert("RGB")
    processed = pe.execute(img0, [
        {"id": "b", "type": "brightness", "params": {"amount": 60}, "inputs": []}])
    sessions.replace_frame(sid, 0, processed["image"])
    rec2 = sessions.get(sid)
    check("帧0标记已编辑", rec2["frames"][0]["edited"] is True)
    check("帧1未编辑", rec2["frames"][1]["edited"] is False)
    check("编辑后时长不变", [f["duration_ms"] for f in rec2["frames"]] == [120, 270])
    check("编辑后帧序不变", [f["index"] for f in rec2["frames"]] == [0, 1])
    check("编辑后画布尺寸不变", sessions.frame_image(sid, 0).size == (80, 60))

    print("== 6. 恢复单帧 ==")
    with open(store.file_path(rec_gif["id"]), "rb") as f:
        original = gif_mod.extract_frames(f.read())
    sessions.replace_frame(sid, 0, original["frames"][0], mark_edited=False)
    check("恢复后编辑标记被清除", sessions.get(sid)["frames"][0]["edited"] is False)
    img_reset = sessions.frame_image(sid, 0).convert("RGB")
    # 会话帧统一铺白底，故与「原帧铺白底」对比
    orig_on_white = gif_mod.flatten_on_white(original["frames"][0])
    diff = sum(1 for p, q in zip(orig_on_white.getdata(), img_reset.getdata())
               if p != q)
    check("恢复后像素与原帧（白底）一致", diff == 0, f"{diff} 个差异")

    print("== 7. 尺寸压缩 / 色数：节奏不受影响 ==")
    images = sessions.list_frame_images(sid)
    out_s, meta_s = gif_mod.encode_gif(images, [120, 270], loop=2, max_dim=40, colors=64)
    small = gif_mod.extract_frames(out_s)
    check("压缩后最长边=40", max(small["width"], small["height"]) == 40,
          f"{small['width']}x{small['height']}")
    check("压缩后时长不变", small["durations"] == [120, 270])
    check("压缩后帧数不变", small["frame_count"] == 2)

    print("== 8. 多张不同尺寸静态图合成：画布适配 + 时长准确 ==")
    static_frames = [
        Image.new("RGB", (100, 100), (200, 30, 30)),
        Image.new("RGB", (50, 80), (30, 200, 30)),
        Image.new("RGBA", (120, 60), (30, 30, 200, 255)),
    ]
    out_c, meta_c = gif_mod.encode_gif(static_frames, [100, 250, 70], loop=0)
    comp = gif_mod.extract_frames(out_c)
    check("合成帧数=3", comp["frame_count"] == 3)
    check("所有帧同画布（首帧 100x100）",
          all(fr.size == (100, 100) for fr in comp["frames"]))
    check("逐帧时长准确", comp["durations"] == [100, 250, 70], str(comp["durations"]))
    check("无限循环", comp["loop"] == 0)

    sessions.delete(sid)
    check("会话可删除", sessions.get(sid) is None)

    print()
    if failures:
        print(f"失败 {len(failures)} 项：{failures}")
        sys.exit(1)
    print("全部 GIF 冒烟通过 ✔")


if __name__ == "__main__":
    main()
