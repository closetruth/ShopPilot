# ShopPilot 阶段十五设计：LangGraph 图架构改造

## 意图

当前架构是「一个 ReAct agent 包打天下」：五个工具全给模型，模型自己决定
查什么。实测暴露的问题：

- **意图不分的浪费**：顾客说「你好」，模型也要过一遍完整的 prompt 与全部
  工具描述，浪费 token 且可能误调工具
- **投诉场景失控**：情绪激烈的顾客不该再让模型自由发挥，应固定话术
- **闲聊零价值**：「你好」「在吗」走完整 agent 链路是纯浪费
- **上下文不在图里**：历史消息每轮从 MySQL 拼接传入，图自身没有可持久化的
  工作状态（比如多轮收集订单号的槽位）

这一轮把对话改成 LangGraph StateGraph：意图识别 → 分流到专门节点，
主力 agent 只处理业务数据类，会话状态用 checkpoint 持久化。

## 目标

1. LangGraph 图含：指代消解、意图识别、意图分流、知识检索（含置信度闸）、
   主力 agent、投诉模板、闲聊模板、日志记录
2. 意图四类：知识检索 / 业务数据 / 投诉 / 闲聊；规则先行，LLM 兜底
3. 主力 agent 只拿业务工具，缺信息反问顾客，有步数与 token 上限
4. 会话工作状态用 MySQL checkpoint 持久化；消息历史仍在 MySQL 消息表
5. 每轮对话落一条结构化日志（意图、置信度、节点路径、耗时）
6. 置信度弱的问题落库（复用 r14 的 low_confidence_queries，数据飞轮）

## 非目标

- 不改 r13 混合检索（继续用纯向量 + 0.65 阈值）
- 不做多意图并行处理（一条消息只归一类）
- 不做意图分类的评估数据集
- 不改前端交互（SSE 协议不变）
- 不做流式中断后的状态恢复

## 反目标

- 不允许投诉类消息进主力 agent 自由发挥
- 不允许意图识别消耗超过一次 LLM 调用（规则能定的不调模型）
- 不允许把消息历史塞进 checkpoint（与 MySQL 双写会造成不一致）

## 架构

```
用户消息（含近几轮历史，来自 MySQL）
   ↓
[指代消解]  仅当消息含代词或极短且上轮有未决话题
   ↓          LLM 用历史改写成独立问题；多数消息跳过此步
[意图识别]  规则先行（正则），分不出才调 LLM
   ↓
┌──────────┬──────────┬──────────┬──────────┐
│ 知识检索   │ 业务数据   │ 投诉      │ 闲聊      │
│ ↓        │ ↓        │ ↓        │ ↓        │
│ 向量检索   │ 主力agent │ 固定话术   │ 固定话术   │
│ r14闸     │ ReAct    │ 安抚+转人工│ 零LLM调用 │
│ 够→带引用  │ 业务工具   │          │          │
│ 弱→兜底话术│ 反问/上限  │          │          │
│   ↓      │          │          │          │
│ 落库飞轮   │          │          │          │
└──────────┴──────────┴──────────┴──────────┘
   ↓
[日志记录]  agent_traces 表，每轮一条
   ↓
SSE 流式返回（协议不变）
```

## 关键决定

| 决定 | 选择 | 理由 |
|---|---|---|
| 意图识别 | 规则优先 + LLM 兜底 | 明显意图零延迟零成本；隐晦的交给模型 |
| checkpoint 存储 | MySQL（与业务同库） | 状态与业务数据同库，不引入新存储 |
| 历史分工 | MySQL 存消息，checkpoint 只存工作状态 | /api/history、前端还原都建在消息表上，不动 |
| 主力 agent 工具 | 只拿业务工具 | 知识走专门节点，职责清晰 |
| 指代消解 | 按需触发，不每次跑 | 多数消息无代词，白跑一次 LLM 调用 |
| 停止条件 | recursion_limit + token 计数 | LangGraph 原生支持前者，后者用回调 |

## 图的节点规格

### 1. resolve_reference（指代消解）

**触发条件**：消息含「它/这个/那个/该/上述」等代词，或消息长度 ≤ 6 字
且上一轮意图是知识检索/业务数据（延续话题的短消息）。

**行为**：一次 LLM 调用，输入最近 4 轮历史 + 当前消息，输出改写后的独立
问题（如「它怎么样了」→「ORDER20260001 的工单处理到哪一步了」）。

**输出**：`resolved_query`，同时保留原消息。后续节点用 resolved_query。

### 2. classify_intent（意图识别）

**规则先行**（按顺序，命中即返回，零 LLM 调用）：

| 规则 | 意图 |
|---|---|
| 正则命中「你好/在吗/谢谢/再见/晚安」等寒暄词 | 闲聊 |
| 正则命中「投诉/曝光/315/12315/工商局/客服态度」 | 投诉 |
| 含 `ORDER` 开头订单号，或「查/退/换/发货/物流/订单/工单」 | 业务数据 |
| 含「多少钱/有没有/是什么/退不退/能不能」等疑问模式 | 知识检索 |
| 其余 | 走 LLM 兜底 |

**LLM 兜底**：一次调用，输入改写后的消息 + 四类定义与各两三个例子，
输出 JSON `{"intent": "...", "confidence": 0.0-1.0}`。解析失败按业务数据
处理（宁可进 agent 反问，不可漏）。

### 3. knowledge_retrieval（知识检索节点）

复用 r14 的逻辑：向量检索 → 0.65 置信度闸 → 够则返回带引用的资料给
生成节点（一次轻量 LLM 调用组织回答）；弱则返回兜底话术 + 写入
`low_confidence_queries`。

**注意**：此节点不再是工具，是图的节点。模型不再自主决定查不查——
意图识别已经判定这是知识类问题。

### 4. business_agent（主力 agent）

`create_react_agent` 但工具集裁剪为业务四件：`lookup_product`、
`list_my_orders`、`lookup_work_order`、`draft_work_order`。

**不含 `search_knowledge`**——知识类意图不进这个 agent。

**停止条件**：
- `recursion_limit = 15`（LangGraph 配置）
- token 上限：累计 input+output 超过阈值（默认 8000）时中断，输出兜底话术
  「这个问题比较复杂，我帮你转人工处理」

**缺信息反问**：由 prompt 约束（现有「缺订单号就调 list_my_orders」规则保留）。

### 5. complaint_template / chitchat_template（模板节点）

不调 LLM，固定话术直接返回：

- **投诉**：安抚 + 已记录 + 转人工。同时调 `draft_work_order` 的底层函数
  直接建一张「投诉建议」工单（source='template'），让工单面板能看到
- **闲聊**：自我介绍 + 能力清单

### 6. log_trace（日志节点）

所有路径汇合后执行。写 `agent_traces` 表：

```sql
CREATE TABLE agent_traces (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    conversation_id VARCHAR(32) NOT NULL,
    visitor_id   VARCHAR(64)  NOT NULL,
    message      TEXT         NOT NULL,
    resolved_query TEXT       NULL,
    intent       VARCHAR(32)  NOT NULL,
    intent_method VARCHAR(16) NOT NULL,   -- rule / llm
    confidence   FLOAT        NULL,
    node_path    VARCHAR(256) NOT NULL,   -- 如 "resolve>classify>knowledge"
    top_score    FLOAT        NULL,
    tool_calls   VARCHAR(256) NULL,       -- 用到的工具，逗号分隔
    steps        INT          NULL,       -- agent 步数
    total_tokens INT          NULL,
    latency_ms   INT          NOT NULL,
    detail       JSON         NULL,       -- 各节点耗时等
    created_at   DATETIME(6)  NOT NULL,
    KEY idx_traces_conv (conversation_id),
    KEY idx_traces_intent (intent)
)
```

写入失败只记日志，不影响返回。

### 7. checkpoint（会话工作状态）

**state schema 自定义**，只含工作字段，不含消息：

```python
class ConversationState(TypedDict):
    resolved_query: str        # 消解后的独立问题
    intent: str                # 四类之一
    intent_confidence: float
    collected_slots: dict      # 如 {"order_no": "ORDER20260001"}
    pending_topic: str         # 未决话题（用于短消息延续判断）
    turn_count: int
```

**消息不进 state**：每轮从 MySQL 读历史，作为输入传给图。checkpoint 里
只有几百字节的工作字段。

**存储**：`langgraph-checkpoint-mysql` 社区包，连业务同库。包较新，需先
验证可用性；不行则退 SQLite（本地文件），设计上留了退路——checkpoint
的接口是 `BaseCheckpointSaver`，换存储只改构造处。

## 与现有代码的关系

| 现有 | 处置 |
|---|---|
| `app/agent.py` 的 `build_agent()` | 改为构建图；ReAct 部分变成 `business_agent` 节点 |
| `app/tools.py` 的 TOOLS | 拆两组：业务四件归 agent；`search_knowledge` 退役（逻辑移入检索节点） |
| r14 的置信度闸 | 逻辑从工具移入检索节点，行为不变 |
| `app/main.py` 的 SSE 循环 | astream_events 不变，新增事件类型（节点切换） |
| `prompt.py` | 拆三份：主力 agent prompt、意图分类 prompt、消解 prompt |
| 前端 | SSE 协议不变；`tool` 事件可能减少（模板路径无工具调用） |

**`search_knowledge` 工具退役**：它的检索逻辑全部移入图的检索节点。
工具形式依赖「模型自主决定调用」，而新架构里这个决定由意图识别做出。

## 风险

- **意图识别错误没有回头路**：一条知识类消息被误判为闲聊，会拿到模板话术
  而不是真回答。缓解：闲聊模板话术里加「如果需要查询订单或售后问题请告诉我」，
  给自己留台阶；LLM 兜底置信度低于 0.5 时按业务数据处理（宁可多问）
- **langgraph-checkpoint-mysql 包太新**：可能与我们用的 langgraph 1.2.18
  不兼容。验证不通就退 SQLite
- **指代消解可能改错**：LLM 改写把「它」错误关联到别的订单。缓解：只在
  高置信场景触发；消解结果与原消息一起给后续节点，不丢弃原话
- **投诉模板太死板**：顾客情绪激动时模板显得敷衍。缓解：模板里明确
  转人工路径，且直接建工单让顾客看到行动
- **两个 LLM 调用叠加**（消解 + 意图兜底）最坏 1 秒延迟。缓解：消解只对
  含代词的消息触发；意图规则覆盖常见模式

## 验收

1. 「你好」→ 闲聊模板，零 LLM 调用（日志 intent_method=rule）
2. 「我要投诉你们」→ 投诉模板 + 自动生成投诉工单（面板可见）
3. 「退货运费谁出」→ 知识检索，回答带引用编号（与 r14 行为一致）
4. 「我的订单到哪了」→ 主力 agent 调 list_my_orders 反问哪一单
5. 「它怎么样了」（上轮讨论过某订单）→ 消解后进正确节点
6. 复杂业务问题达到步数上限时输出转人工话术，不死循环
7. agent_traces 表每轮有一条完整记录
8. checkpoint 在 MySQL 中可查到工作状态；重启后继续会话时槽位仍在
9. SSE 协议与前端不兼容变化为零
10. 全量测试通过

## 留给构建者的决定

- 意图规则的正则细节与词表
- token 计数的实现方式（回调或手动累计）
- 消解 prompt 的具体措辞
- checkpoint 验证失败退 SQLite 时的路径处理

---

## 补充：会话恢复场景的分析（定稿依据）

「历史分工」定方案 A（MySQL 存消息，checkpoint 只存工作状态）后，
用户追问「到时候恢复会话呢」。逐场景分析后确认 A。

| 恢复场景 | 用什么 | 结论 |
|---|---|---|
| 刷新页面 / 隔天回来 | MySQL 消息表（现有 /api/history） | 零改动 |
| 图运行中断（agent 跑一半进程挂） | 不恢复，顾客重发消息即可 | ReAct 循环幂等，重跑比续跑干净；r14 的 finally 已保证已生成部分落库 |
| 跨轮槽位延续（分多条消息提供订单号） | checkpoint 的工作字段（pending_topic、collected_slots） | 这才是 checkpoint 的本职，几百字节轻快 |
| 会话回看 / 数据飞轮分析 | MySQL + agent_traces，SQL 直查 | checkpoint 的 blob 不可 SQL 查 |

关键判断：**没有任何恢复场景需要 checkpoint 里存消息**。方案 B 的恢复
（图中断续跑）对 ReAct 没有实际价值——中断点的工具调用状态不可靠，
重来更干净；而它带来的代价（checkpoint 按版本存全量消息、历史读取层
全部重写、失败时连消息一起丢）远超收益。

结论：方案 A 定稿。
