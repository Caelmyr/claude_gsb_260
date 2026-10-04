/* 视图：GIF 动图工作台。
 *
 * 两个标签页：
 *  1) 多图合成 —— 从图库多选或直接上传，帧按卡片顺序排列（可上移/下移/删除），
 *     设置统一或逐帧时长（毫秒）、循环次数、最长边压缩，合成后在线预览并可下载。
 *  2) 拆帧编辑 —— 上传一张 GIF，服务端按帧索引拆成有序静态图；逐帧应用平台已有
 *     滤镜/调整（或批量应用到全部帧）、逐帧改时长、一键还原；重新合帧回 GIF。
 *
 * 节奏不变量由后端保证（拆出的时长原样写回），前端默认锁定时长并醒目展示
 * 「保持原节奏」，只有显式解锁后才能改某一帧的时长。
 */
window.Views = window.Views || {};
window.Views.gif = (function () {
  const C = window.Common;

  // 合成页状态：有序帧列表 [{id?, file?, name, url, size, width, height, duration}]
  let composeFrames = [];
  let splitState = { session: null };

  // ------------------------------------------------------------------ 合成
  function renderCompose(root) {
    root.innerHTML = `
      <div class="split">
        <div class="col">
          <div class="panel">
            <div class="panel-title">选择帧（按顺序合成）</div>
            <div class="toolbar">
              <label class="btn btn-primary btn-sm" id="gf-upload-btn">➕ 上传图片</label>
              <button class="btn btn-sm" id="gf-pick-btn">🖼️ 从图库选择</button>
              <input type="file" id="gf-file" accept="image/*" multiple hidden>
              <span class="spacer"></span>
              <button class="btn btn-sm btn-ghost" id="gf-clear">清空</button>
            </div>
            <div id="gf-order"><div class="empty"><span class="big">🎞️</span>还没有帧<br>上传图片或从图库选择，顺序就是播放顺序（第 1 张为起始帧）</div></div>
          </div>
        </div>
        <div class="col">
          <div class="panel">
            <div class="panel-title">动图参数</div>
            <div class="field">
              <label>每帧时长 <span class="hint">毫秒；GIF 实际精度为 10ms，服务端会归一化并提示</span></label>
              <div class="range-row">
                <input type="range" id="gf-duration" min="20" max="2000" step="10" value="100">
                <span class="range-val" id="gf-duration-val">100 ms</span>
              </div>
            </div>
            <div class="field">
              <label>循环次数 <span class="hint">0 = 无限循环</span></label>
              <input type="number" id="gf-loop" min="0" max="100" value="0">
            </div>
            <div class="field">
              <label>尺寸压缩：输出最长边 <span class="hint">留空 / 0 保持原尺寸</span></label>
              <input type="number" id="gf-width" min="0" max="1280" step="2" placeholder="例如 480，按比例缩放">
            </div>
            <div class="field">
              <label class="hint" id="gf-summary">共 0 帧 · 预计总时长 0 ms</label>
            </div>
            <button class="btn btn-primary" id="gf-run" disabled>🎬 合成 GIF</button>
          </div>
          <div class="panel">
            <div class="panel-title">合成结果</div>
            <div class="stage" id="gf-result"><span class="dim">选择至少 2 张图片后合成</span></div>
          </div>
        </div>
      </div>`;

    const fileInput = root.querySelector("#gf-file");
    root.querySelector("#gf-upload-btn").onclick = () => fileInput.click();
    fileInput.onchange = () => {
      const files = Array.from(fileInput.files || []);
      fileInput.value = "";
      addLocalFiles(root, files);
    };
    root.querySelector("#gf-pick-btn").onclick = () => openGalleryPicker(root);
    root.querySelector("#gf-clear").onclick = () => {
      composeFrames = [];
      renderOrder(root);
    };
    root.querySelector("#gf-duration").addEventListener("input", (e) => {
      root.querySelector("#gf-duration-val").textContent = e.target.value + " ms";
      composeFrames.forEach((f) => { f.duration = Number(e.target.value); });
      updateSummary(root);
    });
    root.querySelector("#gf-run").onclick = () => runCompose(root);
    renderOrder(root);
  }

  function addLocalFiles(root, files) {
    files.forEach((file) => {
      composeFrames.push({
        file,
        name: file.name,
        url: URL.createObjectURL(file),
        size: file.size,
        duration: Number(root.querySelector("#gf-duration").value),
      });
    });
    renderOrder(root);
  }

  function addImageIds(root, images) {
    const duration = Number(root.querySelector("#gf-duration").value);
    images.forEach((im) => {
      composeFrames.push({
        id: im.id, name: im.filename,
        url: im.thumbnail_url, size: im.size_bytes,
        width: im.width, height: im.height,
        duration,
      });
    });
    renderOrder(root);
  }

  function renderOrder(root) {
    const box = root.querySelector("#gf-order");
    if (!composeFrames.length) {
      box.innerHTML = `<div class="empty"><span class="big">🎞️</span>还没有帧<br>上传图片或从图库选择，顺序就是播放顺序</div>`;
    } else {
      box.innerHTML = `<div class="gif-frame-list">` + composeFrames.map((f, i) => `
        <div class="gif-frame-item" data-i="${i}">
          <span class="gif-idx">#${i + 1}</span>
          <img src="${C.esc(f.url)}" draggable="false">
          <div class="gif-frame-info">
            <div class="card-name" title="${C.esc(f.name)}">${C.esc(f.name)}</div>
            <div class="gif-dur-row">
              <input type="number" class="gif-dur" data-i="${i}" min="20" max="10000" step="10" value="${f.duration}">
              <span>ms</span>
            </div>
          </div>
          <div class="gif-frame-ops">
            <button class="btn btn-sm btn-ghost gf-up" ${i === 0 ? "disabled" : ""}>↑</button>
            <button class="btn btn-sm btn-ghost gf-down" ${i === composeFrames.length - 1 ? "disabled" : ""}>↓</button>
            <button class="btn btn-sm btn-danger gf-del">✕</button>
          </div>
        </div>`).join("") + `</div>`;

      box.querySelectorAll(".gf-up").forEach((b) => b.onclick = () => moveFrame(root, +b.closest(".gif-frame-item").dataset.i, -1));
      box.querySelectorAll(".gf-down").forEach((b) => b.onclick = () => moveFrame(root, +b.closest(".gif-frame-item").dataset.i, 1));
      box.querySelectorAll(".gf-del").forEach((b) => b.onclick = () => {
        composeFrames.splice(+b.closest(".gif-frame-item").dataset.i, 1);
        renderOrder(root);
      });
      box.querySelectorAll(".gif-dur").forEach((inp) => inp.onchange = () => {
        const i = +inp.dataset.i;
        const v = Math.max(20, Math.min(10000, Number(inp.value) || 100));
        inp.value = v;
        composeFrames[i].duration = v;
        updateSummary(root);
      });
    }
    updateSummary(root);
  }

  function moveFrame(root, i, delta) {
    const j = i + delta;
    if (j < 0 || j >= composeFrames.length) return;
    const t = composeFrames[i];
    composeFrames[i] = composeFrames[j];
    composeFrames[j] = t;
    renderOrder(root);
  }

  function updateSummary(root) {
    const total = composeFrames.reduce((s, f) => s + (f.duration || 0), 0);
    const el = root.querySelector("#gf-summary");
    if (el) el.textContent = `共 ${composeFrames.length} 帧 · 预计总时长 ${total} ms`;
    root.querySelector("#gf-run").disabled = composeFrames.length < 2;
  }

  async function runCompose(root) {
    const loop = Number(root.querySelector("#gf-loop").value) || 0;
    const maxWidth = Number(root.querySelector("#gf-width").value) || 0;
    const stage = root.querySelector("#gf-result");
    stage.innerHTML = `<div class="loading">合成中…</div>`;

    const local = composeFrames.filter((f) => f.file);
    let jsonResp = null;
    try {
      if (local.length) {
        // 有本地上传帧时走 multipart（本接口同时承载本会话内的全部帧）
        const fd = new FormData();
        composeFrames.forEach((f) => {
          if (f.file) fd.append("files", f.file);
        });
        // 图库引用的帧无法混入 multipart：这种组合场景提示用户（极少用）
        if (composeFrames.some((f) => !f.file)) {
          stage.innerHTML = `<span class="dim">请统一使用「上传图片」或「图库选择」，暂不混用</span>`;
          return;
        }
        fd.append("durations_ms", JSON.stringify(composeFrames.map((f) => f.duration)));
        fd.append("loop", String(loop));
        if (maxWidth) fd.append("max_width", String(maxWidth));
        jsonResp = await Api.post("/api/gif/compose", fd, true);
      } else {
        jsonResp = await Api.post("/api/gif/compose", {
          image_ids: composeFrames.map((f) => f.id),
          durations_ms: composeFrames.map((f) => f.duration),
          loop, max_width: maxWidth || undefined,
        });
      }
    } catch (e) {
      stage.innerHTML = `<span class="dim">合成失败：${C.esc(e.message)}</span>`;
      return;
    }
    showGifResult(stage, jsonResp, "compose");
  }

  function showGifResult(stage, r, source) {
    const mismatch = JSON.stringify(r.durations_ms) !== JSON.stringify(r.requested_durations_ms);
    stage.innerHTML = `
      <img src="${r.file_url}?t=${Date.now()}" class="gif-preview" autoplay loop>
      <div class="gif-meta">
        <div>${r.frame_count} 帧 · ${r.width}×${r.height} · ${C.fmtBytes(r.size_bytes)} · 总时长 ${r.total_duration_ms} ms · ${r.loop === 0 ? "无限循环" : "循环 " + r.loop + " 次"}</div>
        <div class="gif-durs" title="逐帧时长（毫秒）">时长: ${r.durations_ms.join(" / ")} ms</div>
        ${mismatch ? `<div class="warn">⚠ 部分请求时长已按 GIF 精度（10ms）归一化：请求 ${r.requested_durations_ms.join("/")} → 实际 ${r.durations_ms.join("/")}</div>` : ""}
        <div class="toolbar" style="margin-top:10px">
          <a class="btn btn-primary btn-sm" href="${r.file_url}" download="animation.gif">⬇ 下载 GIF</a>
        </div>
      </div>`;
  }

  // ------------------------------------------------------------------ 图库多选
  function openGalleryPicker(root) {
    C.fetchImages().then((images) => {
      const chosen = new Set();
      const m = C.modal(`
        <div id="gf-pick-grid" style="max-height:50vh;overflow:auto;margin:8px 0"></div>
        <div class="toolbar">
          <span class="hint" id="gf-pick-count">已选 0 张（点击卡片多选，按点击顺序加入）</span>
          <span class="spacer"></span>
          <button class="btn btn-sm" id="gf-pick-cancel">取消</button>
          <button class="btn btn-primary btn-sm" id="gf-pick-ok">加入帧序列</button>
        </div>`, "从图库选择（按点击顺序）");
      const grid = m.el.querySelector("#gf-pick-grid");
      grid.innerHTML = C.galleryHTML(images);
      const ordered = [];
      grid.addEventListener("click", (e) => {
        const card = e.target.closest(".card");
        if (!card) return;
        const id = card.dataset.id;
        if (chosen.has(id)) {
          chosen.delete(id);
          card.classList.remove("selected");
          const k = ordered.findIndex((x) => x.id === id);
          if (k >= 0) ordered.splice(k, 1);
        } else {
          chosen.add(id);
          card.classList.add("selected");
          ordered.push(images.find((x) => x.id === id));
        }
        m.el.querySelector("#gf-pick-count").textContent =
          `已选 ${ordered.length} 张（点击顺序 = 帧顺序）`;
      });
      m.el.querySelector("#gf-pick-cancel").onclick = m.close;
      m.el.querySelector("#gf-pick-ok").onclick = () => {
        addImageIds(root, ordered);
        m.close();
      };
    });
  }

  // ------------------------------------------------------------------ 拆帧
  function renderSplit(root) {
    root.innerHTML = `
      <div class="panel">
        <div class="panel-title">上传 GIF 拆帧 <span class="dim">拆出后可逐帧滤镜/调整，再合回动图；帧顺序与时长严格保持</span></div>
        <div class="toolbar">
          <label class="btn btn-primary btn-sm" id="gs-upload-btn">📤 选择 GIF 文件</label>
          <button class="btn btn-sm" id="gs-pick-btn">从图库选择已上传的 GIF</button>
          <input type="file" id="gs-file" accept="image/gif" hidden>
          <span class="spacer"></span>
          <span class="hint" id="gs-hint"></span>
        </div>
        <div id="gs-drop" class="gif-drop">或将 GIF 拖拽到此处</div>
      </div>
      <div id="gs-workspace"></div>`;

    const fileInput = root.querySelector("#gs-file");
    root.querySelector("#gs-upload-btn").onclick = () => fileInput.click();
    fileInput.onchange = () => {
      if (fileInput.files && fileInput.files[0]) splitFromFile(root, fileInput.files[0]);
      fileInput.value = "";
    };
    root.querySelector("#gs-pick-btn").onclick = () => openGifGallery(root);

    const drop = root.querySelector("#gs-drop");
    ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => {
      e.preventDefault(); drop.classList.add("over");
    }));
    ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => {
      e.preventDefault(); drop.classList.remove("over");
    }));
    drop.addEventListener("drop", (e) => {
      const f = e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) splitFromFile(root, f);
    });

    if (splitState.session) renderFrames(root);
  }

  function openGifGallery(root) {
    C.fetchImages().then((images) => {
      const gifs = images.filter((im) => /\\.gif$/i.test(im.filename) || im.format === "GIF");
      const m = C.modal(`
        <div style="max-height:50vh;overflow:auto;margin:8px 0">
          ${gifs.length ? C.galleryHTML(gifs) : '<div class="empty">图库中没有 GIF，请先在「图像管理」上传</div>'}
        </div>
        <div class="toolbar"><span class="spacer"></span><button class="btn btn-sm" id="gs-g-cancel">关闭</button></div>`,
        "选择图库中的 GIF");
      m.el.querySelector("#gs-g-cancel").onclick = m.close;
      m.el.addEventListener("click", (e) => {
        const card = e.target.closest(".card");
        if (!card) return;
        m.close();
        splitFromImageId(root, card.dataset.id);
      });
    });
  }

  async function splitFromFile(root, file) {
    const fd = new FormData();
    fd.append("file", file);
    await doSplit(root, () => Api.post("/api/gif/split", fd, true), file.name);
  }

  async function splitFromImageId(root, imageId) {
    await doSplit(root, () => Api.post("/api/gif/split", { image_id: imageId }));
  }

  async function doSplit(root, loader, name) {
    const hint = root.querySelector("#gs-hint");
    hint.textContent = "拆帧中…";
    try {
      const r = await loader();
      splitState.session = r.session;
      hint.textContent = "";
      renderFrames(root);
      C.toast(`已拆出 ${r.session.frame_count} 帧，总时长 ${r.total_duration_ms} ms`);
    } catch (e) {
      hint.textContent = "";
      C.toast("拆帧失败：" + e.message, "error");
    }
  }

  function renderFrames(root) {
    const s = splitState.session;
    if (!s) return;
    const ws = root.querySelector("#gs-workspace");
    ws.innerHTML = `
      <div class="panel">
        <div class="panel-title">帧序列（${s.frame_count} 帧 · ${s.width}×${s.height} · 总时长 ${s.total_duration_ms} ms · ${s.loop === 0 ? "无限循环" : "循环 " + s.loop + " 次"}）</div>
        <div class="toolbar">
          <button class="btn btn-sm" id="gs-filter-all">✨ 滤镜应用到全部帧</button>
          <label class="btn btn-sm btn-ghost"><input type="checkbox" id="gs-keep-rhythm" checked style="margin-right:6px">保持原节奏（锁定逐帧时长）</label>
          <span class="spacer"></span>
          <button class="btn btn-sm btn-ghost" id="gs-close">换一张 GIF</button>
        </div>
        <div class="gif-frames-grid" id="gs-grid"></div>
      </div>
      <div class="panel">
        <div class="panel-title">重新合成</div>
        <div class="toolbar">
          <label class="hint">覆盖循环（留空沿用原设置 ${s.loop === 0 ? "无限" : s.loop + " 次"}）：
            <input type="number" id="gs-loop" min="0" max="100" style="width:90px;display:inline-block;margin-left:8px" placeholder="默认"></label>
          <label class="hint">最长边压缩：
            <input type="number" id="gs-width" min="0" max="1280" style="width:110px;display:inline-block;margin-left:8px" placeholder="不压缩"></label>
          <span class="spacer"></span>
          <button class="btn btn-primary" id="gs-rebuild">🎬 重新合成为 GIF</button>
        </div>
        <div class="stage" id="gs-result" style="margin-top:12px"><span class="dim">逐帧编辑后点击合成；不做任何修改时，节奏与原 GIF 完全一致</span></div>
      </div>`;

    const grid = ws.querySelector("#gs-grid");
    grid.innerHTML = s.frames.map((f) => `
      <div class="gif-edit-card ${f.edited ? "edited" : ""}" data-i="${f.index}">
        <div class="gif-edit-head"><span>#${f.index + 1}</span>${f.edited ? '<span class="tag-edited">已编辑</span>' : ""}</div>
        <div class="gif-edit-imgs">
          <img src="${f.work_url}?t=${Date.now()}" class="work" title="当前帧">
        </div>
        <div class="gif-dur-row">
          <input type="number" class="gs-dur" data-i="${f.index}" min="20" max="10000" step="10"
                 value="${f.duration_ms}" disabled>
          <span>ms</span>
          <span class="hint" title="该帧在原 GIF 中的时长">原 ${f.original_duration_ms}</span>
        </div>
        <div class="gif-edit-ops">
          <button class="btn btn-sm gf-edit-one">滤镜</button>
          <button class="btn btn-sm btn-ghost gf-reset-one" ${f.edited ? "" : "disabled"}>还原</button>
        </div>
      </div>`).join("");

    ws.querySelector("#gs-close").onclick = () => {
      splitState.session = null;
      renderSplit(root);
    };
    ws.querySelector("#gs-keep-rhythm").onchange = (e) => {
      grid.querySelectorAll(".gs-dur").forEach((inp) => { inp.disabled = e.target.checked; });
    };
    grid.querySelectorAll(".gs-dur").forEach((inp) => inp.onchange = async () => {
      const i = +inp.dataset.i;
      try {
        const r = await Api.put(`/api/gif/sessions/${s.id}/frames/${i}`,
                                { duration_ms: Number(inp.value) });
        inp.value = r.actual_duration_ms;
        if (r.actual_duration_ms !== r.requested_duration_ms) {
          C.toast(`GIF 精度为 10ms，已调整为 ${r.actual_duration_ms} ms`);
        }
        patchFrame(root, r.frame);
      } catch (e) { C.toast(e.message, "error"); }
    });
    grid.querySelectorAll(".gf-edit-one").forEach((b) => b.onclick = () =>
      openFrameFilter(root, +b.closest(".gif-edit-card").dataset.i, false));
    grid.querySelectorAll(".gf-reset-one").forEach((b) => b.onclick = async () => {
      const i = +b.closest(".gif-edit-card").dataset.i;
      try {
        const r = await Api.post(`/api/gif/sessions/${s.id}/frames/${i}/reset`, {});
        patchFrame(root, r.frame);
        C.toast(`第 ${i + 1} 帧已还原`);
      } catch (e) { C.toast(e.message, "error"); }
    });
    ws.querySelector("#gs-filter-all").onclick = () => openFrameFilter(root, -1, true);
    ws.querySelector("#gs-rebuild").onclick = () => runRebuild(root);
  }

  async function runRebuild(root) {
    const s = splitState.session;
    const stage = root.querySelector("#gs-result");
    const loopRaw = root.querySelector("#gs-loop").value;
    const widthRaw = root.querySelector("#gs-width").value;
    const payload = {};
    if (loopRaw !== "") payload.loop = Number(loopRaw);
    if (widthRaw) payload.max_width = Number(widthRaw);
    stage.innerHTML = `<div class="loading">合成中…</div>`;
    try {
      const r = await Api.post(`/api/gif/sessions/${s.id}/rebuild`, payload);
      showGifResult(stage, r);
    } catch (e) {
      stage.innerHTML = `<span class="dim">合成失败：${C.esc(e.message)}</span>`;
    }
  }

  /* 用服务端返回的新帧视图就地更新一张卡片（避免整列表闪烁、滚动跳动）。 */
  function patchFrame(root, f) {
    const card = root.querySelector(`.gif-edit-card[data-i="${f.index}"]`);
    if (!card) return;
    card.classList.toggle("edited", f.edited);
    card.querySelector(".work").src = `${f.work_url}?t=${Date.now()}`;
    card.querySelector(".gs-dur").value = f.duration_ms;
    card.querySelector(".gf-reset-one").disabled = !f.edited;
    const head = card.querySelector(".gif-edit-head");
    head.innerHTML = `<span>#${f.index + 1}</span>${f.edited ? '<span class="tag-edited">已编辑</span>' : ""}`;
    splitState.session.frames[f.index] = f;
  }

  // ------------------------------------------------------------------ 帧滤镜弹窗
  async function openFrameFilter(root, index, all) {
    const nodes = await C.fetchNodes();
    // 逐帧处理只允许不改变画面尺寸的操作：滤镜、颜色类；
    // 旋转/裁剪/缩放等几何操作会破坏帧间尺寸一致性，不在这里暴露。
    const allowed = nodes.filter((n) => ["滤镜", "颜色"].includes(n.category));
    const groups = {};
    allowed.forEach((n) => (groups[n.category] = groups[n.category] || []).push(n));

    const m = C.modal(`
      <div class="toolbar" style="margin:6px 0">
        <select id="ff-node">
          ${Object.keys(groups).map((cat) =>
            `<optgroup label="${C.esc(cat)}">${groups[cat].map((n) =>
              `<option value="${n.type}">${C.esc(n.label)}</option>`).join("")}</optgroup>`).join("")}
        </select>
      </div>
      <div id="ff-params"></div>
      <div class="toolbar">
        <span class="hint">${all ? "将应用到全部帧（不改变时长与帧序）" : `应用到第 ${index + 1} 帧`}</span>
        <span class="spacer"></span>
        <button class="btn btn-sm" id="ff-cancel">取消</button>
        <button class="btn btn-primary btn-sm" id="ff-apply">应用</button>
      </div>`, all ? "批量滤镜" : `第 ${index + 1} 帧滤镜/调整`);

    const nodeSel = m.el.querySelector("#ff-node");
    const paramBox = m.el.querySelector("#ff-params");
    let currentForm = null;

    function renderParams() {
      const node = allowed.find((n) => n.type === nodeSel.value);
      currentForm = C.schemaForm(node.schema, node.defaults);
      paramBox.innerHTML = currentForm.html;
      currentForm.bind(paramBox);
    }
    nodeSel.onchange = renderParams;
    renderParams();

    m.el.querySelector("#ff-cancel").onclick = m.close;
    m.el.querySelector("#ff-apply").onclick = async () => {
      const params = currentForm.collect(paramBox);
      const op = { node: nodeSel.value, params };
      const s = splitState.session;
      m.close();
      try {
        if (all) {
          const r = await Api.post(`/api/gif/sessions/${s.id}/frames/all/op`, { op });
          r.frames.forEach((f) => patchFrame(root, f));
          C.toast(`已应用到全部 ${r.frames.length} 帧`);
        } else {
          const r = await Api.post(`/api/gif/sessions/${s.id}/frames/${index}/op`, { op });
          patchFrame(root, r.frame);
          C.toast(`第 ${index + 1} 帧已更新`);
        }
      } catch (e) { C.toast(e.message, "error"); }
    };
  }

  // ------------------------------------------------------------------ 外壳
  return {
    mount(el) {
      el.innerHTML = `
        <div class="gif-tabs toolbar">
          <button class="btn btn-sm gif-tab active" data-tab="compose">🖼️ 多图合成 GIF</button>
          <button class="btn btn-sm gif-tab" data-tab="split">✂️ GIF 拆帧编辑</button>
        </div>
        <div id="gif-tab-compose" class="gif-tabpane"></div>
        <div id="gif-tab-split" class="gif-tabpane" hidden></div>`;
      const panes = {
        compose: el.querySelector("#gif-tab-compose"),
        split: el.querySelector("#gif-tab-split"),
      };
      el.querySelectorAll(".gif-tab").forEach((b) => b.onclick = () => {
        el.querySelectorAll(".gif-tab").forEach((x) => x.classList.toggle("active", x === b));
        const tab = b.dataset.tab;
        panes.compose.hidden = tab !== "compose";
        panes.split.hidden = tab !== "split";
        if (tab === "split" && !panes.split.dataset.mounted) {
          renderSplit(panes.split);
          panes.split.dataset.mounted = "1";
        }
      });
      renderCompose(panes.compose);
    },
    refresh() {},
  };
})();
