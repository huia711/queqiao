(function () {
  "use strict";

  const { api, escapeHtml, relativeTime, readerUrl, STATUS_TEXT, PIPELINE_TEXT } = window.QQ;
  const MODEL_KEY = "qq-model";
  const ARXIV_PATTERN = /(\d{4}\.\d{4,5}(v\d+)?)|([a-z][a-z\-]+(\.[A-Z]{2})?\/\d{7}(v\d+)?)/;

  const views = {
    home: document.getElementById("homeView"),
    digest: document.getElementById("digestView"),
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
    const path = window.QQ.appPath(location.pathname).replace(/\/+$/, "") || "/";
    const routes = { "/library": "library", "/history": "library", "/settings": "settings", "/about": "about", "/digest": "digest" };
    const name = routes[path] || "home";
    for (const [key, view] of Object.entries(views)) view.classList.toggle("hidden", key !== name);
    for (const link of document.querySelectorAll(".topbar nav a")) link.classList.toggle("active", link.dataset.route === name);
    document.title = { home: "鹊桥 Queqiao · 论文翻译与对照阅读", digest: "每日论文 · 鹊桥", library: "文库 · 鹊桥", settings: "设置 · 鹊桥", about: "说明 · 鹊桥" }[name];
    if (name === "about") loadConfig();
    if (name === "settings") loadSettings();
    if (name === "digest") loadDigest();
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
    if (!value) return;
    if (!value.match(ARXIV_PATTERN)) {
      showNotice(notice, "");
      await runSearch(value);
      return;
    }
    await openArxiv(value, notice);
  });

  // Open an arXiv paper: reuse an existing translation of it (any model) so nobody spends quota on a
  // duplicate; "用其他模型重新翻译" in the reader is still there. Otherwise submit a new job.
  async function openArxiv(value, notice) {
    const button = document.getElementById("arxivSubmit");
    button.disabled = true;
    if (notice) showNotice(notice, "");
    try {
      const found = await api(`/api/arxiv/lookup?id=${encodeURIComponent(value)}`);
      if (found.job) {
        location.href = readerUrl(found.job);
        return;
      }
      const job = await api("/api/arxiv", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ input: value, model: currentModel() }),
      });
      location.href = readerUrl(job);
    } catch (error) {
      if (notice) showNotice(notice, error.message, true);
      button.disabled = false;
    }
  }

  document.querySelector("#arxivPanel .hint").addEventListener("click", (event) => {
    const example = event.target.closest("[data-example]");
    if (!example) return;
    event.preventDefault();
    const input = document.getElementById("arxivInput");
    input.value = example.dataset.example;
    document.getElementById("arxivForm").requestSubmit();
  });

  // ---------------------------------------------------------------------------
  // Fuzzy search
  // ---------------------------------------------------------------------------

  const searchBox = document.getElementById("searchResults");

  function authorLine(paper) {
    const names = (paper.authors || []).slice(0, 4).join(", ");
    return names + ((paper.author_count || 0) > 4 ? " et al." : "");
  }

  function paperAction(paper, label = "翻译") {
    if (paper.job) {
      const text = paper.job.status === "done" ? "阅读译文" : "查看进度";
      return `<a class="btn small primary" href="${readerUrl(paper.job)}">${text}</a>`;
    }
    return `<button class="btn small primary" type="button" data-translate="${escapeHtml(paper.arxiv_id)}">${label}</button>`;
  }

  function searchCard(paper) {
    const year = (paper.published || "").slice(0, 4);
    return `<article class="paper-card">
      <div class="paper-head">
        <h3><a href="${escapeHtml(paper.abs_url)}" target="_blank" rel="noopener">${escapeHtml(paper.title)}</a></h3>
        ${paper.score !== null && paper.score !== undefined ? `<span class="match" title="匹配度">${paper.score}</span>` : ""}
      </div>
      <div class="meta"><span class="mono">arXiv:${escapeHtml(paper.arxiv_id)}</span> · ${escapeHtml(year)}${paper.category ? ` · ${escapeHtml(paper.category)}` : ""} · ${escapeHtml(authorLine(paper))}</div>
      ${paper.reason ? `<p class="reason">${escapeHtml(paper.reason)}</p>` : ""}
      <details><summary>摘要</summary><p>${escapeHtml(paper.abstract)}</p></details>
      <div class="paper-actions">${paperAction(paper)}<a class="btn small ghost" href="${escapeHtml(paper.abs_url)}" target="_blank" rel="noopener">arXiv ↗</a></div>
    </article>`;
  }

  async function runSearch(query) {
    const button = document.getElementById("arxivSubmit");
    button.disabled = true;
    searchBox.classList.remove("hidden");
    searchBox.innerHTML = `<div class="search-wait"><span class="spinner"></span>正在理解描述并检索 arXiv……通常需要 10–20 秒</div>`;
    try {
      const data = await api("/api/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ q: query }),
      });
      const notes = (data.notes || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
      const head = `<div class="search-head"><b>${data.results.length ? `找到 ${data.results.length} 篇可能的论文` : "没有找到匹配的论文"}</b>
        <span>${data.candidates ? `从 ${data.candidates} 个 arXiv 候选中挑选` : ""}${data.seconds ? ` · ${data.seconds} 秒` : ""}</span>
        <button class="btn small ghost" type="button" data-close-search>收起</button></div>`;
      const queries = (data.queries || []).length
        ? `<details class="search-queries"><summary>实际检索语句</summary>${data.queries.map((item) => `<code>${escapeHtml(item)}</code>`).join("")}</details>`
        : "";
      const empty = data.results.length ? "" : `<p class="search-empty">可以换一种说法，或者加上作者、年份、模型或 benchmark 的名字。目前只检索 arXiv；本地 PDF 可以在「本地 PDF」里上传。</p>`;
      searchBox.innerHTML = head + (notes ? `<ul class="search-notes">${notes}</ul>` : "") + data.results.map(searchCard).join("") + empty + queries;
    } catch (error) {
      searchBox.innerHTML = `<div class="notice bad">${escapeHtml(error.message)}</div>`;
    } finally {
      button.disabled = false;
    }
  }

  document.addEventListener("click", (event) => {
    const translate = event.target.closest("[data-translate]");
    if (translate) {
      event.preventDefault();
      translate.disabled = true;
      translate.textContent = "提交中…";
      openArxiv(translate.dataset.translate, document.getElementById("arxivNotice")).finally(() => {
        translate.disabled = false;
        translate.textContent = "翻译";
      });
      return;
    }
    if (event.target.closest("[data-close-search]")) searchBox.classList.add("hidden");
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

  // ---------------------------------------------------------------------------
  // Daily digest
  // ---------------------------------------------------------------------------

  const TIER_TEXT = { must: "优先精读", watch: "值得关注", brief: "简讯" };
  let digestReport = null;
  let digestTier = "all";
  let digestTimer = null;

  function formatStamp(text) {
    if (!text || text.length < 12) return text || "";
    return `${text.slice(0, 4)}-${text.slice(4, 6)}-${text.slice(6, 8)} ${text.slice(8, 10)}:${text.slice(10, 12)} UTC`;
  }

  function digestCard(item) {
    const tier = `<span class="chip tier-${item.tier}">${TIER_TEXT[item.tier]}</span>`;
    const extra = [
      item.area ? `<span class="chip">${escapeHtml(item.area)}</span>` : "",
      `<span class="chip mono">${escapeHtml(item.category || "")}</span>`,
      item.event !== "新论文" ? `<span class="chip blue">${escapeHtml(item.event)} v${item.version}</span>` : "",
    ].join("");
    const paper = { ...item, abs_url: `https://arxiv.org/abs/${item.arxiv_id}` };
    return `<article class="paper-card digest-card tier-${item.tier}">
      <div class="paper-head"><h3><a href="${paper.abs_url}" target="_blank" rel="noopener">${escapeHtml(item.title)}</a></h3><span class="match" title="模型打分（仅基于摘要）">${item.score}</span></div>
      <div class="meta"><span class="mono">arXiv:${escapeHtml(item.arxiv_id)}</span> · 首发 ${escapeHtml(item.published)}${item.updated !== item.published ? ` · 更新 ${escapeHtml(item.updated)}` : ""} · ${escapeHtml(authorLine(item))}</div>
      <div class="chips">${tier}${extra}</div>
      ${item.tldr ? `<p class="tldr">${escapeHtml(item.tldr)}</p>` : ""}
      ${item.why ? `<p class="why"><b>对你的意义</b>${escapeHtml(item.why)}</p>` : ""}
      ${item.caveat ? `<p class="caveat"><b>待核实</b>${escapeHtml(item.caveat)}</p>` : ""}
      <details><summary>摘要</summary><p>${escapeHtml(item.abstract)}</p></details>
      <div class="paper-actions">${paperAction(paper, "全文翻译")}<a class="btn small ghost" href="https://arxiv.org/pdf/${escapeHtml(item.arxiv_id)}" target="_blank" rel="noopener">PDF ↗</a></div>
    </article>`;
  }

  function renderDigest() {
    const body = document.getElementById("digestBody");
    const report = digestReport;
    if (!report) {
      body.innerHTML = `<div class="empty">还没有日报。${document.documentElement.dataset.access === "admin" ? "点右上角「生成今天的日报」试试，大约需要 1–2 分钟。" : ""}</div>`;
      return;
    }
    const counts = { must: 0, watch: 0, brief: 0 };
    for (const item of report.items) counts[item.tier] += 1;
    for (const button of document.querySelectorAll("#digestFilter button")) {
      const tier = button.dataset.tier;
      button.dataset.label = button.dataset.label || button.textContent;
      button.textContent = `${button.dataset.label} (${tier === "all" ? report.items.length : counts[tier]})`;
      button.classList.toggle("on", tier === digestTier);
    }
    const items = report.items.filter((item) => digestTier === "all" || item.tier === digestTier);
    const hits = Object.entries(report.hits || {}).map(([name, count]) => `${escapeHtml(name)} ${count}`).join(" · ");
    const problems = (report.problems || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
    body.innerHTML = `
      <div class="digest-overview">
        <div class="digest-meta">
          <span>覆盖 ${escapeHtml(formatStamp(report.window[0]))} – ${escapeHtml(formatStamp(report.window[1]))}</span>
          <span>候选 ${report.candidates} 篇${report.already_reported ? `（${report.already_reported} 篇之前已报道，已跳过）` : ""} · 入选 ${report.items.length} 篇</span>
          <span class="mono">${escapeHtml(report.model)} · ${report.seconds} 秒</span>
        </div>
        ${report.overview ? `<div class="overview-text">${window.QQ.renderMarkdown(report.overview)}</div>` : ""}
        <p class="digest-foot">各主题命中：${hits || "—"}。筛选和打分只基于标题与摘要，是初筛而不是结论。</p>
        ${problems ? `<ul class="warn-list">${problems}</ul>` : ""}
      </div>
      <div class="digest-list">${items.map(digestCard).join("") || `<div class="empty">这一类今天没有论文</div>`}</div>`;
  }

  function renderDigestStatus(status) {
    const element = document.getElementById("digestStatus");
    const runButton = document.getElementById("digestRunBtn");
    runButton.disabled = Boolean(status && status.running);
    if (status && status.running) {
      element.className = "digest-status";
      element.innerHTML = `<span class="spinner"></span>正在生成：${escapeHtml(status.stage || "")}`;
    } else if (status && status.error) {
      element.className = "digest-status bad";
      element.textContent = `上次生成失败：${status.error}`;
    } else {
      element.className = "digest-status hidden";
    }
  }

  async function loadDigest(day) {
    clearTimeout(digestTimer);
    try {
      const index = await api("/api/digest");
      const select = document.getElementById("digestDate");
      const chosen = day || select.value || index.dates[0];
      select.innerHTML = index.dates.length
        ? index.dates.map((item) => `<option value="${item}"${item === chosen ? " selected" : ""}>${item}</option>`).join("")
        : `<option value="">暂无日报</option>`;
      renderDigestStatus(index.status);
      digestReport = chosen && index.dates.includes(chosen) ? await api(`/api/digest/${chosen}`) : null;
      renderDigest();
      profileCache = index.profile;
      if (index.status.running) digestTimer = setTimeout(() => loadDigest(), 4000);
    } catch (error) {
      document.getElementById("digestBody").innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`;
    }
  }

  let profileCache = null;
  document.getElementById("digestDate").addEventListener("change", (event) => loadDigest(event.target.value));
  document.getElementById("digestFilter").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-tier]");
    if (!button) return;
    digestTier = button.dataset.tier;
    renderDigest();
  });
  document.getElementById("digestRunBtn").addEventListener("click", async () => {
    try {
      const status = await api("/api/digest/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      renderDigestStatus(status);
      digestTimer = setTimeout(() => loadDigest(new Date().toLocaleDateString("sv-SE")), 3000);
    } catch (error) {
      alert(error.message);
    }
  });

  const profileDialog = document.getElementById("profileDialog");
  document.getElementById("digestProfileBtn").addEventListener("click", async () => {
    if (!profileCache) profileCache = (await api("/api/digest")).profile;
    const form = profileDialog.querySelector("form");
    form.elements.interests.value = profileCache.interests;
    form.elements.topics.value = profileCache.topics.map((topic) => `${topic.name} | ${topic.query}`).join("\n");
    form.elements.categories.value = (profileCache.categories || []).join(", ");
    form.elements.days.value = profileCache.days;
    profileDialog.showModal();
  });
  profileDialog.addEventListener("close", async () => {
    const form = profileDialog.querySelector("form");
    try {
      if (profileDialog.returnValue === "reset") {
        if (!confirm("恢复为默认的研究画像？")) return;
        profileCache = await api("/api/digest/profile", { method: "DELETE" });
      } else if (profileDialog.returnValue === "ok") {
        const topics = form.elements.topics.value
          .split("\n")
          .map((line) => line.split("|"))
          .filter((parts) => parts.length >= 2)
          .map(([name, ...rest]) => ({ name: name.trim(), query: rest.join("|").trim() }));
        profileCache = await api("/api/digest/profile", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            interests: form.elements.interests.value,
            topics,
            categories: form.elements.categories.value.split(",").map((item) => item.trim()).filter(Boolean),
            days: Number(form.elements.days.value) || 3,
          }),
        });
      }
    } catch (error) {
      alert(error.message);
    }
  });

  // ---------------------------------------------------------------------------
  // Library cards
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
        ${job.status === "failed" ? `<button class="btn small writer-only" data-retry="${job.id}">重试</button>` : ""}
        <button class="btn small ghost danger admin-only" data-delete="${job.id}" title="删除记录、文件与笔记">删除</button>
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
      if (data.restricted) {
        setSettingsStatus("只有管理员邀请码可以查看和修改设置", "bad");
        return;
      }
      const values = data.settings;
      for (const [key, info] of Object.entries(values)) {
        const input = settingsForm.elements[key];
        if (!input) continue;
        if (key === "default_model" || key === "assist_model") continue;
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
      const assist = document.getElementById("settingsAssistModel");
      const assistValue = values.assist_model.value;
      assist.innerHTML =
        `<option value="">与翻译模型相同</option>` +
        (assistValue && !ids.includes(assistValue) ? `<option value="${escapeHtml(assistValue)}">${escapeHtml(assistValue)}</option>` : "") +
        models.models.map((model) => `<option value="${escapeHtml(model.id)}">${escapeHtml(model.name)}</option>`).join("");
      assist.value = assistValue;
      assist.disabled = values.assist_model.source === "env";
      const assistName = assist.closest(".setting").querySelector(".setting-name");
      assistName.querySelector(".source-tag")?.remove();
      assistName.insertAdjacentHTML("beforeend", `<span class="source-tag">${SOURCE_TEXT[values.assist_model.source]}</span>`);
      document.getElementById("settingsWhere").innerHTML =
        `设置保存在 <code>${escapeHtml(data.settings_file)}</code>；论文、译文和笔记保存在 <code>${escapeHtml(data.data_dir)}</code>。环境变量的优先级最高。`;
      loadInvites();
    } catch (error) {
      setSettingsStatus(error.message, "bad");
    }
  }

  document.getElementById("zoteroTest").addEventListener("click", async () => {
    const status = document.getElementById("zoteroStatus");
    status.className = "setting-status";
    status.textContent = "先保存设置，再测试…";
    try {
      await api("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(settingsPayload()) });
      const result = await api("/api/zotero/test", { method: "POST" });
      status.className = `setting-status ${result.ok ? "ok" : "bad"}`;
      status.textContent = result.ok ? `连接成功：${result.username || ""}，库中 ${result.items} 个条目` : `失败：${result.error}`;
    } catch (error) {
      status.className = "setting-status bad";
      status.textContent = error.message;
    }
  });

  async function loadInvites() {
    const section = document.getElementById("inviteAdmin");
    try {
      const data = await api("/api/access/codes");
      section.classList.toggle("hidden", data.mode !== "invite" && !data.codes.length);
      const rows = data.codes.map((code) => {
        const used = code.used_today || {};
        const limits = code.limits || {};
        const quota = (kind) => `${used[kind] || 0}/${limits[kind] ? limits[kind] : "∞"}`;
        return `<tr class="${code.disabled ? "disabled" : ""}">
          <td>${escapeHtml(code.name)} <small class="mono">${escapeHtml(code.id)}</small></td>
          <td>${code.role === "admin" ? "管理员" : "成员"} · 今天翻译 ${quota("translate")} · AI ${quota("ai")}${code.disabled ? " · 已停用" : ""}
          ${code.disabled ? "" : `<button class="btn small ghost danger" type="button" data-revoke="${escapeHtml(code.id)}">停用</button>`}</td></tr>`;
      });
      document.getElementById("inviteTable").innerHTML = rows.join("") || `<tr><td>还没有邀请码</td><td></td></tr>`;
      if (location.hash === "#invites" && !section.classList.contains("hidden")) {
        section.scrollIntoView({ behavior: "smooth", block: "start" });
        section.querySelector("[name=name]").focus({ preventScroll: true });
      }
    } catch (error) {
      section.classList.add("hidden");
    }
  }

  document.getElementById("inviteForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const notice = document.getElementById("inviteNew");
    try {
      const result = await api("/api/access/codes", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: form.elements.name.value,
          role: form.elements.role.value,
          translate: Number(form.elements.translate.value || 0),
          ai: Number(form.elements.ai.value || 0),
        }),
      });
      notice.classList.remove("hidden", "bad");
      notice.innerHTML = `已为 ${escapeHtml(result.entry.name)} 生成邀请码（只显示这一次）：<code class="invite-code">${escapeHtml(result.code)}</code> <button class="btn small" type="button" data-copy="${escapeHtml(result.code)}">复制</button>`;
      form.reset();
      loadInvites();
    } catch (error) {
      showNotice(notice, error.message, true);
    }
  });

  document.addEventListener("click", async (event) => {
    const copy = event.target.closest("[data-copy]");
    const revoke = event.target.closest("[data-revoke]");
    if (copy) {
      await navigator.clipboard.writeText(copy.dataset.copy).catch(() => {});
      copy.textContent = "已复制";
    }
    if (revoke && confirm("停用这个邀请码？停用后无法恢复。")) {
      try {
        await api(`/api/access/codes/${revoke.dataset.revoke}/revoke`, { method: "POST" });
      } catch (error) {
        alert(error.message);
      }
      loadInvites();
    }
  });

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
  window.addEventListener("qq-access", () => route());
  window.QQ.loadAccess().then(() => {
    loadModels();
    refreshJobs();
  });
})();
