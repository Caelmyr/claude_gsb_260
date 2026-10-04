/* 视图：GIF 动图。
 * 两个标签页：
 *   合成 —— 从图库选多张静态图，按顺序排成帧，设置逐帧时长/循环/压缩后合成 GIF；
 *   拆解 —— 上传或选择一张 GIF，逐帧列出，对单帧或全部帧做滤镜调整后原样合回。
 */
window.Views = window.Views || {};
window.Views.gif = (function () {
  const C = window.Common;

  // 逐帧编辑可用的快捷节点（后端支持全部节点，这里只暴露最常用的滤镜/颜色）
  const QUICK_NODES = [
    { type: "brightness", label: "亮度", params: { amount: 30 } },
    { type: "contrast", label: "对比度", params: { amount: 25 } },
    { type: "saturation", label: "饱和度", params: { amount: 30 } },
    { type: "grayscale", label: "灰度化", params: { mode: "luma" } },
    { type: "invert", label: "反相", params: {} },
    { type: "sharpen", label: "锐化", params: { amount: 40 } },
    { type: "blur", label: "模糊", params: { mode: "gaussian", radius: 2 } },
  ];

  const state = {
    tab: "compose",
    // 合成页
    picked: [],          // [{id, filename, thumbnail_url, width, height, duration}]
    // 拆解页
    session: null,
    selectedFrame: 0,
    busy: false,
  };

  // ---------------------------------------------------------------- 工具
  function el() {
    return document.querySelector('.view[data-view="gif"]');
  }
  function setBusy(on, text) {
    state.busy = on;
    const mask = el().querySelector("#gif-busy");
    if (mask) {
      mask.hidden = !on;
      mask.querySelector(".gif-busy-text").textContent = text || "处理中…";
    }
  }

  // ================================================================ 合成页
  function renderCompose(root) {
    root.innerHTML = `
      <div class="split" style="grid-template-columns: 320px 1fr">
        <div>
          <div class="panel">
            <div class="panel-title">选择静态图<span class="dim">点击按选择顺序加入帧序列</span></div>
            <div class="toolbar" style="margin-bottom:8px">
              <button class="btn btn-sm" id="gc-upload">上传新图</button>
              <input type="file" id="gc-file" multiple accept="image/*" hidden>
              <button class="btn btn-sm" id="gc-refresh">刷新图库</button>
            </div>
            <div id="gc-gallery" style="max-height:420px;overflow:auto"></div>
          </div>
        </div>
        <div>
          <div class="panel">
            <div class="panel-title">帧序列<span class="dim">顺序就是播放顺序，可拖拽或用按钮调整</span></div>
            <div class="toolbar">
              <span class="badge" id="gc-count">0 帧</span>
              <span class="spacer" style="flex:1"></span>
              <label class="dim" style="font-size:12px">统一时长(ms)</label>
              <input type="number" id="gc-dur" value="100" min="20" max="10000" step="10" style="width:80px">
              <button class="btn btn-sm" id="gc-apply-dur">应用到全部</button>
              <button class="btn btn-sm btn-danger" id="gc-clear">清空</button>
            </div>
            <div id="gc-frames" class="gif-frames"></div>
          </div>
          <div class="panel">
            <div class="panel-title">输出设置</div>
            <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px">
              <div class="field"><label>循环</label>
                <select id="gc-loop">
                  <option value="0" selected>无限循环</option>
                  <option value="1">播放 2 次</option>
                  <option value="2">播放 3 次</option>
                  <option value="5">播放 6 次</option>
                </select>
              </div>
              <div class="field"><label>尺寸压缩（最长边）<span class="hint">留空=不压缩</span></label>
                <select id="gc-maxdim">
                  <option value="" selected>原始尺寸</option>
                  <option value="320">≤ 320px</option>
                  <option value="480">≤ 480px</option>
                  <option value="640">≤ 640px</option>
                  <option value="800">≤ 800px</option>
                </select>
              </div>
              <div class="field"><label>调色板色数</label>
                <select id="gc-colors">
                  <option value="256" selected>256（画质优先）</option>
                  <option value="128">128</option>
                  <option value="64">64</option>
                  <option value="32">32（体积优先）</option>
                </select>
              </div>
            </div>
            <button class="btn btn-primary" id="gc-run">合成 GIF</button>
          </div>
          <div class="panel" id="gc-result-panel" hidden>
            <div class="panel-title">合成结果</div>
            <div class="stage"><img id="gc-result-img" alt="result"></div>
            <div class="keypoint-stats" style="line-height:1.9;margin-top:10px" id="gc-result-meta"></div>
            <div class="toolbar" style="margin-top:10px">
              <a class="btn btn-sm" id="gc-download" download="composed.gif">下载 GIF</a>
            </div>
          </div>
        </div>
      </div>`;

    loadGallery(root);
    root.querySelector("#gc-upload").onclick = () => root.querySelector("#gc-file").click();
    root.querySelector("#gc-file").onchange = async (e) => {
      if (!e.target.files.length) return;
      const r = await Api.upload(Array.from(e.target.files));
      if (r.saved.length) { C.toast(`已上传 ${r.saved.length} 张，点击缩略图加入序列`, "success"); C.invalidate("images"); }
      if (r.skipped.length) C.toast(`${r.skipped.length} 张被跳过`, "error");
      loadGallery(root);
      e.target.value = "";
    };
    root.querySelector("#gc-refresh").onclick = () => { C.invalidate("images"); loadGallery(root); };
    root.querySelector("#gc-apply-dur").onclick = () => {
      const d = Math.max(20, Number(root.querySelector("#gc-dur").value) || 100);
      state.picked.forEach((p) => { p.duration = d; });
      drawFrames(root);
    };
    root.querySelector("#gc-clear").onclick = () => { state.picked = []; drawFrames(root); };
    root.querySelector("#gc-run").onclick = () => compose(root);
    drawFrames(root);
  }

  async function loadGallery(root) {
    const box = root.querySelector("#gc-gallery");
    box.innerHTML = `<div class="loading">加载图库…</div>`;
    const images = await C.fetchImages();
    if (!images.length) {
      box.innerHTML = `<div class="empty">图库为空，请先上传静态图</div>`;
      return;
    }
    box.innerHTML = `<div class="grid" style="grid-template-columns:repeat(auto-fill,minmax(110px,1fr))">` +
      images.filter((im) => (im.format || "").toUpperCase() !== "GIF").map((im) => `
        <div class="card" data-id="${C.esc(im.id)}">
          <img class="thumb" src="${C.esc(im.thumbnail_url)}" loading="lazy">
          <div class="card-meta">
            <div class="card-name" title="${C.esc(im.filename)}">${C.esc(im.filename)}</div>
            <div class="card-dim">${im.width}×${im.height}</div>
          </div>
        </div>`).join("") + `</div>`;
    box.onclick = (e) => {
      const card = e.target.closest(".card");
      if (!card) return;
      const im = images.find((x) => x.id === card.dataset.id);
      if (!im) return;
      if (state.picked.some((p) => p.id === im.id)) { C.toast("该图已在帧序列中", "error"); return; }
      state.picked.push({ id: im.id, filename: im.filename, thumbnail_url: im.thumbnail_url,
                          width: im.width, height: im.height, duration: 100 });
      drawFrames(root);
      C.toast(`已加入第 ${state.picked.length} 帧`, "success");
    };
  }

  function drawFrames(root) {
    const box = root.querySelector("#gc-frames");
    root.querySelector("#gc-count").textContent = `${state.picked.length} 帧`;
    if (!state.picked.length) {
      box.innerHTML = `<div class="empty"><span class="big">🎞️</span>从左侧图库点选图像，按选择顺序组成帧序列</div>`;
      return;
    }
    box.innerHTML = state.picked.map((p, i) => `
      <div class="gif-frame" data-idx="${i}" draggable="true">
        <div class="gif-frame-idx">#${i + 1}</div>
        <img src="${C.esc(p.thumbnail_url)}">
        <div class="gif-frame-ctl">
          <div class="gif-frame-name" title="${C.esc(p.filename)}">${C.esc(p.filename)}</div>
          <div class="select-row">
            <label>时长</label>
            <input type="number" min="20" step="10" value="${p.duration}" data-dur="${i}">
            <span class="dim">ms</span>
          </div>
          <div class="toolbar" style="margin:6px 0 0;gap:4px">
            <button class="btn btn-sm" data-move="-1" data-idx="${i}" ${i === 0 ? "disabled" : ""}>←</button>
            <button class="btn btn-sm" data-move="1" data-idx="${i}" ${i === state.picked.length - 1 ? "disabled" : ""}>→</button>
            <button class="btn btn-sm btn-danger" data-rm="${i}">移除</button>
          </div>
        </div>
      </div>`).join("");

    box.querySelectorAll("[data-dur]").forEach((inp) => {
      inp.onchange = () => {
        const i = Number(inp.dataset.dur);
        state.picked[i].duration = Math.max(20, Number(inp.value) || 100);
      };
    });
    box.querySelectorAll("[data-move]").forEach((btn) => {
      btn.onclick = () => {
        const i = Number(btn.dataset.idx), d = Number(btn.dataset.move);
        const j = i + d;
        [state.picked[i], state.picked[j]] = [state.picked[j], state.picked[i]];
        drawFrames(root);
      };
    });
    box.querySelectorAll("[data-rm]").forEach((btn) => {
      btn.onclick = () => { state.picked.splice(Number(btn.dataset.rm), 1); drawFrames(root); };
    });
    // 拖拽排序
    let dragIdx = null;
    box.querySelectorAll(".gif-frame").forEach((card) => {
      card.ondragstart = () => { dragIdx = Number(card.dataset.idx); card.style.opacity = ".4"; };
      card.ondragend = () => { card.style.opacity = "1"; };
      card.ondragover = (e) => e.preventDefault();
      card.ondrop = (e) => {
        e.preventDefault();
        const target = Number(card.dataset.idx);
        if (dragIdx === null || dragIdx === target) return;
        const [moved] = state.picked.splice(dragIdx, 1);
        state.picked.splice(target, 0, moved);
        drawFrames(root);
      };
    });
  }

  async function compose(root) {
    if (state.busy) return;
    if (state.picked.length < 2) { C.toast("至少选择 2 张静态图", "error"); return; }
    setBusy(true, "正在合成 GIF…");
    try {
      const body = {
        image_ids: state.picked.map((p) => p.id),
        durations: state.picked.map((p) => Math.max(20, Number(p.duration) || 100)),
        loop: Number(root.querySelector("#gc-loop").value),
        colors: Number(root.querySelector("#gc-colors").value),
      };
      const maxDim = root.querySelector("#gc-maxdim").value;
      if (maxDim) body.max_dim = Number(maxDim);
      const r = await Api.post("/api/gif/compose", body);
      const panel = root.querySelector("#gc-result-panel");
      panel.hidden = false;
      root.querySelector("#gc-result-img").src = `${r.file_url}?t=${Date.now()}`;
      root.querySelector("#gc-download").href = `${r.file_url}?download=1`;
      const total = r.effective_durations.reduce((a, b) => a + b, 0);
      root.querySelector("#gc-result-meta").innerHTML = `
        <div><span class="dim">帧数</span> ${r.frame_count} ·
            <span class="dim">尺寸</span> ${r.width}×${r.height} ·
            <span class="dim">大小</span> ${C.fmtBytes(r.size_bytes)}</div>
        <div><span class="dim">每帧时长</span> <span class="mono">[${r.effective_durations.join(", ")}]</span> ms</div>
        <div><span class="dim">一轮总时长</span> ${total} ms · <span class="dim">循环</span> ${r.loop === 0 ? "无限" : r.loop + 1 + " 次"}</div>
        <div>${r.rhythm_preserved
          ? '<span class="badge green">✔ 帧时长与设置完全一致</span>'
          : '<span class="badge amber">⚠ 部分时长被 GIF 厘秒精度取整</span>'}</div>`;
      C.toast("合成完成", "success");
      C.invalidate("images");
    } catch (e) {
      C.toast("合成失败：" + e.message, "error");
    } finally {
      setBusy(false);
    }
  }

  // ================================================================ 拆解页
  function renderDecompose(root) {
    root.innerHTML = `
      <div class="panel">
        <div class="panel-title">选择 GIF 动图</div>
        <div class="split" style="grid-template-columns:1fr 1fr">
          <div id="gd-gallery"></div>
          <div>
            <div id="gd-drop" style="border:2px dashed var(--border-strong);border-radius:10px;padding:24px;text-align:center;color:var(--text-dim);cursor:pointer">
              <div style="font-size:24px">📥</div><div>上传一张 GIF 并立即拆解</div>
            </div>
            <input type="file" id="gd-file" accept="image/gif" hidden>
          </div>
        </div>
      </div>
      <div id="gd-session"></div>`;
    loadGifGallery(root);
    const drop = root.querySelector("#gd-drop");
    const input = root.querySelector("#gd-file");
    drop.onclick = () => input.click();
    input.onchange = async () => {
      if (!input.files.length) return;
      const up = await Api.upload([input.files[0]]);
      if (!up.saved.length) { C.toast("上传失败", "error"); return; }
      C.invalidate("images");
      await decompose(root, up.saved[0].id);
      input.value = "";
    };
    if (state.session) drawSession(root);
  }

  async function loadGifGallery(root) {
    const box = root.querySelector("#gd-gallery");
    const images = await C.fetchImages();
    const gifs = images.filter((im) => (im.format || "").toUpperCase() === "GIF");
    if (!gifs.length) {
      box.innerHTML = `<div class="empty" style="padding:18px">图库中暂无 GIF<br><span class="dim">可在右侧直接上传</span></div>`;
      return;
    }
    box.innerHTML = `<div class="grid" style="grid-template-columns:repeat(auto-fill,minmax(120px,1fr))">` +
      gifs.map((im) => `
        <div class="card" data-id="${C.esc(im.id)}">
          <img class="thumb" src="${C.esc(im.thumbnail_url)}" loading="lazy">
          <span class="card-badge">GIF</span>
          <div class="card-meta">
            <div class="card-name" title="${C.esc(im.filename)}">${C.esc(im.filename)}</div>
            <div class="card-dim">${im.width}×${im.height} · ${C.fmtBytes(im.size_bytes)}</div>
          </div>
        </div>`).join("") + `</div>`;
    box.onclick = (e) => {
      const card = e.target.closest(".card");
      if (card) decompose(root, card.dataset.id);
    };
  }

  async function decompose(root, imageId) {
    setBusy(true, "正在拆解 GIF…");
    try {
      const r = await Api.post("/api/gif/decompose", { image_id: imageId });
      state.session = r.session;
      state.selectedFrame = 0;
      drawSession(root);
      C.toast(`已拆出 ${r.session.frame_count} 帧`, "success");
    } catch (e) {
      C.toast("拆解失败：" + e.message, "error");
    } finally {
      setBusy(false);
    }
  }

  function drawSession(root) {
    const s = state.session;
    if (!s) {
      root.querySelector("#gd-session").innerHTML = "";
      return;
    }
    const box = root.querySelector("#gd-session");
    box.innerHTML = `
      <div class="split" style="grid-template-columns: 1fr 320px;align-items:start">
        <div class="panel">
          <div class="panel-title">逐帧预览<span class="dim">顺序与时长保持原 GIF；点击一帧后在右侧编辑</span></div>
          <div class="toolbar">
            <span class="badge">${s.frame_count} 帧</span>
            <span class="badge">${s.width}×${s.height}</span>
            <span class="badge">一轮 ${s.total_duration_ms} ms</span>
            <span class="badge">${s.loop === 0 ? "无限循环" : "循环 " + s.loop + " 次"}</span>
            <span style="flex:1"></span>
            <button class="btn btn-sm" id="gd-all">对全部帧应用滤镜…</button>
          </div>
          <div class="gif-frames" id="gd-frames">
            ${s.frames.map((f) => `
              <div class="gif-frame ${f.index === state.selectedFrame ? "selected" : ""}" data-idx="${f.index}">
                <div class="gif-frame-idx">#${f.index + 1}${f.edited ? ' <span class="badge green" style="padding:0 5px">已编辑</span>' : ""}</div>
                <img src="${C.esc(f.file_url)}?v=${root.dataset.tick || "0"}" loading="lazy">
                <div class="gif-frame-ctl">
                  <div class="dim mono">${f.duration_ms} ms</div>
                </div>
              </div>`).join("")}
          </div>
        </div>
        <div>
          <div class="panel" id="gd-editor"></div>
          <div class="panel">
            <div class="panel-title">重新合成</div>
            <div class="field"><label>循环</label>
              <select id="gd-loop">
                <option value="" selected>沿用原设置（${s.loop === 0 ? "无限" : s.loop + " 次"}）</option>
                <option value="0">无限循环</option>
                <option value="1">播放 2 次</option>
                <option value="2">播放 3 次</option>
              </select>
            </div>
            <div class="field"><label>尺寸压缩（最长边）<span class="hint">留空=原始尺寸</span></label>
              <select id="gd-maxdim">
                <option value="" selected>不压缩</option>
                <option value="320">≤ 320px</option>
                <option value="480">≤ 480px</option>
                <option value="640">≤ 640px</option>
              </select>
            </div>
            <div class="field"><label>调色板色数</label>
              <select id="gd-colors">
                <option value="256" selected>256</option><option value="128">128</option>
                <option value="64">64</option><option value="32">32</option>
              </select>
            </div>
            <button class="btn btn-primary" id="gd-rebuild">按原帧序与时长合回 GIF</button>
          </div>
          <div class="panel" id="gd-result-panel" hidden>
            <div class="panel-title">合成结果</div>
            <div class="stage"><img id="gd-result-img"></div>
            <div class="keypoint-stats" style="line-height:1.9;margin-top:10px" id="gd-result-meta"></div>
            <div class="toolbar" style="margin-top:10px">
              <a class="btn btn-sm" id="gd-download" download="rebuilt.gif">下载 GIF</a>
            </div>
          </div>
        </div>
      </div>`;

    box.querySelectorAll("#gd-frames .gif-frame").forEach((card) => {
      card.onclick = () => {
        state.selectedFrame = Number(card.dataset.idx);
        box.querySelectorAll("#gd-frames .gif-frame").forEach((x) => x.classList.remove("selected"));
        card.classList.add("selected");
        drawEditor(root);
      };
    });
    box.querySelector("#gd-all").onclick = () => editAll(root);
    box.querySelector("#gd-rebuild").onclick = () => rebuild(root);
    drawEditor(root);
    const rp = root.dataset.result;
    if (rp) {
      const r = JSON.parse(rp);
      showRebuildResult(root, r);
    }
  }

  function drawEditor(root) {
    const s = state.session;
    const f = s.frames[state.selectedFrame];
    const box = root.querySelector("#gd-editor");
    box.innerHTML = `
      <div class="panel-title">第 ${state.selectedFrame + 1} / ${s.frame_count} 帧
        <span class="dim mono">${f.duration_ms} ms（时长不变）</span></div>
      <div class="stage" style="min-height:120px;margin-bottom:12px">
        <img src="${C.esc(f.file_url)}?v=${root.dataset.tick || "0"}" id="gd-frame-img" style="max-height:300px">
      </div>
      <div class="field">
        <label>快捷调整</label>
        <div class="toolbar" style="gap:6px" id="gd-quick">
          ${QUICK_NODES.map((n) => `<button class="btn btn-sm" data-node="${n.type}">${n.label}</button>`).join("")}
          <button class="btn btn-sm btn-ghost" data-node="__reset">↺ 恢复原帧</button>
        </div>
      </div>
      <div class="dim" style="font-size:11px">调整只作用于当前帧；时长与帧序保持不变。</div>`;
    box.querySelectorAll("[data-node]").forEach((btn) => {
      btn.onclick = () => applyQuick(root, btn.dataset.node);
    });
  }

  function nodeFor(type) {
    const spec = QUICK_NODES.find((n) => n.type === type);
    return spec ? [{ id: "q", type: spec.type, params: { ...spec.params }, inputs: [] }] : null;
  }

  async function applyQuick(root, type) {
    if (state.busy) return;
    const s = state.session;
    const i = state.selectedFrame;
    setBusy(true, "处理帧中…");
    try {
      if (type === "__reset") {
        const r = await Api.post(`/api/gif/sessions/${s.id}/frames/${i}/reset`, {});
        C.toast("已恢复原始帧", "success");
        r.frame && finishFrameEdit(root, r.frame);
      } else {
        const r = await Api.post(`/api/gif/sessions/${s.id}/frames/${i}/process`,
                                 { nodes: nodeFor(type) });
        if (r.error) { C.toast(r.error, "error"); return; }
        finishFrameEdit(root, r.frame);
      }
    } catch (e) {
      C.toast("处理失败：" + e.message, "error");
    } finally {
      setBusy(false);
    }
  }

  function finishFrameEdit(root, frameView) {
    const s = state.session;
    s.frames[frameView.index] = frameView;
    root.dataset.tick = String(Date.now());
    drawSession(root);
  }

  async function editAll(root) {
    const choices = QUICK_NODES.map((n, i) => `${i + 1}. ${n.label}`).join("\n");
    const answer = prompt(`对全部 ${state.session.frame_count} 帧应用哪个滤镜？\n${choices}\n（输入序号，取消则不执行）`);
    if (answer == null) return;
    const spec = QUICK_NODES[Number(answer) - 1];
    if (!spec) { C.toast("无效选择", "error"); return; }
    setBusy(true, `逐帧处理 ${state.session.frame_count} 帧…`);
    try {
      const r = await Api.post(`/api/gif/sessions/${state.session.id}/frames/apply-all`,
                               { nodes: [{ id: "all", type: spec.type, params: { ...spec.params }, inputs: [] }] });
      state.session = r.session;
      root.dataset.tick = String(Date.now());
      drawSession(root);
      C.toast(r.failed.length ? `完成，${r.failed.length} 帧失败` : "已应用到全部帧",
               r.failed.length ? "error" : "success");
    } catch (e) {
      C.toast("批量处理失败：" + e.message, "error");
    } finally {
      setBusy(false);
    }
  }

  async function rebuild(root) {
    if (state.busy) return;
    const s = state.session;
    const body = {};
    const loop = root.querySelector("#gd-loop").value;
    if (loop !== "") body.loop = Number(loop);
    const maxDim = root.querySelector("#gd-maxdim").value;
    if (maxDim) body.max_dim = Number(maxDim);
    body.colors = Number(root.querySelector("#gd-colors").value);
    setBusy(true, "正在重新合成…");
    try {
      const r = await Api.post(`/api/gif/sessions/${s.id}/rebuild`, body);
      const orig = s.frames.map((f) => f.original_duration_ms);
      const sameAsOriginal = JSON.stringify(r.effective_durations) === JSON.stringify(orig);
      r.same_as_original = sameAsOriginal && body.loop === undefined && !body.max_dim;
      showRebuildResult(root, r);
      root.dataset.result = JSON.stringify(r);
      C.invalidate("images");
      C.toast("重新合成完成", "success");
    } catch (e) {
      C.toast("合成失败：" + e.message, "error");
    } finally {
      setBusy(false);
    }
  }

  function showRebuildResult(root, r) {
    const panel = root.querySelector("#gd-result-panel");
    panel.hidden = false;
    root.querySelector("#gd-result-img").src = `${r.file_url}?t=${Date.now()}`;
    root.querySelector("#gd-download").href = `${r.file_url}?download=1`;
    root.querySelector("#gd-result-meta").innerHTML = `
      <div><span class="dim">帧数</span> ${r.frame_count} ·
          <span class="dim">尺寸</span> ${r.width}×${r.height} ·
          <span class="dim">大小</span> ${C.fmtBytes(r.size_bytes)}</div>
      <div><span class="dim">每帧时长</span> <span class="mono">[${r.effective_durations.join(", ")}]</span> ms</div>
      <div>${r.same_as_original
        ? '<span class="badge green">✔ 与原 GIF 帧序、时长、循环完全一致</span>'
        : r.rhythm_preserved
          ? '<span class="badge green">✔ 节奏与设置一致</span>'
          : '<span class="badge amber">⚠ 部分时长被厘秒精度取整</span>'}</div>`;
    panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  // ---------------------------------------------------------------- 外壳
  const view = {
    mount(root) {
      root.innerHTML = `
        <div class="toolbar" style="margin-bottom:14px">
          <div class="tabs">
            <button class="tab ${state.tab === "compose" ? "active" : ""}" data-tab="compose">🎞️ 多图合成 GIF</button>
            <button class="tab ${state.tab === "decompose" ? "active" : ""}" data-tab="decompose">✂️ GIF 拆帧编辑</button>
          </div>
        </div>
        <div id="gif-page"></div>
        <div class="gif-busy" id="gif-busy" hidden><div class="loading">⏳ <span class="gif-busy-text">处理中…</span></div></div>`;
      root.querySelectorAll(".tab").forEach((t) => {
        t.onclick = () => {
          state.tab = t.dataset.tab;
          root.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === t));
          view.refresh();
        };
      });
      view.refresh();
    },
    refresh() {
      const page = el().querySelector("#gif-page");
      if (state.tab === "compose") renderCompose(page);
      else renderDecompose(page);
    },
  };
  return view;
})();
