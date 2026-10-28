# ShopPilot 阶段二设计：工具调用 + MySQL

日期：2026-10-28
状态：待确认

## 意图

顾客现在问「我的工单到哪一步了」「这款杯子有没有蓝色、多少钱」时，AI 只能回答
「以商品详情页为准」。这一轮让 AI 能**真的去查**——通过工具读取工单与商品数据，
基于真实结果回答，不再靠话术兜底。

同时把存储从 SQLite 迁到 Docker MySQL，为后续多实例、更大数据量做准备。

## 目标

1. AI 能自主决定是否调用工具、调哪个、传什么参数
2. 工具：查商品、查工单
3. 工具参数经过校验，非法输入返回结构化错误供模型纠正，而不是抛异常中断对话
4. 工单查询按当前顾客隔离，不能查别人的工单
5. 存储迁移到 Docker MySQL，现有会话与消息不丢
6. 前端能看到「正在查询…」的工具调用状态

## 非目标

- 写操作工具（改工单、下单、退款）
- 工具调用的人工审批
- 知识库 / RAG（商品检索走结构化查询，不是向量检索）
- 登录、跨设备身份

## 反目标

- 不允许模型在没有工具返回结果的情况下编造工单状态或价格
- 不允许工具被当作任意 SQL 入口——所有查询参数走预编译语句
- 不允许工单查询越过 visitor 边界

## 架构变更

```
浏览器 ──POST /api/chat──> FastAPI ──> LangGraph Agent ──┬─> ChatOpenAI (DeepSeek)
   ▲                            │                        └─> @tool 查商品 / 查工单
   └──── SSE: delta / tool ─────┘                                   │
                                                             MySQL (Docker)
```

编排从「直接调 LLM」换成 LangGraph 的 ReAct 循环：模型可多轮调用工具，
拿到结果后继续推理，直到给出最终回答。

## 数据模型（MySQL）

```sql
-- 会话与消息：从 SQLite 平移，字段不变
conversations(id VARCHAR(32) PK, visitor_id VARCHAR(64), created_at DATETIME(6))
messages(id BIGINT AUTO_INCREMENT PK, conversation_id VARCHAR(32),
         role VARCHAR(16), content MEDIUMTEXT, created_at DATETIME(6))

-- 新增：商品
products(id VARCHAR(32) PK, name VARCHAR(128), category VARCHAR(64),
         color VARCHAR(32), price_cents INT, stock INT,
         description TEXT)

-- 新增：工单
work_orders(id VARCHAR(32) PK, visitor_id VARCHAR(64), order_no VARCHAR(32),
            product_id VARCHAR(32), status VARCHAR(32),
            issue TEXT, created_at DATETIME(6), updated_at DATETIME(6))
```

字符集 `utf8mb4`，时区 UTC。`work_orders.visitor_id` 是访问控制字段。

## 工具设计

两个工具都用 LangChain `@tool` 定义，带 Pydantic 参数模型做校验。

### `lookup_product`

| 参数 | 类型 | 校验 |
|---|---|---|
| `keyword` | str \| None | 至少一个参数有值；长度 1–32 |
| `product_id` | str \| None | 同样 |

返回：匹配商品列表（ID、名称、颜色、价格、库存）。最多 5 条。
**不暴露成本价、供应商等内部字段。**

### `lookup_work_order`

| 参数 | 类型 | 校验 |
|---|---|---|
| `order_no` | str | 必填，格式 `^[A-Z0-9]{8,32}$` |

返回：该顾客名下、指定订单号的工单状态、关联商品、最后更新时间。
**调用时自动注入当前 visitor_id**（通过 LangGraph runtime context），
查询条件同时匹配 `order_no` 与 `visitor_id`。查不到时返回「未找到该订单的工单」，
不透露该订单号是否存在。

### 参数校验策略

用 Pydantic 模型 + `@tool(args_schema=...)`。校验失败时**不抛异常**，
返回 `{"error": "参数不合法：order_no 需为 8-32 位大写字母或数字"}`，
让模型自行修正后重试。连续失败 2 次仍不合法，则模型按提示词要求向顾客澄清。

## 流式协议变更

`POST /api/chat` 的 SSE 事件新增两类：

| 载荷 | 含义 |
|---|---|
| `{"delta": "文本"}` | 文本增量（不变） |
| `{"tool": "lookup_product", "status": "start"}` | 工具开始调用 |
| `{"tool": "lookup_product", "status": "done"}` | 工具返回 |

前端在消息气泡上方显示「正在查询商品…」的轻量状态条，完成后消失。
工具入参与返回值不直接展示给顾客。

## 依赖

| 包 | 用途 |
|---|---|
| `langgraph` | ReAct 循环编排 |
| `pymysql` | MySQL 驱动 |
| `cryptography` | MySQL 8 默认认证插件需要 |

## 本地环境

`docker-compose.yml` 起 MySQL 8.4：

- 端口 `3307:3306`（避开本机可能占用的 3306）
- 数据卷持久化
- 库名 `shoppilot`，账号密码从 `.env` 读取

首次启动自动建表并灌入种子数据（若干商品 + 该访客的示例工单）。

## 迁移

一次性脚本 `scripts/migrate_sqlite_to_mysql.py`：读现有 `data/shoppilot.db`，
写入 MySQL，保留原 `id` 与时间戳。执行后 SQLite 文件保留不删。

## 验收标准

1. 顾客问「你们有蓝色的杯子吗」，AI 调用 `lookup_product`，回答基于真实数据
2. 顾客问「我的订单 12345678 到哪了」，AI 调用 `lookup_work_order`
3. 传入不存在的订单号，回答「未找到」，且不泄露该单号是否存在
4. 用 A 顾客的 order_no 在 B 顾客浏览器里查询，查不到
5. 工具参数非法时，模型收到错误提示并纠正，对话不中断
6. 前端显示工具调用状态，结束后消失
7. 现有历史会话迁移后仍可查看
8. `uv run pytest` 全绿

## 留给构建者的决定

- 种子数据的商品与工单内容
- 工具调用状态的视觉样式
- 迁移脚本是否需要 dry-run 模式
