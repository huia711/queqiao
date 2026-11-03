(function () {
  "use strict";

  const { api, url, appPath, escapeHtml, isDark, setTheme, readerUrl, STATUS_TEXT, PIPELINE_TEXT } = window.QQ;
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
        const response = await fetch(url(`/api/jobs/${job.id}/notes/${kind}`), {
          method: "PUT",
          headers: { "Content-Type": "application/pdf" },
          body: data,
          keepalive: keepalive && data.byteLength < 60 * 1024,
        });
        if (response.status === 401) {
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
            window.open(url("/login"), "_blank", "noopener");
            alert(error.message);
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
    try {
      job = await api(`/api/jobs/${job.id}`);
      render();
    } catch (error) {
      toast(error.message);
    }
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

  const panels = { notesPanel: document.getElementById("notesPanel"), infoPanel: document.getElementById("infoPanel") };
  const panelButtons = { notesPanel: document.getElementById("notesBtn"), infoPanel: document.getElementById("infoBtn") };

  function togglePanel(name, force) {
    for (const [key, panel] of Object.entries(panels)) {
      const open = key === name ? (force ?? panel.classList.contains("hidden")) : false;
      panel.classList.toggle("hidden", !open);
      panelButtons[key].classList.toggle("active", open);
    }
  }

  panelButtons.notesPanel.addEventListener("click", () => togglePanel("notesPanel"));
  panelButtons.infoPanel.addEventListener("click", () => togglePanel("infoPanel"));
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

  (async function boot() {
    setView(localStorage.getItem("qq-view") || "split");
    if (localStorage.getItem("qq-swapped") === "1") main.classList.add("swapped");
    setSaveState("idle");
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
  })();
})();
