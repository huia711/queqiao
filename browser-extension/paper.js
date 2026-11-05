// Shared helpers: find an arXiv ID and build Queqiao URLs. Loaded by the content script and the
// service worker (importScripts), so it only touches globalThis.
(function (root) {
  const DEFAULT_BASE = "http://127.0.0.1:8686";
  const NEW_ID = /\d{4}\.\d{4,5}(v\d+)?/;
  const OLD_ID = /[a-z-]+(\.[A-Z]{2})?\/\d{7}(v\d+)?/;

  function arxivId(url, metaId) {
    if (metaId) return metaId.trim();
    if (!url) return null;
    let host = "";
    try {
      host = new URL(url).hostname;
    } catch (error) {
      return null;
    }
    const match = url.match(NEW_ID) || (/arxiv/i.test(host) ? url.match(OLD_ID) : null);
    return match ? match[0] : null;
  }

  function cleanTitle(title) {
    return (title || "").replace(/\s*[|\-–—]\s*(OpenReview|arXiv|alphaXiv)\s*$/i, "").trim();
  }

  function queqiaoUrl(base, id, title) {
    const root = (base || DEFAULT_BASE).replace(/\/+$/, "");
    if (id) return `${root}/arxiv/${id}`;
    return `${root}/?q=${encodeURIComponent(cleanTitle(title))}`;
  }

  async function getBase() {
    const { base } = await chrome.storage.sync.get({ base: DEFAULT_BASE });
    return base || DEFAULT_BASE;
  }

  root.Queqiao = { DEFAULT_BASE, arxivId, cleanTitle, queqiaoUrl, getBase };
})(globalThis);
