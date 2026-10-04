"""GIF 拆帧会话存储。

一次「拆帧」产生一个会话：
  data/gif_sessions/<sid>/frame_0000.png ...  全画布 RGBA 帧（无损 PNG）
  data/gif_sessions/<sid>/session.json        帧序、时长、循环、来源等元数据

帧文件用「序号零填充」命名，列表按序号排序，杜绝帧序错乱；单帧被滤镜处理
后原地覆盖该序号的 PNG（先写 tmp 再 rename），序号与时长不变。

会话存的是「作品态」：编辑过的帧保留 RGBA，重新合成时统一铺白底编码 GIF。
"""
import io
import os
import uuid

from PIL import Image

from . import gif as gif_mod
from .storage import JsonStore, atomic_write_bytes, now_iso


def _frame_name(index):
    return f"frame_{index:04d}.png"


class GifSessionStore:
    def __init__(self, sessions_dir):
        self.sessions_dir = sessions_dir
        os.makedirs(sessions_dir, exist_ok=True)

    # ------------------------------------------------------------------ 读
    def _dir(self, sid):
        return os.path.join(self.sessions_dir, sid)

    def _meta_path(self, sid):
        return os.path.join(self._dir(sid), "session.json")

    def get(self, sid):
        if not os.path.exists(self._meta_path(sid)):
            return None
        return JsonStore(self._meta_path(sid), None).read()

    def frame_path(self, sid, index):
        rec = self.get(sid)
        if not rec or not (0 <= index < len(rec.get("frames", []))):
            return None
        p = os.path.join(self._dir(sid), rec["frames"][index]["file"])
        return p if os.path.exists(p) else None

    def frame_image(self, sid, index):
        p = self.frame_path(sid, index)
        if not p:
            return None
        try:
            return Image.open(p)
        except Exception:  # noqa: BLE001
            return None

    def list_frame_images(self, sid):
        """按帧序返回 [RGBA Image, ...]。"""
        rec = self.get(sid)
        if not rec:
            return None
        images = []
        for i in range(len(rec["frames"])):
            img = self.frame_image(sid, i)
            if img is None:
                return None
            images.append(img.convert("RGBA"))
        return images

    # ------------------------------------------------------------------ 写
    def create(self, extracted, source=None):
        """由 gif.extract_frames 的结果落盘一个会话，返回会话记录。"""
        sid = uuid.uuid4().hex
        os.makedirs(self._dir(sid), exist_ok=True)

        frame_recs = []
        total_duration = 0
        for i, (img, dur) in enumerate(zip(extracted["frames"], extracted["durations"])):
            name = _frame_name(i)
            buf = io.BytesIO()
            img.convert("RGBA").save(buf, "PNG")
            atomic_write_bytes(os.path.join(self._dir(sid), name), buf.getvalue())
            frame_recs.append({
                "index": i,
                "file": name,
                "duration_ms": dur,
                "original_duration_ms": dur,
                "edited": False,
            })
            total_duration += dur

        rec = {
            "id": sid,
            "source": source or {},
            "frames": frame_recs,
            "loop": extracted["loop"],
            "width": extracted["width"],
            "height": extracted["height"],
            "total_duration_ms": total_duration,
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        JsonStore(self._meta_path(sid), rec).write(rec)
        return rec

    def replace_frame(self, sid, index, image, mark_edited=True):
        """用处理后的图像覆盖某一帧（序号/时长不动）。返回新记录或 None。

        mark_edited=False 用于「恢复原帧」：像素还原的同时清掉已编辑标记。
        """
        rec = self.get(sid)
        if not rec or not (0 <= index < len(rec.get("frames", []))):
            return None
        name = rec["frames"][index]["file"]
        # 统一到会话画布（等比缩放 + 居中白底），保证后续合帧尺寸一致
        target = (rec["width"], rec["height"])
        frame = gif_mod.flatten_on_white(image.convert("RGBA"), target).convert("RGBA")
        buf = io.BytesIO()
        frame.save(buf, "PNG")
        atomic_write_bytes(os.path.join(self._dir(sid), name), buf.getvalue())

        def _upd(doc):
            doc = dict(doc)
            frames = list(doc.get("frames", []))
            fr = dict(frames[index])
            fr["edited"] = bool(mark_edited)
            frames[index] = fr
            doc["frames"] = frames
            doc["updated_at"] = now_iso()
            return doc

        return JsonStore(self._meta_path(sid)).update(_upd)

    def set_durations(self, sid, durations):
        """批量修改帧时长（重新合成前的节奏调整）。"""
        rec = self.get(sid)
        if not rec or len(durations) != len(rec["frames"]):
            return None
        normalized = [gif_mod.normalize_duration(d) for d in durations]

        def _upd(doc):
            doc = dict(doc)
            frames = []
            for i, fr in enumerate(doc["frames"]):
                fr = dict(fr)
                fr["duration_ms"] = normalized[i]
                frames.append(fr)
            doc["frames"] = frames
            doc["total_duration_ms"] = sum(normalized)
            doc["updated_at"] = now_iso()
            return doc

        return JsonStore(self._meta_path(sid)).update(_upd)

    def delete(self, sid):
        import shutil
        if not self.get(sid) and not os.path.isdir(self._dir(sid)):
            return False
        # 整个会话目录一起回收（帧 PNG、session.json、原子写残留的 .bak/.tmp）
        shutil.rmtree(self._dir(sid), ignore_errors=True)
        return True

    # ------------------------------------------------------------------ 视图
    def view(self, rec):
        """给前端的会话视图（帧文件直接走 HTTP，不放二进制）。"""
        return {
            "id": rec["id"],
            "source": rec.get("source", {}),
            "loop": rec.get("loop", 0),
            "width": rec["width"],
            "height": rec["height"],
            "frame_count": len(rec["frames"]),
            "total_duration_ms": rec.get("total_duration_ms", 0),
            "created_at": rec.get("created_at"),
            "frames": [{
                "index": fr["index"],
                "duration_ms": fr["duration_ms"],
                "original_duration_ms": fr.get("original_duration_ms", fr["duration_ms"]),
                "edited": fr.get("edited", False),
                "file_url": f"/api/gif/sessions/{rec['id']}/frames/{fr['index']}/file",
            } for fr in rec["frames"]],
        }
