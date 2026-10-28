const STORAGE_KEY = "shoppilot.session";

const messagesEl = document.getElementById("messages");
const formEl = document.getElementById("composer");
const inputEl = document.getElementById("input");
const sendEl = document.getElementById("send");
const statusEl = document.getElementById("status");
const convListEl = document.getElementById("convList");
const newChatEl = document.getElementById("newChat");
const sidebarEl = document.getElementById("sidebar");
const toggleEl = document.getElementById("toggle");
const ordersBtn = document.getElementById("ordersBtn");
const ordersPanel = document.getElementById("ordersPanel");
const ordersList = document.getElementById("ordersList");
const ordersEmpty = document.getElementById("ordersEmpty");
const woList = document.getElementById("woList");
const woEmpty = document.getElementById("woEmpty");
const ordersClose = document.getElementById("ordersClose");
const tabOrders = document.getElementById("tabOrders");
const tabWorkOrders = document.getElementById("tabWorkOrders");
const standaloneBtn = document.getElementById("standaloneBtn");
const standaloneDraftHost = document.getElementById("standaloneDraftHost");

let session = loadSession();
let conversationId = session.conversation_id || null;
let busy = false;

/* ---------- 会话状态 ---------- */

function loadSession() {
  try {
    const raw = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
    if (raw && typeof raw.visitor_id === "string" && raw.visitor_id) {
      return {
        visitor_id: raw.visitor_id,
        conversation_id:
          typeof raw.conversation_id === "string" ? raw.conversation_id : null,
      };
    }
  } catch {
    /* 解析失败按新顾客处理 */
  }
  return { visitor_id: crypto.randomUUID(), conversation_id: null };
}

function saveSession() {
  session.conversation_id = conversationId;
  localStorage.setItem(STORAGE_KEY, JSON.stringify(session));
}

/* ---------- 渲染 ---------- */

function setStatus(text, cls) {
  statusEl.textContent = text;
  statusEl.className = "status" + (cls ? " " + cls : "");
}

function clearMessages() {
  messagesEl.replaceChildren();
}

function addRow(role, text = "") {
  const row = document.createElement("div");
  row.className = "row " + role;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = text;
  row.appendChild(bubble);
  messagesEl.appendChild(row);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return { row, bubble };
}

function setText(bubble, text) {
  bubble.textContent = text;
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function renderGreeting() {
  addRow("bot", "您好，我是店铺客服。请问有什么可以帮您？");
}

/* ---------- 工单 ---------- */

const STATUS_CLASS = {
  待处理: "st-pending",
  处理中: "st-progress",
  已完结: "st-done",
};

function statusBadge(status) {
  const span = document.createElement("span");
  span.className = "wo-status " + (STATUS_CLASS[status] || "");
  span.textContent = status;
  return span;
}

function renderDraftCard(draft) {
  const wrap = document.createElement("div");
  wrap.className = "wo-card";

  const title = document.createElement("div");
  title.className = "wo-card-title";
  title.textContent = "待确认工单";
  wrap.appendChild(title);

  const rows = [
    ["订单号", draft.order_no || "未关联订单"],
    ["问题类型", draft.issue_type],
    ["涉及商品", draft.product || "（未关联具体商品）"],
    ["问题描述", draft.description],
  ];
  for (const [label, value] of rows) {
    const row = document.createElement("div");
    row.className = "wo-row";
    const k = document.createElement("span");
    k.className = "wo-k";
    k.textContent = label;
    const v = document.createElement("span");
    v.className = "wo-v";
    v.textContent = value;
    row.append(k, v);
    wrap.appendChild(row);
  }

  const actions = document.createElement("div");
  actions.className = "wo-actions";
  const confirmBtn = document.createElement("button");
  confirmBtn.type = "button";
  confirmBtn.className = "wo-confirm";
  confirmBtn.textContent = "确认提交";
  const cancelBtn = document.createElement("button");
  cancelBtn.type = "button";
  cancelBtn.className = "wo-cancel";
  cancelBtn.textContent = "取消";
  actions.append(confirmBtn, cancelBtn);
  wrap.appendChild(actions);

  confirmBtn.addEventListener("click", async () => {
    confirmBtn.disabled = true;
    cancelBtn.disabled = true;
    try {
      const res = await fetch("/api/work-orders", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          draft_id: draft.draft_id,
          visitor_id: session.visitor_id,
        }),
      });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      renderSubmittedCard(wrap, data.work_order, data.reason);
      loadWorkOrders();
    } catch {
      actions.replaceChildren();
      const err = document.createElement("p");
      err.className = "wo-error";
      err.textContent = "提交失败，请稍后重试";
      wrap.appendChild(err);
    }
  });

  cancelBtn.addEventListener("click", () => {
    wrap.remove();
  });

  const row = document.createElement("div");
  row.className = "row bot";
  row.appendChild(wrap);
  messagesEl.appendChild(row);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function renderSubmittedCard(card, order, reason) {
  card.replaceChildren();
  const title = document.createElement("div");
  title.className = "wo-card-title";
  title.textContent = reason === "duplicate" ? "已存在同类工单" : "工单已提交";

  const no = document.createElement("div");
  no.className = "wo-no";
  no.textContent = order.work_order_no;

  const meta = document.createElement("div");
  meta.className = "wo-meta";
  meta.append(statusBadge(order.status));
  const t = document.createElement("span");
  t.textContent = order.order_no
    ? order.issue_type + " · " + order.order_no
    : order.issue_type + " · 未关联订单";
  meta.appendChild(t);

  card.append(title, no, meta);

  const descRow = document.createElement("div");
  descRow.className = "wo-row";
  const dk = document.createElement("span");
  dk.className = "wo-k";
  dk.textContent = "问题描述";
  const dv = document.createElement("span");
  dv.className = "wo-v";
  dv.textContent = order.description;
  descRow.append(dk, dv);
  card.appendChild(descRow);

  if (order.product) {
    const p = document.createElement("div");
    p.className = "wo-row";
    const k = document.createElement("span");
    k.className = "wo-k";
    k.textContent = "涉及商品";
    const v = document.createElement("span");
    v.className = "wo-v";
    v.textContent = order.product;
    p.append(k, v);
    card.appendChild(p);
  }

  const link = document.createElement("button");
  link.type = "button";
  link.className = "wo-link";
  link.textContent = "在「我的工单」中查看";
  link.addEventListener("click", () => openOrdersPanel("workorders"));
  card.appendChild(link);
}

function formatTime(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

const ISSUE_TYPE_OPTIONS = [
  "运输破损",
  "发货延迟",
  "质量问题",
  "退换货",
  "咨询",
  "投诉建议",
  "其他",
];

// 这几类不需要订单号：投诉服务、咨询政策本身与某笔订单无关
const ORDERLESS_ISSUE_TYPES = ["咨询", "投诉建议", "其他"];

function fillIssueTypes(select) {
  for (const t of ISSUE_TYPE_OPTIONS) {
    const opt = document.createElement("option");
    opt.value = t;
    opt.textContent = ORDERLESS_ISSUE_TYPES.includes(t) ? t + "（无需订单）" : t;
    select.appendChild(opt);
  }
}

function buildDraftForm({ orderNo, onDone, onCancel }) {
  const form = document.createElement("form");
  form.className = "draft-form";

  const typeSel = document.createElement("select");
  typeSel.className = "draft-input";
  fillIssueTypes(typeSel);
  if (!orderNo) {
    typeSel.value = "咨询";
  }

  const desc = document.createElement("textarea");
  desc.className = "draft-input";
  desc.rows = 3;
  desc.placeholder = "请描述具体情况（至少 10 字），如现象、时间、部位";
  desc.maxLength = 500;

  const hint = document.createElement("p");
  hint.className = "draft-hint";
  hint.textContent = "";

  const actions = document.createElement("div");
  actions.className = "wo-actions";
  const submit = document.createElement("button");
  submit.type = "submit";
  submit.className = "wo-confirm";
  submit.textContent = "提交工单";
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "wo-cancel";
  cancel.textContent = "取消";
  actions.append(submit, cancel);

  form.append(typeSel, desc, hint, actions);

  cancel.addEventListener("click", onCancel);

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = desc.value.trim();
    if (text.length < 10) {
      hint.textContent = "请至少填写 10 个字，说明具体现象";
      return;
    }
    const type = typeSel.value;
    const needsOrder = !ORDERLESS_ISSUE_TYPES.includes(type);
    if (!orderNo && needsOrder) {
      hint.textContent = "「" + type + "」必须关联订单，请从订单列表中选择";
      return;
    }

    submit.disabled = true;
    cancel.disabled = true;
    hint.textContent = "正在提交…";
    try {
      const draftRes = await fetch("/api/work-orders/drafts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          visitor_id: session.visitor_id,
          order_no: orderNo || null,
          issue_type: type,
          description: text,
        }),
      });
      if (!draftRes.ok) {
        const err = await draftRes.json().catch(() => ({}));
        throw new Error(err.detail || "HTTP " + draftRes.status);
      }
      const draft = await draftRes.json();

      if (draft.duplicate) {
        onDone({
          duplicate: true,
          existing_work_order_no: draft.existing_work_order_no,
          existing_status: draft.existing_status,
        });
        return;
      }

      const res = await fetch("/api/work-orders", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          draft_id: draft.draft_id,
          visitor_id: session.visitor_id,
        }),
      });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      onDone(data);
      loadOrders();
      loadWorkOrders();
    } catch (err) {
      submit.disabled = false;
      cancel.disabled = false;
      hint.textContent = err.message || "提交失败，请稍后重试";
    }
  });

  desc.focus();
  return { form, desc, typeSel };
}

function renderOrderItem(order) {
  const item = document.createElement("div");
  item.className = "order-item";

  const top = document.createElement("div");
  top.className = "wo-item-top";
  const no = document.createElement("span");
  no.className = "wo-no";
  no.textContent = order.order_no;
  top.appendChild(no);

  const meta = document.createElement("div");
  meta.className = "wo-item-meta";
  meta.textContent =
    (order.product || "（未知商品）") +
    (order.quantity > 1 ? " ×" + order.quantity : "") +
    " · ¥" + order.amount_yuan.toFixed(2);

  const time = document.createElement("div");
  time.className = "wo-item-time";
  time.textContent = "下单于 " + formatTime(order.created_at);

  item.append(top, meta, time);

  if (order.work_orders.length) {
    const tags = document.createElement("div");
    tags.className = "wo-tags";
    for (const wo of order.work_orders) {
      const tag = document.createElement("span");
      tag.className = "wo-tag";
      tag.append(statusBadge(wo.status));
      const t = document.createElement("span");
      t.textContent = wo.issue_type;
      tag.appendChild(t);
      tags.appendChild(tag);
    }
    item.appendChild(tags);
  }

  const actions = document.createElement("div");
  actions.className = "wo-actions";
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "wo-confirm";
  btn.textContent = "就此单提交工单";
  btn.addEventListener("click", () => openDraftForm(item, order));
  actions.appendChild(btn);
  item.appendChild(actions);

  return item;
}

function openDraftForm(item, order) {
  if (item.querySelector(".draft-form")) return;

  const { form } = buildDraftForm({
    orderNo: order.order_no,
    onCancel: () => form.remove(),
    onDone: (data) => {
      form.remove();
      item.appendChild(resultBlock(data));
    },
  });

  item.appendChild(form);
}

function resultBlock(data) {
  const done = document.createElement("div");
  done.className = "order-done";
  if (data.duplicate) {
    const t = document.createElement("span");
    t.textContent =
      "该订单已有同类工单 " +
      data.existing_work_order_no +
      "（" +
      data.existing_status +
      "），不重复提交";
    done.appendChild(t);
    return done;
  }
  done.append(statusBadge(data.work_order.status));
  const t = document.createElement("span");
  t.textContent =
    (data.reason === "duplicate" ? "已有同类工单 " : "已提交 ") +
    data.work_order.work_order_no;
  done.appendChild(t);
  return done;
}

function openStandaloneForm() {
  const host = document.getElementById("standaloneDraftHost");
  if (host.querySelector(".draft-form")) return;

  const { form } = buildDraftForm({
    orderNo: null,
    onCancel: () => form.remove(),
    onDone: (data) => {
      form.remove();
      host.appendChild(resultBlock(data));
    },
  });

  host.appendChild(form);
  host.scrollIntoView({ block: "nearest" });
}

async function loadOrders() {
  try {
    const res = await fetch(
      "/api/orders?visitor_id=" + encodeURIComponent(session.visitor_id)
    );
    if (!res.ok) return;
    const data = await res.json();
    const list = data.orders || [];
    ordersList.replaceChildren();
    ordersEmpty.hidden = list.length > 0;
    for (const order of list) {
      ordersList.appendChild(renderOrderItem(order));
    }
  } catch {
    /* 面板加载失败不影响对话 */
  }
}

async function loadWorkOrders() {
  try {
    const res = await fetch(
      "/api/work-orders?visitor_id=" + encodeURIComponent(session.visitor_id)
    );
    if (!res.ok) return;
    const data = await res.json();
    const list = data.work_orders || [];
    woList.replaceChildren();
    woEmpty.hidden = list.length > 0;
    for (const order of list) {
      woList.appendChild(renderOrderItemForWorkOrder(order));
    }
  } catch {
    /* 面板加载失败不影响对话 */
  }
}

function renderOrderItemForWorkOrder(order) {
  const item = document.createElement("div");
  item.className = "wo-item";
  item.dataset.no = order.work_order_no;

  const top = document.createElement("div");
  top.className = "wo-item-top";
  const no = document.createElement("span");
  no.className = "wo-no";
  no.textContent = order.work_order_no;
  top.append(no, statusBadge(order.status));

  const meta = document.createElement("div");
  meta.className = "wo-item-meta";
  meta.textContent = order.order_no
    ? order.issue_type + " · " + order.order_no
    : order.issue_type + " · 未关联订单";

  const desc = document.createElement("div");
  desc.className = "wo-item-desc";
  desc.textContent = order.description;

  item.append(top, meta);
  if (order.product) {
    const p = document.createElement("div");
    p.className = "wo-item-meta";
    p.textContent = "涉及商品：" + order.product;
    item.appendChild(p);
  }
  item.appendChild(desc);

  const time = document.createElement("div");
  time.className = "wo-item-time";
  time.textContent = "更新于 " + formatTime(order.updated_at);
  item.appendChild(time);

  return item;
}

function switchPanelTab(tab) {
  const showOrders = tab === "orders";
  tabOrders.classList.toggle("active", showOrders);
  tabWorkOrders.classList.toggle("active", !showOrders);
  ordersList.hidden = !showOrders;
  ordersEmpty.hidden = !showOrders || ordersList.children.length > 0;
  woList.hidden = showOrders;
  woEmpty.hidden = showOrders || woList.children.length > 0;
  standaloneBtn.hidden = !showOrders;
  if (!showOrders) {
    standaloneDraftHost.replaceChildren();
  }
}

async function openOrdersPanel(tab = "orders") {
  ordersPanel.hidden = false;
  await Promise.all([loadOrders(), loadWorkOrders()]);
  switchPanelTab(tab);
}

function closeOrdersPanel() {
  ordersPanel.hidden = true;
}

ordersBtn.addEventListener("click", () => openOrdersPanel());
ordersClose.addEventListener("click", closeOrdersPanel);
tabOrders.addEventListener("click", () => switchPanelTab("orders"));
tabWorkOrders.addEventListener("click", () => switchPanelTab("workorders"));
standaloneBtn.addEventListener("click", openStandaloneForm);

function renderConversations(list) {
  convListEl.replaceChildren();
  if (!list.length) {
    const empty = document.createElement("div");
    empty.className = "conv-empty";
    empty.textContent = "还没有对话记录";
    convListEl.appendChild(empty);
    return;
  }
  for (const c of list) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "conv-item" + (c.id === conversationId ? " active" : "");
    btn.textContent = c.title;
    btn.title = c.title;
    btn.addEventListener("click", () => selectConversation(c.id));
    convListEl.appendChild(btn);
  }
}

async function loadConversations() {
  try {
    const res = await fetch(
      "/api/conversations?visitor_id=" + encodeURIComponent(session.visitor_id)
    );
    if (!res.ok) return;
    const data = await res.json();
    renderConversations(data.conversations);
  } catch {
    /* 列表加载失败不影响当前对话 */
  }
}

async function loadHistory() {
  clearMessages();
  if (!conversationId) {
    renderGreeting();
    return;
  }
  try {
    const res = await fetch(
      "/api/history?conversation_id=" +
        encodeURIComponent(conversationId) +
        "&visitor_id=" +
        encodeURIComponent(session.visitor_id)
    );
    if (res.status === 404) {
      conversationId = null;
      saveSession();
      renderGreeting();
      return;
    }
    if (!res.ok) {
      renderGreeting();
      return;
    }
    const data = await res.json();
    if (!data.messages.length) {
      renderGreeting();
      return;
    }
    for (const m of data.messages) {
      addRow(m.role === "user" ? "me" : "bot", m.content);
    }
  } catch {
    renderGreeting();
  }
}

/* ---------- 交互 ---------- */

async function selectConversation(id) {
  if (busy) return;
  conversationId = id;
  saveSession();
  await loadHistory();
  await loadConversations();
  sidebarEl.classList.remove("open");
  inputEl.focus();
}

function startNewChat() {
  if (busy) return;
  conversationId = null;
  saveSession();
  clearMessages();
  renderGreeting();
  loadConversations();
  sidebarEl.classList.remove("open");
  inputEl.focus();
}

function autoGrow() {
  inputEl.style.height = "auto";
  inputEl.style.height = Math.min(inputEl.scrollHeight, 140) + "px";
}

inputEl.addEventListener("input", autoGrow);
inputEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    formEl.requestSubmit();
  }
});

newChatEl.addEventListener("click", startNewChat);
toggleEl.addEventListener("click", () => sidebarEl.classList.toggle("open"));

async function send(text) {
  busy = true;
  sendEl.disabled = true;
  setStatus("正在输入…", "busy");

  addRow("me", text);
  const { row, bubble } = addRow("bot");
  bubble.classList.add("streaming");

  const toolHint = document.createElement("div");
  toolHint.className = "tool-hint";
  toolHint.hidden = true;
  row.insertBefore(toolHint, bubble);

  let acc = "";
  let failed = false;
  let createdNew = false;

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: text,
        visitor_id: session.visitor_id,
        conversation_id: conversationId,
      }),
    });

    if (!res.ok) throw new Error("HTTP " + res.status);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let idx;
      while ((idx = buffer.indexOf("\n\n")) !== -1) {
        const raw = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);

        const line = raw.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        const payload = line.slice(6);

        if (payload === "[DONE]") continue;

        let msg;
        try {
          msg = JSON.parse(payload);
        } catch {
          continue;
        }

        if (msg.conversation_id) {
          if (!conversationId) createdNew = true;
          conversationId = msg.conversation_id;
          saveSession();
        } else if (msg.work_order_draft) {
          renderDraftCard(msg.work_order_draft);
        } else if (msg.tool) {
          if (msg.status === "start") {
            toolHint.textContent =
              msg.tool === "draft_work_order"
                ? "正在准备工单…"
                : "正在查询" + (msg.label || "") + "…";
            toolHint.hidden = false;
          } else {
            toolHint.hidden = true;
          }
        } else if (msg.delta) {
          acc += msg.delta;
          setText(bubble, acc);
        } else if (msg.error) {
          failed = true;
          row.className = "row bot error";
          setText(bubble, msg.error);
        }
      }
    }
  } catch {
    failed = true;
    row.className = "row bot error";
    setText(bubble, "网络异常，请稍后重试");
  } finally {
    bubble.classList.remove("streaming");
    toolHint.hidden = true;
    if (!acc && !failed) setText(bubble, "（无回复）");
    busy = false;
    sendEl.disabled = false;
    setStatus("在线");
    if (createdNew) loadConversations();
    inputEl.focus();
  }
}

formEl.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = inputEl.value.trim();
  if (!text || busy) return;
  inputEl.value = "";
  autoGrow();
  send(text);
});

/* ---------- 启动 ---------- */

saveSession();
loadConversations();
loadHistory().then(() => inputEl.focus());
