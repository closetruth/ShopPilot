# r20 数据飞轮设计

## 1. 一句话

把「对话现场」变成「可归因、可度量、可复跑、可改进」的闭环：顾客反馈与轨迹
负责发现问题，成本与 Langfuse 负责定位问题，评估流水线负责证明改动有效。

```
采集                归因                    沉淀               验证           改进
反馈 / 轨迹 / 低置信  →  Langfuse 瀑布 + 成本  →  候选样本  →  评估流水线  →  改 FAQ / 词表 / 阈值 / prompt
   ↑                                                                                    │
   └────────────────────────────────────────────────────────────────────────────────────┘
```

## 2. 现状与断点

| 已有 | 位置 | 本轮怎么用 |
|---|---|---|
| 轮级轨迹 `agent_traces` | `app/repo/insights.py` | 飞轮原始料，补 `insufficient` 字段 |
| 低置信度池 `low_confidence_queries` | `app/repo/insights.py` | 候选样本来源之一 |
| 工具审计 `tool_audit_logs` | `app/repo/audit.py` | 失败归因第三来源 |
| 业务 agent 双上限（步数/token） | `app/chat/agent.py` | 单轮闸保留，扩展到会话与全局 |
| token 计数 | `app/context/tokens.py` | 成本计量复用同一口径 |
| 检索评估脚本 + 473 条标注集 | `scripts/eval_retrieval.py` | 指标算法与样本格式复用 |
| SSE 事件通道 | `app/api/customer.py` | 多推一个 `message_id` |
| 管理页与静态托管 | `app/api/admin.py`、`app/static/admin*` | 新增飞轮看板页 |

四个断点：

1. **消息没有 id 贯通**。`append_message` 不返回 id，`/api/history` 不返回 id，
   反馈无处挂载。
2. **成本算不准**。只有业务 agent 的 token 被数过；消解、意图兜底、知识生成、
   摘要四个调用的 token 全漏。
3. **没有端到端评估**。只有离线检索评估（Recall@K），不能回答「改了 prompt
   之后答得更好还是更差」。
4. **没有负反馈入口**。顾客不满意时唯一信号是低置信度表，漏掉「检索命中了
   但答得不对」这一类。

## 3. 目标与非目标

**目标**

- 每条 AI 回答可点 👍/👎，原因标签与文字均为可选，可覆盖、可撤销
- 每次 LLM 调用落一行用量，成本按会话、按节点、按天可查
- 三层预算闸（单轮 / 会话日 / 全局日），触闸降级而不是报错
- Langfuse 全链路追踪，远程 docker 自托管，无 key 时自动关闭
- 端到端评估流水线：确定性指标为主，可挂阈值退出码
- 负反馈、低置信度、评估失败三源汇聚成候选样本，人工采纳后进评估集
- 一个飞轮看板页，把上面四块摆在一起

**非目标**

- 不做工具级限流/熔断（r19 审计已够定位）
- 不做 Langfuse 的 dataset / online scoring（本轮只用 tracing）
- 不做定时调度（不引 APScheduler/Celery；手动触发 + 系统计划任务说明）
- 不做多租户 RBAC（管理接口仍然无鉴权，仅本地开发）
- 不改检索策略本身（r13 结论仍有效）
- 不做反馈的自动采纳（噪声会污染评估集）

## 4. 结构与接线

```
app/
  observability/            # 新增：观测与成本
    __init__.py
    usage.py                # LangChain 回调 → llm_usage，价目与成本换算
    budget.py               # 三层预算判定 + 触闸记录
    langfuse.py             # 可选回调装配，缺 key 自动关闭
  evals/                    # 新增：评估逻辑
    __init__.py
    metrics.py              # 纯函数：引用抽取、单条判定、聚合
    runner.py               # 跑一次评估：读样本 → 跑图 → 落库 → 出报告
  repo/
    feedback.py             # 用户反馈读写
    usage.py                # 用量与成本聚合查询
    evals.py                # 样本 / 运行 / 单条结果
  api/
    flywheel.py             # 反馈 + 看板 + 评估接口（不塞进 300 行的 admin.py）
  static/
    admin_flywheel.html / admin_flywheel.js
scripts/
  eval_pipeline.py          # 离线跑评估（服务停止时用）
deploy/
  langfuse-compose.yml      # 远程 docker 自托管
  langfuse.env.example      # 占位符，不含真密钥
```

接线只改三处，都很薄：

- `app/api/customer.py`：入口查全局预算、`config` 挂回调、SSE 多推 `message_id`
- `app/chat/graph.py`：state 增 `insufficient` / `eval_mode`，知识节点透传
- `app/context/summarizer.py`：摘要调用带用量回调，成本不漏

## 5. 数据模型

### 5.1 `message_feedback`

```sql
CREATE TABLE IF NOT EXISTS message_feedback (
    id              BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
    message_id      BIGINT       NOT NULL,
    conversation_id VARCHAR(32)  NOT NULL,
    visitor_id      VARCHAR(64)  NOT NULL,
    rating          TINYINT      NOT NULL,
    reason          VARCHAR(32)  NULL,
    comment         VARCHAR(500) NULL,
    created_at      DATETIME(6)  NOT NULL,
    updated_at      DATETIME(6)  NOT NULL,
    UNIQUE KEY uk_feedback_message (message_id),
    KEY idx_feedback_conv (conversation_id, created_at),
    KEY idx_feedback_rating (rating, created_at),
    KEY idx_feedback_visitor (visitor_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

- `rating`：`1` 赞 / `-1` 踩。**同一 message 一人一条**，`UNIQUE` 保证，重复
  提交覆盖（`ON DUPLICATE KEY UPDATE`）。
- `reason` 可空，取值限定 `irrelevant` / `inaccurate` / `unsolved` / `verbose`
  / `other`；前端显示中文，库里存码值，聚合稳定。
- `comment` 可空，超 500 字截断。
- 不加外键：`messages` 已有级联，反馈属于观测数据，删会话时残留无妨（查询
  一律带 `conversation_id`）。

### 5.2 `llm_usage`

```sql
CREATE TABLE IF NOT EXISTS llm_usage (
    id              BIGINT      NOT NULL AUTO_INCREMENT PRIMARY KEY,
    conversation_id VARCHAR(32) NULL,
    visitor_id      VARCHAR(64) NULL,
    source          VARCHAR(16) NOT NULL,
    node            VARCHAR(32) NULL,
    model           VARCHAR(64) NOT NULL,
    input_tokens    INT         NOT NULL DEFAULT 0,
    output_tokens   INT         NOT NULL DEFAULT 0,
    cost_micros     BIGINT      NOT NULL DEFAULT 0,
    latency_ms      INT         NULL,
    created_at      DATETIME(6) NOT NULL,
    KEY idx_usage_conv_time (conversation_id, created_at),
    KEY idx_usage_source_time (source, created_at),
    KEY idx_usage_node_time (node, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

- `source`：`chat` 线上对话 / `bg` 后台摘要 / `eval` 评估运行。评估成本必须
  能与线上成本分开算，否则「今天烧了多少钱」会被自己的评估污染。
- `node`：LangGraph 注入的 `langgraph_node`（`resolve` / `classify` /
  `knowledge` / `agent`），取不到填 `unknown`。这是「哪个环节贵」的唯一依据。
- `cost_micros` 用整数微元（1 元 = 1,000,000），避免浮点累加误差。
- 一次调用一行，不做批量聚合表——聚合靠 SQL，数据量级是每日几百到几千行。

### 5.3 `budget_events`

```sql
CREATE TABLE IF NOT EXISTS budget_events (
    id              BIGINT      NOT NULL AUTO_INCREMENT PRIMARY KEY,
    scope           VARCHAR(16) NOT NULL,
    reason          VARCHAR(32) NOT NULL,
    conversation_id VARCHAR(32) NULL,
    visitor_id      VARCHAR(64) NULL,
    detail          JSON        NULL,
    created_at      DATETIME(6) NOT NULL,
    KEY idx_budget_scope_time (scope, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

`scope` = `turn` / `conversation` / `global`；`reason` = `token_limit` /
`step_limit` / `conversation_budget` / `global_budget`。看板据此回答「今天被
拦了多少次、拦在哪一层」。

### 5.4 `eval_cases`

```sql
CREATE TABLE IF NOT EXISTS eval_cases (
    id               BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
    query            VARCHAR(512) NOT NULL,
    expected_intent  VARCHAR(16)  NULL,
    expected_doc_ids VARCHAR(256) NULL,
    expected_behavior VARCHAR(16) NOT NULL DEFAULT 'answer',
    source           VARCHAR(16)  NOT NULL,
    source_ref       VARCHAR(64)  NULL,
    status           VARCHAR(16)  NOT NULL DEFAULT 'candidate',
    note             VARCHAR(256) NULL,
    created_at       DATETIME(6)  NOT NULL,
    updated_at       DATETIME(6)  NOT NULL,
    KEY idx_cases_status (status, created_at),
    KEY idx_cases_source (source)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

- `expected_doc_ids`：`|` 分隔的 FAQ **doc_id**（如 `P-RET-001|P-RET-002`）。
  与 r13 标注集同口径，便于复用。
- `expected_behavior`：`answer` / `refuse` / `handoff`。
- `source`：`manual` / `feedback` / `low_conf` / `eval_fail`。
- `status`：`candidate` → `accepted` →（可选）`retired`。**只有 accepted 参与
  评估**，候选必须人工过一眼，避免把「顾客问错」当成「知识库缺」。
- `source_ref`：来源记录 id（反馈 id / 低置信度 id / 评估 item id），用于回溯。

### 5.5 `eval_runs`

```sql
CREATE TABLE IF NOT EXISTS eval_runs (
    id           VARCHAR(32)  NOT NULL PRIMARY KEY,
    status       VARCHAR(16)  NOT NULL,
    sample_size  INT          NOT NULL DEFAULT 0,
    config_json  JSON         NULL,
    metrics_json JSON         NULL,
    report_path  VARCHAR(256) NULL,
    error        TEXT         NULL,
    started_at   DATETIME(6)  NOT NULL,
    finished_at  DATETIME(6)  NULL,
    KEY idx_runs_status (status, started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

`status`：`running` / `done` / `failed`。

### 5.6 `eval_run_items`

```sql
CREATE TABLE IF NOT EXISTS eval_run_items (
    id          BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
    run_id      VARCHAR(32)  NOT NULL,
    case_id     BIGINT       NOT NULL,
    query       VARCHAR(512) NOT NULL,
    intent      VARCHAR(16)  NULL,
    answer      TEXT         NULL,
    cited_ids   VARCHAR(512) NULL,
    passed      TINYINT(1)   NOT NULL DEFAULT 0,
    refused     TINYINT(1)   NOT NULL DEFAULT 0,
    checks_json JSON         NULL,
    latency_ms  INT          NULL,
    cost_micros BIGINT       NOT NULL DEFAULT 0,
    error       TEXT         NULL,
    created_at  DATETIME(6)  NOT NULL,
    KEY idx_items_run (run_id, id),
    KEY idx_items_passed (run_id, passed)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

`checks_json` 存单条判定的明细（哪项没过），失败项下钻靠它。

## 6. 消息 id 贯通与反馈采集

### 6.1 贯通

| 位置 | 变更 |
|---|---|
| `app/repo/chats.append_message` | 返回 `int`（`lastrowid`），原调用方忽略返回值不受影响 |
| `app/repo/chats.load_history` | 返回项增加 `id`（`/api/history` 需要） |
| `app/api/customer.py` | 助手消息落库后拿到 `message_id`，SSE 推 `{"message_id": 123}`，位置在 `[DONE]` 之前 |
| 客户端中断分支 | `finally` 里补落库的消息同样记录 id，但此时 SSE 已断，前端刷新后才拿到 |

`message_id` 只对助手消息有意义：反馈对象是「这条回答」，不是提问。

### 6.2 SSE 协议变更

```
data: {"conversation_id": "..."}
data: {"delta": "..."}
data: {"message_id": 1024}     ← 新增，紧邻 [DONE] 之前
data: [DONE]
```

前端按事件顺序处理即可，不依赖 `[DONE]` 之前必须有什么。

### 6.3 反馈语义

- **rating 必填**，`1` 或 `-1`。
- **reason / comment 全可选**。点 👎 立刻以 `rating=-1` 落库，随后出现可选
  的原因标签与补充框；选标签或填文字会再提交一次，覆盖同一条记录。
- **撤销**：对已提交的方向再点一次，前端发 `rating=0`，服务端删除该记录。
- **归属校验**：`message_id` 必须属于当前 `visitor_id` 的会话，否则 404（不
  区分「不存在」与「不是你的」，防探测）。
- **只能对助手消息反馈**，对 `role='user'` 的消息返回 400。

### 6.4 落库纪律

写库失败直接 500（反馈是用户主动动作，静默失败会让顾客以为提交成功）。
读库失败不影响对话页渲染，前端只隐藏反馈控件。

## 7. 成本计量与三层闸

### 7.1 计量

`app/observability/usage.py` 实现 `UsageCallbackHandler(BaseCallbackHandler)`：

| 钩子 | 动作 |
|---|---|
| `on_chat_model_start` / `on_llm_start` | 记 `run_id → 起始时刻` |
| `on_llm_end` | 取 usage → 算成本 → 落一行 → 清计时 |
| `on_llm_error` | 只清计时，不落行（失败的调用不计费也无 token 可数） |

usage 取值优先级：`response.generations[0][0].message.usage_metadata`
（`input_tokens` / `output_tokens`）→ `response.llm_output["token_usage"]`
（`prompt_tokens` / `completion_tokens`）→ 都没有则跳过该次调用并
`logger.debug`（不让观测把对话打挂）。

**成本换算**（价目单位是「元 / 百万 token」）：

```
cost_micros = round(input_tokens * price_in + output_tokens * price_out)
```

因为 `tokens × (元/1e6 token) = 微元`，一步到位，不用先算元再换算。
价目为 0（未配置）时成本记 0，看板显示「未配置价目」而不是假的 ¥0.00。

**node / source 归属**：

- `node` 取 `metadata.get("langgraph_node")`，取不到填 `unknown`
- `source` 取 `metadata.get("shoppilot_source", "chat")`
- `conversation_id` / `visitor_id` 先看构造参数，再退到
  `metadata["shoppilot_conversation_id"]` / `["shoppilot_visitor_id"]`

回调挂在 `graph.astream_events(..., config={"callbacks": [handler]})` 上。
LangGraph 会把父级 `RunnableConfig` 的 callbacks 通过 contextvar 传给节点内
的子调用，因此主力 agent 内部的模型调用、知识节点的生成调用都会被覆盖。
**这条依赖必须用测试钉住**（Task 4 的 `test_usage_covers_all_nodes`）；若实测
不成立，退路是在 `run_business_agent_with_limit` 里显式接收 `callbacks` 并
并入自身 `config`。

后台摘要不在图内，`maybe_summarize` 自己构造 `build_usage_handler(source="bg",
conversation_id=...)` 并随 `ainvoke(config={"callbacks": [...]})` 传入。

### 7.2 三层闸

| 层 | 变量 | 默认 | 判定时机 | 触线行为 |
|---|---|---|---|---|
| 单轮 | `TURN_TOKEN_LIMIT` | 8000 | agent 循环内 | 现有「转人工」话术，`stopped_by=token_limit` |
| 会话日 | `CONVERSATION_DAILY_TOKEN_BUDGET` | 0（关） | 组装上下文前 | 跳梗概 + 历史预算减半 + 照常回答 |
| 全局日 | `GLOBAL_DAILY_COST_YUAN` | 0（关） | 请求入口 | 一次模型都不调，返回固定话术 |

- 「日」按本地时区当天 00:00 起算，与运维直觉一致。
- 全局闸必须在入口判定：等到 agent 内部再拦，钱已经烧了。
- 全局触闸也**不返回 429**：顾客看到的是「今日服务量已满，请稍后再试或转人工」
  的 200 流，前端体验与普通回复一致，避免被当成故障。
- 会话级降级不阻断回答：`build_context(..., shrink=True)` 让原文预算减半并
  跳过梗概（梗概本身也占 token），保证会话还能继续，只是记得少一些。
- 每次触闸写一条 `budget_events`。单轮触闸的写入放在
  `_record_turn_trace` 同一处（那里已经拿到 `stopped_by`）。

### 7.3 为什么是「软闸」

硬闸（直接 429）会让顾客在不知情的情况下被拒绝，且无法区分「系统故障」与
「预算用尽」。软闸的代价是仍会消耗少量 token（降级后每轮仍需一次模型调用），
换来的是体验连续与可观测。对本地开发与小流量演示，这是正确取舍；真要硬停，
把 `GLOBAL_DAILY_COST_YUAN` 调到刚好等于昨日成本即可。

## 8. Langfuse 接入

### 8.1 形态：远程 docker 自托管

Langfuse v3 自托管需要 web + worker + ClickHouse + Redis + Postgres + S3
（MinIO）六个服务，本地全跑会与 Milvus 抢内存。因此部署到已有远程 docker
主机（与 Milvus 同一台），本机只作为客户端。

仓库提供 `deploy/langfuse-compose.yml` 与 `deploy/langfuse.env.example`：

- 端口：web `3005:3000`（避开 Milvus 的 19530/9091/8333/9333）
- 卷：postgres / clickhouse / redis / minio 四个命名卷
- 密钥：`SALT`、`ENCRYPTION_KEY`、`NEXTAUTH_SECRET` 走 env 文件，**不入库**
- `TELEMETRY_ENABLED=false`
- 初始化：`docker compose --env-file langfuse.env -f langfuse-compose.yml up -d`
- 首次访问 `http://<远程IP>:3005` 建组织与项目，拿 public/secret key

本机 `.env` 配：

```
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=http://192.168.6.23:3005
```

### 8.2 代码

`app/observability/langfuse.py`：

```python
def enabled() -> bool            # 三个变量齐备才 True
def build_handler() -> CallbackHandler | None
def trace_metadata(conversation_id, visitor_id) -> dict
def flush() -> None              # 关停时冲刷缓冲，异常吞掉
```

- 依赖 `langfuse>=3,<4`，`CallbackHandler` 来自 `langfuse.langchain`
- trace 属性**通过 config metadata 传递**（v3 的官方路径）：
  `langfuse_session_id` / `langfuse_user_id` / `langfuse_tags`，不依赖
  v2 构造函数参数，也不依赖 `propagate_attributes` 的异步上下文
- `tags` 取 `["chat"]`——intent 在请求发起时还不知道，它由 span 上的
  `langgraph_node` 表达，不硬塞进 tag；`metadata` 另放 `visitor_id`
- 装配失败（导入异常、网络不可达）只 `logger.warning` 一次，之后
  `enabled()` 返回 False，进程内不再重试

### 8.3 与 MySQL 的分工

| 用途 | 存哪 | 理由 |
|---|---|---|
| 产品看板（反馈数、成本、评估） | MySQL | 便宜、可 SQL 聚合、服务内直接查 |
| 排障（一次回答走了哪些 span、哪步慢） | Langfuse | 瀑布图、逐 span 输入输出 |

不把 Langfuse 当唯一数据源：它是可选的，缺 key 时飞轮看板仍然完整可用。

## 9. 评估流水线

### 9.1 样本来源

| 来源 | 触发 | 落 `eval_cases` 的字段 |
|---|---|---|
| 负反馈 | 看板点「转候选」 | `query`=该轮用户提问（按 `conversation_id` 回溯上一条 user 消息）、`source=feedback` |
| 低置信度 | 看板点「转候选」 | `query`=低置信度记录原文、`source=low_conf`、`expected_behavior=refuse` 待人工改 |
| 评估失败 | 看板上「加入候选」 | 复制该 item 的 query，`source=eval_fail` |
| 手工 | 看板表单 | `source=manual` |

候选必须人工改成 `accepted` 才参与评估——自动采纳会把「顾客问错」变成
「知识库缺」，把评估集带偏。

### 9.2 指标（确定性优先）

单条判定（`app/evals/metrics.py`，纯函数，可单测）：

| 检查 | 规则 | 适用 |
|---|---|---|
| `route_ok` | `expected_intent` 为空则跳过；否则 `state.intent == expected_intent` | 全部 |
| `citation_hit` | `expected_doc_ids` 为空则跳过；否则回答中任一处引用属于期望集合 | 全部 |
| `citation_valid` | 回答中每个 `[P-...]` 都能在 `faq_documents` 找到且 `enabled=1` | 全部 |
| `refusal_ok` | `expected_behavior=refuse` ⇔ `state.insufficient is True` | knowledge |
| `handoff_ok` | `expected_behavior=handoff` ⇔ 回答含「人工」 | complaint / 触线 |

`passed` = 所有适用检查为真。**不引入 LLM 裁判做默认判定**：同一份回答两次
判定可能不一致，评估就失去可比性。`--judge` 是可选增强，只做抽样打分，不进
`passed`。

聚合指标：`pass_rate`、`citation_hit_rate`、`citation_valid_rate`、
`refusal_accuracy`、`route_accuracy`、`p50_latency_ms`、`p95_latency_ms`、
`total_cost_micros`、`n`。

### 9.3 执行位置

**服务内后台任务优先**。Milvus Lite 独占数据文件，服务运行时 CLI 打不开
向量库，所以：

- 正常路径：`POST /api/admin/eval/run` → 建 `eval_runs`（`running`）→
  `asyncio.create_task` 跑 → 前端轮询 `GET /api/admin/eval/runs/{id}`
- 离线路径：服务停止后 `uv run python -m scripts.eval_pipeline`，同一套
  runner，额外支持阈值退出码

### 9.4 runner 行为

```python
async def run_eval(run_id: str, *, limit: int = 30, case_ids=None, judge=False) -> dict
```

- 取 `status=accepted` 的样本，按 `id` 升序，取前 `limit` 条
- 每条一个独立 thread：`thread_id = f"eval-{run_id}-{i}"`，`history=[]`，
  `visitor_id = f"eval-{run_id}"`，`eval_mode=True`
- 调 `graph.ainvoke`（不是流式），读最终 state 的
  `final_text` / `intent` / `insufficient` / `node_path`
- 计时：墙钟延迟；成本：跑前后 `sum_cost_since(source="eval")` 差值
- 逐条写 `eval_run_items`，跑完写 `metrics_json` 与
  `docs/eval/r20-{run_id}.md`（含失败项表格）
- 任何一条抛异常 → 该 item 记 `error`，不中断整轮；整轮异常 →
  `status=failed` + `error`
- `eval_mode=True` 时知识节点**不写** `low_confidence_queries`，否则评估
  自己会往飞轮池里灌垃圾

### 9.5 阈值

```
uv run python -m scripts.eval_pipeline --limit 30 \
  --fail-under pass_rate=0.8 --fail-under citation_hit=0.7
```

低于阈值退出码 1，可直接挂 CI 或计划任务。指标名与 `metrics_json` 的键一致。

## 10. 接口清单

**顾客侧**

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/feedback` | 提交/覆盖/撤销一条反馈 |
| GET | `/api/feedback` | 按会话回显：`conversation_id` + `visitor_id` |

**管理侧（无鉴权，本地开发）**

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/flywheel/feedback?days=7&rating=-1&limit=50` | 反馈概览 + 最近负反馈 |
| GET | `/api/admin/flywheel/cost?days=7` | 总量 / 按节点 / 按天 / 按来源 / 触闸次数 |
| GET | `/api/admin/flywheel/low-confidence?limit=50` | 低置信度池（复用现有数据） |
| GET | `/api/admin/eval/runs?limit=20` | 运行历史 |
| POST | `/api/admin/eval/run` | 触发一次评估（后台），返回 `run_id` |
| GET | `/api/admin/eval/runs/{run_id}` | 单次结果 + 失败项 |
| GET | `/api/admin/eval/cases?status=candidate` | 样本列表 |
| POST | `/api/admin/eval/cases` | 手工新增样本 |
| POST | `/api/admin/eval/cases/{case_id}/status` | 采纳 / 淘汰 |
| POST | `/api/admin/eval/cases/from-feedback/{feedback_id}` | 负反馈转候选 |
| POST | `/api/admin/eval/cases/from-low-conf/{low_conf_id}` | 低置信度转候选 |
| POST | `/api/admin/eval/cases/from-item/{item_id}` | 评估失败项转候选 |
| GET | `/admin/flywheel` | 飞轮看板页（静态） |

`POST /api/admin/eval/run` 必须带 `limit` 且 ≤ 50，缺省 30——评估是要花钱的，
接口层面强制显式给出规模。

## 11. 前端

### 11.1 对话页（`index.html` / `app.js` / `style.css`）

- 每条 AI 回答下方一行：👍 / 👎
- 点 👎 → 立刻 POST `rating=-1` → 原地展开可选原因标签
  （答非所问 / 信息不准确 / 没解决 / 太啰嗦 / 其他）+ 可选补充文字框
- 点标签 → 再 POST 一次（带 `reason`），覆盖同一条记录，标签置为选中态
- 填文字 → 点「提交」再 POST 一次（带 `comment`）
- 已提交后控件显示「已收到反馈」，同方向再点一次 = 撤销（`rating=0`）
- 刷新后状态保留：`loadHistory` 拿到 `id` 后拉一次
  `GET /api/feedback?conversation_id=...` 回填
- 反馈控件只在 `message_id` 到达后渲染；历史消息直接按 id 渲染

### 11.2 飞轮看板（`admin_flywheel.html` / `admin_flywheel.js`，复用 `admin.css`）

四块，自上而下：

1. **反馈**：近 7 天赞/踩数、踩的比例、最近负反馈表（时间、提问、原因、
   补充、会话），每行带「转候选」
2. **成本**：今日 token 与 ¥、按节点分布、按天趋势、触闸次数
3. **评估**：运行历史表（时间、样本数、pass_rate、p50、成本、状态），
   顶部「立即跑评估」（弹 `limit` 输入，默认 30），点开某次看失败项
4. **样本**：候选/已采纳列表，行内「采纳」「淘汰」；顶部手工新增表单

不做图表库：节点分布用横向条形（CSS 宽度百分比），趋势用简单数字列表。
不引 Chart.js 之类的依赖。

## 12. 配置项

| 变量 | 默认 | 说明 |
|---|---|---|
| `TURN_TOKEN_LIMIT` | 8000 | 单轮 token 上限（原 `agent.py` 常量改为读 env） |
| `CONVERSATION_DAILY_TOKEN_BUDGET` | 0 | 会话日预算，0=关 |
| `GLOBAL_DAILY_COST_YUAN` | 0 | 全局日成本上限（元），0=关 |
| `LLM_PRICE_INPUT_PER_MTOK` | 0 | 输入价，元/百万 token |
| `LLM_PRICE_OUTPUT_PER_MTOK` | 0 | 输出价，元/百万 token |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` | 空 | 任一缺失则关闭追踪 |
| `EVAL_DEFAULT_LIMIT` | 30 | 单次评估默认样本数 |

全部进 `.env.example`（占位值），真值只在本地 `.env`。

## 13. 测试策略

| 层次 | 用例 |
|---|---|
| 消息 id | `append_message` 返回 id；`/api/history` 带 id；SSE 序列含 `message_id` 且在 `[DONE]` 前 |
| 反馈 | 覆盖 / 撤销 / 跨访客 404 / 对 user 消息 400 / rating 非法 422 / 聚合数字 |
| 用量 | 回调落行（node、source、tokens、成本换算）；usage 缺失时跳过不炸；写库失败不影响模型调用 |
| 用量覆盖 | 跑一次含知识 + 业务两条路的图，断言 `llm_usage` 里 `node` 至少出现 `knowledge` 与 `agent`（钉住回调传播） |
| 预算 | 全局闸：入口返回固定话术 + 一条 `budget_events`；会话闸：`shrink=True` 时梗概被跳过、历史减半、仍能回答；单轮闸：`stopped_by=token_limit` 且有事件 |
| Langfuse | 无 key → `enabled()` False、handler 为 None、对话正常；有 key（mock 环境变量）→ 构造出 handler 且 metadata 键名正确；装配异常只警告一次 |
| 评估指标 | 引用抽取（多种格式）、引用合规、拒答判定、路由判定、聚合（p50/p95、比率） |
| 评估 runner | 用假图跑 3 条样本：全部落 item、失败项 `passed=0`、`metrics_json` 键齐、`eval_mode` 不写低置信度、报告文件生成 |
| 评估接口 | 触发返回 `run_id`、轮询从 `running` 到 `done`、`limit>50` 返回 422 |
| 看板接口 | 各端点字段齐全、`days` 参数生效、空库不炸 |
| 回归 | 现有全部测试保持绿 |

数据库相关测试沿用 r19 手法：共享 MySQL、独立 `visitor_id`/`run_id` 前缀、
断言用前后差值，不依赖清库。

## 14. 风险与坑

1. **回调传播是最大不确定项**。LangGraph 节点内 `agent.astream()` 是否继承
   父级 callbacks，取决于 `ensure_config` 的 contextvar 合并。Task 4 用一条
   端到端测试钉住；不成立就退到显式传 `callbacks`，代价是 `agent.py` 多一个
   参数、`graph.py` 多一行透传。
2. **Langfuse v3 的 API 面比 v2 窄**。构造函数不再收 `session_id`/`user_id`，
   必须走 config metadata 的 `langfuse_*` 键。计划里第一步就是装包后核验
   `CallbackHandler` 的真实签名，再写代码——不按记忆写。
3. **`langfuse` 依赖可能与 `langchain-core 1.7 / langgraph 1.2` 冲突**。先
   `uv add --dry-run` 看解析结果；冲突则钉到能装上的最高 3.x，仍不行就把
   Langfuse 做成可选依赖（缺包时 `enabled()` False，服务照跑）。
4. **评估会污染数据**。`eval_mode` 必须同时挡住低置信度落库与 `agent_traces`
   （轨迹只在 `customer.py` 写，天然不受影响）。thread 与 visitor 前缀 `eval-`
   让看板能排除。
5. **全局闸的「日」边界**。用本地时区，不用 UTC——否则跨零点行为与运维直觉
   不一致，验收时容易误判「没生效」。
6. **反馈表无限增长**。一轮对话一条回答，量级与消息表同阶，本轮只建索引不
   归档，标注为已知欠账。
7. **远程 docker 部署不在本机控制**。部署任务依赖 ssh 可达与远端 docker 版本
   （v3 compose 需要较新的 docker）。不可达时降级为「写好 compose + 文档，
   本地不接 Langfuse」，飞轮其余部分不受影响。

## 15. 验收标准

| # | 断言 | 怎么验 |
|---|---|---|
| 1 | 对话页每条回答可 👍/👎，点踩不选原因也能落库 | 浏览器点一遍，查 `message_feedback` 表有 `rating=-1`、`reason=NULL` 的行 |
| 2 | 选原因/填文字后覆盖同一条记录，刷新后状态保留 | 刷新页面，按钮保持选中态与「已收到反馈」 |
| 3 | 同方向再点一次撤销 | 表里该行消失 |
| 4 | 反馈数字在看板随提交变化 | `/admin/flywheel` 的赞/踩计数变化 |
| 5 | 成本卡显示今日 token 与 ¥、按节点分布 | 问几轮后看板出现 `knowledge` / `agent` 两类节点 |
| 6 | 全局闸生效 | `GLOBAL_DAILY_COST_YUAN=0.01` 重启后对话返回固定话术，`budget_events` 多一条 `scope=global` |
| 7 | 会话闸降级不阻断 | `CONVERSATION_DAILY_TOKEN_BUDGET=500` 下继续对话仍有回答，梗概不再更新 |
| 8 | Langfuse 有 key 时 trace 可见 | 打开 `http://192.168.6.23:3005`，按 `conversation_id` 搜到 session，含各节点 span |
| 9 | Langfuse 无 key 时零噪声 | 清空三个变量重启，日志无 warning、对话正常 |
| 10 | 评估跑通并可下钻 | 看板点「立即跑评估」→ `running` → `done`，指标与失败项可见，`docs/eval/r20-*.md` 生成 |
| 11 | 候选闭环 | 负反馈「转候选」→「采纳」→ 下次评估样本数 +1 |
| 12 | 阈值退出码 | `--fail-under pass_rate=0.99` 时退出码非 0 |
| 13 | 回归 | `uv run pytest` 全绿 |
| 14 | 浏览器实跑 | 1–12 全在浏览器点过，不是只跑单测 |