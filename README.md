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
| `SILICONFLOW_API_KEY` | 嵌入服务密钥 | `sk-...` |
| `EMBED_MODEL` | 嵌入模型 | `BAAI/bge-m3` |
| `EMBED_BASE_URL` | 嵌入服务地址 | `https://api.siliconflow.cn/v1` |
| `VECTOR_DB_PATH` | Milvus Lite 数据文件 | `data/milvus.db` |

> 不要用 `MILVUS_URI` 作变量名——pymilvus 会把它当服务端地址解析，
> 相对文件路径会被判为非法 URI。

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
- `GET /api/orders?visitor_id=...` — 该访客的订单，含已关联工单
- `GET /api/work-orders?visitor_id=...` — 该访客的工单
- `POST /api/work-orders/drafts` — 顾客主动提单时建草稿
- `POST /api/work-orders` — 凭 `draft_id` 确认提交（只接受 id，不接受内容）
- `GET /health` — 健康检查

SSE 事件：

| 载荷 | 含义 |
|---|---|
| `{"conversation_id": "..."}` | 会话 ID（首个事件） |
| `{"delta": "..."}` | 文本增量 |
| `{"tool": "...", "label": "...", "status": "start\|done"}` | 工具调用状态 |
| `{"work_order_draft": {...}}` | 待确认工单草稿，由后端直推 |
| `[DONE]` | 正常结束 |
| `{"error": "..."}` | 出错 |

## 工具

| 工具 | 能力 | 约束 |
|---|---|---|
| `lookup_product` | 按关键词或 ID 查商品价格、颜色、库存 | 不返回成本价等内部字段 |
| `list_my_orders` | 列出当前顾客的订单 | 无参数，只返回本人订单 |
| `lookup_work_order` | 按订单号查工单进度 | 只能查本人；查不到时不区分「不存在」与「不属于你」 |
| `draft_work_order` | 起草待确认的工单 | **不写库**，仅生成草稿供前端确认 |
| `search_knowledge` | 按语义检索商品与本人历史工单 | 只能命中公开数据与本人私有数据 |

参数经 Pydantic 校验，非法时返回结构化错误让模型自行纠正，不中断对话。

## 向量检索

**只有政策知识进向量库。** 商品与工单按结构化条件查询——工单是私有数据，
放进索引会在检索层与回表层各需要一次隔离校验；商品规模小，全量返回让
模型判断比语义检索更可靠。

FAQ 写入 MySQL 后嵌入（BGE-M3，1024 维）并写入 Milvus Lite。
MySQL 是唯一事实来源，向量索引可随时重建；检索命中的内容一律回表取原文。

```bash
# 导入政策文档（可重复执行）
uv run python -m scripts.import_faq

# 服务停止时重建索引
uv run python -m scripts.reindex --drop

# 服务运行中（Milvus Lite 独占文件，须在服务进程内重建）
curl -X POST "http://127.0.0.1:8000/api/admin/reindex?drop=true"
```

`POST /api/admin/reindex` 当前无鉴权，生产部署前必须加保护。

## 政策知识库

`data/faq/` 存放 11 篇政策文档，覆盖退货、退款、物流、发票、价保、会员、
投诉，按 `## ` 小节切分为 47 条知识。

> **来源与许可**：文档取自第三方开源仓库，该仓库未附带 LICENSE，
> 当前仅用于本地开发与演示，条款内容亦为虚构规则。
> 上线前必须替换为本店真实政策。详见 [data/faq/README.md](data/faq/README.md)。

## 工单写入路径

AI 无法直接建单。工单只可能由顾客点击产生，两条路径：

1. **订单提单**：我的订单 → 选订单 → 填类型与描述 → 提交
2. **无订单提单**：面板顶部按钮 → 选「咨询／投诉建议／其他」 → 填描述 → 提交
3. **聊天起草**：AI 起草 → 界面出现确认卡片 → 顾客点「确认提交」

草稿存库（30 分钟过期），确认接口只接受 `draft_id`，内容不可篡改。

### 问题类型与订单要求

| 类型 | 需要订单号 |
|---|---|
| 运输破损 / 发货延迟 / 质量问题 / 退换货 | 是 |
| 咨询 / 投诉建议 / 其他 | 否 |

校验在 Pydantic 层，模型无法绕过。无订单工单按「访客 + 类型」判重。

## 暂不包含

登录、跨设备身份、知识库/RAG、工具写操作（改工单、退款）、转人工、部署。
详见 `docs/superpowers/specs/`。
