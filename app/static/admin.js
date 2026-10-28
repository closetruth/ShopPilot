const API = "/api/admin/faq";

const els = {
  count: document.getElementById("count"),
  catList: document.getElementById("catList"),
  itemList: document.getElementById("itemList"),
  editorEmpty: document.getElementById("editorEmpty"),
  editorForm: document.getElementById("editorForm"),
  title: document.getElementById("fTitle"),
  section: document.getElementById("fSection"),
  category: document.getElementById("fCategory"),
  content: document.getElementById("fContent"),
  catOptions: document.getElementById("catOptions"),
  contentLen: document.getElementById("contentLen"),
  hint: document.getElementById("editorHint"),
  save: document.getElementById("saveBtn"),
  toggle: document.getElementById("toggleBtn"),
  del: document.getElementById("deleteBtn"),
  status: document.getElementById("editorStatus"),
  newBtn: document.getElementById("newBtn"),
  resyncBtn: document.getElementById("resyncBtn"),
};

/** 当前编辑项。null 表示新建或未选中。 */
let current = null;
let currentCategory = null;
let items = [];

/* ---------- 数据 ---------- */

async function fetchItems() {
  const res = await fetch(API);
  if (!res.ok) throw new Error("HTTP " + res.status);
  const data = await res.json();
  items = data.items || [];
}

async function fetchCategories() {
  const res = await fetch(API + "/categories");
  if (!res.ok) return [];
  const data = await res.json();
  return data.categories || [];
}

/* ---------- 渲染 ---------- */

function setStatus(text, isError = false) {
  els.status.textContent = text;
  els.status.style.color = isError ? "var(--error)" : "var(--muted)";
}

function renderCategories(cats) {
  els.catList.replaceChildren();

  const total = items.length;
  const all = document.createElement("button");
  all.type = "button";
  all.className = "cat-chip" + (currentCategory === null ? " active" : "");
  all.textContent = "全部 " + total;
  all.addEventListener("click", () => {
    currentCategory = null;
    render();
  });
  els.catList.appendChild(all);

  for (const c of cats) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "cat-chip" + (currentCategory === c.category ? " active" : "");
    btn.textContent = c.category + " " + c.count;
    btn.addEventListener("click", () => {
      currentCategory = c.category;
      render();
    });
    els.catList.appendChild(btn);

    const opt = document.createElement("option");
    opt.value = c.category;
    els.catOptions.appendChild(opt);
  }
}

function renderItems() {
  const list = currentCategory
    ? items.filter((i) => i.category === currentCategory)
    : items;

  els.itemList.replaceChildren();
  for (const item of list) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "item" + (item.enabled ? "" : " disabled");
    if (current && current.id === item.id) btn.classList.add("active");

    const title = document.createElement("div");
    title.className = "item-title";
    title.textContent = item.title;

    const sub = document.createElement("div");
    sub.className = "item-sub";
    sub.textContent = item.section + (item.enabled ? "" : " · 已停用");

    btn.append(title, sub);
    btn.addEventListener("click", () => selectItem(item));
    els.itemList.appendChild(btn);
  }

  els.count.textContent = list.length + " 条";
}

function render() {
  renderItems();
}

/* ---------- 编辑 ---------- */

function showHint(text, isError = false) {
  if (!text) {
    els.hint.hidden = true;
    return;
  }
  els.hint.textContent = text;
  els.hint.classList.toggle("error", isError);
  els.hint.hidden = false;
}

function selectItem(item) {
  current = item;
  els.editorEmpty.hidden = true;
  els.editorForm.hidden = false;

  els.title.value = item.title;
  els.section.value = item.section;
  els.category.value = item.category;
  els.content.value = item.content;
  els.contentLen.textContent = String(item.content.length);

  els.toggle.hidden = false;
  els.del.hidden = false;
  els.toggle.textContent = item.enabled ? "停用" : "启用";
  els.toggle.classList.toggle("btn-danger", item.enabled);

  setStatus("");
  showHint("");
  render();
}

function startNew() {
  current = null;
  els.editorEmpty.hidden = true;
  els.editorForm.hidden = false;

  els.title.value = "";
  els.section.value = "";
  els.category.value = currentCategory || "";
  els.content.value = "";
  els.contentLen.textContent = "0";

  els.toggle.hidden = true;
  els.del.hidden = true;

  setStatus("新建");
  showHint("");
  render();
  els.title.focus();
}

/* ---------- 提交 ---------- */

function validate() {
  const title = els.title.value.trim();
  const section = els.section.value.trim();
  const category = els.category.value.trim();
  const content = els.content.value.trim();

  if (title.length < 2) return "标题至少 2 个字";
  if (section.length < 2) return "小节名至少 2 个字";
  if (!category) return "请填写分类";
  if (content.length < 20) return "正文至少 20 个字，太短没有信息量";
  return null;
}

async function save(e) {
  e.preventDefault();
  const problem = validate();
  if (problem) {
    showHint(problem, true);
    return;
  }
  showHint("");

  const payload = {
    title: els.title.value.trim(),
    section: els.section.value.trim(),
    category: els.category.value.trim(),
    content: els.content.value.trim(),
  };

  els.save.disabled = true;
  setStatus("保存中…");

  try {
    const isNew = current === null;
    const url = isNew ? API : API + "/" + current.id;
    const res = await fetch(url, {
      method: isNew ? "POST" : "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      const detail =
        typeof err.detail === "string"
          ? err.detail
          : (err.detail || []).map((d) => d.msg).join("；") || "HTTP " + res.status;
      throw new Error(detail);
    }

    const data = await res.json();
    current = data.item;
    await refresh();
    selectItem(items.find((i) => i.id === current.id) || current);
    setStatus("已保存并同步索引");
  } catch (err) {
    showHint(err.message, true);
    setStatus("保存失败", true);
  } finally {
    els.save.disabled = false;
  }
}

async function toggle() {
  if (!current) return;
  els.toggle.disabled = true;
  setStatus("处理中…");
  try {
    const next = !current.enabled;
    const res = await fetch(
      API + "/" + current.id + "/toggle?enabled=" + next,
      { method: "POST" }
    );
    if (!res.ok) throw new Error("HTTP " + res.status);
    await refresh();
    selectItem(items.find((i) => i.id === current.id));
    setStatus(next ? "已启用并同步索引" : "已停用并移出索引");
  } catch (err) {
    setStatus("操作失败：" + err.message, true);
  } finally {
    els.toggle.disabled = false;
  }
}

async function remove() {
  if (!current) return;
  const ok = confirm(
    "确定删除「" + current.title + " / " + current.section + "」？\n此操作不可恢复，若只是暂时下架请用「停用」。"
  );
  if (!ok) return;

  els.del.disabled = true;
  setStatus("删除中…");
  try {
    const res = await fetch(API + "/" + current.id, { method: "DELETE" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    current = null;
    els.editorForm.hidden = true;
    els.editorEmpty.hidden = false;
    await refresh();
    setStatus("已删除");
  } catch (err) {
    setStatus("删除失败：" + err.message, true);
  } finally {
    els.del.disabled = false;
  }
}

async function resync() {
  els.resyncBtn.disabled = true;
  const original = els.resyncBtn.textContent;
  els.resyncBtn.textContent = "重建中…";
  try {
    const res = await fetch("/api/admin/reindex?drop=true", { method: "POST" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const result = await res.json();
    setStatus("索引已重建：" + result.written + " 条");
    els.resyncBtn.textContent = original;
  } catch (err) {
    setStatus("重建失败：" + err.message, true);
    els.resyncBtn.textContent = original;
  } finally {
    els.resyncBtn.disabled = false;
  }
}

/* ---------- 启动 ---------- */

async function refresh() {
  await fetchItems();
  const cats = await fetchCategories();
  renderCategories(cats);
  render();
}

els.content.addEventListener("input", () => {
  els.contentLen.textContent = String(els.content.value.length);
});

els.editorForm.addEventListener("submit", save);
els.toggle.addEventListener("click", toggle);
els.del.addEventListener("click", remove);
els.newBtn.addEventListener("click", startNew);
els.resyncBtn.addEventListener("click", resync);

refresh().catch((err) => {
  setStatus("加载失败：" + err.message, true);
});
