# 电商智能客服 · 第一章：纯对话设计

**日期：** 2026-09-21  
**状态：** 待用户审阅  
**范围：** SSE 多轮对话、Prompt 模板、结构化售后抽取、会话持久化、最简上下文裁剪；不含工具调用 / Agent

## 1. 目标与验收

### 1.1 目标

跑通电商智能客服的「纯对话」底座：多轮上下文、流式输出、可配置模型接入、售后描述结构化抽取。

### 1.2 验收标准

1. `curl` 调对话接口能看到 SSE 逐 token 推送。
2. 同一 `session_id` 连续问两轮，第二轮能接住第一轮上下文（可用 `GET .../messages` 核对落库）。
3. 发一段售后描述，经 `/api/v1/extract` 或 `mode=extract` 拿到结构化 JSON。

### 1.3 本章不做

- 工具调用、Agent 循环、RAG
- 登录鉴权、多租户
- 生产级迁移工具（可用 SQLAlchemy `create_all`）

## 2. 技术选型（冻结）

| 层 | 选型 |
|----|------|
| API | Python + FastAPI |
| LLM 编排 | LangChain（LCEL 链，非 LangGraph） |
| 模型协议 | 应用侧统一 OpenAI 协议；`ChatOpenAI` + `.env` 中的 base_url / key / model |
| 持久化 | SQLite + SQLAlchemy |
| 配置 | `.env`（含模型与裁剪预算） |
| 聊天页 | 极简静态页，Vibe Coding，不走正式 Superpowers 子流程 |

实现中若与上述选型冲突，**停下来问用户**，不得自行换方案。

## 3. 架构

```
Chat UI (static) ──HTTP──► FastAPI
                              │
                              ├─ POST /api/v1/chat
                              ├─ POST /api/v1/extract
                              ├─ GET  /api/v1/sessions/{id}/messages
                              └─ GET  /health
                              │
                              ▼
                    services/
                      chat_service
                      extract_service
                      context_manager
                              │
                              ▼
                    llm/factory.py  →  ChatOpenAI(base_url, api_key, model)
                              │
                              ▼
                    SQLAlchemy  →  SQLite (sessions / messages)
```

**职责边界**

- **API 层**：校验、SSE 封装、HTTP 错误码；不写业务 Prompt。
- **chat_service**：建/取会话、写 user、裁剪、流式生成、写 assistant。
- **extract_service**：`PromptTemplate` + `with_structured_output`。
- **context_manager**：轮数硬上限 + token 预算裁剪；只影响送模上下文，不删库。
- **llm/factory**：唯一创建模型客户端的地方。

## 4. API 契约

### 4.1 `POST /api/v1/chat`

**请求**

```json
{
  "session_id": "可选 UUID；空则新建",
  "message": "本轮用户话",
  "mode": "chat"
}
```

- `message`：必填非空字符串。
- `mode`：`chat`（默认）| `extract`。
- 客户端**不**传完整历史；服务端以库内历史为权威。

**mode=chat → SSE**（`Content-Type: text/event-stream`）

```
data: {"type":"session","session_id":"..."}
data: {"type":"token","content":"..."}
data: {"type":"done"}
```

出错：

```
data: {"type":"error","message":"..."}
```

**流程：** 取/建会话 → 写入本轮 user → 读历史 → 裁剪 → `astream` → 写回完整 assistant 文本。

**mode=extract → JSON（非 SSE）**

对**本轮 `message`** 做抽取。写入本轮 user 消息；**不**将抽取 JSON 当作 assistant 消息入库（避免污染对话）。响应体同 §4.2。

### 4.2 `POST /api/v1/extract`

**请求**

```json
{
  "text": "售后描述...",
  "session_id": "可选，仅审计关联，不强制读写对话历史"
}
```

**响应**

```json
{
  "order_id": "ORD-12345",
  "intent_type": "logistics",
  "expected_resolution": "尽快补发或退款",
  "raw_summary": "一句话摘要"
}
```

`order_id` 可为 `null`。`intent_type` 枚举见 §5.2。

### 4.3 `GET /api/v1/sessions/{session_id}/messages`

返回该会话按 `seq` 排序的 `user` / `assistant` 消息列表（调试与多轮验收用）。

### 4.4 `GET /health`

`{"status":"ok"}`

### 4.5 错误码

| 场景 | 行为 |
|------|------|
| 校验失败 | 422 |
| `session_id` 不存在 | 404 |
| 上游模型失败 | chat：SSE `error`；extract：HTTP 502 |
| 结构化解析失败 | 502，不返回半截假字段 |
| 流式中途失败 | 已写 user 保留；**不落库**残缺 assistant |

## 5. Prompt 与结构化输出

### 5.1 对话 System Prompt

使用 LangChain `PromptTemplate` / `ChatPromptTemplate` 管理，内容须明确：

- 角色：电商智能客服；礼貌、简洁；先澄清再给方案。
- 约束：不编造物流/库存/政策；不确定则追问订单号与问题；不承诺无法核实的赔付。
- 范围：售前、订单、物流、售后；超范围礼貌拒答并引导。
- 语言：默认中文短句，除非用户使用其他语言。

System 消息**不入库**，每次请求由模板注入。

### 5.2 售后抽取 Schema

| 字段 | 类型 | 说明 |
|------|------|------|
| `order_id` | `str \| null` | 订单号，抽不到为 null；禁止臆造 |
| `intent_type` | 枚举 | `refund` / `return` / `exchange` / `repair` / `cancel` / `logistics` / `invoice` / `other` |
| `expected_resolution` | `str` | 用户期望方案，短文本 |
| `raw_summary` | `str` | 一句话摘要 |

实现：Pydantic 模型 + `model.with_structured_output(...)`。抽取专用 Prompt：只抽取、不闲聊。

## 6. 会话存储（SQLite + SQLAlchemy）

### 6.1 表

- **sessions**：`id` (UUID PK), `created_at`, `updated_at`
- **messages**：`id`, `session_id` (FK), `role` (`user`|`assistant`), `content`, `created_at`, `seq`

### 6.2 行为

- 无 `session_id`：创建 session，经 SSE `session` 事件或 JSON 字段返回 id。
- 有 `session_id`：加载历史；不存在 → 404。
- `DATABASE_URL` 默认 `sqlite:///./shoppilot.db`；启动时 `create_all`。

## 7. 上下文裁剪

1. System 始终注入，不计入「历史轮数」。
2. 硬上限：最近 `MAX_HISTORY_TURNS` 轮（一对 user+assistant 为 1 轮；末尾孤儿 user 计 1 轮）。
3. Token 预算：`MAX_CONTEXT_TOKENS`（含 system 估算）；超出则从最旧非 system 消息删除，尽量成对删除。
4. Token 估算：字符粗估（如 `ceil(len/4)`）；不引入外部 tokenizer 服务。
5. **裁剪只影响送模上下文；库内消息全文保留。**

## 8. 配置（`.env`）

| 变量 | 用途 |
|------|------|
| `OPENAI_API_BASE` | 上游 Base URL |
| `OPENAI_API_KEY` | 密钥 |
| `MODEL_NAME` | 模型名 |
| `MAX_CONTEXT_TOKENS` | 送模 token 预算 |
| `MAX_HISTORY_TURNS` | 历史轮数硬上限 |
| `DATABASE_URL` | SQLAlchemy URL |

GPT / Claude / DeepSeek / Ollama 等均通过更换上述三项模型配置接入，应用代码不按厂商分支。

## 9. 聊天页（Vibe Coding 例外）

- 极简单页：气泡列表、输入框、持有 `session_id`、消费 SSE 逐字展示。
- 可选按钮：对当前输入调用 `/extract`。
- FastAPI `StaticFiles` 挂载；不强制前端框架。
- **不**走 brainstorm / TDD / formal code review；按用户描述迭代效果。

## 10. 验证策略

| 对象 | 方式 |
|------|------|
| SSE / 多轮 | 手工 `curl` + 同 session 两轮；`GET messages` |
| 结构化抽取 | 标注样例集 `evals/aftersales_extract.jsonl`（≥5 条）+ 脚本比对 `intent_type` / `order_id`；`expected_resolution` 宽松 |
| `context_manager` | 单元测试：超预算时送模规模不超过配置 |
| Prompt 文案 | 样例与人工抽查，不作无意义单测 |

说明：可单测的模块（裁剪等）可写测试；纯 Prompt / 数据类抽取以评估集验证，替代严格 TDD 红绿循环。聊天页除外。

## 11. 推荐目录结构（实现时以 plan 为准）

```
app/
  main.py
  api/
  services/
  llm/
  prompts/
  db/
evals/
  aftersales_extract.jsonl
static/          # 聊天页
docs/superpowers/
dev-notes/ch01.md
.env.example
requirements.txt
```

## 12. 决策记录（brainstorm）

| 议题 | 决策 |
|------|------|
| 编排方式 | 薄 FastAPI + LangChain LCEL（非 LangGraph、非手写 httpx） |
| 历史权威 | 服务端 SQLite；客户端只传 `session_id` + 本轮 `message` |
| 抽取暴露 | `/extract` + `chat?mode=extract` 两者都做 |
| 诉求枚举 | 8 类（含 cancel/logistics/invoice） |
| 裁剪 | token 预算为主 + 轮数硬上限，均可配置 |
| 聊天页 | 本章做，Vibe Coding |
| 流式失败落库 | 不落残缺 assistant |

## 13. 风险与开放点

- 不同上游对 `with_structured_output` / tool_calling 支持不一；若某模型走不通，**停下来问用户**，不擅自改协议。
- Token 粗估与真实计费可能有偏差；本章以预算控制为目的即可。
- 单进程 SQLite 足够本地验收；并发与部署不在本章范围。
