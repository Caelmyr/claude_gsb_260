"""REST API：所有后端能力通过 JSON 接口暴露给前端。

这是各模块的粘合层：图像管理、流水线 CRUD、运行、特征/检测/分割/风格、
批处理、结果对比、预设、历史。单图运算接口统一走 _run_op（带缓存）。
"""
import hashlib
import io
import json
import os

from flask import Blueprint, jsonify, request, send_file
from PIL import Image, ImageChops

from . import config, pipeline as pipeline_engine
from . import gif_engine
from . import nodes as nodes_mod
from .algorithms import detection, features, segmentation, style, util
from .batch import BatchManager, process_image
from .cache import ResultCache, make_key
from .gif_sessions import GifSessionStore
from .history import HistoryManager
from .image_store import ImageStore
from .nodes import CATEGORIES, get_public_nodes
from .storage import JsonStore, now_iso
from . import storage as storage_mod

# ---------------------------------------------------------------------------
# 单例（模块导入即创建，app.py 入口先 ensure_dirs）
# ---------------------------------------------------------------------------
config.ensure_dirs()
image_store = ImageStore()
cache = ResultCache()
history = HistoryManager()
batch = BatchManager(image_store, cache, history)
gif_sessions = GifSessionStore()
presets_store = JsonStore(config.PRESETS_JSON, [])

bp = Blueprint("api", __name__, url_prefix="/api")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _image_view(rec):
    """给前端用的图像记录视图（附加缩略图/文件 URL）。"""
    return {
        "id": rec["id"], "filename": rec["filename"], "format": rec["format"],
        "width": rec["width"], "height": rec["height"],
        "size_bytes": rec["size_bytes"], "created_at": rec["created_at"],
        "tags": rec.get("tags", []), "note": rec.get("note", ""),
        "annotations": rec.get("annotations", []),
        "thumbnail_url": f"/api/images/{rec['id']}/thumbnail",
        "file_url": f"/api/images/{rec['id']}/file",
    }


def _load_full_image(image_id):
    rec = image_store.get(image_id)
    if not rec:
        return None, None
    from PIL import Image
    path = image_store.file_path(image_id)
    if not path:
        return None, None
    return rec, Image.open(path)


def _run_op(image_id, op_name, params, func):
    """单图运算通用流程：载入 -> 缓存查询 -> 执行 -> 缓存结果。"""
    rec, img = _load_full_image(image_id)
    if not rec:
        return None, ({"error": "图像不存在"}, 404)
    work = util.downscale_to_max(util.ensure_rgb(img), config.MAX_DIM)
    key = make_key(rec["hash"], op_name, json.dumps(params, sort_keys=True))

    cached = cache.get(key)
    if cached:
        entry = cache.get_entry(cached) or {}
        return {"result_id": cached, "cache_hit": True, **entry.get("meta", {})}, None

    result = func(work, params)
    image_out = result.get("image", work)
    meta = {k: v for k, v in result.items() if k != "image"}
    result_id = cache.put(key, image_out, meta)
    return {"result_id": result_id, "cache_hit": False, **meta}, None


def _result_view(entry):
    return {
        "result_id": entry.get("result_id"),
        "key": entry.get("key"),
        "width": entry.get("width"),
        "height": entry.get("height"),
        "size_bytes": entry.get("size_bytes"),
        "created_at": entry.get("created_at"),
        "meta": entry.get("meta", {}),
        "file_url": f"/api/results/{entry.get('result_id')}/file",
    }


def _pipeline_view(p):
    return {
        "id": p["id"], "name": p["name"], "nodes": p["nodes"],
        "version": p.get("version", 1), "created_at": p.get("created_at"),
        "updated_at": p.get("updated_at"),
    }


# ---------------------------------------------------------------------------
# 元信息
# ---------------------------------------------------------------------------
@bp.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "version": "1.0.0",
        "images": len(image_store.list_records()),
        "results": len(cache.list_results()),
        "history": len(history.list()),
        "pipelines": len(pipelines_store.read()),
    })


@bp.get("/config")
def get_config():
    return jsonify({
        "max_upload_mb": config.MAX_UPLOAD_MB,
        "max_dim": config.MAX_DIM,
        "preview_dim": config.PREVIEW_DIM,
        "categories": CATEGORIES,
        "batch_workers": config.MAX_BATCH_WORKERS,
        "gif": {
            "default_duration_ms": config.GIF_DEFAULT_DURATION_MS,
            "min_duration_ms": config.GIF_MIN_DURATION_MS,
            "max_duration_ms": config.GIF_MAX_DURATION_MS,
            "max_frames": config.GIF_MAX_FRAMES,
            "max_width": config.GIF_MAX_WIDTH,
        },
    })


@bp.get("/nodes")
def get_nodes():
    return jsonify({"nodes": get_public_nodes(), "categories": CATEGORIES})


@bp.get("/styles")
def get_styles():
    return jsonify({"styles": style.list_styles()})


@bp.get("/reconcile")
def reconcile():
    issues = image_store.reconcile()
    return jsonify({"issues": issues, "ok": not issues["orphan_meta"] and not issues["orphan_files"]})


# ---------------------------------------------------------------------------
# 图像
# ---------------------------------------------------------------------------
@bp.get("/images")
def list_images():
    return jsonify({"images": [_image_view(r) for r in image_store.list_records()]})


@bp.post("/images")
def upload_images():
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "未收到文件"}), 400
    saved, skipped = [], []
    for f in files:
        data = f.read()
        if len(data) > config.MAX_UPLOAD_BYTES:
            skipped.append({"filename": f.filename, "reason": "超过大小限制"})
            continue
        if not data:
            continue
        try:
            rec = image_store.save_upload(data, f.filename or "upload")
            saved.append(_image_view(rec))
        except Exception as exc:  # noqa: BLE001
            skipped.append({"filename": f.filename, "reason": str(exc)})
    return jsonify({"saved": saved, "skipped": skipped})


@bp.get("/images/<image_id>")
def get_image(image_id):
    rec = image_store.get(image_id)
    if not rec:
        return jsonify({"error": "not found"}), 404
    return jsonify(_image_view(rec))


@bp.patch("/images/<image_id>")
def patch_image(image_id):
    data = request.get_json(silent=True) or {}
    rec = image_store.update_meta(image_id, data)
    if not rec:
        return jsonify({"error": "not found"}), 404
    return jsonify(_image_view(rec))


@bp.delete("/images/<image_id>")
def delete_image(image_id):
    if not image_store.delete(image_id):
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True})


@bp.get("/images/<image_id>/file")
def image_file(image_id):
    rec = image_store.get(image_id)
    if not rec:
        return jsonify({"error": "not found"}), 404
    path = image_store.file_path(image_id)
    if not path:
        return jsonify({"error": "not found"}), 404
    ext = rec.get("ext", "").lower()
    mimetype = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".bmp": "image/bmp",
        ".webp": "image/webp", ".tiff": "image/tiff",
    }.get(ext, "application/octet-stream")
    return send_file(path, mimetype=mimetype)


@bp.get("/images/<image_id>/thumbnail")
def image_thumbnail(image_id):
    path = image_store.thumbnail_path(image_id)
    if not path or not __import__("os").path.exists(path):
        # 回退到全图（缩略图缺失时）
        path = image_store.file_path(image_id)
        if not path:
            return jsonify({"error": "not found"}), 404
    return send_file(path, mimetype="image/jpeg")


@bp.post("/images/<image_id>/annotations")
def add_annotation(image_id):
    data = request.get_json(silent=True) or {}
    rec = image_store.add_annotation(image_id, data.get("box"), data.get("label", ""),
                                     data.get("color", "#ff5252"))
    if not rec:
        return jsonify({"error": "not found"}), 404
    return jsonify(_image_view(rec))


@bp.delete("/images/<image_id>/annotations/<int:index>")
def delete_annotation(image_id, index):
    rec = image_store.delete_annotation(image_id, index)
    if not rec:
        return jsonify({"error": "not found"}), 404
    return jsonify(_image_view(rec))


# ---------------------------------------------------------------------------
# 流水线
# ---------------------------------------------------------------------------
pipelines_store = JsonStore(config.PIPELINES_JSON, {})


def _next_version(p):
    return int(p.get("version", 1)) + 1


@bp.get("/pipelines")
def list_pipelines():
    items = sorted(pipelines_store.read().values(), key=lambda p: p.get("updated_at", ""), reverse=True)
    return jsonify({"pipelines": [_pipeline_view(p) for p in items]})


@bp.post("/pipelines")
def create_pipeline():
    data = request.get_json(silent=True) or {}
    pid = __import__("uuid").uuid4().hex
    rec = {
        "id": pid, "name": data.get("name", "未命名流水线"),
        "nodes": data.get("nodes", []),
        "version": 1, "versions": [],
        "created_at": now_iso(), "updated_at": now_iso(),
    }
    errors = pipeline_engine.validate(rec["nodes"])
    rec["valid"] = not errors
    def _upd(doc):
        doc = dict(doc)
        doc[pid] = rec
        return doc
    pipelines_store.update(_upd)
    return jsonify({**_pipeline_view(rec), "valid": rec["valid"], "errors": errors})


@bp.get("/pipelines/<pid>")
def get_pipeline(pid):
    p = pipelines_store.read().get(pid)
    if not p:
        return jsonify({"error": "not found"}), 404
    return jsonify(_pipeline_view(p))


@bp.put("/pipelines/<pid>")
def update_pipeline(pid):
    data = request.get_json(silent=True) or {}
    errors = pipeline_engine.validate(data.get("nodes", []))

    def _upd(doc):
        doc = dict(doc)
        p = doc.get(pid)
        if not p:
            return doc
        p = dict(p)
        # 保留旧版本快照
        versions = list(p.get("versions", []))
        versions.insert(0, {"version": p.get("version", 1), "nodes": p.get("nodes", []),
                            "updated_at": p.get("updated_at")})
        p["nodes"] = data.get("nodes", p["nodes"])
        p["name"] = data.get("name", p["name"])
        p["version"] = _next_version(p)
        p["versions"] = versions[:config.PIPELINE_MAX_VERSIONS]
        p["updated_at"] = now_iso()
        p["valid"] = not errors
        doc[pid] = p
        return doc

    pipelines_store.update(_upd)
    p = pipelines_store.read().get(pid)
    if not p:
        return jsonify({"error": "not found"}), 404
    return jsonify({**_pipeline_view(p), "valid": not errors, "errors": errors})


@bp.delete("/pipelines/<pid>")
def delete_pipeline(pid):
    def _upd(doc):
        doc = dict(doc)
        doc.pop(pid, None)
        return doc
    pipelines_store.update(_upd)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# 运行 / 结果
# ---------------------------------------------------------------------------
@bp.post("/run")
def run_pipeline():
    data = request.get_json(silent=True) or {}
    image_id = data.get("image_id")
    nodes = data.get("nodes")
    pipeline_id = data.get("pipeline_id")
    pipeline_name = data.get("pipeline_name")

    if not nodes and pipeline_id:
        p = pipelines_store.read().get(pipeline_id)
        if p:
            nodes = p.get("nodes", [])
            pipeline_name = p.get("name")
    if image_id is None or nodes is None:
        return jsonify({"error": "缺少 image_id 或 nodes"}), 400

    res = process_image(image_store, cache, history, image_id, nodes,
                        pipeline_id=pipeline_id, pipeline_name=pipeline_name)
    if res["error"]:
        return jsonify({"error": res["error"], "history_id": res["history_id"]}), 200
    entry = cache.get_entry(res["result_id"]) or {}
    return jsonify({
        "result_id": res["result_id"],
        "cache_hit": res["cache_hit"],
        "history_id": res["history_id"],
        "file_url": f"/api/results/{res['result_id']}/file",
        "meta": entry.get("meta", {}),
        "node_results": (res["exec_result"] or {}).get("node_results", []),
    })


@bp.get("/results")
def list_results():
    return jsonify({"results": [_result_view(e) for e in cache.list_results()]})


@bp.get("/results/<result_id>")
def get_result(result_id):
    entry = cache.get_entry(result_id)
    if not entry:
        return jsonify({"error": "not found"}), 404
    return jsonify(_result_view(entry))


@bp.get("/results/<result_id>/file")
def result_file(result_id):
    entry = cache.get_entry(result_id)
    path = cache.result_path(result_id)
    if not path:
        return jsonify({"error": "not found"}), 404
    return send_file(path, mimetype=entry.get("mime") or "image/png")


# ---------------------------------------------------------------------------
# GIF 动图：多图合成 / 拆帧 / 逐帧处理 / 重新合帧
# ---------------------------------------------------------------------------
def _parse_gif_options(data):
    """读取合帧公共参数：loop、max_width（尺寸压缩）。"""
    try:
        loop = int(data.get("loop", 0))
    except (TypeError, ValueError):
        loop = 0
    max_width = data.get("max_width") or data.get("max_dim") or None
    if max_width is not None:
        try:
            max_width = max(16, min(config.GIF_MAX_WIDTH, int(max_width)))
        except (TypeError, ValueError):
            max_width = None
    return max(0, loop), max_width


def _save_gif_result(gif_bytes, meta, cache_key):
    """把合成的 GIF 登记进结果缓存，返回前端视图。"""
    result_id = cache.put_bytes(
        cache_key, gif_bytes, file_ext="gif", mime="image/gif",
        width=meta["width"], height=meta["height"], meta=meta)
    return _gif_result_view(result_id, meta)


def _gif_cache_key(kind, hashes, durations_raw, loop, max_width):
    """由原始输入生成确定性缓存键（归一化是确定性的，故键在请求间稳定）。"""
    return make_key("gif", kind, json.dumps(hashes),
                    json.dumps(durations_raw, sort_keys=True), loop, max_width or 0)


def _gif_result_view(result_id, meta):
    return {
        "result_id": result_id,
        "file_url": f"/api/results/{result_id}/file",
        "frame_count": meta["frame_count"],
        "width": meta["width"], "height": meta["height"],
        "durations_ms": meta["durations_ms"],
        "requested_durations_ms": meta["requested_durations_ms"],
        "total_duration_ms": meta["total_duration_ms"],
        "loop": meta["loop"],
        "size_bytes": meta["size_bytes"],
    }


def _cached_or_build(cache_key, frames, durations, loop, max_width):
    """命中缓存直接返回视图；否则合成 GIF、登记缓存后返回。"""
    cached_id = cache.get(cache_key)
    if cached_id:
        entry = cache.get_entry(cached_id) or {}
        return _gif_result_view(cached_id, entry.get("meta", {}))
    gif_bytes, meta = gif_engine.compose_gif(
        frames, durations_ms=durations, loop=loop, max_width=max_width)
    return _save_gif_result(gif_bytes, meta, cache_key)


def _parse_form_durations(raw):
    """表单里的 duration 字段：标量字符串，或 JSON 数组字符串 [100,200]。"""
    if raw is None or raw == "":
        return None
    try:
        parsed = json.loads(raw)
        return parsed
    except (TypeError, ValueError):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None


@bp.post("/gif/compose")
def gif_compose():
    """多张静态图按顺序合成 GIF。

    支持两种入参：
      1) multipart/form-data：files 多文件（按上传顺序）+ duration/loop/max_width 表单字段；
      2) application/json：{image_ids: [...], durations_ms, loop, max_width}
         引用「图像管理」里已上传的静态图。
    帧顺序严格按数组/文件顺序；durations_ms 可为标量或逐帧列表（毫秒）。
    """
    if request.content_type and request.content_type.startswith("multipart/"):
        files = request.files.getlist("files")
        if not files:
            return jsonify({"error": "未收到文件"}), 400
        frames, hashes, filename = [], [], None
        for f in files:
            data = f.read()
            if not data:
                continue
            filename = filename or f.filename
            try:
                img = Image.open(io.BytesIO(data))
                img.load()
            except Exception:  # noqa: BLE001
                return jsonify({"error": f"无法读取文件：{f.filename}"}), 400
            frames.append(img)
            hashes.append(hashlib.sha256(data).hexdigest())
        raw_durations = request.form.get("duration") or request.form.get("durations_ms")
        durations = _parse_form_durations(raw_durations)
        try:
            loop = max(0, int(request.form.get("loop", 0)))
        except (TypeError, ValueError):
            loop = 0
        max_width = None
        if request.form.get("max_width"):
            try:
                max_width = max(16, min(config.GIF_MAX_WIDTH,
                                        int(request.form.get("max_width"))))
            except (TypeError, ValueError):
                max_width = None
    else:
        data = request.get_json(silent=True) or {}
        image_ids = data.get("image_ids") or []
        if not image_ids:
            return jsonify({"error": "缺少 image_ids"}), 400
        frames, hashes = [], []
        for iid in image_ids:
            path = image_store.file_path(iid)
            if not path:
                return jsonify({"error": f"图像不存在：{iid}"}), 404
            frames.append(Image.open(path))
            hashes.append(iid)
        filename = None
        durations = data.get("durations_ms")
        loop, max_width = _parse_gif_options(data)

    if not frames:
        return jsonify({"error": "没有可合成的帧"}), 400
    key = _gif_cache_key("compose", hashes, durations, loop, max_width)
    try:
        view = _cached_or_build(key, frames, durations, loop, max_width)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(view)


@bp.post("/gif/split")
def gif_split():
    """上传一张 GIF，拆成有序静态帧并建立会话。

    multipart：file（GIF）。返回会话视图（逐帧 URL、每帧时长、循环次数）。
    也可传 JSON {image_id} 引用图库中已上传的 GIF。
    """
    if request.content_type and request.content_type.startswith("multipart/"):
        f = request.files.get("file")
        if not f:
            return jsonify({"error": "未收到文件"}), 400
        data, filename = f.read(), f.filename
    else:
        data_req = request.get_json(silent=True) or {}
        iid = data_req.get("image_id")
        path = image_store.file_path(iid) if iid else None
        if not path:
            return jsonify({"error": "图像不存在"}), 404
        with open(path, "rb") as fh:
            data = fh.read()
        rec = image_store.get(iid)
        filename = rec.get("filename") if rec else None

    if len(data) > config.MAX_UPLOAD_BYTES:
        return jsonify({"error": "文件超过大小限制"}), 400
    try:
        result = gif_engine.extract_frames(data)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    session = gif_sessions.create(
        result["frames"], result["durations_ms"], result["loop"],
        filename=filename or "animation.gif")
    return jsonify({"session": session,
                    "total_duration_ms": sum(result["durations_ms"])})


@bp.get("/gif/sessions")
def gif_list_sessions():
    return jsonify({"sessions": [gif_sessions.session_view(s)
                                 for s in gif_sessions.list_sessions()]})


@bp.get("/gif/sessions/<session_id>")
def gif_get_session(session_id):
    s = gif_sessions.get(session_id)
    if not s:
        return jsonify({"error": "会话不存在或已过期"}), 404
    return jsonify({"session": gif_sessions.session_view(s)})


@bp.delete("/gif/sessions/<session_id>")
def gif_delete_session(session_id):
    if not gif_sessions.delete(session_id):
        return jsonify({"error": "会话不存在"}), 404
    return jsonify({"ok": True})


@bp.get("/gif/sessions/<session_id>/frames/<int:index>")
def gif_frame_file(session_id, index):
    s = gif_sessions.get(session_id)
    if not s or not (0 <= index < len(s["frames"])):
        return jsonify({"error": "帧不存在"}), 404
    kind = "original" if request.args.get("kind") == "original" else "work"
    path = gif_sessions.frame_path(session_id, index, kind)
    if not os.path.exists(path):
        path = gif_sessions.frame_path(session_id, index, "work")
    return send_file(path, mimetype="image/png")


def _apply_frame_op(image, op):
    """对单帧应用一个轻量操作。

    op 两种形态：
      {"node": "<节点类型>", "params": {...}} —— 复用流水线节点注册表（滤镜/颜色/几何）；
      {"nodes": [流水线节点...]}               —— 复用整条流水线引擎。
    返回处理后的图像。
    """
    if op.get("nodes"):
        res = pipeline_engine.execute(image, op["nodes"])
        if res.get("error"):
            raise ValueError(res["error"])
        return res["image"]
    ntype = op.get("node")
    spec = nodes_mod.get_node(ntype) if ntype else None
    if spec is None:
        raise ValueError(f"未知操作类型：{ntype}")
    params = dict(spec["defaults"])
    params.update(op.get("params") or {})
    out_img, _ = spec["handler"](image, params, {})
    return out_img


@bp.post("/gif/sessions/<session_id>/frames/<int:index>/op")
def gif_frame_op(session_id, index):
    """对某一帧做滤镜/调整（结果写入 work.png，original 不动）。"""
    s = gif_sessions.get(session_id)
    if not s or not (0 <= index < len(s["frames"])):
        return jsonify({"error": "帧不存在"}), 404
    data = request.get_json(silent=True) or {}
    op = data.get("op")
    if not op:
        return jsonify({"error": "缺少 op"}), 400
    work_path = gif_sessions.frame_path(session_id, index, "work")
    try:
        img = util.ensure_rgb(Image.open(work_path))
        out = _apply_frame_op(img, op)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"处理失败：{type(exc).__name__}: {exc}"}), 400
    view = gif_sessions.update_frame(session_id, index, image=out, op=op)
    return jsonify({"frame": view})


@bp.post("/gif/sessions/<session_id>/frames/<int:index>/reset")
def gif_frame_reset(session_id, index):
    view = gif_sessions.reset_frame(session_id, index)
    if view is None:
        return jsonify({"error": "帧不存在"}), 404
    return jsonify({"frame": view})


@bp.put("/gif/sessions/<session_id>/frames/<int:index>")
def gif_frame_update(session_id, index):
    """调整某帧时长（毫秒，服务端归一化到合法 GIF 时长）。"""
    s = gif_sessions.get(session_id)
    if not s or not (0 <= index < len(s["frames"])):
        return jsonify({"error": "帧不存在"}), 404
    data = request.get_json(silent=True) or {}
    requested, actual = gif_engine.normalize_durations_ms([data.get("duration_ms")], 1)
    view = gif_sessions.update_frame(session_id, index, duration_ms=actual[0])
    return jsonify({"frame": view,
                    "requested_duration_ms": requested[0],
                    "actual_duration_ms": actual[0]})


@bp.post("/gif/sessions/<session_id>/frames/all/op")
def gif_frame_op_all(session_id):
    """把同一个滤镜/调整批量应用到所有帧（不改时长，只改画面）。"""
    s = gif_sessions.get(session_id)
    if not s:
        return jsonify({"error": "会话不存在或已过期"}), 404
    data = request.get_json(silent=True) or {}
    op = data.get("op")
    if not op:
        return jsonify({"error": "缺少 op"}), 400
    views = []
    for i, fr in enumerate(s["frames"]):
        try:
            img = util.ensure_rgb(Image.open(gif_sessions.frame_path(session_id, i, "work")))
            out = _apply_frame_op(img, op)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"第 {i + 1} 帧处理失败：{exc}"}), 400
        views.append(gif_sessions.update_frame(session_id, i, image=out, op=op))
    return jsonify({"frames": views})


@bp.post("/gif/sessions/<session_id>/rebuild")
def gif_rebuild(session_id):
    """把会话当前的逐帧 work 画面按时长重新合成为 GIF。

    帧顺序固定为会话清单的 index 升序（不读目录）；默认沿用每帧当前时长，
    因此「拆帧 -> 不改任何东西 -> 重新合帧」节奏与原动图一致。
    可选参数：loop（覆盖循环次数）、max_width（尺寸压缩）、durations_ms
    （整体覆盖逐帧时长，一般不用）。
    """
    s = gif_sessions.get(session_id)
    if not s:
        return jsonify({"error": "会话不存在或已过期"}), 404
    data = request.get_json(silent=True) or {}
    loop, max_width = _parse_gif_options(data)
    # 未显式给 loop 时沿用原动图的循环设置
    if "loop" not in data:
        loop = s.get("loop", 0)

    frames, durations = [], []
    for i, fr in enumerate(s["frames"]):
        path = gif_sessions.frame_path(session_id, i, "work")
        frames.append(Image.open(path))
        durations.append(fr["duration_ms"])
    if data.get("durations_ms") is not None:
        durations = data.get("durations_ms")

    # 缓存指纹包含每帧 work 文件内容 + 时长，画面或节奏变化都不会错误命中
    digests = [session_id]
    for i in range(len(s["frames"])):
        with open(gif_sessions.frame_path(session_id, i, "work"), "rb") as fh:
            digests.append(hashlib.sha256(fh.read()).hexdigest()[:16])

    try:
        key = _gif_cache_key("rebuild", digests, durations, loop, max_width)
        view = _cached_or_build(key, frames, durations, loop, max_width)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    view["session_id"] = session_id
    return jsonify(view)


# ---------------------------------------------------------------------------
# 特征 / 检测 / 分割 / 风格
# ---------------------------------------------------------------------------
@bp.post("/features")
def run_features():
    data = request.get_json(silent=True) or {}
    params = {"method": data.get("method", "sift"),
              "max_points": data.get("max_points", 120)}
    res, err = _run_op(data.get("image_id"), "features", params, features.extract_keypoints)
    if err:
        return err[0], err[1]
    return jsonify(res)


@bp.post("/features/match")
def run_match():
    data = request.get_json(silent=True) or {}
    a = data.get("image_id_a")
    b = data.get("image_id_b")
    params = {"method": data.get("method", "sift"),
              "max_points": data.get("max_points", 120),
              "max_matches": data.get("max_matches", 40)}
    rec_a, img_a = _load_full_image(a)
    rec_b, img_b = _load_full_image(b)
    if not rec_a or not rec_b:
        return jsonify({"error": "图像不存在"}), 404
    wa = util.downscale_to_max(util.ensure_rgb(img_a), config.MAX_DIM)
    wb = util.downscale_to_max(util.ensure_rgb(img_b), config.MAX_DIM)
    key = make_key(rec_a["hash"], rec_b["hash"], "match", json.dumps(params, sort_keys=True))
    cached = cache.get(key)
    if cached:
        entry = cache.get_entry(cached) or {}
        return jsonify({"result_id": cached, "cache_hit": True, **entry.get("meta", {})})
    result = features.match(wa, wb, params)
    image_out = result.get("image", wa)
    meta = {k: v for k, v in result.items() if k != "image"}
    result_id = cache.put(key, image_out, meta)
    return jsonify({"result_id": result_id, "cache_hit": False, **meta})


@bp.post("/detect")
def run_detect():
    data = request.get_json(silent=True) or {}
    params = {"method": data.get("method", "saliency"),
              "max_boxes": data.get("max_boxes", 20),
              "threshold": data.get("threshold") if data.get("auto", True) is False else None,
              "min_size": data.get("min_size", 0.02)}
    res, err = _run_op(data.get("image_id"), "detect", params, detection.detect)
    if err:
        return err[0], err[1]
    return jsonify(res)


@bp.post("/segment")
def run_segment():
    data = request.get_json(silent=True) or {}
    params = {"method": data.get("method", "threshold"),
              "value": data.get("value"), "block": data.get("block", 15),
              "colors": data.get("colors", 6), "alpha": data.get("alpha", 0.45)}
    res, err = _run_op(data.get("image_id"), "segment", params, segmentation.segment)
    if err:
        return err[0], err[1]
    return jsonify(res)


@bp.post("/style")
def run_style():
    data = request.get_json(silent=True) or {}
    params = {"style": data.get("style", "oil"), "strength": data.get("strength", 100)}
    res, err = _run_op(data.get("image_id"), "style", params, style.apply)
    if err:
        return err[0], err[1]
    return jsonify(res)


# ---------------------------------------------------------------------------
# 对比 / 差异
# ---------------------------------------------------------------------------
@bp.post("/compare/diff")
def compare_diff():
    data = request.get_json(silent=True) or {}
    rec, img_a = _load_full_image(data.get("image_id"))
    result_id = data.get("result_id")
    img_b = cache.result_image(result_id)
    if not rec or img_b is None:
        return jsonify({"error": "图像或结果不存在"}), 404

    # 对齐到同一尺寸（以结果尺寸为准）
    img_a = util.ensure_rgb(img_a).resize(img_b.size, Image.Resampling.LANCZOS)
    diff = ImageChops.difference(img_a, img_b).convert("L")
    hist = diff.histogram()
    total = sum(hist)
    mse = sum(i * c for i, c in enumerate(hist)) / max(total, 1)
    import math
    rmse = math.sqrt(mse)
    psnr = 100.0 if mse < 1e-9 else 20 * math.log10(255.0 / max(rmse, 1e-6))
    changed = sum(c for i, c in enumerate(hist) if i > 8) / max(total, 1)

    # 热力图：差异放大 + 伪彩色
    heat = diff.point(lambda v: util.clamp(v * 4))
    heat_rgb = colorize_heat(heat)
    result_id = cache.put(make_key(rec["hash"], result_id, "diff"), heat_rgb)
    return jsonify({
        "result_id": result_id,
        "file_url": f"/api/results/{result_id}/file",
        "metrics": {"mse": round(mse, 2), "rmse": round(rmse, 2),
                    "psnr": round(psnr, 2), "changed_ratio": round(changed, 4)},
    })


def colorize_heat(gray):
    """把差异灰度映射为热力伪彩色。"""
    lut = []
    for i in range(256):
        t = i / 255.0
        if t < 0.5:
            r = int(t * 2 * 255)
            g = 0
            b = int((0.5 - t) * 2 * 255)
        else:
            r = 255
            g = int((t - 0.5) * 2 * 255)
            b = 0
        lut.append((r, g, b))
    from PIL import Image
    try:
        return gray.point(lut, "RGB")
    except Exception:
        out = Image.new("RGB", gray.size)
        out.putdata([lut[v] for v in gray.getdata()])
        return out


# ---------------------------------------------------------------------------
# 批处理
# ---------------------------------------------------------------------------
@bp.get("/batch")
def list_batch():
    return jsonify({"jobs": batch.list_jobs()})


@bp.post("/batch")
def create_batch():
    data = request.get_json(silent=True) or {}
    nodes = data.get("nodes")
    pipeline_id = data.get("pipeline_id")
    pipeline_name = data.get("pipeline_name")
    if not nodes and pipeline_id:
        p = pipelines_store.read().get(pipeline_id)
        if p:
            nodes = p.get("nodes", [])
            pipeline_name = p.get("name")
    image_ids = data.get("image_ids", [])
    if not nodes or not image_ids:
        return jsonify({"error": "缺少 nodes 或 image_ids"}), 400
    job = batch.enqueue(nodes, image_ids, pipeline_id=pipeline_id, pipeline_name=pipeline_name)
    return jsonify({"job_id": job["id"]})


@bp.get("/batch/<job_id>")
def get_batch(job_id):
    job = batch.get_job(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify(job)


@bp.post("/batch/<job_id>/cancel")
def cancel_batch(job_id):
    job = batch.cancel(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify(job)


# ---------------------------------------------------------------------------
# 预设
# ---------------------------------------------------------------------------
def _preset_view(p):
    return {"id": p["id"], "name": p["name"], "scope": p["scope"],
            "node_type": p.get("node_type"), "params": p.get("params", {}),
            "created_at": p.get("created_at")}


@bp.get("/presets")
def list_presets():
    return jsonify({"presets": [_preset_view(p) for p in presets_store.read()]})


@bp.post("/presets")
def create_preset():
    data = request.get_json(silent=True) or {}
    pid = __import__("uuid").uuid4().hex
    p = {"id": pid, "name": data.get("name", "预设"),
         "scope": data.get("scope", "filter"), "node_type": data.get("node_type"),
         "params": data.get("params", {}), "created_at": now_iso()}
    presets_store.update(lambda doc: [p] + doc)
    return jsonify(_preset_view(p))


@bp.put("/presets/<pid>")
def update_preset(pid):
    data = request.get_json(silent=True) or {}

    def _upd(doc):
        for i, p in enumerate(doc):
            if p["id"] == pid:
                p = dict(p)
                p["name"] = data.get("name", p["name"])
                p["params"] = data.get("params", p["params"])
                doc[i] = p
                break
        return doc
    presets_store.update(_upd)
    for p in presets_store.read():
        if p["id"] == pid:
            return jsonify(_preset_view(p))
    return jsonify({"error": "not found"}), 404


@bp.delete("/presets/<pid>")
def delete_preset(pid):
    presets_store.update(lambda doc: [p for p in doc if p["id"] != pid])
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# 历史
# ---------------------------------------------------------------------------
@bp.get("/history")
def list_history():
    return jsonify({"history": history.list()})


@bp.get("/history/<history_id>")
def get_history(history_id):
    e = history.get(history_id)
    if not e:
        return jsonify({"error": "not found"}), 404
    return jsonify(e)


@bp.delete("/history/<history_id>")
def delete_history(history_id):
    e = history.get(history_id)
    if e and e.get("result_id"):
        cache.delete_result(e["result_id"])
    history.delete(history_id)
    return jsonify({"ok": True})


@bp.post("/history/<history_id>/restore")
def restore_history(history_id):
    snapshot = history.restore_snapshot(history_id)
    if snapshot is None:
        return jsonify({"error": "not found"}), 404
    pid = __import__("uuid").uuid4().hex
    e = history.get(history_id)
    rec = {
        "id": pid,
        "name": f"恢复自 {e.get('pipeline_name') or '历史'}",
        "nodes": snapshot.get("nodes", []),
        "version": 1, "versions": [],
        "created_at": now_iso(), "updated_at": now_iso(),
        "valid": True,
    }
    pipelines_store.update(lambda doc: {**doc, pid: rec})
    return jsonify(_pipeline_view(rec))


def init_app(app):
    """在应用启动时注册蓝图并做一次性一致性检查。"""
    app.register_blueprint(bp)
    issues = image_store.reconcile()
    if issues["orphan_files"] or issues["orphan_meta"]:
        app.logger.info("启动一致性检查发现孤儿：%s", issues)
    return app


def _reset_singletons():
    """测试辅助：config 路径被重定向后，重建所有文件绑定的单例。"""
    global image_store, cache, history, batch, gif_sessions, presets_store
    global pipelines_store
    config.ensure_dirs()
    image_store = ImageStore()
    cache = ResultCache()
    history = HistoryManager()
    batch = BatchManager(image_store, cache, history)
    gif_sessions = GifSessionStore()
    presets_store = JsonStore(config.PRESETS_JSON, [])
    pipelines_store = JsonStore(config.PIPELINES_JSON, {})
