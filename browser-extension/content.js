// Adds a “鹊桥翻译” button to arXiv abstract and HTML pages.
(async function () {
  const { arxivId, queqiaoUrl, getBase } = globalThis.Queqiao;
  const meta = document.querySelector('meta[name="citation_arxiv_id"]');
  const id = arxivId(location.href, meta && meta.content);
  if (!id || document.getElementById("queqiao-button")) return;
  const link = document.createElement("a");
  link.id = "queqiao-button";
  link.className = "queqiao-button";
  link.textContent = "鹊桥翻译";
  link.title = "用鹊桥打开中文译文（没有的话会自动开始翻译）";
  link.target = "_blank";
  link.rel = "noopener";
  link.href = queqiaoUrl(await getBase(), id);
  chrome.storage.onChanged.addListener((changes) => {
    if (changes.base) link.href = queqiaoUrl(changes.base.newValue, id);
  });
  const list = document.querySelector(".full-text ul");
  if (list) {
    const item = document.createElement("li");
    link.classList.add("abs-button");
    item.appendChild(link);
    list.insertBefore(item, list.firstChild);
  } else {
    link.classList.add("floating");
    document.body.appendChild(link);
  }
})();
