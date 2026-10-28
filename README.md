# ShopPilot

电商智能客服系统。阶段一：纯对话 MVP——网页聊天、流式回复、多轮记忆、会话落库。

## 快速开始

```bash
uv sync
cp .env.example .env    # 填入你的 DeepSeek key
uv run uvicorn app.main:app --reload
```

浏览器打开 <http://127.0.0.1:8000>。

## 环境变量

| 变量 | 说明 | 示例 |
|---|---|---|
| `OPENAI_API_KEY` | 模型服务的 API key | `sk-...` |
| `OPENAI_BASE_URL` | OpenAI 兼容接口地址 | `https://api.deepseek.com/v1` |
| `CHAT_MODEL` | 模型名 | `deepseek-chat` |
| `HISTORY_LIMIT` | 每轮携带的历史消息条数 | `20` |
| `DB_PATH` | SQLite 文件路径 | `data/shoppilot.db` |

换模型供应商只改前三个变量，代码无需改动。

## 测试

```bash
uv run pytest
```

## 结构

```
app/
  main.py      FastAPI：SSE 对话接口、历史接口、静态托管
  llm.py       ChatOpenAI 工厂（OpenAI 兼容）
  store.py     SQLite 会话与消息读写
  prompt.py    客服人设
  static/      聊天页面
docs/          设计文档
plans/         实施计划
```

## 接口

- `POST /api/chat` — 请求 `{"message": "...", "conversation_id": "可选"}`，返回 SSE 流
- `GET /api/history?conversation_id=...` — 拉取历史消息
- `GET /health` — 健康检查

SSE 事件：`{"conversation_id": "..."}` → `{"delta": "..."}` 多次 → `[DONE]`，异常时为 `{"error": "..."}`。

## 阶段一不包含

登录、多用户、知识库、订单工具调用、转人工、部署。详见 `docs/superpowers/specs/`。
