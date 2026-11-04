(function () {
  "use strict";

  const STATUS_TEXT = { queued: "排队中", running: "翻译中", done: "已完成", failed: "失败" };
  const PIPELINE_TEXT = { latex: "源码翻译", babeldoc: "PDF 版式翻译" };
  const KEY_STORAGE = "qq-invite";

  // One-time setup link: …/queqiao/#invite=<code> stores the code in this browser and removes it from
  // the address bar. A URL fragment is never sent to the server, proxies or logs.
  (function adoptInviteFromLink() {
    const match = location.hash.match(/(?:^#|&)invite=([^&]+)/);
    if (!match) return;
    try {
      localStorage.setItem(KEY_STORAGE, decodeURIComponent(match[1]).trim());
    } catch (error) {
      /* storage unavailable */
    }
    history.replaceState(null, "", location.pathname + location.search);
  })();
  // Set by the server when Queqiao is served under a path prefix such as "/queqiao".
  const BASE = (window.QQ_BASE || "").replace(/\/+$/, "");

  function url(path) {
    if (!path || !path.startsWith("/") || path.startsWith("//")) return path;
    if (BASE && (path === BASE || path.startsWith(BASE + "/"))) return path;
    return BASE + path;
  }

  function appPath(pathname) {
    if (BASE && (pathname === BASE || pathname.startsWith(BASE + "/"))) return pathname.slice(BASE.length) || "/";
    return pathname;
  }

  function toLogin() {
    const here = appPath(location.pathname) + location.search;
    location.href = url(`/login?next=${encodeURIComponent(here)}`);
  }

  function inviteKey() {
    return localStorage.getItem(KEY_STORAGE) || "";
  }

  function withKey(options) {
    const merged = { ...(options || {}) };
    const key = inviteKey();
    if (key) merged.headers = { ...(merged.headers || {}), "X-Queqiao-Key": key };
    return merged;
  }

  let access = null;
  async function loadAccess(force = false) {
    if (access && !force) return access;
    try {
      const response = await fetch(url("/api/access/me"), withKey());
      access = response.ok ? await response.json() : { mode: "open", can_write: true };
    } catch (error) {
      access = { mode: "open", can_write: true };
    }
    document.documentElement.dataset.access = access.can_write ? (access.role === "admin" ? "admin" : "member") : "guest";
    renderAccessChip();
    window.dispatchEvent(new CustomEvent("qq-access", { detail: access }));
    return access;
  }

  function renderAccessChip() {
    const chip = document.getElementById("accessChip");
    if (!chip || !access) return;
    chip.classList.toggle("hidden", access.mode !== "invite");
    chip.textContent = access.can_write ? `${access.name}` : "输入邀请码";
    chip.title = access.local
      ? "本机直连，自动是管理员，不需要邀请码；点击可以给别人生成邀请码"
      : access.can_write ? `已用邀请码登录（${access.role === "admin" ? "管理员" : "成员"}），点击管理` : "访客只能阅读已有的翻译；输入邀请码后可以提交翻译、使用 AI 研读";
    chip.classList.toggle("on", Boolean(access.can_write));
  }

  function ensureInviteDialog() {
    let dialog = document.getElementById("inviteDialog");
    if (dialog) return dialog;
    dialog = document.createElement("dialog");
    dialog.id = "inviteDialog";
    dialog.className = "model-dialog invite-dialog";
    dialog.innerHTML = `<form method="dialog">
      <strong>邀请码</strong>
      <p class="invite-reason">翻译和 AI 研读会消耗模型额度，只对持有邀请码的人开放。没有邀请码也可以自由阅读文库里已有的论文。</p>
      <input class="field" name="code" placeholder="qq-…" autocomplete="off" spellcheck="false">
      <p class="invite-state"></p>
      <div class="invite-make hidden">
        <strong>给别人生成邀请码</strong>
        <p>生成的是成员邀请码：每天最多翻译 10 篇、使用 AI 研读 200 次。邀请码只显示这一次，可以随时停用。</p>
        <div class="invite-make-row">
          <input class="field" name="who" placeholder="给谁用，例如：张三" autocomplete="off">
          <button class="btn primary" type="button" data-role="make">生成</button>
        </div>
        <div class="invite-made"></div>
        <a class="invite-manage" data-role="manage">管理全部邀请码（额度、停用、管理员）→</a>
      </div>
      <div class="row">
        <button class="btn ghost danger" value="logout" type="submit" data-role="logout">退出</button>
        <span class="spacer"></span>
        <button class="btn" value="cancel">取消</button>
        <button class="btn primary" value="ok">确定</button>
      </div>
    </form>`;
    document.body.appendChild(dialog);
    dialog.querySelector("[data-role=manage]").href = url("/settings#invites");
    dialog.querySelector("[data-role=make]").addEventListener("click", async () => {
      const who = dialog.querySelector("[name=who]");
      const box = dialog.querySelector(".invite-made");
      if (!who.value.trim()) {
        who.focus();
        return;
      }
      try {
        const result = await api("/api/access/codes", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: who.value.trim(), role: "member" }),
        });
        const link = `${location.origin}${url("/")}#invite=${encodeURIComponent(result.code)}`;
        box.innerHTML = `<div>邀请码（${escapeHtml(result.entry.name)}）：<code>${escapeHtml(result.code)}</code></div>
          <div class="invite-made-actions"><button class="btn small" type="button" data-copy-text="${escapeHtml(result.code)}">复制邀请码</button>
          <button class="btn small" type="button" data-copy-text="${escapeHtml(link)}">复制一键登录链接</button></div>`;
        who.value = "";
      } catch (error) {
        box.textContent = error.message;
      }
    });
    dialog.addEventListener("click", async (event) => {
      const copy = event.target.closest("[data-copy-text]");
      if (!copy) return;
      await navigator.clipboard.writeText(copy.dataset.copyText).catch(() => {});
      copy.textContent = "已复制";
    });
    dialog.addEventListener("close", async () => {
      const value = dialog.querySelector("[name=code]").value.trim();
      if (dialog.returnValue === "logout") {
        localStorage.removeItem(KEY_STORAGE);
        await loadAccess(true);
      } else if (dialog.returnValue === "ok" && value) {
        localStorage.setItem(KEY_STORAGE, value);
        const result = await loadAccess(true);
        if (!result.can_write) {
          localStorage.removeItem(KEY_STORAGE);
          await loadAccess(true);
          openInvite("邀请码无效或已停用");
          return;
        }
      }
      if (dialog._resolve) dialog._resolve(access && access.can_write);
      dialog._resolve = null;
    });
    return dialog;
  }

  function openInvite(message) {
    const dialog = ensureInviteDialog();
    dialog.querySelector(".invite-state").textContent = message || (access && access.can_write ? `当前：${access.name}` : "");
    dialog.querySelector("[name=code]").value = "";
    dialog.querySelector("[data-role=logout]").classList.toggle("hidden", !inviteKey());
    const admin = Boolean(access && access.can_write && access.role === "admin");
    dialog.querySelector(".invite-make").classList.toggle("hidden", !admin);
    dialog.querySelector(".invite-made").innerHTML = "";
    dialog.querySelector(".invite-reason").classList.toggle("hidden", admin);
    dialog.querySelector("[name=code]").classList.toggle("hidden", admin);
    return new Promise((resolve) => {
      dialog._resolve = resolve;
      if (!dialog.open) dialog.showModal();
    });
  }

  async function api(path, options) {
    const response = await fetch(url(path), withKey(options));
    if (response.status === 401) {
      const body = await response.clone().json().catch(() => ({}));
      if (body.code === "invite") {
        openInvite(body.detail);
        throw new Error(body.detail || "需要邀请码");
      }
      toLogin();
      throw new Error("登录已过期，请重新登录");
    }
    let data = null;
    try {
      data = await response.json();
    } catch (error) {
      data = null;
    }
    if (!response.ok) {
      const detail = data && data.detail ? data.detail : `请求失败（HTTP ${response.status}）`;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return data;
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[char]);
  }

  // Minimal, escape-first Markdown for model answers: paragraphs, bullet / numbered lists, **bold**,
  // `code`, $math$ (shown as code) and page references like [p.3] (clickable via data-page).
  function renderInline(text) {
    return escapeHtml(text)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\$([^$\n]{1,200})\$/g, '<code class="math">$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\[p\.(\d+)(?:[-–](\d+))?\]/g, (match, page, end) => `<a class="page-ref" data-page="${page}">p.${page}${end ? `–${end}` : ""}</a>`);
  }

  function renderMarkdown(text) {
    const out = [];
    let list = null;
    const close = () => {
      if (list) out.push(`</${list}>`);
      list = null;
    };
    for (const raw of String(text || "").split(/\n/)) {
      const line = raw.trimEnd();
      const bullet = line.match(/^\s*[-*•]\s+(.*)/);
      const numbered = line.match(/^\s*\d+[.)、]\s+(.*)/);
      if (bullet || numbered) {
        const kind = bullet ? "ul" : "ol";
        if (list !== kind) {
          close();
          out.push(`<${kind}>`);
          list = kind;
        }
        out.push(`<li>${renderInline((bullet || numbered)[1])}</li>`);
        continue;
      }
      close();
      if (!line.trim()) continue;
      const heading = line.match(/^#{1,4}\s+(.*)/);
      const quote = line.match(/^\s*>\s?(.*)/);
      if (heading) out.push(`<p><strong>${renderInline(heading[1])}</strong></p>`);
      else if (quote) out.push(`<blockquote>${renderInline(quote[1])}</blockquote>`);
      else out.push(`<p>${renderInline(line)}</p>`);
    }
    close();
    return out.join("");
  }

  function isDark() {
    return document.documentElement.dataset.theme === "dark";
  }

  function setTheme(dark) {
    if (dark) document.documentElement.dataset.theme = "dark";
    else delete document.documentElement.dataset.theme;
    localStorage.setItem("qq-theme", dark ? "dark" : "light");
    window.dispatchEvent(new CustomEvent("qq-theme", { detail: { dark } }));
  }

  function bindThemeToggle(button) {
    if (!button) return;
    button.addEventListener("click", () => setTheme(!isDark()));
  }

  function relativeTime(seconds) {
    const diff = Date.now() / 1000 - seconds;
    if (diff < 60) return "刚刚";
    if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
    if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
    const date = new Date(seconds * 1000);
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
  }

  function readerUrl(job) {
    if (job.kind === "arxiv" && job.arxiv_id) {
      return url(`/arxiv/${job.arxiv_id}?job=${job.id}`);
    }
    return url(`/job/${job.id}`);
  }

  async function loadGatewayStatus(pill) {
    if (!pill) return null;
    const dot = pill.querySelector(".dot");
    const label = pill.querySelector(".label");
    try {
      const health = await api("/api/health");
      if (health.magpie.ok) {
        dot.className = "dot ok";
        label.textContent = `Magpie · ${health.magpie.models} 个模型`;
        pill.title = `Magpie 网关：${health.magpie.base_url}`;
      } else {
        dot.className = "dot bad";
        label.textContent = "Magpie 未连接";
        pill.title = `无法连接 ${health.magpie.base_url}`;
      }
      return health;
    } catch (error) {
      dot.className = "dot bad";
      label.textContent = "服务异常";
      return null;
    }
  }

  window.QQ = {
    api,
    url,
    appPath,
    toLogin,
    BASE,
    withKey,
    loadAccess,
    openInvite,
    access: () => access,
    escapeHtml,
    renderMarkdown,
    isDark,
    setTheme,
    bindThemeToggle,
    relativeTime,
    readerUrl,
    loadGatewayStatus,
    STATUS_TEXT,
    PIPELINE_TEXT,
  };

  document.addEventListener("click", (event) => {
    if (event.target.closest("#accessChip")) openInvite();
  });
})();
