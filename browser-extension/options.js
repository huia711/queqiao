(async function () {
  const input = document.getElementById("base");
  const status = document.getElementById("status");
  input.value = await globalThis.Queqiao.getBase();
  document.querySelectorAll("[data-base]").forEach((button) =>
    button.addEventListener("click", () => {
      input.value = button.dataset.base;
    })
  );
  document.getElementById("save").addEventListener("click", async () => {
    const value = input.value.trim().replace(/\/+$/, "");
    if (!/^https?:\/\//.test(value)) {
      status.textContent = "地址需要以 http:// 或 https:// 开头";
      return;
    }
    await chrome.storage.sync.set({ base: value });
    status.textContent = "已保存";
  });
})();
