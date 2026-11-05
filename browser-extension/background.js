importScripts("paper.js");

const { arxivId, queqiaoUrl, getBase } = globalThis.Queqiao;

async function openFor(url, title, openerTab) {
  const base = await getBase();
  const target = queqiaoUrl(base, arxivId(url), title);
  chrome.tabs.create({ url: target, index: openerTab ? openerTab.index + 1 : undefined });
}

chrome.action.onClicked.addListener((tab) => openFor(tab.url, tab.title, tab));

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({ id: "queqiao-link", title: "用鹊桥翻译这个链接", contexts: ["link"] });
  chrome.contextMenus.create({ id: "queqiao-selection", title: "在鹊桥里搜索“%s”", contexts: ["selection"] });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const base = await getBase();
  if (info.menuItemId === "queqiao-link") {
    const id = arxivId(info.linkUrl);
    chrome.tabs.create({ url: queqiaoUrl(base, id, id ? "" : info.linkUrl), index: tab.index + 1 });
  } else if (info.menuItemId === "queqiao-selection") {
    const text = info.selectionText.trim().replace(/^arxiv:\s*/i, "");
    const id = /^(\d{4}\.\d{4,5}(v\d+)?|[a-z-]+(\.[A-Z]{2})?\/\d{7}(v\d+)?)$/.test(text) ? text : null;
    chrome.tabs.create({ url: queqiaoUrl(base, id, info.selectionText), index: tab.index + 1 });
  }
});
