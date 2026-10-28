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
