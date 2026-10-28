# ShopPilot

电商智能客服系统。AI 客服能查真实商品数据与顾客自己的工单进度，支持多会话。

## 快速开始

```bash
docker compose up -d                      # 起 MySQL 8.4（端口 3307）
uv sync
cp .env.example .env                      # 填入 DeepSeek key
uv run python -m scripts.seed             # 可选：灌演示商品与工单
uv run uvicorn app.main:app --reload
```

浏览器打开 <http://127.0.0.1:8000>。

## 环境变量

| 变量 | 说明 | 示例 |
|---|---|---|
| `OPENAI_API_KEY` | 模型服务 API key | `sk-...` |
| `OPENAI_BASE_URL` | OpenAI 兼容接口地址 | `https://api.deepseek.com/v1` |
| `CHAT_MODEL` | 模型名 | `deepseek-chat` |
| `HISTORY_LIMIT` | 每轮携带的历史消息条数 | `20` |
| `MYSQL_HOST` / `MYSQL_PORT` | 数据库地址 | `127.0.0.1` / `3307` |
| `MYSQL_USER` / `MYSQL_PASSWORD` | 数据库账号 | `shoppilot` / `shoppilot` |
| `MYSQL_DATABASE` | 库名 | `shoppilot` |

换模型供应商只改前三个变量，代码无需改动。

## 测试

```bash
uv run pytest
```

测试连的是同一个 MySQL 库，用随机 `visitor_id` 隔离，不影响已有数据。

## 结构

```
app/
  main.py      FastAPI：SSE 对话、历史、会话列表、静态托管
  agent.py     LangGraph ReAct Agent 构建
  tools.py     lookup_product / lookup_work_order
  schemas.py   工具参数校验模型
  llm.py       ChatOpenAI 工厂（OpenAI 兼容）
  store.py     MySQL 读写：会话、消息、商品、工单
  prompt.py    客服人设与工具使用规则
  static/      聊天页面
scripts/
  seed.py                    演示数据（幂等）
  migrate_sqlite_to_mysql.py 旧 SQLite 数据导入（幂等，支持 --dry-run）
docs/          设计文档与提示词
plans/         实施计划
```

## 接口

- `POST /api/chat` — 请求 `{"message": "...", "visitor_id": "...", "conversation_id": "可选"}`，返回 SSE 流
- `GET /api/history?conversation_id=...&visitor_id=...` — 拉取历史，越权返回 404
- `GET /api/conversations?visitor_id=...` — 该访客的会话列表
- `GET /health` — 健康检查

SSE 事件：

| 载荷 | 含义 |
|---|---|
| `{"conversation_id": "..."}` | 会话 ID（首个事件） |
| `{"delta": "..."}` | 文本增量 |
| `{"tool": "lookup_product", "label": "商品", "status": "start\|done"}` | 工具调用状态 |
| `[DONE]` | 正常结束 |
| `{"error": "..."}` | 出错 |

## 工具

| 工具 | 能力 | 约束 |
|---|---|---|
| `lookup_product` | 按关键词或 ID 查商品价格、颜色、库存 | 不返回成本价等内部字段 |
| `lookup_work_order` | 按订单号查工单进度 | 强制注入当前 visitor_id，只能查本人；查不到时不区分「不存在」与「不属于你」 |
| `create_work_order` | 提交售后工单 | 需顾客明确同意；系统内无此订单归属时拒绝；同单同类型未完结不重复建 |

参数经 Pydantic 校验，非法时返回结构化错误让模型自行纠正，不中断对话。

## 暂不包含

登录、跨设备身份、知识库/RAG、工具写操作（改工单、退款）、转人工、部署。
详见 `docs/superpowers/specs/`。
