# ShopPilot 阶段一设计：纯对话 MVP

日期：2026-10-28
状态：已确认

## 意图

为电商店铺做一个智能客服系统。阶段一的目标是**跑通纯对话链路**：
用户在网页上发消息，AI 以店铺客服身份流式回复，多轮上下文不丢失，
会话落库可追溯。这一阶段不追求"能回答业务问题"，只追求
**链路正确、结构可扩展**——后续的 RAG、订单工具、转人工都挂在这条链路上。

## 目标

1. 网页聊天界面，发送消息后流式逐字显示回复
2. 多轮对话上下文保持（取最近 N 条历史）
3. 会话与消息持久化到 SQLite，刷新页面后历史还在
4. 模型接入走 OpenAI 兼容接口，换供应商只改 `.env`
5. 客服人设可独立维护

## 非目标

- 登录 / 多用户隔离
- 商品与政策知识库（RAG）
- 订单、物流、退款等业务工具调用
- 人工转接、工单系统
- 意图识别、指标看板、告警
- Docker / CI / 部署
- 前端框架

## 反目标

- 宁可少一个功能，也不允许"看起来能答其实在编"。凡涉及具体政策、
  价格、时效，阶段一由人设 prompt 强制要求以商品页为准，
  不得虚构具体数字。
- 不允许为了阶段一方便而写死供应商——换模型必须是改配置而非改代码。

## 约束

- Python 3.13（uv 管理），依赖锁定在 `pyproject.toml`
- 单人本地运行，`uv run uvicorn` 起服务
- API key 只存在 `.env`，不进版本库

## 架构

```
浏览器 ──POST /api/chat──> FastAPI ──> ChatOpenAI(base_url=DeepSeek)
   ▲                          │                    │
   └──── SSE 流式 delta ──────┘<── astream 分块 ───┘
                              │
                          SQLite
```

### 模块职责

| 文件 | 职责 |
|---|---|
| `app/main.py` | FastAPI 应用；`POST /api/chat` SSE 端点；静态文件托管 |
| `app/llm.py` | `ChatOpenAI` 工厂，从环境变量读 key / base_url / model；启动时校验 |
| `app/store.py` | SQLite 建表、读历史、追加消息；连接按请求创建 |
| `app/prompt.py` | 客服人设 system prompt 常量 |
| `app/static/` | 单页聊天界面 |

### 数据模型

```sql
CREATE TABLE conversations (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL
);

CREATE TABLE messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    role            TEXT NOT NULL,      -- user | assistant
    content         TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_messages_conv ON messages(conversation_id, id);
```

### SSE 协议

响应 `Content-Type: text/event-stream`，每个事件一行 `data: <json>`：

| 载荷 | 含义 |
|---|---|
| `{"delta": "文本片段"}` | 增量内容 |
| `[DONE]` | 正常结束（非 JSON，字面量） |
| `{"error": "人话描述"}` | 出错，前端展示为错误气泡 |

### 上下文策略

每轮请求构造：

```
[SystemMessage(prompt.SYSTEM_PROMPT)] + 最近 HISTORY_LIMIT 条历史 + 本次用户消息
```

不引入摘要压缩。`HISTORY_LIMIT` 默认 20，可通过环境变量调整。

### 错误处理边界

只处理三种，不铺开：

1. **缺 key / 配置错误** → 进程启动时抛错退出，不拖到运行时
2. **上游 4xx/5xx / 超时** → SSE 推 `error` 事件，前端显示错误气泡，本轮 assistant 消息不落库
3. **客户端中断** → 生成器被 `GeneratorExit` 关闭，`finally` 中把已累计的内容落库（若长度 > 0）

### 会话标识

前端在 `localStorage` 存 `conversation_id`（UUID）。首次请求后端创建并回传。
阶段一不做"新建会话"按钮，清 localStorage 即开新会话。

## 人设 Prompt 要点

- 角色：店铺在线客服
- 语气：简洁、礼貌、口语化，不堆砌客套
- 边界：政策类问题（退换货、运费、时效）说明"以商品详情页为准"，
  需要用户提供订单号时明确提出
- 禁止：编造具体政策条款、价格、承诺时效

完整文本见 `app/prompt.py`。

## 验收标准

1. `uv sync` 后 `uv run uvicorn app.main:app --reload` 可启动
2. 浏览器打开首页可见聊天界面
3. 连续三轮对话，第三轮 AI 能引用第一轮的内容
4. 刷新页面，历史消息仍在
5. 断开网络或填错 key，界面出现错误气泡而非白屏
6. `uv run pytest` 全绿

## 留给构建者的决定

- HTML/CSS 的具体版式与配色
- 单测覆盖到哪几个函数（最低要求：store 的读写与历史截断）
