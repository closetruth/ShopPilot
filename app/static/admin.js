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
  bulkBtn: document.getElementById("bulkBtn"),
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
  // datalist 也要清空：不清会在每次 refresh 后累积重复的 option
  els.catOptions.replaceChildren();

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

/* ---------- 批量导入 ---------- */

const imp = {
  dialog: document.getElementById("importDialog"),
  step1: document.getElementById("importStep1"),
  step2: document.getElementById("importStep2"),
  heading: document.getElementById("importHeading"),
  text: document.getElementById("importText"),
  title: document.getElementById("importTitle"),
  category: document.getElementById("importCategory"),
  pathMode: document.getElementById("importPathMode"),
  maxChars: document.getElementById("importMaxChars"),
  hint: document.getElementById("importHint"),
  previewHint: document.getElementById("importPreviewHint"),
  previewBtn: document.getElementById("importPreviewBtn"),
  backBtn: document.getElementById("importBackBtn"),
  confirmBtn: document.getElementById("importConfirmBtn"),
  status: document.getElementById("importStatus"),
  confirmStatus: document.getElementById("importConfirmStatus"),
  summary: document.getElementById("importSummary"),
  list: document.getElementById("previewList"),
  close: document.getElementById("importClose"),
};

/** 预览返回的条目；确认导入时从这份数据里挑勾选的那些。 */
let previewChunks = [];

function impHint(el, text, isError = false) {
  if (!text) {
    el.hidden = true;
    return;
  }
  el.textContent = text;
  el.classList.toggle("error", isError);
  el.hidden = false;
}

function openImport() {
  imp.step1.hidden = false;
  imp.step2.hidden = true;
  imp.heading.textContent = "批量导入";
  impHint(imp.hint, "");
  impHint(imp.previewHint, "");
  imp.status.textContent = "";
  imp.confirmStatus.textContent = "";
  if (!imp.category.value && currentCategory) {
    imp.category.value = currentCategory;
  }
  imp.dialog.showModal();
  imp.text.focus();
}

function closeImport() {
  imp.dialog.close();
  previewChunks = [];
}

async function doPreview() {
  const markdown = imp.text.value.trim();
  if (!markdown) {
    impHint(imp.hint, "请先粘贴文档内容", true);
    return;
  }

  imp.previewBtn.disabled = true;
  impHint(imp.hint, "");
  imp.status.textContent = "解析中…";

  try {
    const res = await fetch("/api/admin/faq/bulk/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        markdown,
        title: imp.title.value.trim() || null,
        category: imp.category.value.trim() || null,
        section_mode: imp.pathMode.checked ? "path" : "leaf",
        max_chars: Number(imp.maxChars.value) || 500,
      }),
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
    previewChunks = data.chunks;

    // 标题与分类回填：用户留空时由文档内的 frontmatter/H1 提供
    if (!imp.title.value.trim() && data.title) imp.title.value = data.title;
    if (!imp.category.value.trim() && data.category) imp.category.value = data.category;

    renderPreview(data);
    imp.status.textContent = "";
    imp.step1.hidden = true;
    imp.step2.hidden = false;
  } catch (err) {
    impHint(imp.hint, err.message, true);
    imp.status.textContent = "";
  } finally {
    imp.previewBtn.disabled = false;
  }
}

function renderPreview(data) {
  const s = data.summary;
  const parts = [`共 ${s.chunks} 条`];
  if (s.too_short) parts.push(`${s.too_short} 条不足 20 字`);
  if (s.duplicate_sections.length) {
    parts.push(`${s.duplicate_sections.length} 个重名小节`);
  }
  imp.summary.textContent = parts.join(" · ");

  const notes = [];
  if (s.too_short) {
    notes.push("过短的条目入库会被拒，请在原文补充内容或取消勾选。");
  }
  if (s.duplicate_sections.length) {
    notes.push(
      "重名小节会产生近似重复的向量，可勾选「用小节路径命名」后重新解析。"
    );
  }
  if (data.warnings && data.warnings.length) {
    notes.push(...data.warnings);
  }
  impHint(imp.previewHint, notes.join(" "));

  imp.list.replaceChildren();
  data.chunks.forEach((chunk, i) => {
    imp.list.appendChild(renderPreviewRow(chunk, i));
  });

  updateConfirmLabel();
}

function renderPreviewRow(chunk, index) {
  const row = document.createElement("label");
  row.className = "preview-row";

  const box = document.createElement("input");
  box.type = "checkbox";
  box.className = "preview-check";
  box.checked = chunk.chars >= 20;
  box.dataset.index = String(index);
  box.addEventListener("change", updateConfirmLabel);

  const main = document.createElement("div");
  main.className = "preview-main";

  const head = document.createElement("div");
  head.className = "preview-head";

  const name = document.createElement("span");
  name.className = "preview-section";
  name.textContent = chunk.section;

  const size = document.createElement("span");
  size.className = "preview-size";
  size.textContent = chunk.chars + " 字";

  head.append(name, size);

  if (chunk.kind === "preamble") {
    head.appendChild(makeBadge("节引言", "info"));
  }
  if (chunk.part) {
    head.appendChild(makeBadge(`第 ${chunk.part[0]}/${chunk.part[1]} 段`, "info"));
  }
  if (chunk.chars < 20) {
    head.appendChild(makeBadge("过短", "warn"));
  }

  const excerpt = document.createElement("div");
  excerpt.className = "preview-excerpt";
  excerpt.textContent = chunk.content.slice(0, 100);

  main.append(head, excerpt);
  row.append(box, main);
  return row;
}

function makeBadge(text, kind) {
  const el = document.createElement("span");
  el.className = "preview-badge preview-badge-" + kind;
  el.textContent = text;
  return el;
}

function selectedChunks() {
  const picked = [];
  for (const box of imp.list.querySelectorAll(".preview-check")) {
    if (box.checked) picked.push(previewChunks[Number(box.dataset.index)]);
  }
  return picked;
}

function updateConfirmLabel() {
  const n = selectedChunks().length;
  imp.confirmBtn.textContent = n ? `确认导入 ${n} 条` : "确认导入";
  imp.confirmBtn.disabled = n === 0;
}

async function doImport() {
  const picked = selectedChunks();
  if (!picked.length) return;

  const title = imp.title.value.trim();
  const category = imp.category.value.trim();
  if (title.length < 2) {
    impHint(imp.previewHint, "标题至少 2 个字", true);
    return;
  }
  if (!category) {
    impHint(imp.previewHint, "请填写分类", true);
    return;
  }

  imp.confirmBtn.disabled = true;
  imp.confirmStatus.textContent = "导入中…";

  try {
    const res = await fetch("/api/admin/faq/bulk", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title,
        category,
        items: picked.map((c) => ({ section: c.section, content: c.content })),
      }),
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
    closeImport();
    await refresh();
    setStatus(
      data.indexed < data.total
        ? `已导入 ${data.total} 条，仅 ${data.indexed} 条进入索引，请点「重建索引」`
        : `已导入 ${data.total} 条并写入索引`,
      data.indexed < data.total
    );
  } catch (err) {
    impHint(imp.previewHint, err.message, true);
    imp.confirmStatus.textContent = "";
    imp.confirmBtn.disabled = false;
  }
}

els.bulkBtn.addEventListener("click", openImport);
imp.close.addEventListener("click", closeImport);
imp.previewBtn.addEventListener("click", doPreview);
imp.backBtn.addEventListener("click", () => {
  imp.step1.hidden = false;
  imp.step2.hidden = true;
});
imp.confirmBtn.addEventListener("click", doImport);

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
