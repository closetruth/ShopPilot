/**
 * 精排微调标注页。
 *
 * 一次拉一页待标条目，本地逐条推进；标完一条立刻 POST，失败则留在原地
 * 并提示——标注是不可重建的劳动，静默丢一条就是白干一条。
 * DOM 全部 createElement 构造，不拼 innerHTML（与 admin_flywheel.js 一致）。
 */

const PAGE_SIZE = 20;

const state = {
  items: [],
  index: 0,
  offset: 0,
  total: 0,
  all: 0,
  labeled: 0,
  busy: false,
};

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) {
    const detail = await res.json().catch(() => ({})).catch(() => ({}));
    throw new Error(detail.detail || "HTTP " + res.status);
  }
  return res.json();
}

async function postJSON(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({})).catch(() => ({}));
    throw new Error(detail.detail || "HTTP " + res.status);
  }
  return res.json();
}

let toastTimer = null;
function toast(message) {
  let node = document.querySelector(".ft-toast");
  if (!node) {
    node = el("div", "ft-toast");
    document.body.append(node);
  }
  node.textContent = message;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.remove(), 2200);
}

function sourceLabel(item) {
  const map = { low_conf: "低置信度", r13: "r13 标注集", both: "低置信度 + r13" };
  return map[item.source] || item.source;
}

function renderProgress() {
  const done = state.labeled;
  const total = state.all;
  const pct = total ? Math.round((done / total) * 100) : 0;
  document.getElementById("ftProgressBar").style.width = pct + "%";
  document.getElementById("ftProgressLabel").textContent =
    `已标 ${done} / ${total}（${pct}%），本页剩余 ${state.total - state.index} 条`;
  document.getElementById("ftProgress").textContent = `${done}/${total}`;
}

function renderQuery(item) {
  const card = document.getElementById("ftQueryCard");
  card.replaceChildren();

  card.append(el("p", "ft-query-text", item.query));

  const meta = el("div", "ft-query-meta");
  meta.append(el("span", null, "来源：" + sourceLabel(item)));
  if (item.low_conf_id) meta.append(el("span", null, "低置信度 #" + item.low_conf_id));
  if (item.expected_faq_id) {
    meta.append(el("span", null, "r13 金标：" + item.expected_faq_id));
  }
  meta.append(el("span", null, "候选 " + (item.candidates || []).length + " 条"));
  // 兜底候选来自库里 `retrieved`，没有排序分。标出来免得标的人以为
  // 第一条就是最相关的
  const hasScore = (item.candidates || []).some((c) => typeof c.score === "number");
  if (!hasScore && (item.candidates || []).length) {
    meta.append(el("span", null, "候选为线上检索原始返回，无排序分"));
  }
  card.append(meta);
}

function renderCandidates(item) {
  const box = document.getElementById("ftCandidates");
  box.replaceChildren();

  const candidates = item.candidates || [];
  if (!candidates.length) {
    box.append(el("p", "ft-empty", "检索没有给出候选。若确实无对应条款，按 0 标「知识库缺失」。"));
    return;
  }

  candidates.forEach((cand, i) => {
    const row = el("div", "ft-cand");
    row.dataset.faqId = cand.faq_id;
    row.dataset.key = String(i + 1);

    row.append(el("span", "ft-cand-key", String(i + 1)));

    const body = el("div", "ft-cand-body");
    const head = el("div", "ft-cand-head", cand.title);
    head.append(el("span", "ft-cand-id", cand.faq_id));
    body.append(head, el("div", "ft-cand-text", cand.content));
    row.append(body);

    row.append(
      el(
        "span",
        "ft-cand-score",
        typeof cand.score === "number" ? cand.score.toFixed(3) : "—"
      )
    );

    row.addEventListener("click", () => {
      selectCandidate(row, cand.faq_id);
    });
    box.append(row);
  });
}

function selectCandidate(row, faqId) {
  document.querySelectorAll(".ft-cand").forEach((node) => {
    node.classList.toggle("is-selected", node === row);
  });
  submit({ label: "doc", faq_id: faqId });
}

function renderItem() {
  const item = state.items[state.index];
  const indexLabel = document.getElementById("ftIndex");
  if (!item) {
    document.getElementById("ftQueryCard").replaceChildren(
      el("p", "ft-empty", "这一页没有待标条目了。")
    );
    document.getElementById("ftCandidates").replaceChildren();
    indexLabel.textContent = "";
    document.getElementById("ftPrev").disabled = state.offset === 0;
    document.getElementById("ftNext").disabled = true;
    return;
  }
  renderQuery(item);
  renderCandidates(item);
  indexLabel.textContent = `${state.index + 1} / ${state.items.length}`;
  document.getElementById("ftPrev").disabled = state.index === 0 && state.offset === 0;
  document.getElementById("ftNext").disabled = state.index >= state.items.length - 1;
}

async function submit(payload) {
  const item = state.items[state.index];
  if (!item || state.busy) return;
  state.busy = true;
  try {
    const body = {
      query: item.query,
      label: payload.label,
      faq_id: payload.faq_id || null,
      low_conf_id: item.low_conf_id || null,
    };
    const result = await postJSON("/api/admin/finetune/labels", body);
    state.labeled = result.stats.total;
    toast(payload.label === "doc" ? "已标为命中条款" : payload.label === "missing" ? "已标为知识库缺失" : "已标为噪声");
    advance();
  } catch (err) {
    toast("提交失败：" + err.message);
  } finally {
    state.busy = false;
  }
}

function advance() {
  if (state.index < state.items.length - 1) {
    state.index += 1;
    renderProgress();
    renderItem();
  } else {
    state.offset += PAGE_SIZE;
    load();
  }
}

function skip() {
  if (state.index < state.items.length - 1) {
    state.index += 1;
    renderItem();
  }
}

async function load() {
  try {
    const data = await getJSON(
      `/api/admin/finetune/labels/pending?limit=${PAGE_SIZE}&offset=${state.offset}`
    );
    state.items = data.items;
    state.total = data.total;
    state.all = data.all;
    state.labeled = data.labeled;
    state.index = 0;
    renderProgress();
    renderItem();
  } catch (err) {
    document.getElementById("ftQueryCard").replaceChildren(
      el("p", "ft-empty", "加载失败：" + err.message)
    );
    document.getElementById("ftCandidates").replaceChildren();
  }
}

function bindKeyboard() {
  document.addEventListener("keydown", (event) => {
    if (event.target.tagName === "INPUT" || event.target.tagName === "TEXTAREA") return;
    const item = state.items[state.index];
    if (!item) return;

    const key = event.key.toLowerCase();
    if (key >= "1" && key <= "5") {
      const row = document.querySelector(`.ft-cand[data-key="${key}"]`);
      if (row) {
        event.preventDefault();
        selectCandidate(row, row.dataset.faqId);
      }
      return;
    }
    if (key === "0") {
      event.preventDefault();
      submit({ label: "missing" });
      return;
    }
    if (key === "x") {
      event.preventDefault();
      submit({ label: "noise" });
      return;
    }
    if (key === "arrowdown") {
      event.preventDefault();
      skip();
    }
    if (key === "arrowup") {
      event.preventDefault();
      if (state.index > 0) {
        state.index -= 1;
        renderItem();
      }
    }
  });
}

document.getElementById("ftPrev").addEventListener("click", () => {
  if (state.index > 0) {
    state.index -= 1;
    renderItem();
  } else if (state.offset > 0) {
    state.offset -= PAGE_SIZE;
    load();
  }
});
document.getElementById("ftNext").addEventListener("click", skip);
document.getElementById("ftSkip").addEventListener("click", skip);

const SOURCE_LABELS = {
  human: "人工标注",
  trace_rewrite: "轨迹改写",
  synonym: "同义改写",
  hard_negative: "困难负例",
  random_negative: "随机负例",
  noise_negative: "噪声负例",
};

const LABEL_LABELS = { doc: "命中条款", missing: "知识库缺失", noise: "噪声" };

function renderBuild(stats) {
  const box = document.getElementById("ftBuildResult");
  box.replaceChildren();

  const gate = el("span", "ft-gate " + (stats.gate === "pass" ? "is-pass" : "is-blocked"),
    stats.gate === "pass"
      ? `门禁通过（正样本 ${stats.positive}）`
      : `门禁未过：正样本 ${stats.positive}，需 ${stats.gate_min_positive}`);
  box.append(gate);

  const table = el("table", "ft-build-table");
  const head = el("tr");
  ["分集", "问题数", "训练对数"].forEach((t) => head.append(el("th", null, t)));
  table.append(head);
  ["train", "dev", "test"].forEach((name) => {
    const tr = el("tr");
    tr.append(
      el("td", null, name),
      el("td", null, String(stats.splits[name])),
      el("td", null, String(stats.pairs[name]))
    );
    table.append(tr);
  });
  box.append(table);

  const negs = stats.augment.hard_neg + stats.augment.random_neg;
  const poolLine = el("p", "ft-hint",
    `训练负例 ${negs} 条，评估池 ${stats.pool} 条`
    + (negs === 0 ? "（没有负例，模型只会学到「全都相关」）" : "")
    + (stats.pool < 2 ? "（评估池过小，排序指标不可信）" : "")
  );
  box.append(poolLine);

  const augHead = el("tr");
  ["来源", "条数"].forEach((t) => augHead.append(el("th", null, t)));
  const augTable = el("table", "ft-build-table");
  augTable.append(augHead);
  Object.entries(stats.augment).forEach(([key, value]) => {
    const tr = el("tr");
    tr.append(el("td", null, SOURCE_LABELS[key] || key), el("td", null, String(value)));
    augTable.append(tr);
  });
  box.append(el("h3", null, "训练集来源"), augTable);

  const catHead = el("tr");
  ["类别", ...Object.values(LABEL_LABELS)].forEach((t) => catHead.append(el("th", null, t)));
  const catTable = el("table", "ft-build-table");
  catTable.append(catHead);
  Object.entries(stats.by_category).forEach(([category, counts]) => {
    const tr = el("tr");
    tr.append(el("td", null, category));
    Object.keys(LABEL_LABELS).forEach((key) => {
      tr.append(el("td", null, String(counts[key] || 0)));
    });
    catTable.append(tr);
  });
  box.append(el("h3", null, "分层分布"), catTable);
}

document.getElementById("ftBuild").addEventListener("click", async () => {
  const button = document.getElementById("ftBuild");
  button.disabled = true;
  button.textContent = "生成中…";
  try {
    const result = await postJSON("/api/admin/finetune/datasets/build", {});
    renderBuild(result.stats);
    toast(`已生成数据集 #${result.id}（${result.name}）`);
  } catch (err) {
    toast("生成失败：" + err.message);
  } finally {
    button.disabled = false;
    button.textContent = "生成 train/dev/test";
  }
});

bindKeyboard();
load();

const JOB_STATUS = {
  queued: { label: "排队中", cls: "is-pass" },
  running: { label: "训练中", cls: "is-pass" },
  done: { label: "已完成", cls: "is-pass" },
  failed: { label: "失败", cls: "is-blocked" },
};

let jobTimer = null;
let currentJobId = null;

function renderTrainerState(health) {
  const badge = document.getElementById("ftTrainerState");
  if (!health) {
    badge.className = "ft-gate is-blocked";
    badge.textContent = "训练服务不可达";
    return;
  }
  const dev = health.device || {};
  badge.className = "ft-gate is-pass";
  badge.textContent = dev.cuda
    ? `训练服务在线 · GPU ${dev.gpu || ""}`
    : "训练服务在线 · CPU";
  renderGate(health.gate);
}

function renderGate(gate) {
  const hint = document.getElementById("ftGateHint");
  const button = document.getElementById("ftTrain");
  if (!gate) {
    hint.textContent = "";
    return;
  }
  button.disabled = !gate.ok;
  hint.textContent = gate.ok
    ? `正例 ${gate.train_positives} / 负例 ${gate.train_negatives}，评估池 ${gate.pool} 条`
    : gate.problems.join("；");
}

async function loadTrainer() {
  try {
    renderTrainerState(await getJSON("/api/admin/finetune/trainer"));
  } catch (err) {
    renderTrainerState(null);
    document.getElementById("ftGateHint").textContent = err.message;
  }
}

function readParams() {
  const params = {};
  const epochs = parseInt(document.getElementById("ftEpochs").value, 10);
  const lr = parseFloat(document.getElementById("ftLr").value);
  const maxLength = parseInt(document.getElementById("ftMaxLen").value, 10);
  const batch = parseInt(document.getElementById("ftBatch").value, 10);
  if (epochs) params.epochs = epochs;
  if (lr) params.lr = lr;
  if (maxLength) params.max_length = maxLength;
  if (batch) params.batch_size = batch;
  return params;
}

function metricCell(value) {
  return value === undefined || value === null ? "—" : Number(value).toFixed(4);
}

function renderJob(job) {
  const box = document.getElementById("ftJobResult");
  box.replaceChildren();

  const status = JOB_STATUS[job.status] || { label: job.status, cls: "" };
  const head = el("div", "ft-build-head");
  head.append(
    el("span", "ft-gate " + status.cls, `${status.label} · #${job.id}`),
    el("span", "ft-hint", job.finished_at || job.started_at || "")
  );
  box.append(head);

  const metrics = job.metrics_json || {};
  const best = metrics.best || {};
  const test = metrics.test || {};
  if (best.mrr !== undefined || test.mrr !== undefined) {
    const table = el("table", "ft-build-table");
    const headRow = el("tr");
    ["分集", "MRR", "Top-1", "成对准确率", "随机基线 MRR"].forEach((t) =>
      headRow.append(el("th", null, t))
    );
    table.append(headRow);
    [["dev（最优轮次）", best], ["test", test]].forEach(([name, row]) => {
      const tr = el("tr");
      tr.append(
        el("td", null, name),
        el("td", null, metricCell(row.mrr)),
        el("td", null, metricCell(row.top1)),
        el("td", null, metricCell(row.pair_acc)),
        el("td", null, metricCell(row.random_mrr))
      );
      table.append(tr);
    });
    box.append(table);

    if (test.mrr !== undefined && test.random_mrr !== undefined) {
      const beat = test.mrr > test.random_mrr;
      box.append(
        el("p", beat ? "ft-hint" : "ft-gate is-blocked",
          beat
            ? `test MRR 高于随机基线，模型确实学到了区分信号（版本 ${metrics.version}）`
            : `test MRR 未超过随机基线，这个模型不能发布（版本 ${metrics.version}）`)
      );
    }
  }

  if (job.error) box.append(el("p", "ft-gate is-blocked", job.error));

  const logTail = job.log_tail || [];
  if (logTail.length) {
    box.append(el("pre", "ft-log", logTail.join("\n")));
  }
}

function stopPolling() {
  clearTimeout(jobTimer);
  jobTimer = null;
}

async function pollJob() {
  if (!currentJobId) return;
  try {
    const job = await getJSON(
      `/api/admin/finetune/jobs/${currentJobId}?tail=60`
    );
    renderJob(job);
    if (job.status === "done" || job.status === "failed") {
      stopPolling();
      currentJobId = null;
      loadTrainer();
      loadModels();
      return;
    }
  } catch (err) {
    renderJob({ id: currentJobId, status: "failed", error: "查询任务失败：" + err.message });
    stopPolling();
    currentJobId = null;
    return;
  }
  jobTimer = setTimeout(pollJob, 2000);
}

document.getElementById("ftTrain").addEventListener("click", async () => {
  const button = document.getElementById("ftTrain");
  button.disabled = true;
  button.textContent = "提交中…";
  try {
    const result = await postJSON("/api/admin/finetune/jobs", readParams());
    currentJobId = result.id;
    toast(`任务已提交 #${result.id}`);
    stopPolling();
    pollJob();
  } catch (err) {
    toast("提交失败：" + err.message);
    loadTrainer();
  } finally {
    button.textContent = "开始训练";
  }
});

function readE2e() {
  const rate = Number(document.getElementById("ftE2eDocHit").value);
  const baseline = Number(document.getElementById("ftE2eBaseline").value);
  if (!document.getElementById("ftE2eDocHit").value || !document.getElementById("ftE2eBaseline").value) {
    return {};
  }
  return { e2e_doc_hit_rate: rate, e2e_baseline: baseline };
}

function gateCell(model) {
  if (!model.eval) {
    return el("span", "ft-gate is-blocked", "未评估");
  }
  if (model.gate && model.gate.ok) {
    return el("span", "ft-gate is-pass", "达标");
  }
  const span = el("span", "ft-gate is-blocked", "未达标");
  span.title = ((model.gate && model.gate.failed) || []).join("；");
  return span;
}

async function loadModels() {
  const box = document.getElementById("ftModels");
  box.replaceChildren();

  let data;
  try {
    data = await getJSON("/api/admin/finetune/models");
  } catch (err) {
    box.append(el("p", "ft-empty", "读模型列表失败：" + err.message));
    return;
  }

  const models = data.models || [];
  if (!models.length) {
    box.append(el("p", "ft-empty", "远端还没有训练产出的模型版本。"));
    return;
  }

  const table = el("table", "ft-build-table");
  const head = el("tr");
  ["版本", "test MRR", "Hit@1", "MRR@3", "回放", "门禁", "训练时间", "操作"].forEach((t) =>
    head.append(el("th", null, t))
  );
  table.append(head);

  models.forEach((model) => {
    const tr = el("tr");
    const best = model.best || {};
    const test = model.test || {};
    const evaluation = model.eval || {};
    const evalTest = evaluation.test || {};
    const replay = evaluation.replay || {};
    const active = model.version === data.active;

    tr.append(
      el("td", null, model.version + (active ? "（活跃）" : "")),
      el("td", null, metricCell(best.mrr)),
      el("td", null, metricCell(evalTest.hit1 ?? test.top1)),
      el("td", null, metricCell(evalTest.mrr3 ?? test.mrr)),
      el("td", null, metricCell(replay.fix_rate)),
      el("td", null, gateCell(model)),
      el("td", null, (model.created_at || "").slice(0, 19).replace("T", " "))
    );

    const cell = el("td");
    const evalButton = el("button", "btn", "评估");
    evalButton.type = "button";
    evalButton.addEventListener("click", async () => {
      evalButton.disabled = true;
      evalButton.textContent = "评估中…";
      try {
        const result = await postJSON(
          `/api/admin/finetune/models/${encodeURIComponent(model.version)}/evaluate`,
          readE2e()
        );
        const gate = result.gate || {};
        toast(
          gate.ok
            ? `评估完成，门禁达标`
            : `评估完成，未达标：${(gate.failed || []).join("；")}`
        );
        loadModels();
      } catch (err) {
        toast("评估失败：" + err.message);
        evalButton.disabled = false;
        evalButton.textContent = "评估";
      }
    });
    cell.append(evalButton);

    if (active) {
      cell.append(el("span", "ft-gate is-pass", "当前活跃"));
    } else {
      const button = el("button", "btn", "设为活跃");
      button.type = "button";
      const passed = !!(model.gate && model.gate.ok);
      // 未达标时置灰而不是允许点击后被 409 拦住：点下去才发现不行，
      // 会让人以为是网络问题而不是指标问题
      button.disabled = !passed;
      button.title = passed ? "" : "三把尺未全部达标，不能激活";
      button.addEventListener("click", async () => {
        button.disabled = true;
        try {
          const result = await postJSON(
            `/api/admin/finetune/models/${encodeURIComponent(model.version)}/activate`,
            {}
          );
          toast(`已切换到 ${result.active}`);
          loadModels();
        } catch (err) {
          toast("切换失败：" + err.message);
          button.disabled = false;
        }
      });
      cell.append(button);
    }
    tr.append(cell);
    table.append(tr);
  });

  box.append(table);
}

document.getElementById("ftRefreshModels").addEventListener("click", loadModels);

loadTrainer();
loadModels();