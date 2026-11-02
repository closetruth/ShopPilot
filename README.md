# ShopPilot

电商智能客服系统。对话走 LangGraph 图：意图分流四路（知识检索 / 业务
agent / 投诉模板 / 闲聊模板），AI 客服能查真实商品数据与顾客自己的工单
进度，会话工作状态经 MySQL checkpoint 持久化，支持多会话。

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
| `CHECKPOINT_BACKEND` | 会话状态 checkpoint 存储 | `mysql`（默认）/ `sqlite` |
| `CONTEXT_WINDOW` | token 预算的模型窗口口径 | `8192`（紧凑，生产配真实窗口如 131072） |
| `SILICONFLOW_API_KEY` | 嵌入服务密钥 | `sk-...` |
| `EMBED_MODEL` | 嵌入模型 | `BAAI/bge-m3` |
| `EMBED_BASE_URL` | 嵌入服务地址 | `https://api.siliconflow.cn/v1` |
| `VECTOR_DB_PATH` | Milvus Lite 数据文件 | `data/milvus.db` |
| `TURN_TOKEN_LIMIT` | 单轮 token 上限（第一层成本闸） | `8000` |
| `CONVERSATION_DAILY_TOKEN_BUDGET` | 会话日 token 预算，0 表示关闭 | `0` |
| `GLOBAL_DAILY_COST_YUAN` | 全局日成本上限（元），0 表示关闭 | `0` |
| `LLM_PRICE_INPUT_PER_MTOK` | 输入价目（元/百万 token），不填则成本记 0 | `0` |
| `LLM_PRICE_OUTPUT_PER_MTOK` | 输出价目（元/百万 token），不填则成本记 0 | `0` |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` | Langfuse 追踪，三项缺一即不追踪 | `pk-lf-...` / `sk-lf-...` / `http://langfuse:3000` |

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
  main.py      服务装配：lifespan、路由挂载、静态托管（薄壳）
  api/
    customer.py  顾客侧：对话 SSE、历史、订单、工单、FAQ 详情
    admin.py     管理侧：FAQ CRUD、批量导入、重建索引
    _graph.py    对话图单例
  graph.py     StateGraph 组装：消解→意图→四路分流→日志
  agent.py     业务主力 agent（ReAct，业务四件工具，步数/token 上限）
  intent.py    意图分类：规则先行，LLM 兜底
  resolve.py   指代消解：代词/短消息按需触发
  knowledge_node.py 知识检索节点：改写→检索→0.65 置信度闸→生成回答
  query_rewrite.py  售后口语→政策术语的词表（意图售后词的单一代码源）
  templates.py 投诉/闲聊固定话术（零 LLM），投诉自动建工单
  tools/
    registry.py  工具注册中心：ToolSpec 元数据 + 注册表 + 包装工具
    builtin.py   内置工具：查商品 / 查订单 / 查工单 / 建工单（待确认）
    permission.py 权限模型：调用者白名单 + 数据范围 + 风险等级
    executor.py  执行引擎：校验 → 鉴权 → 预览 → 执行 → 审计
    context.py   AgentContext（注入给工具的访客身份）
    mcp.py       MCP 客户端：拉外部工具并入同一注册表
  schemas.py   工具参数校验模型
  checkpointer.py 会话工作状态 checkpoint（MySQL）
  llm.py       ChatOpenAI 工厂（OpenAI 兼容；流式/非流式两个实例）
  embedding.py BGE-M3 嵌入（硅基流动）
  vector_store.py Milvus 索引与检索（仅 FAQ）
  db.py        MySQL 连接、建表、旧表补列
  repo/
    chats.py     会话与消息
    commerce.py  商品、订单、工单、工单草稿
    knowledge.py FAQ 政策知识
    insights.py  低置信度问题、agent 轨迹
    audit.py     工具调用审计流水
    pending.py   待确认动作
  store.py     上述仓储的转发门面，调用方统一从这里访问
  static/      聊天页面（`/`）与管理页（`/admin`、`/admin/tools`）
mcp_servers/
  logistics/server.py  示例 MCP 服务器（物流时效，stdio）
mcp_servers.json        MCP 服务器配置
scripts/
  seed.py                    演示数据（幂等）
  import_faq.py              政策文档导入（幂等）
  reindex.py                 向量索引重建
  migrate_sqlite_to_mysql.py 旧 SQLite 数据导入（幂等，支持 --dry-run）
data/faq/      政策文档（随仓库版本控制）
docs/          设计文档与提示词
```

## 知识库管理

浏览器打开 `/admin`，可查看、新建、编辑、停用、删除政策知识。
保存后立即同步向量索引，无需手动重建。

**批量导入**：顶栏「批量导入」可粘贴 Markdown 文档，按标题层级自动切成
多条知识，预览确认后一次入库。单节超过 500 字按段落再切；代码围栏与表格
内部的 `#` 不会被当成标题。

```
POST   /api/admin/faq/bulk/preview        切分预览（不写库）
POST   /api/admin/faq/bulk                批量入库（≤200 条）
```

```
GET    /api/admin/faq                     列表（含停用项）
GET    /api/admin/faq/categories          分类与条目数
POST   /api/admin/faq                     新建
PUT    /api/admin/faq/{id}                编辑
DELETE /api/admin/faq/{id}                删除
POST   /api/admin/faq/{id}/toggle         启用/停用
GET    /api/admin/faq/search?q=           调试：查看原始相似度分数
POST   /api/admin/reindex                 全量重建索引
```

> **管理接口目前没有鉴权**，任何能访问服务的人都能修改知识库。
> 上线前必须加保护。

正文要求 20–2000 字，标题与小节名各 2–64 字。小节名参与嵌入，建议写具体
（「运费承担」优于「其他说明」）。

## 接口

- `POST /api/chat` — 请求 `{"message": "...", "visitor_id": "...", "conversation_id": "可选"}`，返回 SSE 流
- `GET /api/history?conversation_id=...&visitor_id=...` — 拉取历史，越权返回 404，每条消息带 `id`
- `GET /api/conversations?visitor_id=...` — 该访客的会话列表
- `GET /api/orders?visitor_id=...` — 该访客的订单，含已关联工单
- `GET /api/work-orders?visitor_id=...` — 该访客的工单
- `POST /api/work-orders/drafts` — 顾客主动提单时建草稿
- `POST /api/work-orders` — 凭 `draft_id` 确认提交（只接受 id，不接受内容）
- `POST /api/tool/confirm` — 确认待确认动作，请求 `{"action_id": "...", "visitor_id": "..."}`
- `POST /api/tool/cancel` — 取消待确认动作，同上
- `GET /api/admin/tools` — 工具注册清单
- `GET /api/admin/audit` — 工具调用流水，支持 `tool` / `visitor_id` / `status` / `limit` / `offset`
- `POST /api/feedback` — 反馈，`{"message_id": 1, "rating": 1|-1, "reason": "可选", "comment": "可选"}`；重复提交覆盖，`rating: 0` 撤销
- `GET /api/admin/eval/cases` — 评估样本列表，支持 `status` / `limit`
- `POST /api/admin/eval/cases` — 新增样本（落候选池）
- `POST /api/admin/eval/cases/{id}/status` — 状态流转 `candidate` / `accepted` / `retired`
- `POST /api/admin/eval/cases/from-feedback/{id}`、`/from-low-conf/{id}`、`/from-item/{id}` — 三路转候选
- `GET /api/admin/flywheel/feedback` / `cost` / `low-confidence` — 看板数据
- `GET /api/admin/eval/runs`、`GET /api/admin/eval/runs/{run_id}` — 评估运行与明细
- `POST /api/admin/eval/run` — 触发评估，`{"limit": 1-50, "case_ids": 可选, "judge": false}`，已有运行在跑返回 409
- `GET /health` — 健康检查

SSE 事件：

| 载荷 | 含义 |
|---|---|
| `{"conversation_id": "..."}` | 会话 ID（首个事件） |
| `{"message_id": 123}` | 本条回答的消息 ID，`[DONE]` 前推送，反馈靠它绑定 |
| `{"delta": "..."}` | 文本增量 |
| `{"tool": "...", "label": "...", "status": "start\|done"}` | 工具调用状态 |
| `{"pending_action": {...}}` | 待确认动作（卡片内容由执行引擎生成） |
| `[DONE]` | 正常结束 |
| `{"error": "..."}` | 出错 |

## 对话流程

每条消息经过 LangGraph 图：

```
指代消解（按需） → 意图识别（规则先行，LLM 兜底）
  → 知识检索：query 词表改写（售后口语→政策术语）→ 向量检索
     → 0.65 置信度闸（不足时原话回退取优）→ 带引用回答 / 拒答+落库
  → 业务数据：主力 agent（业务四件工具，步数 15 / token 8000 上限）
  → 投诉：固定话术 + 自动建「投诉建议」工单
  → 闲聊：固定话术（零 LLM）
  → 日志：agent_traces 落一条（意图/置信度/节点路径/改写词条/耗时）
```

消息历史存 MySQL 消息表；图的会话工作状态（消解结果、意图、槽位、轮次）
经 checkpoint 存同一 MySQL 库，跨进程续跑。

上下文按 token 预算组装（`CONTEXT_WINDOW × 0.25`，减去 system/工具/
输出余量）：梗概（水位线前历史的压缩，事实型、单表存储、后台异步生成）
+ 预算内原文窗口，一起接进主力 agent。原文超预算从最旧截断，梗概接续。
每轮预算账目落 agent_traces，`GET /api/admin/context-usage` 可查。

## 工具

| 工具 | 能力 | 约束 |
|---|---|---|
| `lookup_product` | 按关键词或 ID 查商品价格、颜色、库存 | 不返回成本价等内部字段 |
| `list_my_orders` | 列出当前顾客的订单 | 无参数，只返回本人订单 |
| `lookup_work_order` | 按订单号查工单进度 | 只能查本人；查不到时不区分「不存在」与「不属于你」 |
| `create_work_order` | 准备提交售后工单 | **不写库**，先生成确认卡片，顾客点击后才真正建单 |

参数经 Pydantic 校验，非法时返回结构化错误让模型自行纠正，不中断对话。

### 注册中心与执行引擎

所有工具（含 MCP 外部工具）都在 `app/tools/registry.py` 注册，元数据统一
描述三层权限：数据范围 `scope`（none/visitor/admin）、风险 `risk`
（read/write/destructive）、调用者白名单 `allowed_callers`。
`destructive` 工具在注册期就强制要求顾客确认，配置写错直接启动失败。

调用一律经过 `app/tools/executor.py`：校验 → 鉴权 → （预览）→ 执行 →
审计。每次调用写一行 `tool_audit_logs`，含参数掩码、结果摘要、错误与耗时；
被拒绝和超时的调用同样留痕。`GET /admin/tools` 可看注册清单与调用流水。

需要顾客确认的工具用 `requires_confirmation=True` + `preview` / `summarize`
两个钩子：`preview` 在挂起前做只读检查（不通过就不出卡片），`summarize`
生成卡片内容。动作存 `pending_actions` 表，30 分钟过期，确认时参数从库里
重放——前端只传 `action_id`，不传参数。

**与 `work_order_drafts` 的关系**：草稿表服务「顾客手填提单」这条独立入口
（`POST /api/work-orders/drafts`），`pending_actions` 服务「AI 提议、顾客
确认」这条链路。两者并行，互不影响。

### MCP 外部工具

`mcp_servers.json` 描述要连的服务器，stdio 传输会拉起子进程：

```json
{
  "servers": {
    "logistics": {
      "transport": "stdio",
      "command": "python",
      "args": ["-m", "mcp_servers.logistics.server"],
      "enabled": true,
      "scope": "none",
      "risk": "read",
      "requires_confirmation": false,
      "allowed_callers": ["agent"]
    }
  }
}
```

- `command: "python"` 会被替换成当前解释器，避免 uv 环境里找不到
- `scope` / `risk` / `requires_confirmation` / `allowed_callers` 可按服务器覆盖
- 工具在注册表里叫 `{server}__{tool}`，`source="mcp"`
- 单台服务器连不上只记 warning 并跳过，服务照常启动
- `MCP_ENABLED=1` 开启，`MCP_CONFIG` 指定别的配置文件路径

`mcp_servers/logistics/server.py` 是一个示例服务器（`query_shipping_eta`）。
stdio 传输下 stdout 被 JSON-RPC 独占，服务器日志必须写 stderr。

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

## 数据飞轮

闭环是「反馈 → 样本 → 评估 → 回归」，四步都落在 MySQL，看板入口 `/admin/flywheel`。

六张表：`message_feedback`（反馈）、`llm_usage`（用量计量）、`budget_events`（触闸事件）、
`eval_cases`（样本）、`eval_runs` / `eval_run_items`（运行与明细）。

反馈一条回答最多一行，重复提交覆盖，`rating: 0` 撤销；标签与文字都可留空，
点踩即刻落一条最小信息，之后可再补原因。

成本三层软闸全部是降级不拒绝：单轮 token 上限换短上下文，会话日预算与全局日成本
换兜底话术并记 `budget_events`。观测类写入一律 try/except 兜底，不许打断对话。

评估默认只用确定性指标（引用命中、引用合规、拒答准确、意图准确），LLM 裁判靠
`--judge` 显式开启。手工触发走看板，定时触发走 CLI：

```bash
uv run python scripts/eval_pipeline.py --limit 30
```

阈值 `pass_rate >= 0.8`、`citation_valid_rate >= 0.95` 未达标时退出码为 1，可直接挂计划任务。
样本默认落候选池，人工采纳才进默认评估集。

Langfuse 是可选件：缺配置或装配失败一律静默降级为不追踪，只服务排障，看板不依赖它。
远程自托管编排在 `deploy/langfuse-compose.yml`，环境变量模板 `deploy/langfuse.env.example`。

## 暂不包含

登录、跨设备身份、工具写操作（改工单、退款）、转人工、部署。
详见 `docs/superpowers/specs/`。