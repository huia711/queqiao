(function () {
  "use strict";

  const { api, url, appPath, escapeHtml, isDark, setTheme, readerUrl, withKey, renderMarkdown, STATUS_TEXT, PIPELINE_TEXT } = window.QQ;
  const VIEWER = url("/static/vendor/pdfjs/web/viewer.html");
  const PAGE_COLOR_KEY = "qq-page-color";
  const MODEL_KEY = "qq-model";
  const KINDS = ["translated", "original"];
  const AUTOSAVE_DELAY = 1500;
  const EDITOR = { NONE: 0, FREETEXT: 3, HIGHLIGHT: 9, INK: 15 };
  const KIND_LABEL = { Highlight: "高亮", FreeText: "批注", Ink: "画笔", Stamp: "图片", Text: "便签", Underline: "下划线", StrikeOut: "删除线", Squiggly: "波浪线" };

  const main = document.getElementById("readerMain");
  const frames = { translated: document.getElementById("frameTranslated"), original: document.getElementById("frameOriginal") };
  const states = { translated: document.getElementById("stateTranslated"), original: document.getElementById("stateOriginal") };
  const loaded = { translated: null, original: null };
  const apps = { translated: null, original: null };
  const expectedScroll = { translated: null, original: null };
  const dirty = { translated: false, original: false };
  const saveTimers = { translated: null, original: null };
  const saving = { translated: null, original: null };
  const savedHash = { translated: null, original: null };
  const watchers = { translated: null, original: null };
  let noteSummary = { translated: { count: 0, notes: [] }, original: { count: 0, notes: [] } };
  let notesTarget = "translated";
  let job = null;
  let pollTimer = null;
  let syncEnabled = true;
  let toolbarOpen = false;
  let currentTool = EDITOR.NONE;
  let lastSaveError = null;
  let authWarned = false;

  // ---------------------------------------------------------------------------
  // Resolve which job this page shows (/arxiv/<id>?job=<jobId> or /job/<jobId>)
  // ---------------------------------------------------------------------------

  async function resolveJob() {
    const params = new URLSearchParams(location.search);
    const path = appPath(location.pathname);
    const jobMatch = path.match(/^\/job\/([0-9a-f]{12})/);
    if (jobMatch) return api(`/api/jobs/${jobMatch[1]}`);
    if (params.get("job")) {
      try {
        return await api(`/api/jobs/${params.get("job")}`);
      } catch (error) {
        /* fall through and submit a fresh job */
      }
    }
    const arxivId = decodeURIComponent(path.replace(/^\/arxiv\//, "")).replace(/\/+$/, "");
    const found = await api(`/api/arxiv/lookup?id=${encodeURIComponent(arxivId)}`).catch(() => ({ job: null }));
    if (found.job) {
      history.replaceState({}, "", readerUrl(found.job));
      return found.job;
    }
    const created = await api("/api/arxiv", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ input: arxivId, model: localStorage.getItem(MODEL_KEY) || "" }),
    });
    history.replaceState({}, "", readerUrl(created));
    return created;
  }

  // ---------------------------------------------------------------------------
  // PDF.js viewer frames
  // ---------------------------------------------------------------------------

  function pageColorMode() {
    return localStorage.getItem(PAGE_COLOR_KEY) || "original";
  }

  function effectivePageColor() {
    const mode = pageColorMode();
    if (mode === "auto") return isDark() ? "night" : "original";
    return mode;
  }

  function viewerSrc(kind) {
    const stamp = Date.now();
    const file = `${job.artifact_urls[kind]}?v=${stamp}`;
    return `${VIEWER}?file=${encodeURIComponent(file)}#locale=zh-CN&pagemode=none&zoom=page-width`;
  }

  function loadFrame(kind, force = false) {
    if (!job.artifacts.includes(kind)) return false;
    const key = `${job.id}:${kind}:${job.finished_at || ""}`;
    if (loaded[kind] === key && !force) return true;
    loaded[kind] = key;
    apps[kind] = null;
    dirty[kind] = false;
    savedHash[kind] = null;
    clearInterval(watchers[kind]);
    const frame = frames[kind];
    frame.classList.remove("hidden");
    states[kind].classList.add("hidden");
    frame.onload = () => attachViewer(kind);
    // Navigate only after the iframe is displayed: Chrome can leave an iframe on about:blank when
    // its src changes while it is display:none. setTimeout also runs in background tabs, unlike rAF.
    const target = viewerSrc(kind);
    setTimeout(() => {
      if (loaded[kind] === key) frame.src = target;
    }, 0);
    return true;
  }

  function viewerStyle(doc) {
    let style = doc.getElementById("qq-style");
    if (!style) {
      style = doc.createElement("style");
      style.id = "qq-style";
      doc.head.appendChild(style);
    }
    return style;
  }

  function applyViewerLook(kind) {
    const doc = frames[kind].contentDocument;
    if (!doc || !doc.head) return;
    const dark = isDark();
    doc.documentElement.classList.toggle("is-dark", dark);
    doc.documentElement.classList.toggle("is-light", !dark);
    const color = effectivePageColor();
    const rules = [
      `:root{--body-bg-color:${dark ? "#161514" : "#f4f1eb"};--toolbar-bg-color:${dark ? "#1f1d1b" : "#fbf9f5"};--toolbar-border-color:${dark ? "#35302b" : "#e6e0d6"};--page-border:none}`,
      "#viewerContainer{scrollbar-width:thin}",
      ".pdfViewer .page{box-shadow:0 1px 3px rgb(60 40 20 / .08),0 8px 24px rgb(60 40 20 / .06);border-radius:2px;margin:12px auto 16px!important}",
    ];
    if (!toolbarOpen) {
      rules.push(".toolbar{display:none!important}", "#viewerContainer{inset:0!important}", "#sidebarContainer{top:0!important}");
    }
    if (color === "night") {
      rules.push(
        ".pdfViewer .page .canvasWrapper{filter:invert(.9) hue-rotate(180deg) brightness(1.05) contrast(.92)}",
        ".pdfViewer .page{background:#111!important;box-shadow:none!important}"
      );
    } else if (color === "sepia") {
      rules.push(".pdfViewer .page .canvasWrapper{filter:sepia(.28) saturate(.9) brightness(.98)}", ".pdfViewer .page{background:#f6efdf!important}");
    }
    viewerStyle(doc).textContent = rules.join("\n");
  }

  function attachViewer(kind) {
    const win = frames[kind].contentWindow;
    const doc = frames[kind].contentDocument;
    if (!win || !doc || !doc.head) return;
    applyViewerLook(kind);
    const wait = () => {
      const app = win.PDFViewerApplication;
      if (!app || !app.initializedPromise) {
        setTimeout(wait, 50);
        return;
      }
      app.initializedPromise.then(() => {
        apps[kind] = app;
        const options = win.PDFViewerApplicationOptions;
        if (options) options.set("enableHighlightFloatingButton", true);
        doc.getElementById("viewerContainer").addEventListener("scroll", () => onScroll(kind), { passive: true });
        app.eventBus.on("pagesloaded", () => {
          applyViewerLook(kind);
          watchAnnotations(kind, app);
          applyTool(kind);
          if (kind === "translated") restorePosition();
        });
        app.eventBus.on("pagechanging", rememberPosition);
        app.eventBus.on("annotationeditormodechanged", ({ mode }) => {
          if (mode !== currentTool && toolbarOpen) {
            currentTool = mode;
            renderTools();
          }
        });
        doc.addEventListener("keydown", forwardShortcut);
        doc.addEventListener("mouseup", () => setTimeout(() => onSelection(kind), 10));
        doc.addEventListener("keyup", (event) => {
          if (event.shiftKey) onSelection(kind);
        });
        doc.getElementById("viewerContainer").addEventListener("scroll", hideSelectPop, { passive: true });
      });
    };
    wait();
  }

  function forwardShortcut(event) {
    if ((event.metaKey || event.ctrlKey) && event.key === "s") {
      event.preventDefault();
      saveNow();
    }
  }

  // ---------------------------------------------------------------------------
  // Notes: every edit is written into the PDF by PDF.js and uploaded to the server
  // ---------------------------------------------------------------------------

  // The hash of AnnotationStorage.serializable changes whenever an annotation is added, edited,
  // moved, recoloured or deleted. PDF.js's own "modified" callback only fires on the first change,
  // so we compare hashes on a short interval and after editor events.
  function storageHash(app) {
    const storage = app && app.pdfDocument && app.pdfDocument.annotationStorage;
    if (!storage) return null;
    try {
      const serializable = storage.serializable;
      return serializable && serializable.hash ? serializable.hash : "empty";
    } catch (error) {
      return null;
    }
  }

  function checkForChanges(kind) {
    const app = apps[kind];
    const hash = storageHash(app);
    if (hash === null) return;
    if (savedHash[kind] === null) {
      savedHash[kind] = hash;
      return;
    }
    if (hash !== savedHash[kind] && !dirty[kind]) markDirty(kind);
  }

  function watchAnnotations(kind, app) {
    const storage = app.pdfDocument && app.pdfDocument.annotationStorage;
    if (!storage || storage.__qqWatched) return;
    storage.__qqWatched = true;
    savedHash[kind] = storageHash(app);
    const nudge = () => setTimeout(() => checkForChanges(kind), 60);
    app.eventBus.on("annotationeditorstateschanged", nudge);
    app.eventBus.on("switchannotationeditorparams", nudge);
    app.eventBus.on("annotationeditormodechanged", nudge);
    clearInterval(watchers[kind]);
    watchers[kind] = setInterval(() => checkForChanges(kind), 1000);
  }

  function markDirty(kind) {
    if (!canWrite()) return;
    dirty[kind] = true;
    setSaveState("dirty");
    clearTimeout(saveTimers[kind]);
    saveTimers[kind] = setTimeout(() => saveNotes(kind), AUTOSAVE_DELAY);
  }

  async function saveNotes(kind, { keepalive = false } = {}) {
    clearTimeout(saveTimers[kind]);
    const app = apps[kind];
    if (app) checkForChanges(kind);
    if (!app || !app.pdfDocument || !dirty[kind]) return;
    if (saving[kind]) {
      await saving[kind];
      if (!dirty[kind]) return;
    }
    dirty[kind] = false;
    setSaveState("saving");
    const task = (async () => {
      const hashAtSave = storageHash(app);
      try {
        const data = await app.pdfDocument.saveDocument();
        const response = await fetch(url(`/api/jobs/${job.id}/notes/${kind}`), withKey({
          method: "PUT",
          headers: { "Content-Type": "application/pdf" },
          body: data,
          keepalive: keepalive && data.byteLength < 60 * 1024,
        }));
        if (response.status === 401) {
          const detail = await response.clone().json().catch(() => ({}));
          if (detail.code === "invite") {
            const error = new Error("需要邀请码才能保存笔记");
            error.authExpired = true;
            throw error;
          }
          // Do not navigate away: that would discard the unsaved annotations in this tab.
          const error = new Error("登录已过期：请在新标签页重新登录，再回到这里按 ⌘S 保存");
          error.authExpired = true;
          throw error;
        }
        if (!response.ok) {
          const detail = await response.json().catch(() => ({}));
          throw new Error(detail.detail || `HTTP ${response.status}`);
        }
        const result = await response.json();
        savedHash[kind] = hashAtSave;
        authWarned = false;
        noteSummary[kind] = { exists: true, count: result.count, updated: result.updated, notes: result.notes };
        job.notes = { ...(job.notes || {}), [kind]: true };
        lastSaveError = null;
        renderNotes();
        if (!dirty.translated && !dirty.original) setSaveState("saved", result.updated);
      } catch (error) {
        dirty[kind] = true;
        lastSaveError = error.message;
        setSaveState("error");
        clearTimeout(saveTimers[kind]);
        if (error.authExpired) {
          if (!authWarned) {
            authWarned = true;
            if (/邀请码/.test(error.message)) window.QQ.openInvite(error.message);
            else {
              window.open(url("/login"), "_blank", "noopener");
              alert(error.message);
            }
          }
        } else {
          saveTimers[kind] = setTimeout(() => saveNotes(kind), 8000);
        }
      }
    })();
    saving[kind] = task;
    await task;
    saving[kind] = null;
  }

  function saveNow() {
    return Promise.all(KINDS.map((kind) => saveNotes(kind)));
  }

  function setSaveState(state, updated) {
    const element = document.getElementById("saveState");
    element.dataset.state = state;
    const label = element.querySelector(".label");
    if (state === "dirty") label.textContent = "有未保存的笔记…";
    else if (state === "saving") label.textContent = "正在保存…";
    else if (state === "error") label.textContent = "保存失败，稍后重试";
    else if (state === "readonly") label.textContent = "只读";
    else if (state === "saved") {
      const time = new Date((updated || Date.now() / 1000) * 1000);
      label.textContent = `已保存 ${String(time.getHours()).padStart(2, "0")}:${String(time.getMinutes()).padStart(2, "0")}`;
    } else label.textContent = "笔记保存在本机";
    element.title = state === "error" && lastSaveError ? `保存失败：${lastSaveError}` : "笔记自动保存到本机 data/jobs/<任务>/notes/";
  }

  async function loadNotes() {
    try {
      const summary = await api(`/api/jobs/${job.id}/notes`);
      noteSummary = summary;
      renderNotes();
      const updated = Math.max(summary.translated.updated || 0, summary.original.updated || 0);
      if (updated) setSaveState("saved", updated);
    } catch (error) {
      /* notes are optional */
    }
  }

  function renderNotes() {
    const total = (noteSummary.translated.count || 0) + (noteSummary.original.count || 0);
    const badge = document.getElementById("notesCount");
    badge.textContent = String(total);
    badge.classList.toggle("has", total > 0);
    for (const button of document.querySelectorAll("#notesTarget button")) {
      const count = noteSummary[button.dataset.target].count || 0;
      button.classList.toggle("on", button.dataset.target === notesTarget);
      button.textContent = `${button.dataset.target === "translated" ? "译文" : "原文"} ${count}`;
    }
    for (const hint of document.querySelectorAll("[data-note-hint]")) {
      const count = noteSummary[hint.dataset.noteHint].count || 0;
      hint.textContent = count ? `含 ${count} 条笔记` : "";
    }
    const notes = noteSummary[notesTarget].notes || [];
    const body = document.getElementById("notesBody");
    if (!notes.length) {
      body.innerHTML = `<div class="notes-empty">${notesTarget === "translated" ? "译文" : "原文"}上还没有笔记。<br>选中文字点「高亮」，或用顶部的「批注」「画笔」在页面上记录。</div>
        <p class="side-note">笔记会在修改后约 1.5 秒自动写入 PDF 并保存到本机，关闭页面、换浏览器都不会丢失。</p>`;
      return;
    }
    body.innerHTML = `<div class="note-list">${notes
      .map(
        (note, index) => `<button class="note-item" data-note="${index}" style="--note-color:${escapeHtml(note.color || "var(--accent)")}">
          <div class="note-head"><b>P${note.page}</b><span>${KIND_LABEL[note.kind] || note.kind}</span></div>
          ${note.quote ? `<div class="quote">${escapeHtml(note.quote)}</div>` : ""}
          ${note.text ? `<div class="text">${escapeHtml(note.text)}</div>` : ""}
          ${!note.quote && !note.text ? `<div class="quote">（${KIND_LABEL[note.kind] || "标注"}，无文字）</div>` : ""}
        </button>`
      )
      .join("")}</div>
      <p class="side-note">保存在 <code>data/jobs/${escapeHtml(job.id)}/notes/</code>，可在「更多 → 下载」中导出带笔记的 PDF，用 Preview、Acrobat、Zotero 打开也能看到。</p>`;
  }

  document.getElementById("notesBody").addEventListener("click", (event) => {
    const item = event.target.closest("[data-note]");
    if (!item) return;
    const note = (noteSummary[notesTarget].notes || [])[Number(item.dataset.note)];
    if (!note) return;
    if (main.dataset.view !== "split" && main.dataset.view !== notesTarget) setView(notesTarget);
    scrollToPosition(notesTarget, { page: note.page, fraction: Math.max(0, note.top - 0.08) }, true);
  });

  document.getElementById("notesTarget").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-target]");
    if (!button) return;
    notesTarget = button.dataset.target;
    renderNotes();
  });

  // ---------------------------------------------------------------------------
  // Annotation tools (applied to both panes)
  // ---------------------------------------------------------------------------

  function applyTool(kind) {
    const app = apps[kind];
    if (!app || !app.eventBus || !app.pdfDocument) return;
    app.eventBus.dispatch("switchannotationeditormode", { source: window, mode: currentTool });
  }

  function renderTools() {
    for (const button of document.querySelectorAll(".tool")) {
      button.classList.toggle("active", Number(button.dataset.tool) === currentTool);
    }
  }

  document.querySelectorAll(".tool").forEach((button) =>
    button.addEventListener("click", () => {
      const tool = Number(button.dataset.tool);
      currentTool = tool === currentTool ? EDITOR.NONE : tool;
      renderTools();
      KINDS.forEach(applyTool);
      const hints = { 9: "拖选文字即可高亮", 3: "点击页面任意位置输入批注", 15: "按住鼠标书写，完成后点「阅读」退出" };
      if (hints[currentTool]) toast(hints[currentTool]);
    })
  );

  // ---------------------------------------------------------------------------
  // Synchronized scrolling (page number + offset inside the page)
  // ---------------------------------------------------------------------------

  function pagePosition(kind) {
    const app = apps[kind];
    if (!app || !app.pdfViewer || !app.pdfViewer.pagesCount) return null;
    const viewer = app.pdfViewer;
    const scrollTop = viewer.container.scrollTop;
    let low = 0;
    let high = viewer.pagesCount - 1;
    while (low < high) {
      const middle = (low + high + 1) >> 1;
      const div = viewer.getPageView(middle) && viewer.getPageView(middle).div;
      if (div && div.offsetTop <= scrollTop) low = middle;
      else high = middle - 1;
    }
    const div = viewer.getPageView(low) && viewer.getPageView(low).div;
    if (!div) return null;
    const fraction = (scrollTop - div.offsetTop) / (div.offsetHeight || 1);
    return { page: low + 1, fraction: Math.min(1, Math.max(0, fraction)) };
  }

  function scrollToPosition(kind, position, propagate = false) {
    const app = apps[kind];
    if (!app || !app.pdfViewer || !app.pdfViewer.pagesCount || !position) return;
    const page = Math.min(position.page, app.pdfViewer.pagesCount);
    const view = app.pdfViewer.getPageView(page - 1);
    if (!view || !view.div) return;
    const container = app.pdfViewer.container;
    const target = Math.round(view.div.offsetTop + position.fraction * view.div.offsetHeight);
    if (Math.abs(container.scrollTop - target) < 2) return;
    container.scrollTop = target;
    if (!propagate) expectedScroll[kind] = container.scrollTop;
  }

  function onScroll(kind) {
    const app = apps[kind];
    if (!app || !app.pdfViewer) return;
    const expected = expectedScroll[kind];
    expectedScroll[kind] = null;
    if (expected !== null && Math.abs(app.pdfViewer.container.scrollTop - expected) < 2) return;
    if (!syncEnabled || main.dataset.view !== "split") return;
    scrollToPosition(kind === "translated" ? "original" : "translated", pagePosition(kind));
  }

  function rememberPosition() {
    if (!job) return;
    const position = pagePosition("translated") || pagePosition("original");
    if (position) sessionStorage.setItem(`qq-pos-${job.id}`, JSON.stringify(position));
  }

  function restorePosition() {
    if (!job) return;
    try {
      const position = JSON.parse(sessionStorage.getItem(`qq-pos-${job.id}`) || "null");
      if (position) setTimeout(() => scrollToPosition("translated", position, true), 150);
    } catch (error) {
      /* ignore */
    }
  }

  // ---------------------------------------------------------------------------
  // Job state rendering
  // ---------------------------------------------------------------------------

  function stateCard(kind) {
    const stage = escapeHtml(job.stage || "");
    const percent = Math.max(2, Math.min(100, job.progress || 0));
    const warnings = (job.warnings || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
    if (job.status === "failed") {
      return `<div class="state-card">
        <p class="eyebrow">FAILED</p>
        <h3>翻译没有完成</h3>
        <p class="error">${escapeHtml(job.error || "未知错误")}</p>
        ${warnings ? `<ul>${warnings}</ul>` : ""}
        <div class="row"><button class="btn primary" data-action="retry">重新翻译</button>
        ${job.artifacts.includes("log") ? `<a class="btn" href="${job.artifact_urls.log}" target="_blank">查看构建日志</a>` : ""}</div>
      </div>`;
    }
    if (kind === "original") {
      return `<div class="state-card"><p class="eyebrow">ORIGINAL</p><h3><span class="spinner"></span>正在获取原文</h3><p>${stage}</p></div>`;
    }
    const pipeline = job.pipeline ? ` · ${PIPELINE_TEXT[job.pipeline] || job.pipeline}` : "";
    return `<div class="state-card">
      <p class="eyebrow">TRANSLATING${escapeHtml(pipeline)}</p>
      <h3><span class="spinner"></span>${STATUS_TEXT[job.status] || "处理中"}</h3>
      <p>使用模型 <code>${escapeHtml(job.model)}</code>，译文生成后会自动显示，可以先在右侧阅读原文。</p>
      <div class="progress"><span style="width:${percent.toFixed(1)}%"></span></div>
      <div class="stage"><span>${stage}</span><b>${percent.toFixed(0)}%</b></div>
      ${warnings ? `<ul>${warnings}</ul>` : ""}
    </div>`;
  }

  function renderPanes() {
    for (const kind of KINDS) {
      const ready = loadFrame(kind);
      states[kind].classList.toggle("hidden", ready);
      frames[kind].classList.toggle("hidden", !ready);
      if (!ready) states[kind].innerHTML = stateCard(kind);
    }
    if (job.status === "failed" && !job.artifacts.includes("translated")) {
      states.translated.classList.remove("hidden");
      states.translated.innerHTML = stateCard("translated");
    }
    const toolsReady = job.artifacts.includes("translated") || job.artifacts.includes("original");
    for (const button of document.querySelectorAll(".tool")) button.disabled = !toolsReady;
  }

  function renderTitle() {
    const title = job.meta && job.meta.title ? job.meta.title : job.title || job.source_name || "文档";
    document.title = `${title} · 鹊桥`;
  }

  function formatDuration(item) {
    if (!item.finished_at) return "";
    const seconds = Math.round(item.finished_at - item.created_at);
    return seconds >= 60 ? `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒` : `${seconds} 秒`;
  }

  function renderInfo() {
    const meta = job.meta || {};
    const usage = job.usage || {};
    const rows = [
      ["状态", STATUS_TEXT[job.status] || job.status],
      ["翻译方式", job.pipeline ? PIPELINE_TEXT[job.pipeline] || job.pipeline : "—"],
      ["模型", job.model],
      ["耗时", formatDuration(job) || "—"],
    ];
    if (usage.segments_total) rows.push(["段落", `${usage.segments_translated}/${usage.segments_total}（缓存 ${usage.segments_cached || 0}）`]);
    if (usage.prompt_tokens || usage.completion_tokens) rows.push(["Token", `输入 ${usage.prompt_tokens || 0} · 输出 ${usage.completion_tokens || 0}`]);
    if (meta.engine) rows.push(["排版引擎", meta.engine]);
    if (meta.main_tex) rows.push(["主文件", meta.main_tex]);
    rows.push(["任务目录", `data/jobs/${job.id}/`]);
    const warnings = (job.warnings || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
    const authors = (meta.authors || []).join("、");
    const source = job.arxiv_id
      ? `<span class="mono">arXiv:${escapeHtml(job.arxiv_id)}</span>${meta.category ? ` · ${escapeHtml(meta.category)}` : ""}`
      : escapeHtml(job.source_name || "本地文档");
    const dates = meta.updated ? ` · 更新 ${escapeHtml(meta.updated)}${meta.published && meta.published !== meta.updated ? ` · 首发 ${escapeHtml(meta.published)}` : ""}` : "";
    document.getElementById("infoBody").innerHTML = `
      <h2>${escapeHtml(meta.title || job.title || "文档")}</h2>
      ${authors ? `<div class="authors">${escapeHtml(authors)}</div>` : ""}
      <div class="meta">${source}${dates}</div>
      ${meta.abstract ? `<h4>ABSTRACT</h4><div class="abstract">${escapeHtml(meta.abstract)}</div>` : ""}
      <h4>TRANSLATION</h4>
      <div class="kv">${rows.map(([key, value]) => `<span>${escapeHtml(key)}</span><span>${escapeHtml(value)}</span>`).join("")}</div>
      ${warnings ? `<h4>NOTES</h4><ul class="warn">${warnings}</ul>` : ""}
      <div class="links">
        ${meta.abs_url ? `<a class="btn small" href="${escapeHtml(meta.abs_url)}" target="_blank" rel="noopener">arXiv 原文 ↗</a>` : ""}
        ${job.artifacts.includes("log") ? `<a class="btn small" href="${job.artifact_urls.log}" target="_blank">构建日志</a>` : ""}
        ${meta.zotero && meta.zotero.item ? `<a class="btn small" href="zotero://select/library/items/${escapeHtml(meta.zotero.item)}">在 Zotero 中打开</a>` : ""}
      </div>`;
  }

  function renderMenu() {
    for (const item of document.querySelectorAll("[data-download]")) {
      item.classList.toggle("disabled", !job.artifacts.includes(item.dataset.download));
    }
    for (const item of document.querySelectorAll("[data-clean]")) {
      item.classList.toggle("disabled", !(job.artifacts.includes(item.dataset.clean) && noteSummary[item.dataset.clean].count));
    }
    for (const button of document.querySelectorAll("[data-pagecolor]")) {
      button.classList.toggle("active", button.dataset.pagecolor === pageColorMode());
    }
    document.getElementById("themeItem").textContent = isDark() ? "切换到浅色主题" : "切换到深色主题";
    document.getElementById("clearNotesItem").classList.toggle("disabled", !(noteSummary.translated.count || noteSummary.original.count));
  }

  function render() {
    renderTitle();
    renderPanes();
    renderInfo();
    renderMenu();
  }

  async function poll() {
    clearTimeout(pollTimer);
    const wasActive = job && (job.status === "queued" || job.status === "running");
    try {
      job = await api(`/api/jobs/${job.id}`);
      render();
    } catch (error) {
      toast(error.message);
    }
    if (wasActive && job.status === "done" && !panels.assistPanel.classList.contains("hidden")) setTimeout(loadAssist, 1500);
    if (job.status === "queued" || job.status === "running") pollTimer = setTimeout(poll, 1500);
  }

  // ---------------------------------------------------------------------------
  // Toolbar actions
  // ---------------------------------------------------------------------------

  function setView(view) {
    main.dataset.view = view;
    for (const button of document.querySelectorAll(".segmented button")) button.classList.toggle("active", button.dataset.view === view);
    localStorage.setItem("qq-view", view);
  }

  function toast(message) {
    const element = document.getElementById("toast");
    element.textContent = message;
    element.classList.remove("hidden");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => element.classList.add("hidden"), 2600);
  }

  async function download(name, clean = false) {
    if (!job.artifacts.includes(name)) return;
    if (KINDS.includes(name)) await saveNotes(name);
    const url = job.artifact_urls[name];
    if (name === "log") {
      window.open(url, "_blank");
      return;
    }
    const link = document.createElement("a");
    link.href = `${url}?download=1${clean ? "&clean=1" : ""}`;
    link.download = "";
    document.body.appendChild(link);
    link.click();
    link.remove();
  }

  document.querySelectorAll(".segmented button").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));

  document.getElementById("swapBtn").addEventListener("click", () => {
    main.classList.toggle("swapped");
    localStorage.setItem("qq-swapped", main.classList.contains("swapped") ? "1" : "0");
  });

  document.getElementById("syncBtn").addEventListener("click", (event) => {
    syncEnabled = !syncEnabled;
    event.currentTarget.classList.toggle("active", syncEnabled);
    event.currentTarget.setAttribute("aria-pressed", String(syncEnabled));
    toast(syncEnabled ? "已开启同步滚动" : "已关闭同步滚动");
  });

  const panels = {
    notesPanel: document.getElementById("notesPanel"),
    infoPanel: document.getElementById("infoPanel"),
    assistPanel: document.getElementById("assistPanel"),
  };
  const panelButtons = {
    notesPanel: document.getElementById("notesBtn"),
    infoPanel: document.getElementById("infoBtn"),
    assistPanel: document.getElementById("assistBtn"),
  };

  function togglePanel(name, force) {
    for (const [key, panel] of Object.entries(panels)) {
      const open = key === name ? (force ?? panel.classList.contains("hidden")) : false;
      panel.classList.toggle("hidden", !open);
      panelButtons[key].classList.toggle("active", open);
    }
  }

  panelButtons.notesPanel.addEventListener("click", () => togglePanel("notesPanel"));
  panelButtons.infoPanel.addEventListener("click", () => togglePanel("infoPanel"));
  panelButtons.assistPanel.addEventListener("click", () => {
    togglePanel("assistPanel");
    if (!panels.assistPanel.classList.contains("hidden")) loadAssist();
  });
  document.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => togglePanel(button.dataset.close, false)));

  document.getElementById("toolbarBtn").addEventListener("click", (event) => {
    toolbarOpen = !toolbarOpen;
    event.currentTarget.classList.toggle("active", toolbarOpen);
    event.currentTarget.title = toolbarOpen ? "收起 PDF 工具栏" : "展开 PDF 工具栏（搜索、缩放、更多批注选项）";
    KINDS.forEach(applyViewerLook);
  });

  const moreMenu = document.getElementById("moreMenu");
  document.getElementById("moreBtn").addEventListener("click", (event) => {
    event.stopPropagation();
    renderMenu();
    moreMenu.classList.toggle("hidden");
  });
  document.addEventListener("click", (event) => {
    if (!moreMenu.contains(event.target)) moreMenu.classList.add("hidden");
  });
  moreMenu.addEventListener("click", async (event) => {
    const downloadItem = event.target.closest("[data-download]");
    const cleanItem = event.target.closest("[data-clean]");
    const colorItem = event.target.closest("[data-pagecolor]");
    if (colorItem) {
      localStorage.setItem(PAGE_COLOR_KEY, colorItem.dataset.pagecolor);
      KINDS.forEach(applyViewerLook);
      renderMenu();
      return;
    }
    if (downloadItem || cleanItem) moreMenu.classList.add("hidden");
    if (downloadItem) await download(downloadItem.dataset.download);
    if (cleanItem) await download(cleanItem.dataset.clean, true);
  });

  document.getElementById("themeItem").addEventListener("click", () => {
    setTheme(!isDark());
    renderMenu();
  });
  window.addEventListener("qq-theme", () => KINDS.forEach(applyViewerLook));

  document.getElementById("clearNotesItem").addEventListener("click", async () => {
    moreMenu.classList.add("hidden");
    const target = main.dataset.view === "original" ? "original" : notesTarget;
    const label = target === "translated" ? "译文" : "原文";
    if (!confirm(`清空${label}上的全部笔记？当前版本会移入 notes/history/ 作为备份。`)) return;
    try {
      clearTimeout(saveTimers[target]);
      dirty[target] = false;
      await api(`/api/jobs/${job.id}/notes/${target}`, { method: "DELETE" });
      noteSummary[target] = { exists: false, count: 0, notes: [] };
      renderNotes();
      loadFrame(target, true);
      toast(`已清空${label}笔记`);
    } catch (error) {
      toast(error.message);
    }
  });

  const dialog = document.getElementById("modelDialog");
  document.getElementById("retranslateItem").addEventListener("click", async () => {
    moreMenu.classList.add("hidden");
    if (job.kind !== "arxiv") {
      toast("本地文档请在首页重新上传并选择模型");
      return;
    }
    const select = document.getElementById("dialogModel");
    try {
      const data = await api("/api/models");
      select.innerHTML = data.models
        .map((model) => `<option value="${escapeHtml(model.id)}"${model.id === job.model ? " selected" : ""}>${escapeHtml(model.name)}${model.recommended ? "（推荐）" : ""}</option>`)
        .join("");
    } catch (error) {
      toast(error.message);
      return;
    }
    dialog.showModal();
  });
  dialog.addEventListener("close", async () => {
    if (dialog.returnValue !== "ok") return;
    const model = document.getElementById("dialogModel").value;
    try {
      await saveNow();
      const created = await api("/api/arxiv", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ input: job.arxiv_id, model, force: model === job.model }),
      });
      localStorage.setItem(MODEL_KEY, model);
      location.href = readerUrl(created);
    } catch (error) {
      toast(error.message);
    }
  });

  document.addEventListener("click", async (event) => {
    const retry = event.target.closest("[data-action=retry]");
    if (!retry) return;
    try {
      job = await api(`/api/jobs/${job.id}/retry`, { method: "POST" });
      loaded.translated = null;
      render();
      poll();
    } catch (error) {
      toast(error.message);
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.target.closest("input, textarea, select")) return;
    if ((event.metaKey || event.ctrlKey) && event.key === "s") {
      event.preventDefault();
      saveNow();
      return;
    }
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    if (event.key === "1") setView("original");
    if (event.key === "2") setView("translated");
    if (event.key === "3") setView("split");
    if (event.key === "Escape") {
      moreMenu.classList.add("hidden");
      togglePanel("none");
    }
  });

  // Save pending notes when the page is hidden or closed.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") KINDS.forEach((kind) => saveNotes(kind, { keepalive: true }));
  });
  window.addEventListener("pagehide", () => {
    rememberPosition();
    KINDS.forEach((kind) => saveNotes(kind, { keepalive: true }));
  });
  window.addEventListener("beforeunload", (event) => {
    if (dirty.translated || dirty.original || saving.translated || saving.original) {
      saveNow();
      event.preventDefault();
      event.returnValue = "";
    }
  });

  // ---------------------------------------------------------------------------
  // Boot
  // ---------------------------------------------------------------------------

  function canWrite() {
    const access = window.QQ.access();
    return !access || access.can_write;
  }

  // ---------------------------------------------------------------------------
  // AI reading assistant: guide card, Q&A about selections, reference overview
  // ---------------------------------------------------------------------------

  const selectPop = document.getElementById("selectPop");
  const qaInput = document.getElementById("qaInput");
  let assistTab = "guide";
  let assistData = null;
  let assistTimer = null;
  let pendingSelection = null;
  let qaBusy = false;

  function selectionPage(kind, selection) {
    const node = selection.anchorNode && (selection.anchorNode.nodeType === 1 ? selection.anchorNode : selection.anchorNode.parentElement);
    const page = node && node.closest && node.closest(".page[data-page-number]");
    return page ? Number(page.dataset.pageNumber) : null;
  }

  function onSelection(kind) {
    const frame = frames[kind];
    const win = frame.contentWindow;
    if (!win || currentTool !== EDITOR.NONE) return;
    const selection = win.getSelection();
    const text = selection ? selection.toString().replace(/\s+/g, " ").trim() : "";
    if (!text || text.length < 2 || !selection.rangeCount) {
      hideSelectPop();
      return;
    }
    const rect = selection.getRangeAt(0).getBoundingClientRect();
    const frameRect = frame.getBoundingClientRect();
    pendingSelection = { text: text.slice(0, 3000), page: selectionPage(kind, selection), side: kind };
    selectPop.classList.remove("hidden");
    const width = selectPop.offsetWidth || 260;
    const left = Math.min(Math.max(8, frameRect.left + rect.left + rect.width / 2 - width / 2), window.innerWidth - width - 8);
    const top = frameRect.top + rect.top - 44 < 60 ? frameRect.top + rect.bottom + 8 : frameRect.top + rect.top - 44;
    selectPop.style.left = `${left}px`;
    selectPop.style.top = `${top}px`;
  }

  function hideSelectPop() {
    selectPop.classList.add("hidden");
  }

  selectPop.addEventListener("mousedown", (event) => event.preventDefault());
  selectPop.addEventListener("click", (event) => {
    const button = event.target.closest("[data-ask]");
    if (!button || !pendingSelection) return;
    hideSelectPop();
    openAssist("qa");
    setContext(pendingSelection);
    if (button.dataset.ask === "ask") {
      qaInput.focus();
      return;
    }
    sendQuestion(button.dataset.ask, "");
  });
  document.addEventListener("mousedown", (event) => {
    if (!selectPop.contains(event.target)) hideSelectPop();
  });

  let questionContext = null;
  function setContext(context) {
    questionContext = context;
    const box = document.getElementById("qaContext");
    box.classList.toggle("hidden", !context);
    if (context) {
      const where = context.page ? `${context.side === "translated" ? "译文" : "原文"} p.${context.page}` : "";
      box.querySelector("span").innerHTML = `<b>${escapeHtml(where || "选中")}</b>${escapeHtml(context.text.slice(0, 160))}${context.text.length > 160 ? "…" : ""}`;
    }
  }
  document.getElementById("qaContextClear").addEventListener("click", () => setContext(null));

  function openAssist(tab) {
    togglePanel("assistPanel", true);
    setAssistTab(tab);
    loadAssist();
  }

  function setAssistTab(tab) {
    assistTab = tab;
    for (const button of document.querySelectorAll("#assistTabs button")) button.classList.toggle("on", button.dataset.tab === tab);
    document.getElementById("guideBody").classList.toggle("hidden", tab !== "guide");
    document.getElementById("qaBody").classList.toggle("hidden", tab !== "qa");
    document.getElementById("qaForm").classList.toggle("hidden", tab !== "qa");
    document.getElementById("refsBody").classList.toggle("hidden", tab !== "refs");
    renderAssist();
  }

  document.getElementById("assistTabs").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-tab]");
    if (button) setAssistTab(button.dataset.tab);
  });

  async function loadAssist() {
    if (!job) return;
    clearTimeout(assistTimer);
    try {
      assistData = await api(`/api/jobs/${job.id}/assist`);
    } catch (error) {
      assistData = null;
    }
    renderAssist();
    const running = assistData && (assistData.guide.status === "running" || assistData.references.status === "running");
    if (running && !panels.assistPanel.classList.contains("hidden")) assistTimer = setTimeout(loadAssist, 3000);
  }

  function list(items) {
    const values = (items || []).filter(Boolean);
    return values.length ? `<ul>${values.map((item) => `<li>${renderInlineMd(item)}</li>`).join("")}</ul>` : "";
  }

  function renderInlineMd(text) {
    return renderMarkdown(String(text)).replace(/^<p>|<\/p>$/g, "");
  }

  function stateBlock(state, kind) {
    const label = kind === "guide" ? "导读" : "参考文献速览";
    const ready = job.artifacts.includes("original");
    if (!state || state.status === "none") {
      const hint = kind === "guide"
        ? "大模型通读全文，生成一句话总结、问题、贡献、方法、实验设置、关键结果、局限和值得看的图表。"
        : "列出每篇参考文献在本文中的作用，并为能在 arXiv 上找到的文献写 TL;DR 和核心动机，大约需要 1–2 分钟。";
      return `<div class="assist-empty"><p>${hint}</p>${ready ? `<button class="btn primary small writer-only" data-build="${kind}">生成${label}</button>` : "<p>原文下载完成后可用。</p>"}</div>`;
    }
    if (state.status === "running") return `<div class="assist-empty"><p><span class="spinner"></span>正在生成${label}：${escapeHtml(state.stage || "")}</p></div>`;
    if (state.status === "failed") return `<div class="assist-empty"><p class="error">${escapeHtml(state.error || "生成失败")}</p><button class="btn small writer-only" data-build="${kind}" data-force="1">重新生成</button></div>`;
    return "";
  }

  function renderGuide() {
    const body = document.getElementById("guideBody");
    const state = assistData && assistData.guide;
    const pending = stateBlock(state, "guide");
    if (pending) {
      body.innerHTML = pending;
      return;
    }
    const data = state.data || {};
    const setup = data.setup || {};
    const setupNames = { data: "数据 / 环境", baselines: "基线", metrics: "指标", compute: "规模 / 算力" };
    const setupRows = Object.entries(setupNames).filter(([key]) => setup[key]).map(([key, name]) => `<span>${name}</span><span>${escapeHtml(setup[key])}</span>`).join("");
    const figures = (data.figures || [])
      .map((item) => `<button class="figure-item" data-goto="${item.page}"><b>${escapeHtml(item.label)}</b><span class="mono">p.${item.page}</span><em>${escapeHtml(item.why || item.caption)}</em></button>`)
      .join("");
    body.innerHTML = `
      ${data.tldr ? `<p class="guide-tldr">${escapeHtml(data.tldr)}</p>` : ""}
      ${data.problem ? `<h4>PROBLEM</h4><p>${renderInlineMd(data.problem)}</p>` : ""}
      ${(data.contributions || []).length ? `<h4>CONTRIBUTIONS</h4>${list(data.contributions)}` : ""}
      ${data.method ? `<h4>METHOD</h4><p>${renderInlineMd(data.method)}</p>` : ""}
      ${setupRows ? `<h4>SETUP</h4><div class="kv">${setupRows}</div>` : ""}
      ${(data.results || []).length ? `<h4>KEY RESULTS</h4>${list(data.results)}` : ""}
      ${figures ? `<h4>FIGURES TO READ</h4><div class="figure-list">${figures}</div>` : ""}
      ${(data.limitations || []).length ? `<h4>LIMITATIONS</h4>${list(data.limitations)}` : ""}
      ${(data.questions || []).length ? `<h4>QUESTIONS</h4>${list(data.questions)}` : ""}
      <p class="side-note">由 <code>${escapeHtml(state.model || "")}</code> 根据原文生成，可能有误；数字请对照原文核实。
      <a href="#" class="writer-only" data-build="guide" data-force="1">重新生成</a></p>`;
  }

  function renderQa() {
    const listBox = document.getElementById("qaList");
    const items = (assistData && assistData.qa) || [];
    const modeName = { explain: "解释", plain: "说人话", critique: "审稿视角", ask: "提问" };
    const thinking = qaBusy ? `<div class="qa-item pending"><div class="qa-a"><span class="spinner"></span>正在阅读全文并回答…</div></div>` : "";
    if (!items.length && !qaBusy) {
      listBox.innerHTML = `<div class="assist-empty"><p>在译文或原文里<b>选中一段文字</b>，点浮出的「解释 / 说人话 / 审稿视角 / 提问」；也可以直接在下面提问。回答基于全文并标注页码，点页码可以跳转。</p></div>`;
      return;
    }
    listBox.innerHTML = items
      .map((item) => {
        const where = item.page ? `${item.side === "translated" ? "译文" : "原文"} p.${item.page}` : "";
        const question = item.mode === "ask" ? item.question : modeName[item.mode];
        return `<div class="qa-item" data-qa="${escapeHtml(item.id)}">
          ${item.selection ? `<blockquote class="qa-quote" ${item.page ? `data-goto="${item.page}" data-side="${escapeHtml(item.side)}"` : ""}>${where ? `<b>${escapeHtml(where)}</b>` : ""}${escapeHtml(item.selection.slice(0, 280))}${item.selection.length > 280 ? "…" : ""}</blockquote>` : ""}
          <div class="qa-q"><span>${escapeHtml(question)}</span><button class="icon-btn writer-only" data-qa-delete="${escapeHtml(item.id)}" title="删除这条问答" aria-label="删除">×</button></div>
          <div class="qa-a">${renderMarkdown(item.answer)}</div>
        </div>`;
      })
      .join("") + thinking;
    listBox.parentElement.scrollTop = listBox.parentElement.scrollHeight;
  }

  const WEIGHT_TEXT = { core: "核心", related: "相关", minor: "顺带" };
  let refsFilter = "all";

  function renderRefs() {
    const body = document.getElementById("refsBody");
    const state = assistData && assistData.references;
    const pending = stateBlock(state, "references");
    if (pending) {
      body.innerHTML = pending;
      return;
    }
    const data = state.data || { items: [] };
    const counts = { core: 0, related: 0, minor: 0 };
    for (const item of data.items) counts[item.weight] = (counts[item.weight] || 0) + 1;
    const items = data.items.filter((item) => refsFilter === "all" || item.weight === refsFilter);
    const filters = ["all", "core", "related", "minor"]
      .map((key) => `<button data-ref-filter="${key}" class="${key === refsFilter ? "on" : ""}">${key === "all" ? `全部 ${data.items.length}` : `${WEIGHT_TEXT[key]} ${counts[key] || 0}`}</button>`)
      .join("");
    body.innerHTML = `
      <p class="refs-summary">共 ${data.total} 篇，其中 ${data.on_arxiv} 篇在 arXiv 上找到摘要并生成了 TL;DR${data.truncated ? "（只处理了前 80 篇）" : ""}。</p>
      <div class="seg-mini refs-filter">${filters}</div>
      <div class="ref-list">${items
        .map(
          (item) => `<div class="ref-item weight-${item.weight}">
          <div class="ref-head"><b>[${item.n}]</b><span class="chip ${item.weight === "core" ? "red" : ""}">${WEIGHT_TEXT[item.weight]}</span><span class="ref-year">${escapeHtml(item.year || "")}</span></div>
          <div class="ref-title">${item.arxiv_id ? `<a href="https://arxiv.org/abs/${escapeHtml(item.arxiv_id)}" target="_blank" rel="noopener">${escapeHtml(item.title)}</a>` : escapeHtml(item.title)}</div>
          <div class="ref-authors">${escapeHtml(item.authors || "")}${item.venue ? ` · ${escapeHtml(item.venue)}` : ""}</div>
          ${item.role ? `<p class="ref-role"><b>在本文中</b>${escapeHtml(item.role)}</p>` : ""}
          ${item.tldr ? `<p class="ref-tldr"><b>TL;DR</b>${escapeHtml(item.tldr)}</p>` : ""}
          ${item.motivation ? `<p class="ref-tldr"><b>动机</b>${escapeHtml(item.motivation)}</p>` : ""}
          ${item.arxiv_id ? `<div class="ref-actions"><a class="btn small" href="${url(`/arxiv/${item.arxiv_id}`)}" target="_blank">在鹊桥中打开</a></div>` : ""}
        </div>`
        )
        .join("")}</div>
      <p class="side-note">作用判断来自正文，TL;DR 只依据被引论文的 arXiv 摘要；不在 arXiv 上的文献只列出作用。</p>`;
  }

  function renderAssist() {
    if (!job) return;
    if (assistTab === "guide") renderGuide();
    if (assistTab === "qa") renderQa();
    if (assistTab === "refs") renderRefs();
  }

  async function sendQuestion(mode, question) {
    if (qaBusy) return;
    const context = questionContext;
    const thread = document.getElementById("qaFollow").checked && !context
      ? ((assistData && assistData.qa) || []).slice(-3).map((item) => ({ q: item.question, a: item.answer }))
      : [];
    qaBusy = true;
    document.getElementById("qaSend").disabled = true;
    renderQa();
    try {
      const item = await api(`/api/jobs/${job.id}/ask`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          selection: context ? context.text : "",
          question,
          mode,
          page: context ? context.page : null,
          side: context ? context.side : "translated",
          thread,
        }),
      });
      assistData = assistData || { qa: [], guide: {}, references: {} };
      assistData.qa = [...(assistData.qa || []), item];
      qaInput.value = "";
      setContext(null);
    } catch (error) {
      toast(error.message);
    } finally {
      qaBusy = false;
      document.getElementById("qaSend").disabled = false;
      renderQa();
    }
  }

  document.getElementById("qaForm").addEventListener("submit", (event) => {
    event.preventDefault();
    const question = qaInput.value.trim();
    if (question) sendQuestion("ask", question);
  });
  qaInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      document.getElementById("qaForm").requestSubmit();
    }
  });

  function gotoPage(page, side) {
    const target = side && KINDS.includes(side) ? side : main.dataset.view === "original" ? "original" : "translated";
    if (main.dataset.view !== "split" && main.dataset.view !== target) setView(target);
    scrollToPosition(target, { page: Number(page), fraction: 0 }, true);
  }

  panels.assistPanel.addEventListener("click", async (event) => {
    const build = event.target.closest("[data-build]");
    const goto = event.target.closest("[data-goto]");
    const pageRef = event.target.closest(".page-ref");
    const remove = event.target.closest("[data-qa-delete]");
    const filter = event.target.closest("[data-ref-filter]");
    if (build) {
      event.preventDefault();
      const kind = build.dataset.build;
      try {
        const state = await api(`/api/jobs/${job.id}/${kind}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ force: Boolean(build.dataset.force) }),
        });
        assistData[kind === "guide" ? "guide" : "references"] = state;
        renderAssist();
        assistTimer = setTimeout(loadAssist, 2500);
      } catch (error) {
        toast(error.message);
      }
    } else if (goto) {
      gotoPage(goto.dataset.goto, goto.dataset.side || "original");
    } else if (pageRef) {
      gotoPage(pageRef.dataset.page, "original");
    } else if (remove) {
      try {
        await api(`/api/jobs/${job.id}/qa/${remove.dataset.qaDelete}`, { method: "DELETE" });
        assistData.qa = assistData.qa.filter((item) => item.id !== remove.dataset.qaDelete);
        renderQa();
      } catch (error) {
        toast(error.message);
      }
    } else if (filter) {
      refsFilter = filter.dataset.refFilter;
      renderRefs();
    }
  });

  document.getElementById("exportMdItem").addEventListener("click", async () => {
    moreMenu.classList.add("hidden");
    await saveNow();
    const link = document.createElement("a");
    link.href = url(`/api/jobs/${job.id}/export.md`);
    link.download = "";
    document.body.appendChild(link);
    link.click();
    link.remove();
  });

  document.getElementById("zoteroItem").addEventListener("click", async () => {
    moreMenu.classList.add("hidden");
    await saveNow();
    toast("正在发送到 Zotero…");
    try {
      const result = await api(`/api/jobs/${job.id}/zotero`, { method: "POST" });
      toast(result.created ? "已在 Zotero 中新建条目并附上笔记" : "已更新 Zotero 中的笔记");
      job.meta = { ...job.meta, zotero: { item: result.item } };
      renderInfo();
    } catch (error) {
      toast(error.message);
    }
  });

  (async function boot() {
    setView(localStorage.getItem("qq-view") || "split");
    if (localStorage.getItem("qq-swapped") === "1") main.classList.add("swapped");
    setSaveState("idle");
    await window.QQ.loadAccess();
    if (!canWrite()) setSaveState("readonly");
    try {
      job = await resolveJob();
    } catch (error) {
      states.translated.classList.remove("hidden");
      states.translated.innerHTML = `<div class="state-card"><p class="eyebrow">ERROR</p><h3>无法打开</h3><p class="error">${escapeHtml(error.message)}</p><div class="row"><a class="btn" href="${url("/")}">返回首页</a></div></div>`;
      frames.translated.classList.add("hidden");
      frames.original.classList.add("hidden");
      states.original.classList.add("hidden");
      return;
    }
    render();
    loadNotes();
    if (job.status === "queued" || job.status === "running") poll();
    window.addEventListener("qq-access", () => {
      setSaveState(canWrite() ? "idle" : "readonly");
      if (!canWrite()) {
        currentTool = EDITOR.NONE;
        renderTools();
        KINDS.forEach(applyTool);
      }
      renderAssist();
    });
  })();
})();
