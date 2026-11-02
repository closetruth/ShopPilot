const PAGE_SIZE = 50;

const state = {
  tool: "",
  status: "",
  offset: 0,
  loading: false,
  exhausted: false,
};

const toolRows = document.getElementById("toolRows");
const auditRows = document.getElementById("auditRows");
const toolCount = document.getElementById("toolCount");
const filterTool = document.getElementById("filterTool");
const filterStatus = document.getElementById("filterStatus");
const moreBtn = document.getElementById("moreBtn");
const refreshBtn = document.getElementById("refreshBtn");

function cell(text) {
  const td = document.createElement("td");
  td.textContent = text == null || text === "" ? "—" : String(text);
  return td;
}

function badgeCell(text, className) {
  const td = document.createElement("td");
  const span = document.createElement("span");
  span.className = "tools-badge " + (className || "");
  span.textContent = text;
  td.appendChild(span);
  return td;
}

function renderTools(items) {
  toolRows.replaceChildren();
  toolCount.textContent = "共 " + items.length + " 个工具";

  for (const item of items) {
    const tr = document.createElement("tr");
    tr.append(
      cell(item.name),
      cell(item.label),
      badgeCell(item.source, "src-" + item.source),
      cell(item.scope),
      badgeCell(item.risk, "risk-" + item.risk),
      cell(item.requires_confirmation ? "是" : "否"),
      cell((item.allowed_callers || []).join(" / ")),
      cell(item.timeout_s + "s")
    );
    toolRows.appendChild(tr);
  }

  const current = filterTool.value;
  filterTool.replaceChildren();
  const all = document.createElement("option");
  all.value = "";
  all.textContent = "全部工具";
  filterTool.appendChild(all);
  for (const item of items) {
    const opt = document.createElement("option");
    opt.value = item.name;
    opt.textContent = item.name;
    filterTool.appendChild(opt);
  }
  filterTool.value = current;
}

function detailText(row) {
  if (row.error) return row.error;
  if (!row.args_json) return "—";
  return JSON.stringify(row.args_json);
}

function renderAudit(items, append) {
  if (!append) auditRows.replaceChildren();
  for (const row of items) {
    const tr = document.createElement("tr");
    const detail = cell(detailText(row));
    detail.className = "tools-detail";
    detail.title = detailText(row);
    tr.append(
      cell(row.created_at ? row.created_at.replace("T", " ").slice(0, 19) : "—"),
      cell(row.tool_name),
      badgeCell(row.source, "src-" + row.source),
      cell(row.caller),
      badgeCell(row.status, "status-" + row.status),
      cell(row.duration_ms == null ? "—" : row.duration_ms + "ms"),
      cell(row.visitor_id),
      detail
    );
    auditRows.appendChild(tr);
  }
}

async function fetchJson(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error("HTTP " + res.status);
  return res.json();
}

async function loadTools() {
  const data = await fetchJson("/api/admin/tools");
  renderTools(data.items || []);
}

async function loadAudit({ append = false } = {}) {
  if (state.loading) return;
  state.loading = true;
  moreBtn.disabled = true;

  const params = new URLSearchParams();
  if (state.tool) params.set("tool", state.tool);
  if (state.status) params.set("status", state.status);
  params.set("limit", String(PAGE_SIZE));
  params.set("offset", String(state.offset));

  try {
    const data = await fetchJson("/api/admin/audit?" + params.toString());
    const items = data.items || [];
    renderAudit(items, append);
    state.offset += items.length;
    state.exhausted = items.length < PAGE_SIZE;
    moreBtn.hidden = state.exhausted;
    if (!items.length && !append) {
      const tr = document.createElement("tr");
      const td = cell("暂无记录");
      td.colSpan = 8;
      tr.appendChild(td);
      auditRows.appendChild(tr);
    }
  } catch (e) {
    const tr = document.createElement("tr");
    const td = cell("加载失败：" + e.message);
    td.colSpan = 8;
    tr.appendChild(td);
    auditRows.replaceChildren(tr);
  } finally {
    state.loading = false;
    moreBtn.disabled = false;
  }
}

function resetAndLoad() {
  state.offset = 0;
  state.exhausted = false;
  loadAudit();
}

filterTool.addEventListener("change", () => {
  state.tool = filterTool.value;
  resetAndLoad();
});

filterStatus.addEventListener("change", () => {
  state.status = filterStatus.value;
  resetAndLoad();
});

moreBtn.addEventListener("click", () => {
  if (!state.exhausted) loadAudit({ append: true });
});

refreshBtn.addEventListener("click", async () => {
  refreshBtn.disabled = true;
  try {
    await loadTools();
    resetAndLoad();
  } finally {
    refreshBtn.disabled = false;
  }
});

(async function init() {
  await loadTools();
  await loadAudit();
})();