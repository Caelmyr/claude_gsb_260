"""GIF 能力 HTTP 端到端测试（独立临时 DATA_DIR，不污染真实数据）。"""
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image  # noqa: E402

from server import config  # noqa: E402

# 重定向全部运行时路径到临时目录（必须在导入 server.api 之前）
_D = tempfile.mkdtemp(prefix="gifapi_")
config.DATA_DIR = _D
config.IMAGES_DIR = os.path.join(_D, "images")
config.RESULTS_DIR = os.path.join(_D, "results")
config.THUMBS_DIR = os.path.join(_D, "thumbnails")
config.CACHE_DIR = os.path.join(_D, "cache")
config.META_DIR = os.path.join(_D, "metadata")
config.GIF_FRAMES_DIR = os.path.join(_D, "gif_frames")
config.IMAGES_JSON = os.path.join(config.META_DIR, "images.json")
config.PIPELINES_JSON = os.path.join(config.META_DIR, "pipelines.json")
config.HISTORY_JSON = os.path.join(config.META_DIR, "history.json")
config.PRESETS_JSON = os.path.join(config.META_DIR, "presets.json")
config.QUEUE_JSON = os.path.join(config.META_DIR, "queue.json")
config.CACHE_JSON = os.path.join(config.META_DIR, "cache.json")
config.GIF_SESSIONS_JSON = os.path.join(config.META_DIR, "gif_sessions.json")
config._ALL_DIRS = [
    config.DATA_DIR, config.IMAGES_DIR, config.RESULTS_DIR, config.THUMBS_DIR,
    config.CACHE_DIR, config.META_DIR, config.GIF_FRAMES_DIR,
]

from flask import Flask  # noqa: E402
import server.api as api  # noqa: E402

api._reset_singletons()
app = api.init_app(Flask(__name__))
c = app.test_client()


def png_bytes(im):
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def make_gif_bytes(durations, loop=0, size=(96, 64), colors=None):
    frames = [
        Image.new("RGB", size, ((i * 60) % 256, 80, 160))
        for i in range(len(durations))
    ]
    if colors:
        frames = [Image.new("RGB", size, col) for col in colors]
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:],
                   duration=durations, loop=loop, disposal=2, optimize=False)
    return buf.getvalue()


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ✓", msg)


def main():
    print("== 拆帧 ==")
    raw = make_gif_bytes([100, 150, 200, 250], loop=0)
    r = c.post("/api/gif/split",
               data={"file": (io.BytesIO(raw), "test.gif")},
               content_type="multipart/form-data")
    check(r.status_code == 200, f"拆帧 200（{r.status_code}）")
    sess = r.get_json()["session"]
    sid = sess["id"]
    check([f["duration_ms"] for f in sess["frames"]] == [100, 150, 200, 250],
          "四帧时长按序读出 100/150/200/250")
    check(sess["frame_count"] == 4 and sess["loop"] == 0, "帧数=4 循环=无限")
    check(sess["total_duration_ms"] == 700, "总时长 700ms")

    print("== 帧文件 ==")
    r = c.get(sess["frames"][0]["work_url"])
    check(r.status_code == 200 and r.mimetype == "image/png", "work 帧可下载 PNG")
    r = c.get(sess["frames"][3]["original_url"])
    check(r.status_code == 200 and Image.open(io.BytesIO(r.data)).size == (96, 64),
          "original 帧尺寸 96x64")

    print("== 逐帧滤镜 ==")
    r = c.post(f"/api/gif/sessions/{sid}/frames/1/op",
               json={"op": {"node": "brightness", "params": {"amount": 50}}})
    check(r.status_code == 200 and r.get_json()["frame"]["edited"] is True,
          "第2帧加亮度 -> edited")
    # work 画面确实变了，original 没变
    rw = c.get(sess["frames"][1]["work_url"]).data
    ro = c.get(sess["frames"][1]["original_url"]).data
    check(rw != ro, "work 与 original 内容不同")

    r = c.post(f"/api/gif/sessions/{sid}/frames/all/op",
               json={"op": {"node": "contrast", "params": {"amount": 20}}})
    check(r.status_code == 200 and len(r.get_json()["frames"]) == 4,
          "批量滤镜应用到 4 帧")

    # 多节点流水线 op
    r = c.post(f"/api/gif/sessions/{sid}/frames/0/op", json={"op": {"nodes": [
        {"id": "a", "type": "brightness", "params": {"amount": 10}, "inputs": []},
        {"id": "b", "type": "grayscale", "params": {}, "inputs": ["a"]},
    ]}})
    check(r.status_code == 200, "多节点流水线 op 可执行")

    print("== 单帧时长调整（归一化）==")
    r = c.put(f"/api/gif/sessions/{sid}/frames/0", json={"duration_ms": 23})
    j = r.get_json()
    check(j["actual_duration_ms"] == 20 and j["requested_duration_ms"] == 23,
          "23ms 请求 -> 实际 20ms（centisecond 对齐）")

    print("== 还原帧 ==")
    r = c.post(f"/api/gif/sessions/{sid}/frames/1/reset")
    j = r.get_json()["frame"]
    check(j["edited"] is False and j["duration_ms"] == 150,
          "还原后 edited=false 时长恢复 150")
    check(c.get(sess["frames"][1]["work_url"]).data
          == c.get(sess["frames"][1]["original_url"]).data,
          "还原后 work 字节与 original 一致")

    print("== 重新合帧（节奏保持）==")
    r = c.post(f"/api/gif/sessions/{sid}/rebuild", json={})
    j = r.get_json()
    check(r.status_code == 200, "rebuild 200")
    check(j["durations_ms"] == [20, 150, 200, 250],
          f"合回后逐帧时长 {j['durations_ms']}（第1帧的人工修改保留，其余原样）")
    check(j["loop"] == 0, "沿用原循环设置（无限）")
    rr = c.get(j["file_url"])
    check(rr.mimetype == "image/gif", "结果以 image/gif 返回")
    g = Image.open(io.BytesIO(rr.data))
    durs = []
    for i in range(g.n_frames):
        g.seek(i)
        durs.append(g.info["duration"])
    check(g.n_frames == 4 and durs == [20, 150, 200, 250],
          f"磁盘 GIF 实测 4 帧且时长 {durs}")

    print("== 纯净拆-合节奏一致性（不做任何修改）==")
    sid_pure = c.post("/api/gif/split",
                      data={"file": (io.BytesIO(raw), "t2.gif")},
                      content_type="multipart/form-data").get_json()["session"]["id"]
    j = c.post(f"/api/gif/sessions/{sid_pure}/rebuild", json={}).get_json()
    check(j["durations_ms"] == [100, 150, 200, 250] and j["loop"] == 0,
          "原样拆再合：100/150/200/250 与无限循环完全保持")

    print("== 循环次数保持（有限循环）==")
    raw3 = make_gif_bytes([80, 120], loop=3)
    sid3 = c.post("/api/gif/split",
                  data={"file": (io.BytesIO(raw3), "loop3.gif")},
                  content_type="multipart/form-data").get_json()["session"]["id"]
    j = c.post(f"/api/gif/sessions/{sid3}/rebuild", json={}).get_json()
    check(j["durations_ms"] == [80, 120] and j["loop"] == 3,
          "loop=3 拆合后保持")

    print("== 多图合成（JSON：引用图库）==")
    src = [Image.new("RGB", (120, 90), (i * 80, 40, 90)) for i in range(3)]
    ids = []
    for i, im in enumerate(src):
        up = c.post("/api/images",
                    data={"files": (io.BytesIO(png_bytes(im)), f"f{i}.png")},
                    content_type="multipart/form-data")
        ids.append(up.get_json()["saved"][0]["id"])
    r = c.post("/api/gif/compose",
               json={"image_ids": ids, "durations_ms": [100, 100, 100],
                     "loop": 1, "max_width": 48})
    j = r.get_json()
    check(r.status_code == 200 and j["frame_count"] == 3, "3 帧合成成功")
    check(j["width"] == 48 and j["height"] == 36, "尺寸压缩到最长边 48（等比 48x36）")
    check(j["loop"] == 1 and j["durations_ms"] == [100, 100, 100],
          "循环 1 次、每帧 100ms")

    print("== 多图合成（multipart：直接上传，统一时长）==")
    r = c.post("/api/gif/compose", data={
        "files": [(io.BytesIO(png_bytes(im)), f"f{i}.png") for i, im in enumerate(src)],
        "duration": 130, "loop": 0, "max_width": 200,
    }, content_type="multipart/form-data")
    j = r.get_json()
    check(r.status_code == 200 and j["durations_ms"] == [130, 130, 130],
          "标量 duration=130 应用到全部帧")
    check(j["width"] == 120, "原图未超限时不放大")

    print("== 不同尺寸静态图合成（统一画布）==")
    mixed = [Image.new("RGB", (100, 50), (255, 0, 0)),
             Image.new("RGB", (40, 40), (0, 255, 0))]
    r = c.post("/api/gif/compose", data={
        "files": [(io.BytesIO(png_bytes(im)), f"m{i}.png") for i, im in enumerate(mixed)],
        "duration": 100,
    }, content_type="multipart/form-data")
    j = r.get_json()
    check(r.status_code == 200 and j["width"] == 100 and j["height"] == 50,
          "异尺寸帧统一到首帧画布 100x50")

    print("== 缓存命中 ==")
    r1 = c.post("/api/gif/compose",
                json={"image_ids": ids, "durations_ms": [100, 100, 100],
                      "loop": 1, "max_width": 48})
    r2 = c.post("/api/gif/compose",
                json={"image_ids": ids, "durations_ms": [100, 100, 100],
                      "loop": 1, "max_width": 48})
    check(r1.get_json()["result_id"] == r2.get_json()["result_id"],
          "相同输入合成命中缓存")

    print("== 错误处理 ==")
    r = c.post("/api/gif/split",
               data={"file": (io.BytesIO(png_bytes(src[0])), "x.png")},
               content_type="multipart/form-data")
    check(r.status_code == 400, f"非动图拆帧 400（{r.get_json()['error']}）")
    r = c.post("/api/gif/compose", json={"image_ids": []})
    check(r.status_code == 400, "空帧合成 400")
    r = c.post(f"/api/gif/sessions/{sid}/frames/99/op",
               json={"op": {"node": "brightness"}})
    check(r.status_code == 404, "越界帧索引 404")
    r = c.post(f"/api/gif/sessions/nope/frames/0/op",
               json={"op": {"node": "brightness"}})
    check(r.status_code == 404, "不存在会话 404")
    r = c.post(f"/api/gif/sessions/{sid}/frames/0/op", json={"op": {"node": "不存在的节点"}})
    check(r.status_code == 400, "未知操作类型 400")

    print("== 会话管理 ==")
    r = c.get("/api/gif/sessions")
    check(r.status_code == 200 and len(r.get_json()["sessions"]) >= 3, "会话列表")
    r = c.delete(f"/api/gif/sessions/{sid3}")
    check(r.status_code == 200 and not os.path.exists(
        api.gif_sessions.frame_dir(sid3)), "删除会话并清理帧目录")

    print("\n全部 GIF HTTP 测试通过 ✔")


if __name__ == "__main__":
    main()
