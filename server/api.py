"""REST API：所有后端能力通过 JSON 接口暴露给前端。

这是各模块的粘合层：图像管理、流水线 CRUD、运行、特征/检测/分割/风格、
批处理、结果对比、预设、历史。单图运算接口统一走 _run_op（带缓存）。
"""
import io
import json

from flask import Blueprint, jsonify, request, send_file
from PIL import Image, ImageChops

from . import config, pipeline as pipeline_engine
from .algorithms import detection, features, segmentation, style, util
from .batch import BatchManager, process_image
from .cache import ResultCache, make_key
from . import gif as gif_mod
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
presets_store = JsonStore(config.PRESETS_JSON, [])
gif_sessions = GifSessionStore(config.GIF_SESSIONS_DIR)

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
    path = image_store.file_path(image_id)
    if not path:
        return jsonify({"error": "not found"}), 404
    mimetypes = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".gif": "image/gif", ".bmp": "image/bmp", ".webp": "image/webp",
                 ".tiff": "image/tiff"}
    ext = __import__("os").path.splitext(path)[1].lower()
    return send_file(path, mimetype=mimetypes.get(ext, "application/octet-stream"))


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
    path = cache.result_path(result_id)
    if not path:
        return jsonify({"error": "not found"}), 404
    mimetypes = {".png": "image/png", ".gif": "image/gif", ".jpg": "image/jpeg",
                 ".webp": "image/webp"}
    ext = __import__("os").path.splitext(path)[1].lower()
    return send_file(path, mimetype=mimetypes.get(ext, "image/png"))


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


# ---------------------------------------------------------------------------
# GIF 动图：拆解 / 帧编辑 / 重新合成 / 多图合成
# ---------------------------------------------------------------------------
def _save_gif_to_library(data, filename):
    """把生成的 GIF 登记进图像库（内容寻址、自动去重），返回图像记录。"""
    return image_store.save_upload(data, filename)


def _register_gif_result(data, kind, meta):
    """生成的 GIF 入库 + 登记结果缓存，返回 (图像记录, result_id)。"""
    rec = _save_gif_to_library(data, meta.pop("filename", f"{kind}.gif"))
    key = make_key("gif", kind, rec["hash"], json.dumps(meta, sort_keys=True, default=str))
    with Image.open(io.BytesIO(data)) as probe:
        size = probe.size
    result_id = cache.put_bytes(key, data, ".gif", size, {"kind": "gif", **meta})
    return rec, result_id


def _read_gif_bytes_from_request():
    """从 multipart 上传或 image_id 中取出 GIF 字节。返回 (data, source_info, error_response)。"""
    upload = request.files.get("file")
    image_id = (request.form.get("image_id") if upload else
                (request.get_json(silent=True) or {}).get("image_id"))
    if upload:
        data = upload.read()
        source = {"filename": upload.filename or "upload.gif", "uploaded": True}
    elif image_id:
        path = image_store.file_path(image_id)
        rec = image_store.get(image_id)
        if not path or not rec:
            return None, None, ({"error": "图像不存在"}, 404)
        with open(path, "rb") as f:
            data = f.read()
        source = {"image_id": image_id, "filename": rec.get("filename", ""), "uploaded": False}
    else:
        return None, None, ({"error": "请上传 GIF 文件或指定 image_id"}, 400)
    if not data:
        return None, None, ({"error": "文件为空"}, 400)
    if len(data) > config.MAX_UPLOAD_BYTES:
        return None, None, ({"error": "超过大小限制"}, 413)
    return data, source, None


@bp.post("/gif/decompose")
def gif_decompose():
    """上传/指定一张 GIF，拆成有序全画布帧，创建拆帧会话。"""
    data, source, err = _read_gif_bytes_from_request()
    if err:
        return jsonify(err[0]), err[1]
    try:
        extracted = gif_mod.extract_frames(data, max_dim=config.GIF_DEFAULT_MAX_DIM)
    except gif_mod.GifError as exc:
        return jsonify({"error": str(exc)}), 400
    if extracted["frame_count"] > config.GIF_MAX_FRAMES:
        return jsonify({"error": f"帧数 {extracted['frame_count']} 超过上限 "
                                 f"{config.GIF_MAX_FRAMES}"}), 400
    rec = gif_sessions.create(extracted, source=source)
    return jsonify({"session": gif_sessions.view(rec)})


@bp.get("/gif/sessions/<sid>")
def gif_session_get(sid):
    rec = gif_sessions.get(sid)
    if not rec:
        return jsonify({"error": "会话不存在（可能已过期）"}), 404
    return jsonify({"session": gif_sessions.view(rec)})


@bp.get("/gif/sessions/<sid>/frames/<int:index>/file")
def gif_session_frame_file(sid, index):
    path = gif_sessions.frame_path(sid, index)
    if not path:
        return jsonify({"error": "帧不存在"}), 404
    return send_file(path, mimetype="image/png")


@bp.post("/gif/sessions/<sid>/frames/<int:index>/process")
def gif_session_process_frame(sid, index):
    """对会话中的某一帧执行流水线节点（滤镜/颜色/几何），结果覆盖该帧。

    帧序号与时长不变——这是「拆帧再合帧节奏不变」的保证。
    body: {nodes: [...]}（与滤镜链编辑器同构；为空则原样返回）
    """
    if gif_sessions.get(sid) is None:
        return jsonify({"error": "会话不存在"}), 404
    img = gif_sessions.frame_image(sid, index)
    if img is None:
        return jsonify({"error": "帧不存在"}), 404
    data = request.get_json(silent=True) or {}
    nodes = data.get("nodes") or []
    errors = pipeline_engine.validate(nodes) if nodes else []
    if errors:
        return jsonify({"error": "参数不合法", "details": errors}), 400

    work = util.downscale_to_max(util.ensure_rgb(img), config.MAX_DIM)
    result = pipeline_engine.execute(work, nodes) if nodes else {"image": work}
    rec = gif_sessions.replace_frame(sid, index, result["image"])
    if rec is None:
        return jsonify({"error": "帧保存失败"}), 500
    view = gif_sessions.view(rec)
    frame = view["frames"][index]
    return jsonify({"frame": frame, "node_results": result.get("node_results", [])})


@bp.post("/gif/sessions/<sid>/frames/apply-all")
def gif_session_apply_all(sid):
    """同一组节点批量应用到所有帧（逐帧执行，顺序不变、时长不变）。"""
    rec = gif_sessions.get(sid)
    if rec is None:
        return jsonify({"error": "会话不存在"}), 404
    data = request.get_json(silent=True) or {}
    nodes = data.get("nodes") or []
    errors = pipeline_engine.validate(nodes)
    if errors:
        return jsonify({"error": "参数不合法", "details": errors}), 400

    failed = []
    for i in range(len(rec["frames"])):
        img = gif_sessions.frame_image(sid, i)
        if img is None:
            failed.append({"index": i, "error": "帧文件缺失"})
            continue
        try:
            work = util.downscale_to_max(util.ensure_rgb(img), config.MAX_DIM)
            result = pipeline_engine.execute(work, nodes)
            gif_sessions.replace_frame(sid, i, result["image"])
        except Exception as exc:  # noqa: BLE001
            failed.append({"index": i, "error": str(exc)})
    rec = gif_sessions.get(sid)
    return jsonify({"session": gif_sessions.view(rec), "failed": failed})


@bp.post("/gif/sessions/<sid>/frames/<int:index>/reset")
def gif_session_reset_frame(sid, index):
    """把某一帧恢复为刚拆解时的原始画面（时长不动）。"""
    # 原始帧只在创建时保存；会话里没存副本，这里用源 GIF 重新拆一次
    rec = gif_sessions.get(sid)
    if rec is None:
        return jsonify({"error": "会话不存在"}), 404
    source = rec.get("source") or {}
    image_id = source.get("image_id")
    if source.get("uploaded") or not image_id:
        return jsonify({"error": "上传来源的会话不支持单帧恢复，请重新拆解"}), 400
    path = image_store.file_path(image_id)
    if not path:
        return jsonify({"error": "源 GIF 已被删除"}), 404
    with open(path, "rb") as f:
        extracted = gif_mod.extract_frames(f.read(), max_dim=config.GIF_DEFAULT_MAX_DIM)
    if index >= extracted["frame_count"]:
        return jsonify({"error": "帧不存在"}), 404
    rec = gif_sessions.replace_frame(sid, index, extracted["frames"][index], mark_edited=False)
    return jsonify({"frame": gif_sessions.view(rec)["frames"][index]})


@bp.post("/gif/sessions/<sid>/rebuild")
def gif_session_rebuild(sid):
    """把会话帧按原顺序重新编码为 GIF。

    body（全部可选，不传即「原样合回」）：
      durations   [毫秒...] 覆盖每帧时长（个数必须等于帧数）
      loop        0=无限循环（默认沿用拆解值）
      max_dim     压缩后的最长边
      colors      调色板色数 32/64/128/256
    """
    rec = gif_sessions.get(sid)
    if rec is None:
        return jsonify({"error": "会话不存在"}), 404
    images = gif_sessions.list_frame_images(sid)
    if images is None:
        return jsonify({"error": "帧文件缺失，无法合成"}), 500

    data = request.get_json(silent=True) or {}
    durations = [fr["duration_ms"] for fr in rec["frames"]]
    if data.get("durations") is not None:
        supplied = data.get("durations")
        if not isinstance(supplied, list) or len(supplied) != len(images):
            return jsonify({"error": "时长列表长度必须等于帧数"}), 400
        durations = [gif_mod.normalize_duration(d) for d in supplied]
    loop = data.get("loop", rec.get("loop", 0))
    try:
        loop = max(0, int(loop))
    except (TypeError, ValueError):
        return jsonify({"error": "loop 必须是非负整数"}), 400
    max_dim = data.get("max_dim")
    if max_dim not in (None, ""):
        try:
            max_dim = int(max_dim)
            if max_dim <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return jsonify({"error": "max_dim 必须是正整数"}), 400
    else:
        max_dim = None
    try:
        colors = int(data.get("colors", 256))
    except (TypeError, ValueError):
        return jsonify({"error": "colors 必须是整数"}), 400

    try:
        gif_bytes, meta = gif_mod.encode_gif(
            images, durations, loop=loop, max_dim=max_dim, colors=colors)
    except gif_mod.GifError as exc:
        return jsonify({"error": str(exc)}), 400

    # 节奏校验：编码后回读时长 vs 请求时长
    requested = gif_mod.quantize_durations(durations)
    drift = [abs(a - b) for a, b in zip(meta["effective_durations"], requested)]
    rhythm_preserved = sum(drift) == 0 and meta["frame_count"] == len(images)
    name = f"rebuilt_{(rec.get('source') or {}).get('filename', 'session.gif')}"
    if not name.lower().endswith(".gif"):
        name += ".gif"
    meta.update({"filename": name, "session_id": sid,
                 "requested_durations": requested, "rhythm_preserved": rhythm_preserved})
    img_rec, result_id = _register_gif_result(gif_bytes, "rebuild", meta)

    return jsonify({
        "result_id": result_id,
        "image_id": img_rec["id"],
        "file_url": f"/api/images/{img_rec['id']}/file",
        "rhythm_preserved": rhythm_preserved,
        "effective_durations": meta["effective_durations"],
        "width": meta["width"], "height": meta["height"],
        "frame_count": meta["frame_count"], "loop": meta["loop"],
        "size_bytes": meta["size_bytes"],
    })


@bp.post("/gif/compose")
def gif_compose():
    """把多张静态图按给定顺序合成 GIF。

    body:
      image_ids   [id...]  按列表顺序取帧（顺序即帧序，由前端显式控制）
      duration    统一每帧毫秒数（默认 100）
      durations   [毫秒...] 逐帧时长（优先于 duration）
      loop        0=无限循环（默认）
      max_dim     压缩最长边（可选）
      colors      调色板色数（默认 256）
    """
    payload = request.get_json(silent=True) or {}
    ids = payload.get("image_ids") or []
    if not isinstance(ids, list):
        return jsonify({"error": "image_ids 必须是列表"}), 400

    frames, skipped = [], []
    for iid in ids:
        path = image_store.file_path(iid)
        rec = image_store.get(iid)
        if not path or not rec:
            skipped.append({"image_id": iid, "reason": "图像不存在"})
            continue
        try:
            img = Image.open(path)
            img.load()
            frames.append(img)
        except Exception:  # noqa: BLE001
            skipped.append({"image_id": iid, "reason": "无法识别的图像"})

    if len(frames) < config.GIF_COMPOSE_MIN_FRAMES:
        return jsonify({"error": f"至少需要 {config.GIF_COMPOSE_MIN_FRAMES} 张可用静态图",
                        "skipped": skipped}), 400
    if len(frames) > config.GIF_MAX_FRAMES:
        return jsonify({"error": f"帧数超过上限 {config.GIF_MAX_FRAMES}", "skipped": skipped}), 400

    default_dur = payload.get("duration", 100)
    durations_raw = payload.get("durations")
    if isinstance(durations_raw, list) and len(durations_raw) == len(frames):
        durations = [gif_mod.normalize_duration(d) for d in durations_raw]
    else:
        durations = [gif_mod.normalize_duration(default_dur)] * len(frames)

    try:
        loop = max(0, int(payload.get("loop", 0)))
    except (TypeError, ValueError):
        return jsonify({"error": "loop 必须是非负整数", "skipped": skipped}), 400
    max_dim = payload.get("max_dim")
    if max_dim not in (None, ""):
        try:
            max_dim = int(max_dim)
            if max_dim <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return jsonify({"error": "max_dim 必须是正整数", "skipped": skipped}), 400
    else:
        max_dim = None
    try:
        colors = int(payload.get("colors", 256))
    except (TypeError, ValueError):
        return jsonify({"error": "colors 必须是整数", "skipped": skipped}), 400

    frames = [gif_mod.flatten_on_white(im) for im in frames]
    try:
        gif_bytes, meta = gif_mod.encode_gif(
            frames, durations, loop=loop, max_dim=max_dim, colors=colors)
    except gif_mod.GifError as exc:
        return jsonify({"error": str(exc), "skipped": skipped}), 400

    requested = gif_mod.quantize_durations(durations)
    rhythm_preserved = meta["effective_durations"] == requested
    meta.update({"filename": "composed.gif", "rhythm_preserved": rhythm_preserved,
                 "requested_durations": requested})
    img_rec, result_id = _register_gif_result(gif_bytes, "compose", meta)

    return jsonify({
        "result_id": result_id,
        "image_id": img_rec["id"],
        "file_url": f"/api/images/{img_rec['id']}/file",
        "rhythm_preserved": rhythm_preserved,
        "effective_durations": meta["effective_durations"],
        "width": meta["width"], "height": meta["height"],
        "frame_count": meta["frame_count"], "loop": meta["loop"],
        "size_bytes": meta["size_bytes"], "skipped": skipped,
    })


@bp.delete("/gif/sessions/<sid>")
def gif_session_delete(sid):
    gif_sessions.delete(sid)
    return jsonify({"ok": True})


def init_app(app):
    """在应用启动时注册蓝图并做一次性一致性检查。"""
    app.register_blueprint(bp)
    issues = image_store.reconcile()
    if issues["orphan_files"] or issues["orphan_meta"]:
        app.logger.info("启动一致性检查发现孤儿：%s", issues)
    return app
