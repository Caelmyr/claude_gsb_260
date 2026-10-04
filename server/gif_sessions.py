"""拆帧会话：把一张 GIF 拆出的有序帧与节奏信息持久化在服务端。

为什么需要会话：前端「拆帧 -> 逐帧滤镜/调整 -> 重新合帧」是多次 HTTP 请求，
帧 PNG 落在 data/gif_frames/<session_id>/ 下，gif_sessions.json 保存有序清单
（索引、文件名、原始时长、当前时长）。帧顺序以清单数组为准，不读目录顺序。

每帧同时保留 original.png 与 work.png：
  original - 拆帧时的原始画面，「还原该帧」时一键回到它；
  work     - 逐帧滤镜/调整的当前结果，重新合帧用它。
会话 LRU 淘汰（按 updated_at），删除会话同步清理帧目录。
"""
import os
import shutil
import uuid

from PIL import Image

from . import config
from .storage import JsonStore, atomic_write_bytes, now_iso


class GifSessionStore:
    def __init__(self):
        self.store = JsonStore(config.GIF_SESSIONS_JSON, {})

    # ------------------------------------------------------------------ 读
    def get(self, session_id):
        return self.store.read().get(session_id)

    def list_sessions(self):
        items = sorted(self.store.read().values(),
                       key=lambda s: s.get("updated_at", ""), reverse=True)
        return items

    def frame_dir(self, session_id):
        return os.path.join(config.GIF_FRAMES_DIR, session_id)

    def frame_path(self, session_id, index, kind="work"):
        """kind: work（当前处理结果）/ original（拆帧原画面）。"""
        return os.path.join(self.frame_dir(session_id), f"{index:04d}_{kind}.png")

    def frame_view(self, session_id, frame):
        idx = frame["index"]
        edited = frame.get("edited", False)
        return {
            "index": idx,
            "duration_ms": frame["duration_ms"],
            "original_duration_ms": frame["original_duration_ms"],
            "edited": edited,
            "op": frame.get("op"),
            "work_url": f"/api/gif/sessions/{session_id}/frames/{idx}",
            "original_url": f"/api/gif/sessions/{session_id}/frames/{idx}?kind=original",
        }

    def session_view(self, s):
        return {
            "id": s["id"],
            "filename": s.get("filename", ""),
            "frame_count": len(s["frames"]),
            "width": s.get("width"), "height": s.get("height"),
            "loop": s.get("loop", 0),
            "total_duration_ms": sum(f["duration_ms"] for f in s["frames"]),
            "created_at": s.get("created_at"),
            "updated_at": s.get("updated_at"),
            "frames": [self.frame_view(s["id"], f) for f in s["frames"]],
        }

    # ------------------------------------------------------------------ 写
    def create(self, frames, durations_ms, loop, filename=""):
        """由拆帧结果（PIL 帧 + 时长）建立会话，帧落盘。返回视图 dict。"""
        sid = uuid.uuid4().hex
        os.makedirs(self.frame_dir(sid), exist_ok=True)
        records = []
        try:
            for i, img in enumerate(frames):
                # original 与 work 初始相同
                atomic_write_bytes(self.frame_path(sid, i, "original"),
                                   _png_bytes(img))
                atomic_write_bytes(self.frame_path(sid, i, "work"),
                                   _png_bytes(img))
                records.append({
                    "index": i,
                    "duration_ms": int(durations_ms[i]),
                    "original_duration_ms": int(durations_ms[i]),
                    "edited": False,
                    "op": None,
                })
        except BaseException:
            shutil.rmtree(self.frame_dir(sid), ignore_errors=True)
            raise

        ts = now_iso()
        session = {
            "id": sid, "filename": filename or f"{sid[:8]}.gif",
            "width": frames[0].size[0], "height": frames[0].size[1],
            "loop": loop, "frames": records,
            "created_at": ts, "updated_at": ts,
        }

        def _upd(doc):
            doc = dict(doc)
            doc[sid] = session
            return doc

        self.store.update(_upd)
        self.evict_if_needed()
        return self.session_view(session)

    def update_frame(self, session_id, index, image=None, duration_ms=None,
                     op=None, edited=None):
        """更新某一帧的画面（work.png）/时长/操作记录。返回帧视图，失败返回 None。"""
        state = {"frame": None}

        def _upd(doc):
            doc = dict(doc)
            s = doc.get(session_id)
            if not s or not (0 <= index < len(s["frames"])):
                return doc
            s = dict(s)
            frames = [dict(f) for f in s["frames"]]
            fr = frames[index]
            if duration_ms is not None:
                fr["duration_ms"] = int(duration_ms)
            if op is not None:
                fr["op"] = op
            if edited is not None:
                fr["edited"] = bool(edited)
            elif image is not None or op is not None or duration_ms is not None:
                # 画面变化或时长偏离原始值即视为已编辑
                fr["edited"] = (image is not None or op is not None
                                or fr["duration_ms"] != fr["original_duration_ms"])
            s["frames"] = frames
            s["updated_at"] = now_iso()
            doc[session_id] = s
            state["frame"] = fr
            return doc

        self.store.update(_upd)
        if state["frame"] is None:
            return None
        if image is not None:
            atomic_write_bytes(self.frame_path(session_id, index, "work"),
                               _png_bytes(image))
        return self.frame_view(session_id, state["frame"])

    def reset_frame(self, session_id, index):
        """把某帧 work 还原为 original，时长也恢复。"""
        s = self.get(session_id)
        if not s or not (0 <= index < len(s["frames"])):
            return None
        orig = self.frame_path(session_id, index, "original")
        if not os.path.exists(orig):
            return None
        shutil.copyfile(orig, self.frame_path(session_id, index, "work"))
        fr = s["frames"][index]
        return self.update_frame(session_id, index, duration_ms=fr["original_duration_ms"],
                                 op=None, edited=False)

    def reorder_frames(self, session_id, order):
        """按给定索引顺序重排（接口保留；默认 UI 不开放，防止乱序）。"""
        s = self.get(session_id)
        if not s:
            return None
        by_idx = {f["index"]: f for f in s["frames"]}
        if sorted(order) != list(range(len(s["frames"]))):
            return None

        def _upd(doc):
            doc = dict(doc)
            sess = dict(doc[session_id])
            old = sess["frames"]
            old_by = {f["index"]: f for f in old}
            new_frames = []
            for new_i, old_i in enumerate(order):
                f = dict(old_by[old_i])
                f["index"] = new_i
                new_frames.append(f)
            sess["frames"] = new_frames
            sess["updated_at"] = now_iso()
            doc[session_id] = sess
            return doc

        self.store.update(_upd)
        # 物理文件重排：重写 work/original 到新序号
        # （先收集到内存避免覆盖；帧都在百像素级，且帧数有上限）
        latest = self.get(session_id)
        images = {}
        for new_i, old_i in enumerate(order):
            images[(new_i, "original")] = self.frame_path(session_id, old_i, "original")
            images[(new_i, "work")] = self.frame_path(session_id, old_i, "work")
        tmp_dir = os.path.join(self.frame_dir(session_id), ".reorder")
        os.makedirs(tmp_dir, exist_ok=True)
        for (new_i, kind), src in images.items():
            shutil.copyfile(src, os.path.join(tmp_dir, f"{new_i:04d}_{kind}.png"))
        for fn in os.listdir(self.frame_dir(session_id)):
            if fn.endswith(".png"):
                os.unlink(os.path.join(self.frame_dir(session_id), fn))
        for fn in os.listdir(tmp_dir):
            shutil.move(os.path.join(tmp_dir, fn),
                        os.path.join(self.frame_dir(session_id), fn))
        os.rmdir(tmp_dir)
        return self.session_view(self.get(session_id))

    def delete(self, session_id):
        if not self.get(session_id):
            return False

        def _upd(doc):
            doc = dict(doc)
            doc.pop(session_id, None)
            return doc

        self.store.update(_upd)
        shutil.rmtree(self.frame_dir(session_id), ignore_errors=True)
        return True

    # ------------------------------------------------------------------ 淘汰
    def evict_if_needed(self):
        doc = self.store.read()
        if len(doc) <= config.GIF_SESSION_MAX:
            return 0
        order = sorted(doc.values(), key=lambda s: s.get("updated_at", ""))
        removed = 0
        for s in order:
            if len(doc) - removed <= config.GIF_SESSION_MAX:
                break
            shutil.rmtree(self.frame_dir(s["id"]), ignore_errors=True)
            doc.pop(s["id"], None)
            removed += 1
        if removed:
            self.store.write(doc)
        return removed


def _png_bytes(image):
    from io import BytesIO
    buf = BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue()
