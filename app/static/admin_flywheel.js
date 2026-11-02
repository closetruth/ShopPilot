/**
 * 数据飞轮看板。
 *
 * 四个区块各自独立加载：任一接口失败只在对应区块显示「加载失败」，
 * 其余区块照常展示。DOM 全部用 createElement 构造，不拼 innerHTML。
 */

const API = {
  feedback: "/api/admin/flywheel/feedback?days=7&limit=20",
  cost: "/api/admin/flywheel/cost?days=7",
  runs: "/api/admin/eval/runs?limit=10",
  cases: "/api/admin/eval/cases?status=candidate&limit=20",
};

const REASON_LABELS = {
  irrelevant: "答非所问",
  inaccurate: "信息不准确",
  unsolved: "没解决",
  verbose: "太啰嗦",
  other: "其他",
  unlabeled: "未标原因",
};

async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error("HTTP " + res.status);
  return res.json();
}

async function postJSON(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (!res.ok) throw new Error("HTTP " + res.status);
  return res.json();
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function kpi(label, value) {
  const box = el("div", "fw-kpi-item");
  box.append(el("strong", null, String(value)), el("span", null, label));
  return box;
}

function cell(text) {
  const td = document.createElement("td");
  td.textContent = text == null || text === "" ? "—" : String(text);
  return td;
}

function table(headers) {
  const t = el("table", "fw-table");
  const tr = document.createElement("tr");
  for (const h of headers) tr.appendChild(el("th", null, h));
  t.appendChild(tr);
  return t;
}

function fail(host) {
  host.replaceChildren(el("p", "fw-empty", "加载失败"));
}

function yuan(micros) {
  return "¥" + ((micros || 0) / 1e6).toFixed(4);
}

function formatTime(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso || "—";
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

async function loadFeedback() {
  const host = document.getElementById("fwFeedback");
  try {
    const data = await getJSON(API.feedback);
    const kpis = el("div", "fw-kpi");
    kpis.append(
      kpi("赞", data.positive),
      kpi("踩", data.negative),
      kpi("踩占比", (data.negative_rate * 100).toFixed(1) + "%")
    );
    host.replaceChildren(kpis);

    const reasons = Object.entries(data.by_reason || {});
    if (reasons.length) {
      const max = Math.max(1, ...reasons.map(([, n]) => n));
      for (const [reason, n] of reasons) {
        const row = el("div", "fw-bar");
        const fill = el("div", "fw-bar-fill");
        fill.style.width = Math.round((n / max) * 100) + "%";
        row.append(
          el("span", null, REASON_LABELS[reason] || reason),
          fill,
          el("span", null, n)
        );
        host.appendChild(row);
      }
    }

    if (!data.items.length) {
      host.appendChild(el("p", "fw-empty", "暂无负反馈"));
      return;
    }

    const t = table(["时间", "提问", "原因", "补充", "操作"]);
    for (const it of data.items) {
      const tr = document.createElement("tr");
      tr.append(
        cell(formatTime(it.created_at)),
        cell(it.query),
        cell(REASON_LABELS[it.reason] || it.reason),
        cell(it.comment)
      );
      const op = document.createElement("td");
      const btn = el("button", "btn btn-link", "转候选");
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        try {
          await postJSON("/api/admin/eval/cases/from-feedback/" + it.id);
          btn.textContent = "已转候选";
          loadCases();
        } catch {
          btn.disabled = false;
          btn.textContent = "转候选失败";
        }
      });
      op.appendChild(btn);
      tr.appendChild(op);
      t.appendChild(tr);
    }
    host.appendChild(t);
  } catch {
    fail(host);
  }
}

async function loadCost() {
  const host = document.getElementById("fwCost");
  try {
    const data = await getJSON(API.cost);
    const kpis = el("div", "fw-kpi");
    kpis.append(
      kpi("今日 token", data.today.tokens),
      kpi("今日成本", yuan(data.today.cost_micros)),
      kpi(
        "触闸（全局/会话/单轮）",
        [
          data.budget_events.global,
          data.budget_events.conversation,
          data.budget_events.turn,
        ].join(" / ")
      )
    );
    host.replaceChildren(kpis);

    if (!data.price_configured) {
      host.appendChild(el("p", "fw-empty", "未配置价目，成本恒为 0"));
    }

    if (data.by_node.length) {
      host.appendChild(el("h3", null, "按节点"));
      const max = Math.max(1, ...data.by_node.map((n) => n.tokens));
      for (const node of data.by_node) {
        const row = el("div", "fw-bar");
        const fill = el("div", "fw-bar-fill");
        fill.style.width = Math.round((node.tokens / max) * 100) + "%";
        row.append(el("span", null, node.node), fill, el("span", null, node.tokens));
        host.appendChild(row);
      }
    }

    if (data.by_source.length) {
      host.appendChild(el("h3", null, "按来源"));
      const max = Math.max(1, ...data.by_source.map((n) => n.tokens));
      for (const src of data.by_source) {
        const row = el("div", "fw-bar");
        const fill = el("div", "fw-bar-fill");
        fill.style.width = Math.round((src.tokens / max) * 100) + "%";
        row.append(
          el("span", null, src.source),
          fill,
          el("span", null, src.tokens + " / " + yuan(src.cost_micros))
        );
        host.appendChild(row);
      }
    }

    if (!data.by_day.length) {
      host.appendChild(el("p", "fw-empty", "暂无用量记录"));
      return;
    }
    const t = table(["日期", "token", "成本"]);
    for (const day of data.by_day) {
      const tr = document.createElement("tr");
      tr.append(cell(day.day), cell(day.tokens), cell(yuan(day.cost_micros)));
      t.appendChild(tr);
    }
    host.appendChild(t);
  } catch {
    fail(host);
  }
}

async function loadEval() {
  const host = document.getElementById("fwEval");
  try {
    const data = await getJSON(API.runs);
    const runBtn = el("button", "btn btn-primary", "立即跑评估");
    runBtn.addEventListener("click", async () => {
      const raw = prompt("本次评估样本数（1–50）", "30");
      if (raw === null) return;
      const limit = Number(raw);
      if (!Number.isInteger(limit) || limit < 1 || limit > 50) {
        alert("样本数需为 1–50 的整数");
        return;
      }
      runBtn.disabled = true;
      try {
        await postJSON("/api/admin/eval/run", { limit });
        await pollRuns();
      } catch (err) {
        alert("触发失败：" + err.message);
      } finally {
        runBtn.disabled = false;
        loadEval();
      }
    });
    host.replaceChildren(runBtn);

    if (!data.items.length) {
      host.appendChild(el("p", "fw-empty", "暂无评估记录"));
      return;
    }
    const t = table(["时间", "样本", "pass_rate", "p50", "成本", "状态"]);
    for (const run of data.items) {
      const m = run.metrics || {};
      const tr = document.createElement("tr");
      tr.append(
        cell(formatTime(run.started_at)),
        cell(run.sample_size),
        cell(m.pass_rate == null ? null : (m.pass_rate * 100).toFixed(1) + "%"),
        cell(m.latency_p50_ms == null ? null : m.latency_p50_ms + "ms"),
        cell(yuan(m.cost_micros_total)),
        cell(run.status)
      );
      tr.addEventListener("click", () => showRunDetail(host, run.run_id));
      t.appendChild(tr);
    }
    host.appendChild(t);
  } catch {
    fail(host);
  }
}

async function pollRuns() {
  for (let i = 0; i < 20; i++) {
    await new Promise((r) => setTimeout(r, 3000));
    const data = await getJSON(API.runs).catch(() => ({ items: [] }));
    if (data.items[0] && data.items[0].status !== "running") return;
  }
}

async function showRunDetail(host, runId) {
  const box = el("div", "fw-detail");
  box.appendChild(el("h3", null, "失败项"));
  host.appendChild(box);
  try {
    const data = await getJSON("/api/admin/eval/runs/" + runId);
    const failed = data.items.filter((it) => !it.passed);
    if (!failed.length) box.appendChild(el("p", "fw-empty", "本轮无失败项"));
    for (const item of failed) {
      const row = el("div", "fw-bar");
      row.append(
        el("span", null, item.query),
        el("span", null, item.error || "判定未通过")
      );
      const btn = el("button", "btn btn-link", "转候选");
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        try {
          await postJSON("/api/admin/eval/cases/from-item/" + item.id);
          btn.textContent = "已转候选";
          loadCases();
        } catch {
          btn.disabled = false;
        }
      });
      row.appendChild(btn);
      box.appendChild(row);
    }
  } catch {
    box.appendChild(el("p", "fw-empty", "详情加载失败"));
  }
}

async function loadCases() {
  const host = document.getElementById("fwCases");
  try {
    const data = await getJSON(API.cases);
    const input = el("input", "fw-input");
    input.placeholder = "新增样本提问（必填）";
    const addBtn = el("button", "btn", "新增");
    addBtn.addEventListener("click", async () => {
      const query = input.value.trim();
      if (!query) return;
      addBtn.disabled = true;
      try {
        await postJSON("/api/admin/eval/cases", { query });
        input.value = "";
        loadCases();
      } catch {
        alert("新增失败");
      } finally {
        addBtn.disabled = false;
      }
    });
    const bar = el("div", "fw-actions");
    bar.append(input, addBtn);
    host.replaceChildren(bar);

    if (!data.items.length) {
      host.appendChild(el("p", "fw-empty", "暂无候选样本"));
      return;
    }
    const t = table(["提问", "期望意图", "来源", "操作"]);
    for (const c of data.items) {
      const tr = document.createElement("tr");
      tr.append(cell(c.query), cell(c.expected_intent), cell(c.source));
      const op = document.createElement("td");
      for (const [status, label] of [
        ["accepted", "采纳"],
        ["retired", "淘汰"],
      ]) {
        const btn = el("button", "btn btn-link", label);
        btn.addEventListener("click", async () => {
          btn.disabled = true;
          try {
            await postJSON("/api/admin/eval/cases/" + c.id + "/status", { status });
            loadCases();
          } catch {
            btn.disabled = false;
          }
        });
        op.appendChild(btn);
      }
      tr.appendChild(op);
      t.appendChild(tr);
    }
    host.appendChild(t);
  } catch {
    fail(host);
  }
}

function refreshAll() {
  document.getElementById("fwUpdated").textContent =
    "刷新于 " + formatTime(new Date().toISOString());
  loadFeedback();
  loadCost();
  loadEval();
  loadCases();
}

document.getElementById("fwRefresh").addEventListener("click", refreshAll);
refreshAll();