(function () {
  "use strict";

  const STATUS_TEXT = { queued: "排队中", running: "翻译中", done: "已完成", failed: "失败" };
  const PIPELINE_TEXT = { latex: "源码翻译", babeldoc: "PDF 版式翻译" };
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

  async function api(path, options) {
    const response = await fetch(url(path), options);
    if (response.status === 401) {
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
    escapeHtml,
    isDark,
    setTheme,
    bindThemeToggle,
    relativeTime,
    readerUrl,
    loadGatewayStatus,
    STATUS_TEXT,
    PIPELINE_TEXT,
  };
})();
