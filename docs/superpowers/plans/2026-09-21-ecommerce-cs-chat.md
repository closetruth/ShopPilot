# 电商智能客服 Ch01 纯对话 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 跑通电商智能客服纯对话底座：SQLite 会话、SSE 流式多轮、Prompt 模板、售后结构化抽取、token/轮数裁剪。

**Architecture:** FastAPI 暴露 chat/extract/messages/health；`chat_service` / `extract_service` 经 `context_manager` 与 `PromptTemplate` 调用唯一 `ChatOpenAI` 工厂（OpenAI 协议，base_url/key/model 全来自 `.env`）；会话权威在 SQLite。

**Tech Stack:** Python 3.11+、FastAPI（`fastapi.sse.EventSourceResponse`）、SQLAlchemy 2.0、LangChain（`langchain-openai` / `langchain-core`）、Pydantic v2、pytest、SQLite。

**Spec:** `docs/superpowers/specs/2026-09-21-ecommerce-cs-chat-design.md`

## Global Constraints

- 选型冻结：Python + FastAPI + LangChain LCEL + SQLite/SQLAlchemy + OpenAI 协议；矛盾则停问用户，禁止擅自换方案。
- 本章不做：工具调用、Agent、LangGraph、RAG、鉴权。
- 库用法（FastAPI / LangChain / SQLAlchemy）实现前须 Context7 核对；SSE 使用 `fastapi.sse`（`EventSourceResponse` + `ServerSentEvent`）。
- 可单测模块走 TDD；抽取 Prompt 用 `evals/` 标注样例验证；聊天页 Vibe Coding，不套 TDD/formal review。
- 每完成一个 Task：更新 `dev-notes/ch01.md`，再 commit。
- 环境变量名必须与 spec 一致：`OPENAI_API_BASE`、`OPENAI_API_KEY`、`MODEL_NAME`、`MAX_CONTEXT_TOKENS`、`MAX_HISTORY_TURNS`、`DATABASE_URL`。

---

## File Structure

```
app/
  __init__.py
  main.py                 # FastAPI app、create_all、挂载 static
  config.py               # pydantic-settings
  api/
    __init__.py
    deps.py               # DB session 依赖
    schemas.py            # 请求/响应 DTO
    routes_chat.py        # POST /api/v1/chat
    routes_extract.py     # POST /api/v1/extract
    routes_sessions.py    # GET /api/v1/sessions/{id}/messages
    routes_health.py      # GET /health
  db/
    __init__.py
    base.py               # engine、SessionLocal、get_db、init_db
    models.py             # SessionRow、MessageRow
    repository.py         # 会话/消息 CRUD
  llm/
    __init__.py
    factory.py            # get_chat_model() -> ChatOpenAI
  prompts/
    __init__.py
    chat.py               # 客服 system + ChatPromptTemplate 组装
    extract.py            # 抽取 prompt
  schemas/
    __init__.py
    aftersales.py         # AfterSalesExtract Pydantic
  services/
    __init__.py
    context_manager.py    # trim_messages(...)
    chat_service.py
    extract_service.py
static/
  index.html              # Vibe Coding 聊天页
evals/
  aftersales_extract.jsonl
  run_extract_eval.py
tests/
  test_context_manager.py
  test_repository.py
  test_config.py
requirements.txt
.env.example
pytest.ini
README.md
```

---

### Task 1: 项目脚手架与配置

**Files:**
- Create: `requirements.txt`, `.env.example`, `pytest.ini`, `app/__init__.py`, `app/config.py`, `tests/test_config.py`
- Create: `README.md`（仅安装/启动骨架，演示命令在 Task 8 补全）

**Interfaces:**
- Produces: `Settings`（pydantic-settings），`get_settings()` 带 `lru_cache`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_config.py
import os
from app.config import Settings, get_settings

def test_settings_reads_env(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("OPENAI_API_BASE", "http://localhost:11434/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("MODEL_NAME", "llama3")
    monkeypatch.setenv("MAX_CONTEXT_TOKENS", "2048")
    monkeypatch.setenv("MAX_HISTORY_TURNS", "6")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./test.db")
    s = Settings()
    assert s.openai_api_base == "http://localhost:11434/v1"
    assert s.model_name == "llama3"
    assert s.max_context_tokens == 2048
    assert s.max_history_turns == 6
```

- [ ] **Step 2: Run test — expect FAIL**（`app.config` 不存在）

```bash
pytest tests/test_config.py -v
```

- [ ] **Step 3: 实现配置与依赖文件**

```python
# app/config.py
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    openai_api_base: str = "https://api.openai.com/v1"
    openai_api_key: str = "changeme"
    model_name: str = "gpt-4o-mini"
    max_context_tokens: int = 4096
    max_history_turns: int = 10
    database_url: str = "sqlite:///./mewhelp.db"

@lru_cache
def get_settings() -> Settings:
    return Settings()
```

```text
# requirements.txt
fastapi>=0.128.0
uvicorn[standard]>=0.32.0
sqlalchemy>=2.0.36
pydantic>=2.9.0
pydantic-settings>=2.6.0
langchain-openai>=0.3.0
langchain-core>=0.3.0
python-dotenv>=1.0.0
httpx>=0.27.0
pytest>=8.0.0
pytest-asyncio>=0.24.0
```

```text
# .env.example
OPENAI_API_BASE=https://api.openai.com/v1
OPENAI_API_KEY=sk-...
MODEL_NAME=gpt-4o-mini
MAX_CONTEXT_TOKENS=4096
MAX_HISTORY_TURNS=10
DATABASE_URL=sqlite:///./mewhelp.db
```

```ini
# pytest.ini
[pytest]
asyncio_mode = auto
pythonpath = .
```

- [ ] **Step 4: Run test — expect PASS**

```bash
pytest tests/test_config.py -v
```

- [ ] **Step 5: 更新 dev-notes 并 commit**

在 `dev-notes/ch01.md` 追加 Task 1 完成段（用户关键原话若无则写「按 plan Task 1」；产出；纠偏；翻车）。

```bash
git add requirements.txt .env.example pytest.ini app/__init__.py app/config.py tests/test_config.py README.md dev-notes/ch01.md
git commit -m "chore: scaffold config and project deps for ch01 chat"
```

---

### Task 2: SQLite 模型与 Repository

**Files:**
- Create: `app/db/base.py`, `app/db/models.py`, `app/db/repository.py`, `app/db/__init__.py`
- Test: `tests/test_repository.py`

**Interfaces:**
- Produces:
  - `init_db() -> None`
  - `get_db()` generator
  - `create_session(db) -> SessionRow`
  - `get_session(db, session_id: str) -> SessionRow | None`
  - `add_message(db, session_id: str, role: str, content: str) -> MessageRow`
  - `list_messages(db, session_id: str) -> list[MessageRow]`（按 `seq` 升序）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_repository.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.db.base import Base
from app.db import repository as repo

def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()

def test_create_session_and_messages():
    db = _session()
    s = repo.create_session(db)
    assert s.id
    repo.add_message(db, s.id, "user", "hi")
    repo.add_message(db, s.id, "assistant", "hello")
    msgs = repo.list_messages(db, s.id)
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[0].seq == 1 and msgs[1].seq == 2

def test_get_session_missing():
    db = _session()
    assert repo.get_session(db, "no-such") is None
```

- [ ] **Step 2: Run — expect FAIL**

```bash
pytest tests/test_repository.py -v
```

- [ ] **Step 3: 实现 models / base / repository**

```python
# app/db/models.py
import uuid
from datetime import datetime, timezone
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

class SessionRow(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
    messages: Mapped[list["MessageRow"]] = relationship(back_populates="session")

class MessageRow(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    seq: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    session: Mapped[SessionRow] = relationship(back_populates="messages")
```

```python
# app/db/base.py
from collections.abc import Generator
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from app.config import get_settings

class Base(DeclarativeBase):
    pass

def _engine():
    url = get_settings().database_url
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args)

engine = _engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

def init_db() -> None:
    from app.db import models  # noqa: F401
    Base.metadata.create_all(bind=engine)

def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
```

```python
# app/db/repository.py
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.db.models import MessageRow, SessionRow

def create_session(db: Session) -> SessionRow:
    row = SessionRow()
    db.add(row)
    db.commit()
    db.refresh(row)
    return row

def get_session(db: Session, session_id: str) -> SessionRow | None:
    return db.get(SessionRow, session_id)

def add_message(db: Session, session_id: str, role: str, content: str) -> MessageRow:
    next_seq = db.scalar(
        select(func.coalesce(func.max(MessageRow.seq), 0)).where(MessageRow.session_id == session_id)
    )
    msg = MessageRow(session_id=session_id, role=role, content=content, seq=int(next_seq) + 1)
    db.add(msg)
    session = db.get(SessionRow, session_id)
    if session:
        from datetime import datetime, timezone
        session.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(msg)
    return msg

def list_messages(db: Session, session_id: str) -> list[MessageRow]:
    return list(
        db.scalars(
            select(MessageRow).where(MessageRow.session_id == session_id).order_by(MessageRow.seq.asc())
        )
    )
```

注意：`Base` 定义在 `base.py`，`models.py` 从 `base` 导入；测试里 `from app.db.base import Base` 与 `create_all` 前须 `import app.db.models`。

- [ ] **Step 4: Run — expect PASS**

```bash
pytest tests/test_repository.py -v
```

- [ ] **Step 5: 更新 ch01.md + commit**

```bash
git add app/db tests/test_repository.py dev-notes/ch01.md
git commit -m "feat: add SQLite session and message repository"
```

---

### Task 3: 上下文裁剪 `context_manager`

**Files:**
- Create: `app/services/context_manager.py`, `app/services/__init__.py`
- Test: `tests/test_context_manager.py`

**Interfaces:**
- Produces:
  - `estimate_tokens(text: str) -> int`  # `max(1, (len(text) + 3) // 4)`；空串返回 0
  - `trim_messages(messages: list[dict], *, system_text: str, max_tokens: int, max_turns: int) -> list[dict]`
  - 输入/输出 message dict：`{"role": "user"|"assistant", "content": str}`
  - 返回值**不含** system；调用方自行把 system 放进送模消息

**裁剪规则（与 spec 一致）：**
1. 先按轮裁：从末尾保留至多 `max_turns` 轮（user+assistant 一对为 1 轮；末尾孤儿 user 计 1 轮）。
2. 再按 token：`estimate_tokens(system_text) + sum(estimate_tokens(m["content"]) for m in kept)` ≤ `max_tokens`；超则从最旧成对删除（先删一对 user+assistant；若最旧是孤儿 assistant 则单删）。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_context_manager.py
from app.services.context_manager import estimate_tokens, trim_messages

def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1  # 4 chars -> 1

def test_trim_by_turns():
    msgs = []
    for i in range(5):
        msgs.append({"role": "user", "content": f"u{i}"})
        msgs.append({"role": "assistant", "content": f"a{i}"})
    out = trim_messages(msgs, system_text="sys", max_tokens=10_000, max_turns=2)
    assert [m["content"] for m in out] == ["u3", "a3", "u4", "a4"]

def test_trim_by_token_budget():
    system = "S" * 40  # ~10 tokens
    msgs = [
        {"role": "user", "content": "U" * 40},
        {"role": "assistant", "content": "A" * 40},
        {"role": "user", "content": "recent"},
    ]
    out = trim_messages(msgs, system_text=system, max_tokens=20, max_turns=10)
    assert out[-1]["content"] == "recent"
    assert all(m["content"] != "U" * 40 for m in out) or estimate_tokens(system) + sum(
        estimate_tokens(m["content"]) for m in out
    ) <= 20
```

- [ ] **Step 2: Run — expect FAIL**

```bash
pytest tests/test_context_manager.py -v
```

- [ ] **Step 3: 实现 `trim_messages` / `estimate_tokens`（完整逻辑写入 `app/services/context_manager.py`，禁止占位）**

- [ ] **Step 4: Run — expect PASS**

```bash
pytest tests/test_context_manager.py -v
```

- [ ] **Step 5: 更新 ch01.md + commit**

```bash
git add app/services/context_manager.py app/services/__init__.py tests/test_context_manager.py dev-notes/ch01.md
git commit -m "feat: add token and turn based context trimming"
```

---

### Task 4: LLM 工厂、Prompt、抽取 Schema

**Files:**
- Create: `app/llm/factory.py`, `app/llm/__init__.py`
- Create: `app/schemas/aftersales.py`, `app/schemas/__init__.py`
- Create: `app/prompts/chat.py`, `app/prompts/extract.py`, `app/prompts/__init__.py`
- Test: `tests/test_aftersales_schema.py`（Schema/枚举，不测真实 LLM）

**Interfaces:**
- Produces:
  - `get_chat_model() -> ChatOpenAI`（`base_url=settings.openai_api_base`, `api_key=...`, `model=...`）
  - `AfterSalesExtract`：字段 `order_id: str | None`, `intent_type: Literal[...]`, `expected_resolution: str`, `raw_summary: str`
  - `INTENT_TYPES` = refund/return/exchange/repair/cancel/logistics/invoice/other
  - `build_chat_messages(system: str, history: list[dict]) -> list`（LangChain messages 或 OpenAI dicts，与 `astream` 入参一致）
  - `CUSTOMER_SERVICE_SYSTEM` / `EXTRACT_SYSTEM` 字符串常量（角色与约束按 spec §5）

- [ ] **Step 1: Schema 失败测试**

```python
# tests/test_aftersales_schema.py
import pytest
from pydantic import ValidationError
from app.schemas.aftersales import AfterSalesExtract

def test_valid_extract():
    m = AfterSalesExtract(
        order_id="ORD-1",
        intent_type="logistics",
        expected_resolution="补发",
        raw_summary="包裹丢失",
    )
    assert m.intent_type == "logistics"

def test_invalid_intent():
    with pytest.raises(ValidationError):
        AfterSalesExtract(
            order_id=None,
            intent_type="hack",
            expected_resolution="x",
            raw_summary="y",
        )
```

- [ ] **Step 2: Run — expect FAIL**

```bash
pytest tests/test_aftersales_schema.py -v
```

- [ ] **Step 3: 实现 schema、factory、prompts**

`factory.py`（Context7 已核对）：

```python
from langchain_openai import ChatOpenAI
from app.config import get_settings

def get_chat_model() -> ChatOpenAI:
    s = get_settings()
    return ChatOpenAI(
        model=s.model_name,
        api_key=s.openai_api_key,
        base_url=s.openai_api_base,
        temperature=0.3,
    )
```

`prompts/chat.py`：写清客服角色、不编造、追问订单号、默认中文等（完整中文 system 字符串，非 TBD）。

`prompts/extract.py`：只抽取、禁止臆造订单号；可用 `ChatPromptTemplate.from_messages([("system", ...), ("human", "{text}")])`。

- [ ] **Step 4: Run — expect PASS**

```bash
pytest tests/test_aftersales_schema.py -v
```

- [ ] **Step 5: 更新 ch01.md + commit**

```bash
git add app/llm app/schemas app/prompts tests/test_aftersales_schema.py dev-notes/ch01.md
git commit -m "feat: add LLM factory, prompts, and aftersales schema"
```

---

### Task 5: 抽取服务 + 标注评估集

**Files:**
- Create: `app/services/extract_service.py`
- Create: `evals/aftersales_extract.jsonl`（≥5 条）
- Create: `evals/run_extract_eval.py`
- Test: 无强制单测；**以评估脚本为验证**（替代 TDD）

**Interfaces:**
- Produces: `async def extract_aftersales(text: str) -> AfterSalesExtract`
- 实现：`get_chat_model().with_structured_output(AfterSalesExtract, method="json_schema")` + extract prompt；`ainvoke`

**jsonl 每行示例：**

```json
{"id":"e1","text":"订单ORD-8899一直没送到，希望尽快查物流或退款","expect":{"order_id":"ORD-8899","intent_type":"logistics"}}
```

脚本：对每条调用 `extract_aftersales`，比对 `intent_type` 全等、`order_id` 全等（大小写敏感）；打印通过率；`intent_type`+`order_id` 全对才算 pass；退出码：通过率 < 0.6 则 1（本地无 key 时可 `SKIP_LIVE_EVAL=1` 跳过并打印提示）。

- [ ] **Step 1: 写 ≥5 条标注样例 + 评估脚本骨架（先断言服务可导入）**

- [ ] **Step 2: 实现 `extract_service.py`**

```python
async def extract_aftersales(text: str) -> AfterSalesExtract:
    llm = get_chat_model().with_structured_output(AfterSalesExtract, method="json_schema")
    prompt = ...  # from app.prompts.extract
    return await (prompt | llm).ainvoke({"text": text})
```

- [ ] **Step 3: 有可用 `.env` 时跑评估**

```bash
python evals/run_extract_eval.py
```

Expected: 至少关键字段多数通过；若上游不支持 `json_schema`，**停止并问用户**，勿擅自改 method。

- [ ] **Step 4: 更新 ch01.md + commit**

```bash
git add app/services/extract_service.py evals/ dev-notes/ch01.md
git commit -m "feat: add aftersales extract service and eval set"
```

---

### Task 6: Chat Service（会话 + 流式）

**Files:**
- Create: `app/services/chat_service.py`
- Test: `tests/test_chat_service.py`（用假 LLM，不打真实上游）

**Interfaces:**
- Produces:
  - `async def ensure_session(db, session_id: str | None) -> SessionRow`（None→创建；无效→ raise `SessionNotFound`）
  - `async def stream_chat(db, session_id: str | None, message: str) -> AsyncIterator[dict]`
    - yield `{"type":"session","session_id":...}` 一次
    - yield `{"type":"token","content":...}` 多次
    - yield `{"type":"done"}` 或 `{"type":"error","message":...}`
  - 副作用：写入 user；流成功结束后写入完整 assistant；失败不写残缺 assistant
  - 送模：`system + trim_messages(history)` → `model.astream`

- [ ] **Step 1: 写失败测试（monkeypatch `get_chat_model`）**

```python
# tests/test_chat_service.py
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.db.base import Base
import app.db.models  # noqa
from app.services import chat_service

class FakeChunk:
    def __init__(self, content): self.content = content

class FakeModel:
    async def astream(self, messages):
        for t in ["你", "好"]:
            yield FakeChunk(t)

@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(chat_service, "get_chat_model", lambda: FakeModel())
    return Session()

@pytest.mark.asyncio
async def test_stream_chat_persists_roundtrip(db):
    events = []
    async for ev in chat_service.stream_chat(db, None, "你好"):
        events.append(ev)
    assert events[0]["type"] == "session"
    assert any(e["type"] == "token" for e in events)
    assert events[-1]["type"] == "done"
    sid = events[0]["session_id"]
    from app.db.repository import list_messages
    msgs = list_messages(db, sid)
    assert msgs[0].content == "你好" and msgs[1].content == "你好"
```

- [ ] **Step 2: Run — expect FAIL**

```bash
pytest tests/test_chat_service.py -v
```

- [ ] **Step 3: 实现 `chat_service.py`（含 `SessionNotFound(Exception)`）**

伪代码要点：

```python
async def stream_chat(db, session_id, message):
    session = await ensure_session(db, session_id)
    yield {"type": "session", "session_id": session.id}
    add_message(db, session.id, "user", message)
    history = [{"role": m.role, "content": m.content} for m in list_messages(db, session.id)]
    trimmed = trim_messages(history, system_text=CUSTOMER_SERVICE_SYSTEM, ...)
    lc_messages = build_chat_messages(CUSTOMER_SERVICE_SYSTEM, trimmed)
    buf = []
    try:
        async for chunk in get_chat_model().astream(lc_messages):
            text = chunk.content if isinstance(chunk.content, str) else "".join(...)
            if not text:
                continue
            buf.append(text)
            yield {"type": "token", "content": text}
        add_message(db, session.id, "assistant", "".join(buf))
        yield {"type": "done"}
    except Exception as e:
        yield {"type": "error", "message": str(e)}
```

- [ ] **Step 4: Run — expect PASS**

```bash
pytest tests/test_chat_service.py -v
```

- [ ] **Step 5: 更新 ch01.md + commit**

```bash
git add app/services/chat_service.py tests/test_chat_service.py dev-notes/ch01.md
git commit -m "feat: add streaming chat service with session persistence"
```

---

### Task 7: FastAPI 路由与应用入口

**Files:**
- Create: `app/api/schemas.py`, `app/api/deps.py`, `app/api/routes_*.py`, `app/main.py`
- Modify: 无
- Test: `tests/test_api_health.py`（TestClient）；chat SSE 可用 httpx ASGI 测假 LLM 或手工 curl（实现后）

**Interfaces:**
- `POST /api/v1/chat` body: `{session_id?: str, message: str, mode: "chat"|"extract"="chat"}`
  - `mode=chat` → `response_class=EventSourceResponse`，yield `ServerSentEvent(data=event_dict)`
  - `mode=extract` → JSON `AfterSalesExtract`；写 user；不写 assistant
- `POST /api/v1/extract` body: `{text: str, session_id?: str}`
- `GET /api/v1/sessions/{session_id}/messages`
- `GET /health` → `{"status":"ok"}`
- 启动：`init_db()`；`app.mount("/", StaticFiles(...), name="static")` 注意 API 路由优先注册

**SSE（Context7）：**

```python
from fastapi.sse import EventSourceResponse, ServerSentEvent

@router.post("/api/v1/chat", response_class=EventSourceResponse)
async def chat(...):
    async def gen():
        async for ev in stream_chat(...):
            yield ServerSentEvent(data=ev)
    return EventSourceResponse(gen())
```

若当前安装的 FastAPI **没有** `fastapi.sse`：停问用户（升版本 vs 手写 StreamingResponse），禁止默默降级。

- [ ] **Step 1: `test_health` 失败测试 + 实现 `routes_health` / `main` 最小可启动**

- [ ] **Step 2: 实现全部路由与 `api/schemas.py`**

- [ ] **Step 3:**

```bash
pytest tests/ -v
uvicorn app.main:app --reload --port 8000
```

- [ ] **Step 4: 手工 curl（需真实 `.env`）验证三验收** — 命令写入 README

```bash
# 1) 流式
curl -N -X POST http://127.0.0.1:8000/api/v1/chat ^
  -H "Content-Type: application/json" ^
  -d "{\"message\":\"我的快递丢了，订单ORD-1001\"}"

# 2) 多轮：把上一响应 session_id 填入
curl -N -X POST http://127.0.0.1:8000/api/v1/chat ^
  -H "Content-Type: application/json" ^
  -d "{\"session_id\":\"<SID>\",\"message\":\"希望退款\"}"

curl http://127.0.0.1:8000/api/v1/sessions/<SID>/messages

# 3) 抽取
curl -X POST http://127.0.0.1:8000/api/v1/extract ^
  -H "Content-Type: application/json" ^
  -d "{\"text\":\"订单ORD-1001丢件，想退款\"}"
```

- [ ] **Step 5: 更新 ch01.md + commit**

```bash
git add app/api app/main.py tests/test_api_health.py README.md dev-notes/ch01.md
git commit -m "feat: expose chat SSE extract and session APIs"
```

---

### Task 8: 聊天页（Vibe Coding，非正式流程）

**Files:**
- Create: `static/index.html`（可含内联 CSS/JS；单文件即可）

**要求（按效果迭代，无 TDD/review 门禁）：**
- 输入框发送 → `POST /api/v1/chat`，解析 SSE `data:` JSON
- 保存 `session_id`，多轮续聊
- 助手消息逐 token 追加
- 可选「抽取」按钮 → `/api/v1/extract`
- 打开 `http://127.0.0.1:8000/` 可用

- [ ] **Step 1: 实现极简页并挂载（Task 7 若未 mount 则补上）**
- [ ] **Step 2: 浏览器点一点；按用户反馈改**
- [ ] **Step 3: 更新 ch01.md + commit**

```bash
git add static/index.html app/main.py dev-notes/ch01.md
git commit -m "feat: add minimal vibe-coded chat UI"
```

---

### Task 9: 收尾验收与文档

**Files:**
- Modify: `README.md`（演示命令、配置说明、评估命令）
- Modify: `dev-notes/ch01.md`（finish 段）

- [ ] **Step 1: 跑全量**

```bash
pytest tests/ -v
python evals/run_extract_eval.py
```

- [ ] **Step 2: 按验收标准实测三则 curl，把结果摘要写入 ch01.md finish 段**
- [ ] **Step 3: commit**

```bash
git add README.md dev-notes/ch01.md
git commit -m "docs: add ch01 demo commands and finish notes"
```

---

## Self-Review (plan vs spec)

| Spec 项 | Task |
|---------|------|
| SSE 流式对话 | 6, 7 |
| session 存储 SQLite | 2, 6 |
| 只传 session_id+message | 6, 7 |
| PromptTemplate 客服 | 4 |
| with_structured_output | 4, 5 |
| /extract + mode=extract | 5, 7 |
| 裁剪 token+turns | 3 |
| GET messages / health | 7 |
| 聊天页 | 8 |
| 评估集 | 5 |
| 不做 Agent | 全局约束 |

无 TBD；接口名前后一致（`stream_chat` / `extract_aftersales` / `trim_messages` / `AfterSalesExtract`）。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-21-ecommerce-cs-chat.md`.

**Two execution options:**

1. **Subagent-Driven（推荐）** — 每任务新子代理，任务间两阶段 review  
2. **Inline Execution** — 本会话按 executing-plans 批量推进并设检查点  

Which approach?
