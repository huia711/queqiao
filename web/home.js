(function () {
  "use strict";

  const { api, escapeHtml, relativeTime, readerUrl, STATUS_TEXT, PIPELINE_TEXT } = window.QQ;
  const MODEL_KEY = "qq-model";
  const ARXIV_PATTERN = /(\d{4}\.\d{4,5}(v\d+)?)|([a-z][a-z\-]+(\.[A-Z]{2})?\/\d{7}(v\d+)?)/;

  const views = {
    home: document.getElementById("homeView"),
    library: document.getElementById("libraryView"),
    settings: document.getElementById("settingsView"),
    about: document.getElementById("aboutView"),
  };
  const pickers = [...document.querySelectorAll(".model-picker")];
  let selectedFile = null;
  let pollTimer = null;
  let jobsCache = [];
  let libraryFilter = "all";

  // ---------------------------------------------------------------------------
  // Routing between 翻译 / 文库 / 说明
  // ---------------------------------------------------------------------------

  function route() {
    const path = location.pathname.replace(/\/+$/, "") || "/";
    const routes = { "/library": "library", "/history": "library", "/settings": "settings", "/about": "about" };
    const name = routes[path] || "home";
    for (const [key, view] of Object.entries(views)) view.classList.toggle("hidden", key !== name);
    for (const link of document.querySelectorAll(".topbar nav a")) link.classList.toggle("active", link.dataset.route === name);
    document.title = { home: "鹊桥 Queqiao · 论文翻译与对照阅读", library: "文库 · 鹊桥", settings: "设置 · 鹊桥", about: "说明 · 鹊桥" }[name];
    if (name === "about") loadConfig();
    if (name === "settings") loadSettings();
    window.scrollTo(0, 0);
    return name;
  }

  document.addEventListener("click", (event) => {
    const link = event.target.closest(".topbar nav a, [data-route-link]");
    if (!link || event.metaKey || event.ctrlKey) return;
    const url = new URL(link.href);
    if (url.origin !== location.origin) return;
    event.preventDefault();
    history.pushState({}, "", url.pathname);
    route();
  });
  window.addEventListener("popstate", route);

  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((item) => {
        item.classList.toggle("active", item === tab);
        item.setAttribute("aria-selected", String(item === tab));
      });
      document.getElementById("arxivPanel").classList.toggle("hidden", tab.dataset.tab !== "arxiv");
      document.getElementById("localPanel").classList.toggle("hidden", tab.dataset.tab !== "local");
    });
  });

  // ---------------------------------------------------------------------------
  // Models
  // ---------------------------------------------------------------------------

  function currentModel() {
    return pickers[0] && pickers[0].value ? pickers[0].value : "";
  }

  function showModel() {
    const model = currentModel();
    document.getElementById("footModel").textContent = model ? `当前模型 ${model}` : "";
    document.getElementById("metaModel").textContent = model ? `模型 ${model}` : "模型 —";
  }

  async function loadModels() {
    try {
      const data = await api("/api/models");
      const saved = localStorage.getItem(MODEL_KEY);
      const ids = data.models.map((model) => model.id);
      const chosen = saved && ids.includes(saved) ? saved : data.default;
      const option = (model) =>
        `<option value="${escapeHtml(model.id)}"${model.id === chosen ? " selected" : ""}>${escapeHtml(model.name)}</option>`;
      const recommended = data.models.filter((model) => model.recommended);
      const others = data.models.filter((model) => !model.recommended);
      let html = "";
      if (!ids.includes(data.default)) {
        html += `<option value="${escapeHtml(data.default)}" selected>${escapeHtml(data.default)}（默认）</option>`;
      }
      if (recommended.length) html += `<optgroup label="推荐（快速）">${recommended.map(option).join("")}</optgroup>`;
      if (others.length) html += `<optgroup label="全部模型">${others.map(option).join("")}</optgroup>`;
      for (const picker of pickers) picker.innerHTML = html;
    } catch (error) {
      for (const picker of pickers) picker.innerHTML = `<option value="">（使用服务端默认模型）</option>`;
    }
    showModel();
  }

  for (const picker of pickers) {
    picker.addEventListener("change", () => {
      localStorage.setItem(MODEL_KEY, picker.value);
      for (const other of pickers) if (other !== picker) other.value = picker.value;
      showModel();
    });
  }

  // ---------------------------------------------------------------------------
  // Submitting work
  // ---------------------------------------------------------------------------

  function showNotice(element, message, bad) {
    element.textContent = message;
    element.classList.toggle("bad", Boolean(bad));
    element.classList.toggle("hidden", !message);
  }

  document.getElementById("arxivForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = document.getElementById("arxivInput");
    const notice = document.getElementById("arxivNotice");
    const value = input.value.trim();
    if (!value.match(ARXIV_PATTERN)) {
      showNotice(notice, "无法识别 arXiv 地址或编号，例如 1706.03762 或 https://arxiv.org/abs/1706.03762", true);
      return;
    }
    const button = document.getElementById("arxivSubmit");
    button.disabled = true;
    showNotice(notice, "");
    try {
      const job = await api("/api/arxiv", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ input: value, model: currentModel() }),
      });
      location.href = readerUrl(job);
    } catch (error) {
      showNotice(notice, error.message, true);
      button.disabled = false;
    }
  });

  const dropzone = document.getElementById("dropzone");
  const fileInput = document.getElementById("fileInput");
  const maxUploadMb = 50;

  function setFile(file) {
    const notice = document.getElementById("uploadNotice");
    showNotice(notice, "");
    if (file && !/\.pdf$/i.test(file.name)) {
      showNotice(notice, "目前只支持 PDF 文件", true);
      file = null;
    }
    if (file && file.size > maxUploadMb * 1024 * 1024) {
      showNotice(notice, `文件超过 ${maxUploadMb} MB`, true);
      file = null;
    }
    selectedFile = file;
    document.getElementById("fileChip").classList.toggle("show", Boolean(file));
    document.getElementById("uploadSubmit").disabled = !file;
    if (file) {
      document.getElementById("fileName").textContent = file.name;
      document.getElementById("fileMeta").textContent = `${(file.size / 1024 / 1024).toFixed(1)} MB`;
    }
  }

  fileInput.addEventListener("change", () => setFile(fileInput.files[0] || null));
  document.getElementById("fileClear").addEventListener("click", () => {
    fileInput.value = "";
    setFile(null);
  });
  ["dragenter", "dragover"].forEach((name) =>
    dropzone.addEventListener(name, (event) => {
      event.preventDefault();
      dropzone.classList.add("drag");
    })
  );
  ["dragleave", "drop"].forEach((name) =>
    dropzone.addEventListener(name, (event) => {
      event.preventDefault();
      dropzone.classList.remove("drag");
    })
  );
  dropzone.addEventListener("drop", (event) => {
    const file = event.dataTransfer.files[0];
    if (file) setFile(file);
  });

  document.getElementById("uploadSubmit").addEventListener("click", async () => {
    if (!selectedFile) return;
    const notice = document.getElementById("uploadNotice");
    const button = document.getElementById("uploadSubmit");
    const form = new FormData();
    form.append("file", selectedFile);
    form.append("model", currentModel());
    const pages = document.getElementById("pagesInput").value.trim();
    if (pages) form.append("pages", pages);
    const glossary = document.getElementById("glossaryInput").files[0];
    if (glossary) form.append("glossary", glossary);
    button.disabled = true;
    showNotice(notice, "上传中…");
    try {
      const job = await api("/api/upload", { method: "POST", body: form });
      location.href = readerUrl(job);
    } catch (error) {
      showNotice(notice, error.message, true);
      button.disabled = false;
    }
  });

  // ---------------------------------------------------------------------------
  // Job cards
  // ---------------------------------------------------------------------------

  function hasNotes(job) {
    return Boolean(job.notes && (job.notes.translated || job.notes.original));
  }

  function jobCard(job) {
    const meta = job.meta || {};
    const title = meta.title || job.title || job.source_name || "未命名文档";
    const source = job.kind === "arxiv" ? `arXiv:${escapeHtml(job.arxiv_id || "")}` : escapeHtml(job.source_name || "本地文档");
    const authors = (meta.authors || []).slice(0, 3).join("、") + ((meta.authors || []).length > 3 ? " 等" : "");
    const chips = [
      `<span class="chip ${job.status}">${STATUS_TEXT[job.status] || job.status}</span>`,
      job.pipeline ? `<span class="chip blue">${PIPELINE_TEXT[job.pipeline] || job.pipeline}</span>` : "",
      hasNotes(job) ? `<span class="chip red">✎ 有笔记</span>` : "",
      meta.category ? `<span class="chip mono">${escapeHtml(meta.category)}</span>` : "",
    ].join("");
    const active = job.status === "running" || job.status === "queued";
    const progress = active
      ? `<div class="progress"><span style="width:${Math.max(2, job.progress).toFixed(1)}%"></span></div><div class="stage">${escapeHtml(job.stage)}</div>`
      : "";
    const error = job.status === "failed" && job.error ? `<div class="error-line" title="${escapeHtml(job.error)}">${escapeHtml(job.error)}</div>` : "";
    return `<a class="job-card" href="${readerUrl(job)}" data-id="${job.id}">
      <div class="head"><h3>${escapeHtml(title)}</h3></div>
      <div class="meta"><span class="mono">${source}</span>${authors ? ` · ${escapeHtml(authors)}` : ""}</div>
      <div class="chips">${chips}</div>
      ${progress}${error}
      <div class="foot">
        <span class="meta"><span class="mono">${escapeHtml(job.model)}</span> · ${relativeTime(job.created_at)}</span>
        <span class="spacer"></span>
        ${job.status === "failed" ? `<button class="btn small" data-retry="${job.id}">重试</button>` : ""}
        <button class="btn small ghost danger" data-delete="${job.id}" title="删除记录、文件与笔记">删除</button>
      </div>
    </a>`;
  }

  function matchesFilter(job) {
    if (libraryFilter === "notes") return hasNotes(job);
    if (libraryFilter === "arxiv") return job.kind === "arxiv";
    if (libraryFilter === "upload") return job.kind === "upload";
    if (libraryFilter === "active") return job.status === "running" || job.status === "queued";
    if (libraryFilter === "failed") return job.status === "failed";
    return true;
  }

  function matchesSearch(job) {
    const query = document.getElementById("librarySearch").value.trim().toLowerCase();
    if (!query) return true;
    const meta = job.meta || {};
    return [meta.title, job.title, job.arxiv_id, job.source_name, job.model, (meta.authors || []).join(" ")]
      .filter(Boolean)
      .some((value) => String(value).toLowerCase().includes(query));
  }

  function renderJobs() {
    const jobs = jobsCache;
    const empty = (text) => `<div class="empty">${text}</div>`;
    document.getElementById("recentJobs").innerHTML = jobs.length ? jobs.slice(0, 4).map(jobCard).join("") : empty("还没有翻译记录，先在上面输入一个 arXiv 编号试试。");
    const filtered = jobs.filter((job) => matchesFilter(job) && matchesSearch(job));
    document.getElementById("allJobs").innerHTML = filtered.length ? filtered.map(jobCard).join("") : empty(jobs.length ? "没有符合条件的条目" : "还没有翻译记录");
    document.getElementById("metaPapers").textContent = String(jobs.filter((job) => job.status === "done").length);
    document.getElementById("metaNotes").textContent = String(jobs.filter(hasNotes).length);
    for (const button of document.querySelectorAll("#libraryFilter button")) {
      const key = button.dataset.filter;
      const previous = libraryFilter;
      libraryFilter = key;
      const count = jobs.filter(matchesFilter).length;
      libraryFilter = previous;
      button.textContent = `${button.dataset.label || (button.dataset.label = button.textContent)} (${count})`;
      button.classList.toggle("on", key === libraryFilter);
    }
  }

  async function refreshJobs() {
    clearTimeout(pollTimer);
    try {
      jobsCache = await api("/api/jobs");
      renderJobs();
      if (jobsCache.some((job) => job.status === "running" || job.status === "queued")) {
        pollTimer = setTimeout(refreshJobs, 2000);
      }
    } catch (error) {
      pollTimer = setTimeout(refreshJobs, 5000);
    }
  }

  document.getElementById("libraryFilter").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-filter]");
    if (!button) return;
    libraryFilter = button.dataset.filter;
    renderJobs();
  });
  document.getElementById("librarySearch").addEventListener("input", renderJobs);

  document.addEventListener("click", async (event) => {
    const retry = event.target.closest("[data-retry]");
    const remove = event.target.closest("[data-delete]");
    if (!retry && !remove) return;
    event.preventDefault();
    event.stopPropagation();
    try {
      if (retry) {
        await api(`/api/jobs/${retry.dataset.retry}/retry`, { method: "POST" });
      } else if (confirm("删除这条记录，以及本机上的原文、译文和笔记？")) {
        await api(`/api/jobs/${remove.dataset.delete}`, { method: "DELETE" });
      }
    } catch (error) {
      alert(error.message);
    }
    refreshJobs();
  });

  // ---------------------------------------------------------------------------
  // About page
  // ---------------------------------------------------------------------------

  const settingsForm = document.getElementById("settingsForm");
  const settingsStatus = document.getElementById("settingsStatus");
  const SOURCE_TEXT = { env: "环境变量", file: "已自定义", default: "默认" };

  function setSettingsStatus(text, kind) {
    settingsStatus.textContent = text;
    settingsStatus.className = `setting-status${kind ? ` ${kind}` : ""}`;
  }

  async function loadSettings() {
    setSettingsStatus("");
    try {
      const [data, models] = await Promise.all([api("/api/settings"), api("/api/models").catch(() => ({ models: [] }))]);
      const values = data.settings;
      for (const [key, info] of Object.entries(values)) {
        const input = settingsForm.elements[key];
        if (!input) continue;
        if (key === "default_model") continue;
        input.value = Array.isArray(info.value) ? info.value.join(", ") : info.value ?? "";
        input.disabled = info.source === "env";
        input.title = info.source === "env" ? "由环境变量指定，页面上不能修改" : "";
        const name = input.closest(".setting").querySelector(".setting-name");
        name.querySelector(".source-tag")?.remove();
        name.insertAdjacentHTML("beforeend", `<span class="source-tag">${SOURCE_TEXT[info.source] || info.source}</span>`);
      }
      const select = document.getElementById("settingsModel");
      const current = values.default_model.value;
      const ids = models.models.map((model) => model.id);
      select.innerHTML =
        (ids.includes(current) ? "" : `<option value="${escapeHtml(current)}">${escapeHtml(current)}</option>`) +
        models.models.map((model) => `<option value="${escapeHtml(model.id)}">${escapeHtml(model.name)}</option>`).join("");
      select.value = current;
      select.disabled = values.default_model.source === "env";
      const modelName = select.closest(".setting").querySelector(".setting-name");
      modelName.querySelector(".source-tag")?.remove();
      modelName.insertAdjacentHTML("beforeend", `<span class="source-tag">${SOURCE_TEXT[values.default_model.source]}</span>`);
      document.getElementById("settingsWhere").innerHTML =
        `设置保存在 <code>${escapeHtml(data.settings_file)}</code>；论文、译文和笔记保存在 <code>${escapeHtml(data.data_dir)}</code>。环境变量的优先级最高。`;
    } catch (error) {
      setSettingsStatus(error.message, "bad");
    }
  }

  function settingsPayload() {
    const payload = {};
    for (const element of settingsForm.elements) {
      if (!element.name || element.disabled) continue;
      payload[element.name] = element.type === "number" ? (element.value ? Number(element.value) : null) : element.value.trim();
    }
    return payload;
  }

  settingsForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    setSettingsStatus("保存中…");
    try {
      await api("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(settingsPayload()) });
      setSettingsStatus("已保存，立即生效", "ok");
      localStorage.removeItem(MODEL_KEY);
      await Promise.all([loadSettings(), loadModels()]);
      window.QQ.loadGatewayStatus(document.getElementById("gatewayStatus"));
      setSettingsStatus("已保存，立即生效", "ok");
    } catch (error) {
      setSettingsStatus(error.message, "bad");
    }
  });

  document.getElementById("settingsTest").addEventListener("click", async () => {
    setSettingsStatus("测试中…");
    const payload = settingsPayload();
    try {
      const result = await api("/api/settings/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ magpie_base_url: payload.magpie_base_url || null, magpie_api_key: payload.magpie_api_key || null }),
      });
      if (result.ok) setSettingsStatus(`连接成功，可用模型 ${result.models} 个`, "ok");
      else setSettingsStatus(`连接失败：${result.error}`, "bad");
    } catch (error) {
      setSettingsStatus(error.message, "bad");
    }
  });

  async function loadConfig() {
    const table = document.getElementById("configTable");
    try {
      const health = await api("/api/health");
      const fonts = health.fonts || {};
      const rows = [
        ["Magpie 网关", `${health.magpie.base_url}（${health.magpie.ok ? `已连接，${health.magpie.models} 个模型` : "未连接"}）`],
        ["默认模型", health.default_model],
        ["LaTeX 编译", health.latex ? "可用（latexmk + XeLaTeX）" : "不可用，arXiv 论文将改用 PDF 版式翻译"],
        ["译文字体", [fonts.serif, fonts.sans, fonts.kai].filter(Boolean).join(" / ") || "未检测到"],
        ["数据目录", health.data_dir || "data/"],
      ];
      table.innerHTML = rows.map(([key, value]) => `<tr><td>${escapeHtml(key)}</td><td>${escapeHtml(value)}</td></tr>`).join("");
    } catch (error) {
      table.innerHTML = `<tr><td>读取失败</td><td>${escapeHtml(error.message)}</td></tr>`;
    }
  }

  window.QQ.bindThemeToggle(document.getElementById("themeToggle"));
  window.QQ.loadGatewayStatus(document.getElementById("gatewayStatus"));
  route();
  loadModels();
  refreshJobs();
})();
