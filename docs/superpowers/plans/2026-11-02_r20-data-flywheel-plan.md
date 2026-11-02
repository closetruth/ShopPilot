# r20 实施计划：数据飞轮闭环

> 设计依据：[2026-11-02_r20-data-flywheel-design.md](../specs/2026-11-02_r20-data-flywheel-design.md)
> 拆成 11 个任务，每个任务都能单独跑通测试并单独提交。任务内步骤是
> 「写测试 → 跑红 → 实现 → 跑绿 → 提交」的固定节奏。

## 全局约束

- 技术栈不变：FastAPI + LangGraph + Milvus Lite + 原生 JS + uv（Python 3.13）。
  不引 ORM、不引图表库、不引调度框架。
- 新增依赖只有 `langfuse>=3,<4`，且**必须是可选依赖**——缺 key 时飞轮照常可用。
- 数据访问走 `app.core.db.connect()`，仓储函数放 `app/repo/`，接口层只做校验与编排。
- 观测类写入（用量、预算事件、轨迹）一律 try/except 兜底：**观测失败不许打断对话**。
- 测试一律 mock LLM 与向量层，不调真模型；MySQL 测试数据用 `uuid` 前缀隔离。
- 代码注释/文档字符串用中文，只写「为什么」，不复述「做了什么」。

## 任务总览

| # | 任务 | 主要交付 | 依赖 |
|---|---|---|---|
| 1 | 消息 id 贯通 | `chats.append_message/load_history`、SSE 推 `message_id` | — |
| 2 | 六张新表 + 反馈仓储与接口 | `db.py` 建表、`repo/feedback.py`、`api/feedback.py` | 1 |
| 3 | 对话页反馈控件 | `app.js` / `style.css` | 2 |
| 4 | 用量计量 | `repo/usage.py`、`observability/usage.py`、接线 | — |
| 5 | 三层成本闸 | `cost/budget.py`、入口拦截、`TURN_TOKEN_LIMIT` | 4 |
| 6 | Langfuse 接入 + 远程 compose | `observability/langfuse.py`、`deploy/langfuse*` | 4 |
| 7 | 评估样本库 | `repo/evals.py`、`api/eval_cases` 路由 | 2 |
| 8 | 评估指标 + runner + CLI | `evals/metrics.py`、`evals/runner.py`、`scripts/eval_pipeline.py` | 4,7 |
| 9 | 评估接口与转候选 | `api/flywheel.py` 全量接口 | 2,7,8 |
| 10 | 飞轮看板 | `admin_flywheel.html/.js` | 9 |
| 11 | 配置收口 + 全量回归 | `.env.example`、既有测试回归 | 全部 |

---

## Task 1：消息 id 贯通

**目的**：反馈要绑到具体某条回答，必须先有稳定的 `message_id`。SSE 在
`[DONE]` 前多推一个事件，历史接口带上 id，前端才有锚点。

**文件**

- 改 `app/repo/chats.py`
- 改 `app/api/customer.py`
- 新增 `tests/test_message_id.py`

**接口**

```python
# app/repo/chats.py
def append_message(conversation_id: str, role: str, content: str) -> int   # 返回新消息 id
def load_history(conversation_id: str, limit: int = 20) -> list[dict]     # 每项含 "id"
```

**Step 1：写失败测试**

```python
"""r20 Task 1：消息 id 必须能从 SSE 与历史接口拿到。"""

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    from langgraph.checkpoint.memory import MemorySaver

    from app.chat.graph import build_graph
    from app.api import _graph as graph_singleton

    graph = build_graph(checkpointer=MemorySaver(), llm=None, llm_quiet=None)
    monkeypatch.setattr(graph_singleton, "_compiled_graph", graph)

    from app.main import app

    with TestClient(app) as c:
        yield c


def _events(body: str) -> list[str]:
    return [line[6:] for line in body.splitlines() if line.startswith("data: ")]


def _chat(client, message: str, visitor_id: str) -> list[str]:
    with client.stream(
        "POST", "/api/chat", json={"message": message, "visitor_id": visitor_id}
    ) as resp:
        body = "".join(resp.iter_text())
    return _events(body)


def test_sse_pushes_message_id_before_done(client):
    events = _chat(client, "你好", "msg-id-v1")
    assert events[0].startswith("{")
    assert json.loads(events[0])["conversation_id"]
    assert events[-1] == "[DONE]"
    assert "message_id" in json.loads(events[-2])


def test_history_items_carry_id(client):
    events = _chat(client, "你好", "msg-id-v2")
    conv_id = json.loads(events[0])["conversation_id"]
    data = client.get(
        "/api/history", params={"conversation_id": conv_id, "visitor_id": "msg-id-v2"}
    ).json()
    assert data["messages"], "这一轮必须落了两条消息"
    assert all(isinstance(m["id"], int) for m in data["messages"])
    assert data["messages"][-1]["role"] == "assistant"
```

**Step 2：跑红**

```powershell
uv run pytest -q tests/test_message_id.py
```

预期：两个用例失败（SSE 里没有 `message_id`；历史项没有 `id`）。

**Step 3：实现**

`app/repo/chats.py`：

```python
def append_message(conversation_id: str, role: str, content: str) -> int:
    """返回新消息 id：反馈要绑到具体某条回答，前端靠它定位。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at)"
            " VALUES (%s, %s, %s, %s)",
            (conversation_id, role, content, now()),
        )
        return cur.lastrowid
```

`load_history` 的 SELECT 加上 `id`，返回项补 `"id": r["id"]`，函数返回类型改
`list[dict]`。

`app/api/customer.py` 的 `stream()` 里：

```python
            assistant_text = "".join(collected).strip()
            if assistant_text:
                assistant_id = store.append_message(
                    conversation_id, "assistant", assistant_text
                )
                saved = True
```

在 `yield _sse("[DONE]")` 之前插入：

```python
            if saved:
                # 反馈锚点：前端拿到 id 才渲染赞踩按钮
                yield _sse({"message_id": assistant_id})
```

`finally` 里的补写也改成接返回值（客户端断开时前端已走，不再推 id）：

```python
            if not saved and collected:
                store.append_message(
                    conversation_id, "assistant", "".join(collected).strip()
                )
```

**Step 4：跑绿**

```powershell
uv run pytest -q tests/test_message_id.py tests/test_chat_stream.py
```

**Step 5：提交**

```powershell
git add app/repo/chats.py app/api/customer.py tests/test_message_id.py
git commit -m "r20: SSE 与历史接口贯通消息 id，为反馈提供锚点"
```

---

## Task 2：六张新表 + 反馈仓储与接口

**目的**：一次把 r20 的 schema 建齐（反馈/用量/预算/评估三张），后面各任务
只写仓储与逻辑，不再动 `db.py`。

**文件**

- 改 `app/core/db.py`
- 新增 `app/repo/feedback.py`
- 改 `app/core/store.py`（re-export）
- 新增 `app/api/feedback.py`，改 `app/main.py`（挂路由）
- 新增 `tests/test_feedback.py`

**接口**

```python
# app/repo/feedback.py
def message_of(message_id: int) -> dict | None        # {id, conversation_id, role, visitor_id}
def upsert(message_id, conversation_id, visitor_id, rating, reason=None, comment=None) -> None
def remove(message_id: int) -> bool
def by_conversation(conversation_id: str) -> list[dict]
def by_id(feedback_id: int) -> dict | None
def stats(days: int = 7) -> dict
def list_negative(days: int = 7, limit: int = 50) -> list[dict]
```

**Step 1：建表**

`app/core/db.py` 的 `SCHEMA`（现有建表字符串集合）末尾追加六段
`CREATE TABLE IF NOT EXISTS`，字段严格照设计文档 §5：

- `message_feedback(id BIGINT AI PK, message_id BIGINT UNIQUE, conversation_id,
  visitor_id, rating TINYINT, reason VARCHAR(32) NULL, comment VARCHAR(500) NULL,
  created_at, updated_at)`，索引 `uk_feedback_message(message_id)`、
  `idx_feedback_conv(conversation_id, created_at)`、`idx_feedback_rating(rating, created_at)`
- `llm_usage(id BIGINT AI PK, conversation_id VARCHAR(32) NULL, visitor_id VARCHAR(64) NULL,
  source VARCHAR(16), node VARCHAR(32), model VARCHAR(64), input_tokens INT,
  output_tokens INT, cost_micros INT, latency_ms INT NULL, created_at)`，
  索引 `idx_usage_conv(conversation_id, created_at)`、`idx_usage_created(created_at)`
- `budget_events(id BIGINT AI PK, scope VARCHAR(16), reason VARCHAR(32),
  conversation_id VARCHAR(32) NULL, visitor_id VARCHAR(64) NULL,
  detail VARCHAR(255) NULL, created_at)`，索引 `idx_budget_created(created_at)`
- `eval_cases(id BIGINT AI PK, query TEXT, expected_intent VARCHAR(32) NULL,
  expected_doc_ids JSON NULL, expected_behavior VARCHAR(16) DEFAULT 'answer',
  source VARCHAR(16), source_ref VARCHAR(64) NULL, status VARCHAR(16) DEFAULT 'candidate',
  note VARCHAR(255) NULL, created_at, updated_at)`，索引 `idx_eval_cases_status(status, id)`
- `eval_runs(run_id VARCHAR(32) PK, sample_size INT, config_json JSON NULL,
  metrics_json JSON NULL, report_path VARCHAR(255) NULL, status VARCHAR(16),
  error VARCHAR(255) NULL, started_at, finished_at NULL)`，索引 `idx_eval_runs_started(started_at)`
- `eval_run_items(id BIGINT AI PK, run_id VARCHAR(32), case_id BIGINT, query TEXT,
  intent VARCHAR(32) NULL, answer MEDIUMTEXT NULL, cited_ids JSON NULL,
  passed TINYINT, refused TINYINT, checks_json JSON NULL, latency_ms INT NULL,
  cost_micros INT NULL, error VARCHAR(255) NULL, created_at)`，索引 `idx_eval_items_run(run_id, id)`

**Step 2：写失败测试**

```python
"""r20 Task 2：反馈落库与接口。"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.core import store
from app.repo import feedback


@pytest.fixture
def visitor():
    return "fb-" + uuid.uuid4().hex[:8]


@pytest.fixture
def conv(visitor):
    conv_id = store.create_conversation(visitor)
    mid = store.append_message(conv_id, "assistant", "退货运费由买家承担")
    return conv_id, mid


def test_schema_created():
    """六张新表都在，且能查（空结果不算失败）。"""
    from app.core.db import connect

    tables = [
        "message_feedback", "llm_usage", "budget_events",
        "eval_cases", "eval_runs", "eval_run_items",
    ]
    with connect() as conn, conn.cursor() as cur:
        for t in tables:
            cur.execute(f"SELECT COUNT(*) AS n FROM {t}")
            assert cur.fetchone()["n"] >= 0


def test_upsert_overwrites_same_message(conv):
    """同一条回答重复提交只留一行——顾客连点不该产生重复记录。"""
    conv_id, mid = conv
    feedback.upsert(mid, conv_id, "v", -1, reason="irrelevant")
    feedback.upsert(mid, conv_id, "v", -1, reason="inaccurate", comment="答错了")
    rows = feedback.by_conversation(conv_id)
    assert len(rows) == 1
    assert rows[0]["reason"] == "inaccurate"
    assert rows[0]["comment"] == "答错了"


def test_remove_on_zero_rating(conv):
    conv_id, mid = conv
    feedback.upsert(mid, conv_id, "v", 1)
    feedback.remove(mid)
    assert feedback.by_conversation(conv_id) == []


def test_api_rejects_foreign_message(conv, visitor):
    """别人的消息不能打反馈，按 404 处理，不泄露存在性。"""
    from app.main import app

    conv_id, mid = conv
    with TestClient(app) as client:
        res = client.post(
            "/api/feedback",
            json={
                "message_id": mid,
                "visitor_id": "someone-else",
                "rating": -1,
            },
        )
    assert res.status_code == 404


def test_api_accepts_rating_without_reason_and_comment(conv, visitor):
    """标签与文字都可选：只给 rating 也必须成功。"""
    from app.main import app

    conv_id, mid = conv
    with TestClient(app) as client:
        res = client.post(
            "/api/feedback",
            json={"message_id": mid, "visitor_id": visitor, "rating": 1},
        )
        assert res.status_code == 200
        assert res.json()["rating"] == 1

        listed = client.get(
            "/api/feedback", params={"conversation_id": conv_id, "visitor_id": visitor}
        ).json()
    assert len(listed["items"]) == 1


def test_api_rejects_unknown_reason(conv, visitor):
    from app.main import app

    conv_id, mid = conv
    with TestClient(app) as client:
        res = client.post(
            "/api/feedback",
            json={"message_id": mid, "visitor_id": visitor, "rating": -1, "reason": "瞎编"},
        )
    assert res.status_code == 422
```

**Step 3：实现仓储**

`app/repo/feedback.py`（要点）：

```python
REASONS = ("irrelevant", "inaccurate", "unsolved", "verbose", "other")


def message_of(message_id: int) -> dict | None:
    """带出消息所属会话与访客，用于校验反馈的归属。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT m.id, m.conversation_id, m.role, c.visitor_id
              FROM messages m JOIN conversations c ON c.id = m.conversation_id
             WHERE m.id = %s
            """,
            (message_id,),
        )
        return cur.fetchone()


def upsert(...):
    """一条回答一行：重复提交覆盖，撤销才删。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO message_feedback
                (message_id, conversation_id, visitor_id, rating, reason, comment,
                 created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                rating = VALUES(rating), reason = VALUES(reason),
                comment = VALUES(comment), updated_at = VALUES(updated_at)
            """,
            (message_id, conversation_id, visitor_id, rating, reason, comment, now(), now()),
        )
```

`list_negative` 用子查询回溯该条回答之前最近一条 user 消息作为 `query`（看板
要展示「顾客问了什么」）：

```sql
SELECT f.id, f.message_id, f.conversation_id, f.visitor_id, f.rating,
       f.reason, f.comment, f.created_at,
       (SELECT m.content FROM messages m
         WHERE m.conversation_id = f.conversation_id
           AND m.id < f.message_id AND m.role = 'user'
         ORDER BY m.id DESC LIMIT 1) AS query
  FROM message_feedback f
 WHERE f.rating = -1 AND f.created_at >= %s
 ORDER BY f.id DESC
 LIMIT %s
```

**Step 4：实现接口**

`app/api/feedback.py`：

```python
class FeedbackRequest(BaseModel):
    message_id: int = Field(gt=0)
    visitor_id: str = Field(min_length=1, max_length=64)
    rating: int = Field(ge=-1, le=1)
    reason: str | None = None
    comment: str | None = Field(default=None, max_length=500)


@router.post("/api/feedback")
async def submit(req: FeedbackRequest) -> dict:
    """标签与文字都是可选的：顾客只想点个赞也必须能提交。"""
    msg = feedback.message_of(req.message_id)
    if not msg or msg["visitor_id"] != req.visitor_id or msg["role"] != "assistant":
        raise HTTPException(status_code=404, detail="消息不存在")
    if req.reason is not None and req.reason not in feedback.REASONS:
        raise HTTPException(status_code=422, detail="未知的反馈标签")
    if req.rating == 0:
        removed = feedback.remove(req.message_id)
        return {"removed": removed}
    feedback.upsert(
        req.message_id, msg["conversation_id"], req.visitor_id,
        req.rating, req.reason, (req.comment or "").strip() or None,
    )
    return {"message_id": req.message_id, "rating": req.rating,
            "reason": req.reason, "comment": req.comment}


@router.get("/api/feedback")
async def list_by_conversation(conversation_id: str, visitor_id: str) -> dict:
    if not store.conversation_owned_by(conversation_id, visitor_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"items": feedback.by_conversation(conversation_id)}
```

`app/main.py` 里 `include_router(feedback_router)`；`app/core/store.py` 增加
`by_id` 等 re-export（或直接 import repo，看现有习惯——按现有做法补全
`__all__` 与赋值行）。

**Step 5：跑绿**

```powershell
uv run pytest -q tests/test_feedback.py
```

**Step 6：提交**

```powershell
git add app/core/db.py app/core/store.py app/repo/feedback.py app/api/feedback.py app/main.py tests/test_feedback.py
git commit -m "r20: 六张新表与反馈采集接口，标签文字均可选"
```

---

## Task 3：对话页反馈控件

**目的**：反馈入口在回答下方，不打断阅读；先提交最小信息（赞/踩），再按需
补标签与文字，每一步都是一次覆盖写。

**文件**

- 改 `app/static/app.js`
- 改 `app/static/style.css`
- 不改后端

**Step 1：改 `send()` 与 `loadHistory()`**

`send()` 里声明 `let messageId = null;`，事件分支加：

```javascript
        } else if (msg.message_id) {
          messageId = msg.message_id;
```

`finally` 中在 `renderCitations` 之后：

```javascript
    if (messageId && !failed) attachFeedback(row, messageId);
```

`loadHistory()` 循环里，bot 行渲染后记录 id 并回填已有反馈：

```javascript
    const feedbackMap = await fetchFeedbackMap(conversationId);
    for (const m of data.messages) {
      const { row, bubble } = addRow(m.role === "user" ? "me" : "bot");
      if (m.role === "user") bubble.textContent = m.content;
      else {
        renderCitations(bubble, m.content);
        if (m.id) attachFeedback(row, m.id, feedbackMap[m.id]);
      }
    }
```

`fetchFeedbackMap` 用现有 `fetch` 风格：

```javascript
async function fetchFeedbackMap(conversationId) {
  const map = {};
  try {
    const res = await fetch(
      "/api/feedback?conversation_id=" +
        encodeURIComponent(conversationId) +
        "&visitor_id=" +
        encodeURIComponent(session.visitor_id)
    );
    if (!res.ok) return map;
    const data = await res.json();
    for (const it of data.items) map[it.message_id] = it;
  } catch {
    /* 回填失败不影响对话 */
  }
  return map;
}
```

**Step 2：新增反馈组件**

```javascript
const FEEDBACK_REASONS = [
  ["irrelevant", "答非所问"],
  ["inaccurate", "信息不准确"],
  ["unsolved", "没解决"],
  ["verbose", "太啰嗦"],
  ["other", "其他"],
];

async function postFeedback(messageId, rating, reason, comment) {
  const res = await fetch("/api/feedback", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      message_id: messageId,
      visitor_id: session.visitor_id,
      rating,
      reason: reason || null,
      comment: comment || null,
    }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || "HTTP " + res.status);
  return data;
}

function attachFeedback(row, messageId, existing) {
  if (row.querySelector(".fb-bar")) return;
  const state = {
    rating: existing ? existing.rating : 0,
    reason: (existing && existing.reason) || "",
    comment: (existing && existing.comment) || "",
  };

  const bar = document.createElement("div");
  bar.className = "fb-bar";
  const up = thumb("👍");
  const down = thumb("👎");
  const note = document.createElement("span");
  note.className = "fb-note";
  const panel = document.createElement("div");
  panel.className = "fb-panel";
  panel.hidden = true;
  bar.append(up, down, note, panel);
  row.appendChild(bar);

  function thumb(glyph) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "fb-btn";
    b.textContent = glyph;
    return b;
  }

  const reasonRow = document.createElement("div");
  reasonRow.className = "fb-reasons";
  const chips = {};
  for (const [code, label] of FEEDBACK_REASONS) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "fb-chip";
    chip.textContent = label;
    chip.addEventListener("click", () => submit({ rating: -1, reason: code }));
    chips[code] = chip;
    reasonRow.appendChild(chip);
  }

  const comment = document.createElement("textarea");
  comment.className = "fb-comment";
  comment.rows = 2;
  comment.maxLength = 500;
  comment.placeholder = "补充说明（可选）";
  comment.value = state.comment;

  const submitBtn = document.createElement("button");
  submitBtn.type = "button";
  submitBtn.className = "fb-chip";
  submitBtn.textContent = "提交";
  submitBtn.addEventListener("click", () =>
    submit({
      rating: -1,
      reason: state.reason || null,
      comment: comment.value.trim() || null,
    })
  );
  panel.append(reasonRow, comment, submitBtn);

  function paint() {
    up.classList.toggle("active", state.rating === 1);
    down.classList.toggle("active", state.rating === -1);
    panel.hidden = state.rating !== -1;
    for (const [code, chip] of Object.entries(chips)) {
      chip.classList.toggle("active", state.reason === code);
    }
    note.textContent = state.rating ? "已收到反馈" : "";
  }

  async function submit(patch) {
    const before = { ...state };
    Object.assign(state, patch);
    paint();
    try {
      if (state.rating === 0) await postFeedback(messageId, 0);
      else await postFeedback(messageId, state.rating, state.reason, state.comment);
      if (state.rating === 0) {
        state.reason = "";
        state.comment = "";
        comment.value = "";
      }
      paint();
    } catch {
      Object.assign(state, before);
      paint();
      note.textContent = "提交失败，请稍后重试";
    }
  }

  up.addEventListener("click", () =>
    submit({ rating: state.rating === 1 ? 0 : 1, reason: null, comment: null })
  );
  down.addEventListener("click", () =>
    submit({ rating: state.rating === -1 ? 0 : -1 })
  );

  paint();
}
```

组件行为约定（实现时逐条对齐设计 §11.1）：

1. 点 👍 → `postFeedback(id, 1, null, null)`；再点一次 → `postFeedback(id, 0)` 撤销
2. 点 👎 → `postFeedback(id, -1, null, null)`，展开 `panel`
3. `panel` 内渲染 5 个标签按钮 + 一行 `textarea` + 「提交」按钮
4. 点标签 → `postFeedback(id, -1, code, state.comment)`，选中态加 `.active`
5. 点「提交」→ `postFeedback(id, -1, state.reason || null, text)`，成功后
   `note.textContent = "已收到反馈"`
6. 点已选中的 👎 → 撤销（rating=0），`panel` 收起、状态清空
7. 请求失败 → `note.textContent = "提交失败，请稍后重试"`，按钮恢复可用

`paint()` 只做 UI 同步：按钮 `.active`、`panel.hidden = state.rating !== -1`、
`note.textContent = state.rating ? "已收到反馈" : ""`。

**Step 3：样式**

`app/static/style.css` 末尾追加（沿用现有变量）：

```css
.fb-bar { display: flex; align-items: center; gap: 6px; margin-top: 6px; }
.fb-btn { border: 1px solid var(--line); background: var(--panel); border-radius: 6px;
  padding: 2px 8px; cursor: pointer; font-size: 13px; opacity: .6; }
.fb-btn:hover { opacity: 1; background: var(--hover); }
.fb-btn.active { opacity: 1; border-color: var(--user); background: #eef3ff; }
.fb-note { font-size: 12px; color: var(--muted); }
.fb-panel { flex-basis: 100%; margin-top: 6px; }
.fb-reasons { display: flex; flex-wrap: wrap; gap: 6px; }
.fb-chip { border: 1px solid var(--line); background: var(--panel); border-radius: 999px;
  padding: 3px 10px; font-size: 12px; cursor: pointer; }
.fb-chip.active { border-color: var(--user); color: var(--user); background: #eef3ff; }
.fb-comment { width: 100%; margin-top: 6px; padding: 6px 8px; font: inherit; font-size: 13px;
  border: 1px solid var(--line); border-radius: 8px; resize: vertical; }
```

**Step 4：手动验收**（前端无测试框架，按设计 §11.1 逐条走）

1. `uv run uvicorn app.main:app --reload`，开对话页发一条知识类问题
2. 回答下方出现 👍/👎；点 👎 → 出现标签与文字框
3. 选「答非所问」→ 刷新页面，标签仍为选中态（回填生效）
4. 补一句文字提交 → 再刷新，文字仍在
5. 同方向再点一次 → 控件清空、`message_feedback` 该行被删
6. 断网状态下点 👍 → 出现「提交失败」，不出现 JS 报错

**Step 5：提交**

```powershell
git add app/static/app.js app/static/style.css
git commit -m "r20: 对话页反馈控件，标签文字均可选"
```

---

## Task 4：用量计量

**目的**：每次模型调用落一行 `llm_usage`，`node` 归因来自 LangGraph 注入的
`langgraph_node`，`source` 由调用方声明。这是成本看板与三层闸的数据基础。

**文件**

- 新增 `app/repo/usage.py`
- 新增 `app/observability/usage.py`（`app/observability/__init__.py` 空文件）
- 改 `app/api/customer.py`（chat 接线）
- 改 `app/context/summarizer.py`（source=bg）
- 新增 `tests/test_usage.py`

**接口**

```python
# app/repo/usage.py
def record(conversation_id, visitor_id, source, node, model,
           input_tokens, output_tokens, cost_micros, latency_ms=None) -> None
def sum_tokens_since(since, conversation_id=None, source=None) -> int
def sum_cost_since(since, source=None) -> int
def by_node(since) -> list[dict]
def by_source(since) -> list[dict]
def by_day(since) -> list[dict]
def by_conversation(conversation_id) -> list[dict]

# app/observability/usage.py
def price_input() -> float
def price_output() -> float
def price_configured() -> bool
def cost_micros(input_tokens: int, output_tokens: int) -> int
def build_usage_handler(*, conversation_id=None, visitor_id=None, source="chat") -> UsageCallbackHandler
```

**Step 1：写失败测试**

```python
"""r20 Task 4：用量计量与归因。

关键不确定性是「回调能不能传进节点内的 LLM 调用」，所以用真
BaseChatModel 替身（GenericFakeChatModel）跑真图，验证 langgraph_node 归因。
"""

import uuid

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver

from app.chat.graph import build_graph
from app.core import store
from app.observability import usage as obs
from app.repo import usage as usage_repo


def _fake_llm():
    """真 Runnable，才有 on_llm_end 回调；带 usage_metadata。"""
    msgs = [
        AIMessage(
            content="退回运费由买家承担 [P-RET-001-3]",
            usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        )
        for _ in range(20)
    ]
    return GenericFakeChatModel(messages=iter(msgs))


def test_cost_micros_formula(monkeypatch):
    """价目是元/百万 token，tokens × 价目 直接等于微元。"""
    monkeypatch.setenv("LLM_PRICE_INPUT_PER_MTOK", "2")
    monkeypatch.setenv("LLM_PRICE_OUTPUT_PER_MTOK", "4")
    assert obs.cost_micros(1_000_000, 0) == 2_000_000
    assert obs.cost_micros(0, 1_000_000) == 4_000_000
    assert obs.cost_micros(100, 20) == 280


@pytest.mark.anyio
async def test_usage_attributed_to_graph_node(monkeypatch):
    from app.retrieval import vector_store

    monkeypatch.setattr(
        vector_store, "search",
        lambda query, visitor_id, top_k=3: [
            {"source_id": "P-RET-001-3", "score": 0.9, "text": "退回运费由买家承担"}
        ],
    )
    visitor = "usage-" + uuid.uuid4().hex[:8]
    conv = store.create_conversation(visitor)
    handler = obs.build_usage_handler(conversation_id=conv, visitor_id=visitor)

    graph = build_graph(
        checkpointer=MemorySaver(), llm=_fake_llm(), llm_quiet=_fake_llm()
    )
    await graph.ainvoke(
        {"message": "退货运费谁出", "history": [], "visitor_id": visitor},
        config={"configurable": {"thread_id": conv}, "callbacks": [handler]},
    )

    rows = usage_repo.by_conversation(conv)
    assert rows, "必须落至少一行用量"
    assert any(r["node"] == "knowledge" for r in rows)
    assert all(r["source"] == "chat" for r in rows)
    assert rows[0]["input_tokens"] == 100


@pytest.mark.anyio
async def test_missing_usage_does_not_write(monkeypatch):
    """模型不报用量时跳过落库，不能写 0 行污染统计。"""
    visitor = "usage-" + uuid.uuid4().hex[:8]
    conv = store.create_conversation(visitor)
    handler = obs.build_usage_handler(conversation_id=conv, visitor_id=visitor)
    graph = build_graph(checkpointer=MemorySaver(), llm=None, llm_quiet=None)
    await graph.ainvoke(
        {"message": "你好", "history": [], "visitor_id": visitor},
        config={"configurable": {"thread_id": conv}, "callbacks": [handler]},
    )
    assert usage_repo.by_conversation(conv) == []
```

**Step 2：跑红**

```powershell
uv run pytest -q tests/test_usage.py
```

**Step 3：实现仓储**

`app/repo/usage.py`：`record()` 一条 INSERT；`sum_tokens_since` 用
`SELECT COALESCE(SUM(input_tokens + output_tokens), 0)`，可选按
`conversation_id` / `source` 过滤；`by_node` / `by_source` / `by_day` 用
`GROUP BY`，`by_day` 用 `DATE(created_at)`。

**Step 4：实现回调**

`app/observability/usage.py` 关键点：

```python
class UsageCallbackHandler(BaseCallbackHandler):
    """按模型调用记一行用量。

    node 归因取 metadata["langgraph_node"]——LangGraph 在每个节点执行时把它
    塞进 config metadata；source 取调用方在 config.metadata 里声明的
    shoppilot_source。取不到就退化为 unknown，不许抛错。
    """

    def __init__(self, *, conversation_id=None, visitor_id=None, source="chat"):
        self._starts: dict = {}
        self._conversation_id = conversation_id
        self._visitor_id = visitor_id
        self._source = source
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cost_micros = 0

    def on_chat_model_start(self, serialized, messages, *, run_id, metadata=None, **kwargs):
        self._starts[run_id] = (time.monotonic(), metadata or {}, _model_name(serialized))

    def on_llm_end(self, response, *, run_id, **kwargs):
        started = self._starts.pop(run_id, None)
        if started is None:
            return
        at, meta, model = started
        inp, out = _usage_of(response)
        if inp == 0 and out == 0:
            return
        ...
```

`_usage_of` 先读 `response.generations[0][0].message.usage_metadata`
（`input_tokens`/`output_tokens`），再退化到 `response.llm_output["token_usage"]`
的 `prompt_tokens`/`completion_tokens`；两者都没有返回 `(0, 0)`。

写库段必须整段 try/except + `logger.warning`。

**Step 5：接线**

`app/api/customer.py` 的 `config`：

```python
            usage_handler = build_usage_handler(
                conversation_id=conversation_id,
                visitor_id=req.visitor_id,
                source="chat",
            )
            config = {
                "configurable": {"thread_id": conversation_id},
                "callbacks": [usage_handler],
                "metadata": {
                    "shoppilot_source": "chat",
                    "shoppilot_conversation_id": conversation_id,
                    "shoppilot_visitor_id": req.visitor_id,
                },
            }
```

`app/context/summarizer.py` 的 `maybe_summarize`：构造
`config = {"callbacks": [build_usage_handler(conversation_id=..., source="bg")],
"metadata": {"shoppilot_source": "bg", "shoppilot_conversation_id": conv_id}}`，
调用改成 `await llm.ainvoke(prompt, config=config)`（现有测试替身都是
`**kwargs`，兼容）。

**Step 6：跑绿**

```powershell
uv run pytest -q tests/test_usage.py tests/test_summarizer.py tests/test_graph.py
```

**Step 7：提交**

```powershell
git add app/repo/usage.py app/observability app/api/customer.py app/context/summarizer.py tests/test_usage.py
git commit -m "r20: LLM 用量按节点归因落库，成本计量有数据源"
```

---

## Task 5：三层成本闸

**目的**：单轮闸已有（`agent.py` 的 token 上限），补「会话日 token 预算」与
「全局日成本上限」，统一记 `budget_events`。全部软闸：给降级话术，不返 4xx。

**文件**

- 新增 `app/repo/budget.py`
- 新增 `app/cost/budget.py`（`app/cost/__init__.py` 空文件）
- 改 `app/chat/agent.py`（`TOKEN_LIMIT` 读 env）
- 改 `app/api/customer.py`（入口拦截）
- 新增 `tests/test_budget.py`

**接口**

```python
# app/repo/budget.py
def record(scope, reason, *, conversation_id=None, visitor_id=None, detail=None) -> None
def sum_by_scope(since) -> dict

# app/cost/budget.py
GLOBAL_BLOCK_TEXT: str
CONVERSATION_BLOCK_TEXT: str
def conversation_limit() -> int
def global_limit_micros() -> int
def day_start() -> datetime
def global_blocked() -> bool
def conversation_over_budget(conversation_id: str) -> bool
def record_event(scope, reason, *, conversation_id=None, visitor_id=None, detail=None) -> None
def summary(since) -> dict
```

**Step 1：写失败测试**

```python
"""r20 Task 5：三层成本闸。全部软闸——降级话术，不返 4xx。"""

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core import store
from app.cost import budget
from app.repo import budget as budget_repo
from app.repo import usage as usage_repo


def test_limits_zero_means_off(monkeypatch):
    monkeypatch.setenv("CONVERSATION_DAILY_TOKEN_BUDGET", "0")
    monkeypatch.setenv("GLOBAL_DAILY_COST_YUAN", "0")
    assert budget.conversation_limit() == 0
    assert budget.global_limit_micros() == 0
    assert budget.global_blocked() is False
    assert budget.conversation_over_budget("any") is False


def test_global_blocked_by_accumulated_cost(monkeypatch):
    """今日成本累计达到上限即判定触闸。"""
    monkeypatch.setenv("GLOBAL_DAILY_COST_YUAN", "0.000001")
    usage_repo.record(None, None, "chat", "knowledge", "m", 100, 100, 2, 10)
    assert budget.global_blocked() is True


def test_conversation_budget_counts_today_only(monkeypatch):
    monkeypatch.setenv("CONVERSATION_DAILY_TOKEN_BUDGET", "50")
    conv = store.create_conversation("bg-" + uuid.uuid4().hex[:8])
    usage_repo.record(conv, "bg", "chat", "knowledge", "m", 30, 30, 0, 10)
    assert budget.conversation_over_budget(conv) is True


def test_agent_token_limit_reads_env(monkeypatch):
    monkeypatch.setenv("TURN_TOKEN_LIMIT", "6000")
    import importlib

    from app.chat import agent

    importlib.reload(agent)
    assert agent.TOKEN_LIMIT == 6000
    assert agent.STEP_LIMIT == 15


def test_blocked_chat_returns_soft_text(monkeypatch):
    """触闸后照常回 200 + 话术，不返 429；事件必须落库。"""
    from langgraph.checkpoint.memory import MemorySaver

    from app.api import _graph as graph_singleton
    from app.chat.graph import build_graph
    from app.main import app

    graph_singleton.set_graph_for_testing(
        build_graph(checkpointer=MemorySaver(), llm=None, llm_quiet=None)
    )
    monkeypatch.setattr(budget, "global_blocked", lambda: True)

    visitor = "gate-" + uuid.uuid4().hex[:8]
    with TestClient(app) as client:
        with client.stream(
            "POST", "/api/chat", json={"message": "你好", "visitor_id": visitor}
        ) as resp:
            body = "".join(resp.iter_text())

    events = [line[6:] for line in body.splitlines() if line.startswith("data: ")]
    assert events[-1] == "[DONE]"
    payloads = [json.loads(e) for e in events if e != "[DONE]"]
    assert any(p.get("delta") == budget.GLOBAL_BLOCK_TEXT for p in payloads)
    assert any("message_id" in p for p in payloads)
    assert budget_repo.sum_by_scope(budget.day_start())["global"] >= 1
```

**Step 2：跑红 → Step 3 实现**

`app/cost/budget.py` 判定逻辑：

```python
def global_blocked() -> bool:
    limit = global_limit_micros()
    if limit <= 0:
        return False
    return usage_repo.sum_cost_since(day_start()) >= limit


def conversation_over_budget(conversation_id: str) -> bool:
    """会话日预算按自然日重置，跨天不累计。"""
    limit = conversation_limit()
    if limit <= 0 or not conversation_id:
        return False
    used = usage_repo.sum_tokens_since(day_start(), conversation_id=conversation_id)
    return used >= limit
```

`app/chat/agent.py`：

```python
STEP_LIMIT = 15
TOKEN_LIMIT = int(os.getenv("TURN_TOKEN_LIMIT", "8000") or 8000)
```

**Step 4：入口拦截**

`app/api/customer.py` 的 `stream()` 开头（`yield conversation_id` 之后）：

```python
        if budget.global_blocked():
            text = budget.GLOBAL_BLOCK_TEXT
            scope = "global"
        elif budget.conversation_over_budget(conversation_id):
            text = budget.CONVERSATION_BLOCK_TEXT
            scope = "conversation"
        else:
            text = None
        if text:
            # 软闸：话术照常落库、照常推流，前端与顾客无感中断
            budget.record_event(scope, "blocked", conversation_id=conversation_id,
                                visitor_id=req.visitor_id)
            mid = store.append_message(conversation_id, "assistant", text)
            yield _sse({"delta": text})
            yield _sse({"message_id": mid})
            yield _sse("[DONE]")
            return
```

注意：该分支必须在 `try:` 之前 return，避免走 `finally` 里的摘要任务。

**Step 5：跑绿**

```powershell
uv run pytest -q tests/test_budget.py tests/test_business_agent.py tests/test_message_id.py
```

**Step 6：提交**

```powershell
git add app/repo/budget.py app/cost app/chat/agent.py app/api/customer.py tests/test_budget.py
git commit -m "r20: 会话日预算与全局日成本上限，超限降级不拒绝"
```

---

## Task 6：Langfuse 接入 + 远程 compose

**目的**：排障要看瀑布图，但 Langfuse 只能是可选件。缺 key、主机不可达都
必须静默降级。

**文件**

- 新增 `app/observability/langfuse.py`
- 改 `app/api/customer.py`（挂回调 + metadata）
- 改 `pyproject.toml`（加 `langfuse>=3,<4`）
- 新增 `deploy/langfuse-compose.yml`、`deploy/langfuse.env.example`
- 新增 `tests/test_langfuse.py`

**接口**

```python
def enabled() -> bool
def build_handler()   # -> CallbackHandler | None
def trace_metadata(conversation_id, visitor_id) -> dict
def flush() -> None
```

**Step 1：写失败测试**（不连真 Langfuse）

```python
"""r20 Task 6：Langfuse 可选件——缺配置、装配失败都不得影响对话。"""

from app.observability import langfuse as lf


def test_disabled_when_keys_missing(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    assert lf.enabled() is False
    assert lf.build_handler() is None


def test_metadata_shape(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.setenv("LANGFUSE_HOST", "http://127.0.0.1:3005")
    meta = lf.trace_metadata("conv-1", "visitor-1")
    assert meta["langfuse_session_id"] == "conv-1"
    assert meta["langfuse_user_id"] == "visitor-1"
    assert meta["langfuse_tags"] == ["chat"]
    assert meta["visitor_id"] == "visitor-1"


def test_build_handler_failure_is_silent(monkeypatch):
    """导入/构造失败只吞掉，返回 None。"""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.setenv("LANGFUSE_HOST", "http://127.0.0.1:3005")
    monkeypatch.setattr(lf, "_make_handler", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert lf.build_handler() is None
```

**Step 2：实现**

```python
"""Langfuse 追踪（可选件）。

缺 key 或装配失败一律降级为「不追踪」，并在进程内记住失败，避免每次
请求都重试导入与建连。产品看板的数据源是 MySQL，这里只服务排障。
"""

def enabled() -> bool:
    return all(os.getenv(k, "").strip() for k in
               ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST"))

def _make_handler():
    from langfuse.langchain import CallbackHandler
    return CallbackHandler()

def build_handler():
    global _broken
    if _broken or not enabled():
        return None
    try:
        return _make_handler()
    except Exception:
        _broken = True
        logger.warning("Langfuse 装配失败，本进程不再追踪", exc_info=True)
        return None

def trace_metadata(conversation_id, visitor_id) -> dict:
    return {
        "langfuse_session_id": conversation_id,
        "langfuse_user_id": visitor_id,
        "langfuse_tags": ["chat"],
        "visitor_id": visitor_id,
    }

def flush() -> None:
    try:
        from langfuse import Langfuse
        Langfuse().flush()
    except Exception:
        pass
```

**Step 3：接线**

`app/api/customer.py`：

```python
            callbacks = [usage_handler]
            lf_handler = langfuse.build_handler()
            if lf_handler is not None:
                callbacks.append(lf_handler)
            metadata = {
                "shoppilot_source": "chat",
                "shoppilot_conversation_id": conversation_id,
                "shoppilot_visitor_id": req.visitor_id,
                **langfuse.trace_metadata(conversation_id, req.visitor_id),
            }
```

`app/main.py` 的 shutdown 钩子里调 `langfuse.flush()`（包一层 try/except）。

**Step 4：compose 与 env 模板**

`deploy/langfuse-compose.yml` 六个服务（`langfuse-web`、`langfuse-worker`、
`postgres`、`clickhouse`、`redis`、`minio`），要点：

- web 端口映射 `3005:3000`
- 卷：`langfuse_pg`、`langfuse_ch`, `langfuse_redis`、`langfuse_minio`
- `TELEMETRY_ENABLED=false`
- web 与 worker 共用 `DATABASE_URL`、`SALT`、`ENCRYPTION_KEY`、`NEXTAUTH_SECRET`、
  `NEXTAUTH_URL`、`CLICKHOUSE_*`、`REDIS_*`、`LANGFUSE_S3_*`
- 全部密钥走 `--env-file langfuse.env`，**不进仓库**

`deploy/langfuse.env.example` 只放键名与占位（`CHANGEME`），并注明
`openssl rand -hex 32` 生成方式。首次部署时以官方 v3 compose 为基准校对
环境变量名（Langfuse 3.x 变量名有增补），不一致以官方为准并在设计文档 §8.1
记一笔。

**Step 5：跑绿**

```powershell
uv run pytest -q tests/test_langfuse.py
```

**Step 6：提交**

```powershell
git add app/observability/langfuse.py app/api/customer.py app/main.py pyproject.toml deploy tests/test_langfuse.py uv.lock
git commit -m "r20: Langfuse 追踪接入（可选件）与远程自托管 compose"
```

---

## Task 7：评估样本库

**目的**：样本是评估的输入。来源三路：负反馈、低置信度、评估失败项，加手工
新增。候选池人工采纳后才进入默认评估集。

**文件**

- 新增 `app/repo/evals.py`（本任务只写 cases 部分）
- 新增 `app/api/eval_cases.py`，改 `app/main.py`
- 改 `app/repo/insights.py`（补 `low_confidence_by_id`）
- 新增 `tests/test_eval_cases.py`

**接口**

```python
def create_case(query, *, expected_intent=None, expected_doc_ids=None,
                expected_behavior="answer", source="manual",
                source_ref=None, note=None) -> int
def list_cases(status=None, limit=100) -> list[dict]
def get_case(case_id) -> dict | None
def set_case_status(case_id, status) -> bool
```

**Step 1：写失败测试**

```python
"""r20 Task 7：评估样本库——候选/采纳/淘汰与三路转候选。"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.repo import evals


def test_create_and_list_candidate():
    q = "样本-" + uuid.uuid4().hex[:8]
    case_id = evals.create_case(q, expected_intent="knowledge", source="manual")
    rows = evals.list_cases(status="candidate", limit=5)
    assert rows[0]["id"] == case_id
    assert rows[0]["expected_behavior"] == "answer"


def test_status_transition():
    case_id = evals.create_case("状态-" + uuid.uuid4().hex[:8])
    assert evals.set_case_status(case_id, "accepted") is True
    assert evals.get_case(case_id)["status"] == "accepted"
    assert evals.set_case_status(case_id, "retired") is True


def test_status_rejects_unknown():
    from app.main import app

    case_id = evals.create_case("非法-" + uuid.uuid4().hex[:8])
    with TestClient(app) as client:
        res = client.post(f"/api/admin/eval/cases/{case_id}/status", json={"status": "瞎写"})
    assert res.status_code == 422


def test_from_feedback_requires_query():
    """负反馈没有可回溯的提问时不许造空样本。"""
    from app.main import app

    with TestClient(app) as client:
        res = client.post("/api/admin/eval/cases/from-feedback/999999999")
    assert res.status_code == 404
```

**Step 2：实现仓储**

`expected_doc_ids` 用 JSON 列，读出来时 `json.loads`（pymysql 对 JSON 列返回
字符串，除非设置了转换器——按现有 `pending_actions` 的读法保持一致）。

`list_cases` 支持 `status=None` 表示全部，按 `id DESC`。

**Step 3：实现接口**（本任务三个端点，其余留给 Task 9）

```python
@router.get("/api/admin/eval/cases")
async def list_cases(status: str | None = None, limit: int = 100) -> dict:
    return {"items": evals.list_cases(status=status, limit=min(limit, 200))}


@router.post("/api/admin/eval/cases")
async def create_case(req: CaseRequest) -> dict:
    case_id = evals.create_case(
        req.query.strip(), expected_intent=req.expected_intent,
        expected_doc_ids=req.expected_doc_ids,
        expected_behavior=req.expected_behavior, source="manual", note=req.note,
    )
    return {"id": case_id}


@router.post("/api/admin/eval/cases/{case_id}/status")
async def change_status(case_id: int, req: StatusRequest) -> dict:
    if req.status not in ("candidate", "accepted", "retired"):
        raise HTTPException(status_code=422, detail="未知状态")
    if not evals.set_case_status(case_id, req.status):
        raise HTTPException(status_code=404, detail="样本不存在")
    return {"id": case_id, "status": req.status}
```

`from-feedback/{feedback_id}`：`feedback.by_id` → 取 `query`（Task 2 里
`list_negative` 的回溯子查询复用成一个 `query_for(message_id)`）→ 无 query 返 404；
`create_case(query, source="feedback", source_ref=str(feedback_id))`。

**Step 4：跑绿**

```powershell
uv run pytest -q tests/test_eval_cases.py
```

**Step 5：提交**

```powershell
git add app/repo/evals.py app/repo/insights.py app/api/eval_cases.py app/main.py tests/test_eval_cases.py
git commit -m "r20: 评估样本库与候选池接口"
```

---

## Task 8：评估指标 + runner + CLI

**目的**：确定性指标先行（引用命中、引用合规、拒答准确、意图准确），LLM
裁判只做可选增强，不进默认判定。

**文件**

- 新增 `app/evals/metrics.py`、`app/evals/runner.py`（`__init__.py` 空）
- 新增 `scripts/eval_pipeline.py`
- 改 `app/repo/evals.py`（runs/items 部分）
- 改 `app/chat/graph.py`（`_Observability` 加 `insufficient`）
- 新增 `tests/test_eval_metrics.py`、`tests/test_eval_runner.py`

**接口**

```python
# metrics.py
def extract_citations(text: str) -> list[str]
def doc_id_of(faq_id: str) -> str
def score_case(case, *, intent, answer, insufficient, faq_lookup) -> dict
def aggregate(items: list[dict]) -> dict

# runner.py
async def run_eval(run_id, *, limit=30, case_ids=None, judge=False, graph=None) -> dict
```

**Step 1：写失败测试（纯函数，无 DB）**

```python
"""r20 Task 8：确定性指标。不调模型、不连库。"""

from app.evals import metrics


def test_extract_citations_dedup_and_order():
    text = "看 [P-RET-001-3]，还有 [P-SHIP-002] 与 [P-RET-001-3]"
    assert metrics.extract_citations(text) == ["P-RET-001-3", "P-SHIP-002"]


def test_doc_id_of():
    assert metrics.doc_id_of("P-RET-001-3") == "P-RET-001"
    assert metrics.doc_id_of("P-RET-001") == "P-RET-001"


def test_doc_hit_passes_when_expected_doc_cited():
    case = {"expected_intent": "knowledge", "expected_doc_ids": ["P-RET-001"],
            "expected_behavior": "answer"}
    out = metrics.score_case(
        case, intent="knowledge", answer="运费由买家承担 [P-RET-001-3]",
        insufficient=False, faq_lookup=lambda fid: {"id": fid},
    )
    assert out["passed"] is True
    assert out["checks"] == {"intent": True, "doc_hit": True, "citation_valid": True}


def test_fabricated_citation_fails():
    case = {"expected_doc_ids": ["P-RET-001"], "expected_behavior": "answer"}
    out = metrics.score_case(
        case, intent="knowledge", answer="[P-FAKE-999-1]",
        insufficient=False, faq_lookup=lambda fid: None,
    )
    assert out["checks"]["citation_valid"] is False
    assert out["passed"] is False


def test_refusal_case_requires_no_citation():
    case = {"expected_behavior": "refuse"}
    out = metrics.score_case(
        case, intent="knowledge", answer="这个问题我查不到", insufficient=True,
        faq_lookup=lambda fid: None,
    )
    assert out["checks"] == {"refusal": True, "no_false_citation": True}
    assert out["passed"] is True


def test_aggregate_handles_empty():
    assert metrics.aggregate([])["pass_rate"] == 0.0
    assert metrics.aggregate([])["total"] == 0
```

**Step 2：跑红 → Step 3 实现**

`score_case` 只在 case 声明了对应期望时才打分（缺什么不算什么），
`passed = all(checks.values())`，`checks` 为空时视为通过（只是没有可判定的项，
聚合时按 `total` 计）。

`aggregate` 的比率键名必须与设计 §9.2 一致：`pass_rate`、`intent_accuracy`、
`doc_hit_rate`、`citation_valid_rate`、`refusal_accuracy`，另加 `total`、
`passed`、`error_count`、`latency_p50_ms`、`cost_micros_total`。比率在分母为 0
时返回 `0.0`，不许除零。

**Step 4：图状态补字段**

`app/chat/graph.py`：`_Observability` 加 `insufficient: bool`，
`knowledge_node` 的 `update` 加 `"insufficient": outcome.insufficient`。

**Step 5：runner**

```python
async def run_eval(run_id, *, limit=30, case_ids=None, judge=False, graph=None):
    """跑一轮评估并落库 + 写 Markdown 报告。

    服务内后台任务优先：Milvus Lite 独占数据文件，服务运行时 CLI 打不开。
    graph 可注入，测试用假图，不连模型。
    """
```

流程：`list_cases(accepted)` 或按 `case_ids` 过滤 → 空则直接 `finish_run`
（指标全 0，status=done）→ 逐个 `graph.ainvoke`，`config` 带
`thread_id=f"eval-{run_id}-{case_id}"`、`callbacks=[usage_handler(source="eval")]`、
`metadata={"shoppilot_source": "eval"}` → `score_case` → `add_item` → 收尾
`finish_run(metrics=aggregate(items), report_path=...)`。单样本异常只记该 item 的
`error`，不中断整轮。

报告写到 `docs/eval/r20-{run_id}.md`，内容：指标表 + 失败项清单（query /
checks / 回答前 200 字）。

**Step 6：runner 测试**

```python
@pytest.mark.anyio
async def test_run_with_empty_case_set_finishes():
    run_id = uuid.uuid4().hex[:12]
    evals_repo.create_run(run_id, sample_size=0)
    out = await run_eval(run_id, limit=10)
    assert out["metrics"]["total"] == 0
    assert evals_repo.get_run(run_id)["status"] == "done"


@pytest.mark.anyio
async def test_item_error_does_not_abort_run():
    class BoomGraph:
        async def ainvoke(self, inputs, config=None):
            raise RuntimeError("模型挂了")

    case_id = evals_repo.create_case("失败样本-" + uuid.uuid4().hex[:8])
    evals_repo.set_case_status(case_id, "accepted")
    run_id = uuid.uuid4().hex[:12]
    evals_repo.create_run(run_id, sample_size=1)
    out = await run_eval(run_id, case_ids=[case_id], graph=BoomGraph())
    assert out["metrics"]["error_count"] == 1
```

**Step 7：CLI**

`scripts/eval_pipeline.py`：

```python
"""离线跑一轮评估。服务运行时 Milvus Lite 被独占，请先停服务。"""

import argparse, asyncio, sys, uuid
from app.evals.runner import run_eval

THRESHOLDS = {"pass_rate": 0.8, "citation_valid_rate": 0.95}

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--judge", action="store_true")
    args = ap.parse_args()
    run_id = uuid.uuid4().hex[:12]
    ...
    metrics = asyncio.run(run_eval(run_id, limit=args.limit, judge=args.judge))["metrics"]
    ...
    return 1 if any(metrics[k] < v for k, v in THRESHOLDS.items()) else 0
```

阈值未达标退出码 1，可直接挂计划任务。

**Step 8：跑绿**

```powershell
uv run pytest -q tests/test_eval_metrics.py tests/test_eval_runner.py
```

**Step 9：提交**

```powershell
git add app/evals app/repo/evals.py app/chat/graph.py scripts/eval_pipeline.py tests/test_eval_metrics.py tests/test_eval_runner.py
git commit -m "r20: 确定性评估指标与评估 runner，CLI 阈值可挂计划任务"
```

---

## Task 9：评估接口与飞轮看板数据

**目的**：把看板要的数据一次性给全：反馈概览、成本分布、低置信度池、评估
运行、转候选。

**文件**

- 新增 `app/api/flywheel.py`，改 `app/main.py`
- 新增 `tests/test_flywheel_api.py`

**接口**（签名与返回结构照设计 §10）

```python
GET  /api/admin/flywheel/feedback?days=7&limit=50
GET  /api/admin/flywheel/cost?days=7
GET  /api/admin/flywheel/low-confidence?limit=50
GET  /api/admin/eval/runs?limit=20
POST /api/admin/eval/run            # {"limit": 30, "case_ids": null, "judge": false}
GET  /api/admin/eval/runs/{run_id}
POST /api/admin/eval/cases/from-feedback/{feedback_id}
POST /api/admin/eval/cases/from-low-conf/{low_conf_id}
POST /api/admin/eval/cases/from-item/{item_id}
```

**Step 1：写失败测试**

```python
"""r20 Task 9：飞轮看板数据接口。评估触发用假 runner，不真跑模型。"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.repo import evals


@pytest.fixture
def client(monkeypatch):
    async def fake_run_eval(run_id, *, limit=30, case_ids=None, judge=False, graph=None):
        evals.finish_run(run_id, metrics={"total": 0, "pass_rate": 0.0}, report_path=None)
        return {"run_id": run_id, "metrics": {"total": 0}, "items": []}

    from app.api import flywheel
    monkeypatch.setattr(flywheel, "run_eval", fake_run_eval)

    from app.main import app
    with TestClient(app) as c:
        yield c


def test_cost_endpoint_shape(client):
    data = client.get("/api/admin/flywheel/cost", params={"days": 7}).json()
    for key in ("today", "by_node", "by_source", "by_day", "budget_events", "price_configured"):
        assert key in data


def test_feedback_endpoint_shape(client):
    data = client.get("/api/admin/flywheel/feedback", params={"days": 7}).json()
    for key in ("total", "positive", "negative", "negative_rate", "by_reason", "items"):
        assert key in data


def test_trigger_rejects_oversized_limit(client):
    res = client.post("/api/admin/eval/run", json={"limit": 100})
    assert res.status_code == 422


def test_trigger_returns_run_id(client):
    res = client.post("/api/admin/eval/run", json={"limit": 5})
    assert res.status_code == 200
    run_id = res.json()["run_id"]
    detail = client.get(f"/api/admin/eval/runs/{run_id}").json()
    assert detail["run"]["run_id"] == run_id


def test_from_item_creates_candidate(client):
    run_id = uuid.uuid4().hex[:12]
    evals.create_run(run_id, sample_size=1)
    item_id = evals.add_item(
        run_id, 0, "评估失败提问", intent="knowledge", answer="[P-FAKE-1]",
        cited_ids=["P-FAKE-1"], passed=False, refused=False, checks={"doc_hit": False},
        latency_ms=120, cost_micros=0,
    )
    res = client.post(f"/api/admin/eval/cases/from-item/{item_id}")
    assert res.status_code == 200
    assert res.json()["id"] > 0
```

**Step 2：实现**

`/api/admin/flywheel/cost` 的返回：

```python
{
  "days": 7,
  "today": {"tokens": ..., "cost_micros": ...},
  "by_node": [{"node": ..., "tokens": ..., "cost_micros": ...}],
  "by_source": [...],
  "by_day": [{"day": "2026-11-02", "tokens": ..., "cost_micros": ...}],
  "budget_events": {"global": ..., "conversation": ..., "turn": ...},
  "price_configured": usage.price_configured(),
}
```

`POST /api/admin/eval/run`：

```python
@router.post("/api/admin/eval/run")
async def trigger(req: RunRequest) -> dict:
    """评估要花钱，规模必须显式给出；同一时刻只允许一轮。"""
    if req.limit > 50:
        raise HTTPException(status_code=422, detail="单次评估样本数不得超过 50")
    if evals.running_exists():
        raise HTTPException(status_code=409, detail="已有评估在跑")
    run_id = uuid.uuid4().hex[:12]
    evals.create_run(run_id, sample_size=req.limit, config={...})
    asyncio.create_task(run_eval(run_id, limit=req.limit, case_ids=req.case_ids, judge=req.judge))
    return {"run_id": run_id}
```

`GET /api/admin/eval/runs/{run_id}`：`{"run": ..., "items": ...}`，run 不存在 404。

**Step 3：跑绿**

```powershell
uv run pytest -q tests/test_flywheel_api.py tests/test_eval_cases.py
```

**Step 4：提交**

```powershell
git add app/api/flywheel.py app/main.py tests/test_flywheel_api.py
git commit -m "r20: 飞轮看板数据接口与评估触发/转候选"
```

---

## Task 10：飞轮看板

**目的**：四个区块，只用 CSS 表达分布，不引图表库。

**文件**

- 新增 `app/static/admin_flywheel.html`、`app/static/admin_flywheel.js`
- 改 `app/main.py`（`/admin/flywheel` 静态页路由，照 `admin_tools` 的写法）
- 改 `app/static/admin.css`（追加 `.fw-*` 样式）

**Step 1：页面骨架**

```html
<div class="admin">
  <header class="admin-head">
    <h1>数据飞轮</h1>
    <div class="admin-actions">
      <span class="admin-count" id="fwUpdated"></span>
      <a class="btn" href="/admin/knowledge">知识库</a>
      <a class="btn" href="/admin/tools">工具审计</a>
    </div>
  </header>
  <main class="admin-main fw-main">
    <section class="fw-block" id="fwFeedback"><h2>反馈</h2><div class="fw-body"></div></section>
    <section class="fw-block" id="fwCost"><h2>成本</h2><div class="fw-body"></div></section>
    <section class="fw-block" id="fwEval"><h2>评估</h2><div class="fw-body"></div></section>
    <section class="fw-block" id="fwCases"><h2>样本</h2><div class="fw-body"></div></section>
  </main>
</div>
<script src="/static/admin_flywheel.js"></script>
```

**Step 2：数据渲染**

`admin_flywheel.js` 只做四件事，全部用 `document.createElement`（与
`admin_tools.js` 一致，不拼 innerHTML）：

```javascript
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

async function loadFeedback() {
  const host = document.querySelector("#fwFeedback .fw-body");
  try {
    const data = await getJSON(API.feedback);
    const kpis = el("div", "fw-kpi");
    kpis.append(
      kpi("赞", data.positive),
      kpi("踩", data.negative),
      kpi("踩占比", (data.negative_rate * 100).toFixed(1) + "%")
    );
    host.replaceChildren(kpis);
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
        cell(REASON_LABELS[it.reason]),
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
  const host = document.querySelector("#fwCost .fw-body");
  try {
    const data = await getJSON(API.cost);
    const kpis = el("div", "fw-kpi");
    kpis.append(
      kpi("今日 token", data.today.tokens),
      kpi("今日成本", yuan(data.today.cost_micros)),
      kpi(
        "触闸（全局/会话/单轮）",
        [data.budget_events.global, data.budget_events.conversation, data.budget_events.turn].join(" / ")
      )
    );
    host.replaceChildren(kpis);
    if (!data.price_configured) {
      host.appendChild(el("p", "fw-empty", "未配置价目，成本恒为 0"));
    }
    const max = Math.max(1, ...data.by_node.map((n) => n.tokens));
    for (const node of data.by_node) {
      const row = el("div", "fw-bar");
      const fill = el("div", "fw-bar-fill");
      fill.style.width = Math.round((node.tokens / max) * 100) + "%";
      row.append(el("span", null, node.node), fill, el("span", null, node.tokens));
      host.appendChild(row);
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
  const host = document.querySelector("#fwEval .fw-body");
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
  try {
    const data = await getJSON("/api/admin/eval/runs/" + runId);
    const box = el("div", "fw-detail");
    box.appendChild(el("h3", null, "失败项"));
    const failed = data.items.filter((it) => !it.passed);
    if (!failed.length) box.appendChild(el("p", "fw-empty", "本轮无失败项"));
    for (const item of failed) {
      const row = el("div", "fw-bar");
      row.append(el("span", null, item.query), el("span", null, item.error || "判定未通过"));
      const btn = el("button", "btn btn-link", "转候选");
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        await postJSON("/api/admin/eval/cases/from-item/" + item.id).catch(() => {
          btn.disabled = false;
        });
        btn.textContent = "已转候选";
      });
      row.appendChild(btn);
      box.appendChild(row);
    }
    host.appendChild(box);
  } catch {
    host.appendChild(el("p", "fw-empty", "详情加载失败"));
  }
}

async function loadCases() {
  const host = document.querySelector("#fwCases .fw-body");
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
      for (const [status, label] of [["accepted", "采纳"], ["retired", "淘汰"]]) {
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

function formatTime(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso || "—";
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

document.getElementById("fwUpdated").textContent =
  "刷新于 " + formatTime(new Date().toISOString());
loadFeedback();
loadCost();
loadEval();
loadCases();
```

要点：

- `prompt()` 是唯一的交互输入，取消则不发请求（不静默用默认值）
- 触发成功后每 3 秒轮询一次 `runs` 直到状态变 `done`，最多 20 次
- 任一接口失败只在对应区块显示「加载失败」，其余区块照常

**Step 3：样式**

`app/static/admin.css` 追加：

```css
.fw-main { flex-direction: column; overflow-y: auto; padding: 16px 20px; gap: 16px; }
.fw-block { border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; }
.fw-block h2 { margin: 0 0 10px; font-size: 14px; font-weight: 600; }
.fw-kpi { display: flex; gap: 20px; margin-bottom: 10px; }
.fw-kpi-item { font-size: 13px; color: var(--muted); }
.fw-kpi-item strong { display: block; font-size: 20px; color: var(--ink); }
.fw-bar { display: flex; align-items: center; gap: 8px; font-size: 12px; margin: 4px 0; }
.fw-bar-fill { height: 8px; border-radius: 4px; background: var(--user); }
.fw-table { width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 8px; }
.fw-table th, .fw-table td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line); }
.fw-table th { color: var(--muted); font-weight: 600; }
.fw-table tr:hover td { background: var(--hover); }
.fw-empty { font-size: 12px; color: var(--muted); margin: 6px 0; }
.fw-actions { display: flex; gap: 8px; margin-bottom: 8px; }
.fw-input { flex: 1; padding: 6px 8px; font: inherit; font-size: 13px;
  border: 1px solid var(--line); border-radius: 8px; }
.fw-detail { margin-top: 10px; padding-top: 10px; border-top: 1px dashed var(--line); }
.fw-detail h3 { margin: 0 0 8px; font-size: 13px; }
```

**Step 4：手动验收**

1. 打开 `/admin/flywheel`，四块都有数据（无数据时显示「暂无」而不是空白）
2. 对话页点踩后刷新看板，负反馈出现在表里
3. 点「转候选」→ 样本区出现该 query
4. 「立即跑评估」输入 3 → 状态从 running 变 done，报告路径可点
5. 把 `GLOBAL_DAILY_COST_YUAN` 配成极小值再发消息，成本区「触闸次数」+1

**Step 5：提交**

```powershell
git add app/static/admin_flywheel.html app/static/admin_flywheel.js app/static/admin.css app/main.py
git commit -m "r20: 数据飞轮看板（反馈/成本/评估/样本四区）"
```

---

## Task 11：配置收口与全量回归

**目的**：把所有新开关集中到 `.env.example` 并写清默认值与关闭语义，最后
跑一遍全量测试确认没碰坏 r19。

**文件**

- 改 `.env.example`
- 改 `docs/superpowers/specs/2026-11-02_r20-data-flywheel-design.md`（若实现与设计有偏差，回写一节「实现偏差」）

**Step 1：补 `.env.example`**

```dotenv
# ---------- r20 成本控制 ----------
# 单轮 token 上限（原 app/chat/agent.py 常量）
TURN_TOKEN_LIMIT=8000
# 会话日 token 预算，0=关闭
CONVERSATION_DAILY_TOKEN_BUDGET=0
# 全局日成本上限（元），0=关闭
GLOBAL_DAILY_COST_YUAN=0
# 价目：元/百万 token，用于把 token 折算成钱（不配则成本恒为 0）
LLM_PRICE_INPUT_PER_MTOK=0
LLM_PRICE_OUTPUT_PER_MTOK=0

# ---------- r20 Langfuse（可选，三项齐备才启用） ----------
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=http://192.168.6.23:3005

# ---------- r20 评估 ----------
EVAL_DEFAULT_LIMIT=30
```

**Step 2：全量回归**

```powershell
uv run pytest -q
```

失败项逐个处理；只有确实与 r20 无关的既有失败才允许标注跳过，并在提交信息里
写明原因。

**Step 3：端到端自检**（对照设计 §15 验收标准逐条打勾）

1. 对话页点踩 → `message_feedback` 有行，`reason`/`comment` 可空
2. `llm_usage` 每轮至少一行，`node` 有值
3. 触闸话术可复现（临时把 `CONVERSATION_DAILY_TOKEN_BUDGET` 设成 100）
4. 配齐 Langfuse 三项 → 远程 UI 出现 trace，tag 为 `chat`，session 为会话 id
5. 看板四区都有数据
6. `uv run python scripts/eval_pipeline.py --limit 3` 退出码 0

**Step 4：提交**

```powershell
git add .env.example docs/superpowers
git commit -m "r20: 配置项收口与验收自检"
```

---

## 验收自检清单（对应设计 §15）

- [ ] 反馈可只点赞，也可只点踩，标签与文字都可选
- [ ] 同一条回答重复提交不产生重复行；撤销后行被删
- [ ] 别人的 message_id 打反馈返回 404
- [ ] SSE 在 `[DONE]` 前推 `message_id`；历史接口每条带 `id`
- [ ] `llm_usage` 覆盖 chat / bg / eval 三种 source，node 归因正确
- [ ] 模型不报用量时不写行，不污染统计
- [ ] 会话日预算与全局日成本上限都只降级、不拒绝
- [ ] `budget_events` 能按 scope 计数，看板可见
- [ ] Langfuse 缺 key 时进程内不再重试，对话零影响
- [ ] 评估指标不含 LLM 裁判也能给出 pass_rate
- [ ] 空样本集跑评估不报错、不除零
- [ ] 单样本异常不中断整轮
- [ ] `limit > 50` 被接口拒绝
- [ ] 看板无数据时显示「暂无」
- [ ] `uv run pytest -q` 全绿

## 已知坑与回滚

| 坑 | 处理 |
|---|---|
| Langfuse 3.x 环境变量名与本文档不一致 | 以官方 compose 为准，改动只落在 `deploy/` |
| 远程 docker 不可达 | 代码侧不受影响（可选件），只交付 compose 与配置模板 |
| 评估占满全局日预算 | 评估在 `llm_usage` 里 `source=eval`，看板可分辨；建议非高峰手动触发 |
| Milvus Lite 被服务独占 | 评估优先走服务内后台任务；CLI 仅停机时使用 |
| 反馈接口无鉴权 | 与现有管理接口一致（本地开发），公网部署前需补鉴权 |