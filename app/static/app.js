const STORAGE_KEY = "shoppilot.conversation_id";

const messagesEl = document.getElementById("messages");
const formEl = document.getElementById("composer");
const inputEl = document.getElementById("input");
const sendEl = document.getElementById("send");
const statusEl = document.getElementById("status");

let conversationId = localStorage.getItem(STORAGE_KEY) || null;
let busy = false;

function setStatus(text, cls) {
  statusEl.textContent = text;
  statusEl.className = "status" + (cls ? " " + cls : "");
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

async function restoreHistory() {
  if (!conversationId) return;
  try {
    const res = await fetch(
      "/api/history?conversation_id=" + encodeURIComponent(conversationId)
    );
    if (!res.ok) {
      if (res.status === 404) {
        conversationId = null;
        localStorage.removeItem(STORAGE_KEY);
      }
      return;
    }
    const data = await res.json();
    for (const m of data.messages) {
      addRow(m.role === "user" ? "me" : "bot", m.content);
    }
  } catch {
    /* 历史加载失败不影响新对话 */
  }
}

async function send(text) {
  busy = true;
  sendEl.disabled = true;
  setStatus("正在输入…", "busy");

  addRow("me", text);
  const { row, bubble } = addRow("bot");
  bubble.classList.add("streaming");

  let acc = "";
  let failed = false;

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, conversation_id: conversationId }),
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
          conversationId = msg.conversation_id;
          localStorage.setItem(STORAGE_KEY, conversationId);
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

restoreHistory().then(() => {
  if (!messagesEl.children.length) {
    addRow("bot", "您好，我是店铺客服。请问有什么可以帮您？");
  }
  inputEl.focus();
});
