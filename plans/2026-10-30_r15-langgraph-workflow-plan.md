# r15 LangGraph 图架构改造：实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把「一个 ReAct agent 包打天下」改造成 StateGraph：意图识别分流四节点，主力 agent 只拿业务工具，工作状态用 checkpoint 持久化。

**Architecture:** StateGraph 含 resolve_reference（按需）→ classify_intent → 四路分流（知识检索 / 主力 agent / 投诉模板 / 闲聊模板）→ log_trace 汇合。消息历史不进 checkpoint（每轮从 MySQL 读入，经 runtime context 传入图）；checkpoint 只存工作字段。

**Tech Stack:** LangGraph 1.2.18（StateGraph + create_react_agent）、langgraph-checkpoint-mysql（验证不过退 SQLite）、pymysql。

**Spec:** `docs/superpowers/specs/2026-10-30_r15-langgraph-workflow-design.md`

## Global Constraints

- SSE 协议零不兼容变化（前端不改）：`{"conversation_id"}` → `{"delta"}` → `{"tool"}` → `[DONE]` / `{"error"}`
- 消息历史唯一事实来源是 MySQL 消息表；checkpoint state 里没有消息
- 意图识别每轮至多一次 LLM 调用；指代消解按需至多一次；两处都要 try/except 降级
- 投诉与闲聊路径零 LLM 调用
- `recursion_limit=15`；主力 agent 累计 token 超过 8000 中断并输出转人工话术
- 全程用现有 `AgentContext(visitor_id)`；业务数据按 visitor 隔离的既有规则不变
- 测试连远端 MySQL（192.168.6.23:3306）与独立 Milvus，无需停服务
- 每个任务一个提交，提交信息说明「为什么」

## Review Focus

- **闲聊误判**：顾客的真问题被判成闲聊拿到模板话术。期望：闲聊模板末尾有「如需查询订单或售后问题请告诉我」台阶；测试钉住模板文案包含这句（Task 7）
- **意图规则误伤**：含订单号的闲聊、带「投诉」字眼的知识问题（「投诉的条件是什么」）。期望：投诉规则要求情绪性措辞或明确投诉动作，纯问「投诉流程」归知识（Task 3 的测试用例钉住）
- **LLM 意图输出解析失败**：模型返回非 JSON。期望：降级为业务数据（宁可 agent 反问，不可漏），日志记 warning（Task 3）
- **checkpoint 跨进程恢复**：重启服务后同一 conversation 的槽位仍在。期望：MySQL checkpoint 可跨进程读取（Task 2 与 Task 8 的测试钉住）
- **主力 agent 死循环**：模型反复调工具不收敛。期望：recursion_limit 触发后输出转人工话术而不是 GraphRecursionError 裸异常（Task 6 的测试钉住）

---

### Task 1: checkpoint 存储验证与封装

**Files:**
- Create: `app/checkpointer.py`
- Test: `tests/test_checkpointer.py`

**Interfaces:**
- Consumes: MySQL 连接参数 `app.db.conn_params()`
- Produces: `get_checkpointer() -> BaseCheckpointSaver`（后续 Task 7 的图构建与 Task 8 的接线都依赖）

- [ ] **Step 1: 验证 langgraph-checkpoint-mysql 与 langgraph 1.2.18 兼容**

Run: `uv add langgraph-checkpoint-mysql && uv run python -c "from langgraph.checkpoint.mysql import MySQLSaver; print('ok')"`
Expected: 导入成功；若包或接口不存在，跳过 MySQL 版本，本任务改为实现 SQLite 版（`from langgraph.checkpoint.sqlite import SqliteSaver`），后续任务照常

- [ ] **Step 2: 写失败测试**

```python
def test_checkpoint_roundtrip(mysql_checkpoint):  # fixture 提供 thread_id="test-thread"
    saver = get_checkpointer()
    config = {"configurable": {"thread_id": "test-r15-cp-1"}}
    state = {"resolved_query": "退货运费", "intent": "knowledge", "turn_count": 3}
    saver.put(config, checkpoint_with_values(state), {}, {})
    restored = get_checkpointer().get(config)
    assert restored["channel_values"]["resolved_query"] == "退货运费"
```

（`checkpoint_with_values` 按所装 langgraph 版本的 Checkpoint 构造方式写；fixture 里 thread_id 带随机后缀，避免共享 MySQL 下的并发冲突）

- [ ] **Step 3: 运行确认失败** — `uv run pytest tests/test_checkpointer.py -v`，FAIL: No module named app.checkpointer
- [ ] **Step 4: 实现 `app/checkpointer.py`**

`get_checkpointer()` 按环境变量 `CHECKPOINT_BACKEND`（默认 `mysql`）返回 MySQLSaver（用 `conn_params()` 建连接）或 SqliteSaver（`data/checkpoints.db`）。MySQL 版需要先建 checkpoint 表（用包自带的 setup 或建表语句）。

- [ ] **Step 5: 运行确认通过** — `uv run pytest tests/test_checkpointer.py -v`
- [ ] **Step 6: Commit** — `git add app/checkpointer.py tests/test_checkpointer.py pyproject.toml uv.lock && git commit -m "feat: checkpoint 存储，MySQL 优先 SQLite 兜底"`

---

### Task 2: agent_traces 表与仓储

**Files:**
- Modify: `app/db.py`（SCHEMA 追加 agent_traces 表）
- Modify: `app/repo/insights.py`（新增 trace 写入与查询）
- Modify: `app/store.py`（导出）
- Test: `tests/test_traces.py`

**Interfaces:**
- Produces: `record_trace(conversation_id, visitor_id, message, intent, intent_method, node_path, latency_ms, **optional)` 与 `list_traces(limit=50)`、`trace_stats()`

- [ ] **Step 1: 写失败测试**（覆盖：写入后 list 能按时间倒序取出；intent 统计；detail JSON 往返）

```python
def test_record_and_list():
    store.record_trace("conv-1", "v-1", "你好", intent="chitchat",
                       intent_method="rule", node_path="classify>chitchat", latency_ms=12)
    traces = store.list_traces(10)
    assert traces[0]["intent"] == "chitchat"
    assert store.trace_stats()["by_intent"]["chitchat"] == 1
```

- [ ] **Step 2: 运行确认失败** — FAIL: no such table agent_traces
- [ ] **Step 3: 实现** — 表结构照设计文档的 agent_traces DDL（app/db.py 的 SCHEMA 追加；insights.py 加两个函数，写入失败不影响调用方）
- [ ] **Step 4: 运行确认通过**
- [ ] **Step 5: Commit** — `git commit -m "feat: agent_traces 轨迹表"`

---

### Task 3: 意图分类模块

**Files:**
- Create: `app/intent.py`
- Test: `tests/test_intent.py`

**Interfaces:**
- Consumes: `app.llm.get_llm()`
- Produces: `classify_by_rules(message: str) -> tuple[str, bool]`（返回意图与是否高置信）与 `async classify_intent(message: str, llm) -> IntentResult`；`IntentResult(intent: str, confidence: float, method: str)`，intent ∈ `("knowledge", "business", "complaint", "chitchat")`，method ∈ `("rule", "llm", "fallback")`

- [ ] **Step 1: 写规则层的失败测试**（用例钉住边界）

```python
@pytest.mark.parametrize("message,expected", [
    ("你好", "chitchat"), ("在吗", "chitchat"), ("谢谢啦", "chitchat"),
    ("我要投诉你们", "complaint"), ("你们太差了我要曝光", "complaint"),
    ("投诉的流程是什么", "knowledge"),          # 带「投诉」但问流程 → 知识
    ("查一下我的订单", "business"), ("ORDER20260001 到哪了", "business"),
    ("退货运费谁出", "knowledge"), ("发票能开吗", "knowledge"),
    ("买一送一能退一件吗", None),               # 规则分不出 → (None, False)
])
def test_rules(message, expected):
    intent, confident = classify_by_rules(message)
    if expected is None:
        assert not confident
    else:
        assert (intent, confident) == (expected, True)
```

- [ ] **Step 2: 运行确认失败** — FAIL: No module named app.intent
- [ ] **Step 3: 实现规则层** — 正则词表按设计文档顺序（闲聊 → 投诉 → 业务 → 知识）；投诉规则要求情绪性动作（我要投诉/曝光/举报/315/12315/态度差），纯「投诉流程」不命中
- [ ] **Step 4: 写 LLM 兜底的失败测试**（mock llm 返回合法 JSON、非法 JSON、置信度 0.4 三种）

```python
async def test_llm_fallback_parses_json():
    result = await classify_intent("这个商品我能用吗", FakeLLM('{"intent":"knowledge","confidence":0.8}'))
    assert result.intent == "knowledge" and result.method == "llm"

async def test_llm_unparseable_falls_to_business():
    result = await classify_intent("???", FakeLLM("我说不清"))
    assert result.intent == "business" and result.method == "fallback"

async def test_llm_low_confidence_falls_to_business():
    result = await classify_intent("???", FakeLLM('{"intent":"chitchat","confidence":0.4}'))
    assert result.intent == "business"
```

- [ ] **Step 5: 运行确认失败** → **Step 6: 实现 LLM 兜底**（prompt 内嵌在 intent.py；解析失败返回 fallback）
- [ ] **Step 7: 运行确认通过** — `uv run pytest tests/test_intent.py -v`
- [ ] **Step 8: Commit** — `git commit -m "feat: 意图分类，规则先行 LLM 兜底"`

---

### Task 4: 指代消解模块

**Files:**
- Create: `app/resolve.py`
- Test: `tests/test_resolve.py`

**Interfaces:**
- Produces: `needs_resolution(message: str, pending_topic: str) -> bool`（纯函数，测试钉住触发边界）与 `async resolve_reference(message: str, history: list[dict], llm) -> str`（失败时原样返回 message）

- [ ] **Step 1: 写触发条件的失败测试**

```python
@pytest.mark.parametrize("message,pending,expected", [
    ("它怎么样了", "knowledge", True),
    ("这个能退吗", "business", True),
    ("我的包裹怎么还没到", "", False),        # 无代词且长度足
    ("嗯", "", False),                        # 极短但无未决话题
    ("嗯", "knowledge", True),                # 极短 + 有未决话题
])
def test_trigger(message, pending, expected):
    assert needs_resolution(message, pending) is expected
```

- [ ] **Step 2: 运行确认失败** → **Step 3: 实现 `needs_resolution`**（代词列表：它/这个/那个/该/上述/他/她；极短阈值 ≤6 字）
- [ ] **Step 4: 写 LLM 改写的失败测试**（mock：正常改写、失败回退）

```python
async def test_resolve_rewrites():
    out = await resolve_reference("它怎么样了",
        [{"role": "user", "content": "我的订单 ORDER20260001 到哪了"}], FakeLLM("ORDER20260001 的工单处理到哪一步了"))
    assert out == "ORDER20260001 的工单处理到哪一步了"

async def test_resolve_failure_returns_original():
    out = await resolve_reference("它怎么样了", [], FakeLLM("抛异常"))
    assert out == "它怎么样了"
```

- [ ] **Step 5: 运行确认失败** → **Step 6: 实现 `resolve_reference`**（prompt 内嵌；输入最近 4 轮历史 + 消息；任何异常返回原消息）
- [ ] **Step 7: 运行确认通过** → **Step 8: Commit** — `git commit -m "feat: 指代消解，按需触发失败回退"`

---

### Task 5: 检索节点（从工具迁入图）

**Files:**
- Create: `app/knowledge_node.py`
- Modify: `app/tools.py`（TOOLS 移除 search_knowledge；其函数体保留但标注 deprecated 或直接删除）
- Test: `tests/test_knowledge_node.py`

**Interfaces:**
- Consumes: `app.vector_store.search`、`store.faq_by_id`、`store.record_low_confidence`（r14 逻辑）
- Produces: `async run_knowledge_retrieval(resolved_query: str, visitor_id: str) -> KnowledgeOutcome`；`KnowledgeOutcome(final_text: str, hits: list[dict], insufficient: bool, max_score: float, cited_ids: list[str])`

- [ ] **Step 1: 写失败测试**（三种分支，阈值沿用 r14 的 0.65；需要 SILICONFLOW_API_KEY，沿用 skipif 模式）

```python
@pytest.mark.skipif(not os.getenv("SILICONFLOW_API_KEY"), reason="需要嵌入 API")
async def test_sufficient_returns_hits():
    out = await run_knowledge_retrieval("退货运费谁出", "kn-test")
    assert not out.insufficient and out.cited_ids and "运费" in out.final_text

async def test_irrelevant_is_refused():
    out = await run_knowledge_retrieval("明天几点开会", "kn-test")
    assert out.insufficient and "没有查到" in out.final_text
    # 低置信度池应新增一条（按查询内容查）
```

- [ ] **Step 2: 运行确认失败** → **Step 3: 实现**——逻辑照搬 `app/tools.py` 的 search_knowledge 函数体，两点变化：final_text 由一次轻量 LLM 调用生成（输入命中资料 + r14 的引用格式与拒答规则，从 prompt.py 的对应段落抽成独立常量）；`insufficient` 时的 final_text 用 r14 同款拒答话术（含「禁止一般来说」的 note 逻辑转为直接话术）
- [ ] **Step 4: 运行确认通过** → **Step 5: 从 tools.py 移除 search_knowledge 并确认 TOOLS 只剩四件**
- [ ] **Step 6: 全量回归** — `uv run pytest -q`（现有 test_admin_faq 的调试检索端点用 vector_store 直查，不受影响；若有 test 引用 search_knowledge 按报错更新）
- [ ] **Step 7: Commit** — `git commit -m "feat: 知识检索节点，search_knowledge 工具退役"`

---

### Task 6: 主力 agent 裁剪与上限

**Files:**
- Modify: `app/agent.py`（build_business_agent）
- Modify: `app/prompt.py`（拆出 BUSINESS_PROMPT，含缺信息反问规则；旧 SYSTEM_PROMPT 保留给 prompt 里仍被引用的部分，逐步收缩）
- Test: `tests/test_business_agent.py`

**Interfaces:**
- Consumes: `app.tools.TOOLS`（裁剪后四件）
- Produces: `build_business_agent() -> CompiledStateGraph`（子图，作为父图节点用）与 `TOKEN_LIMIT = 8000`、`STEP_LIMIT = 15` 常量；agent 输入为 `{"messages": [...]}`，context 仍为 AgentContext

- [ ] **Step 1: 写失败测试**（mock LLM，验证工具集与上限）

```python
def test_tools_trimmed_to_business_four():
    agent = build_business_agent()
    names = {t.name for t in agent.nodes["tools"].bound._tools}  # 按实际结构取，实现时可调整取法
    assert names == {"lookup_product", "list_my_orders", "lookup_work_order", "draft_work_order"}

async def test_recursion_limit_stops_loop():
    # mock LLM 永远要求调工具 → 触发 recursion_limit → 输出转人工话术
    out = await run_business_agent_with_limit(messages, FakeAlwaysToolLLM())
    assert "转人工" in out or "人工" in out
```

- [ ] **Step 2: 运行确认失败** → **Step 3: 实现**——`build_business_agent(tools=BUSINESS_TOOLS, prompt=BUSINESS_PROMPT)`；`run_business_agent_with_limit` 用 `recursion_limit=15` 配置跑 astream，捕获 GraphRecursionError 与 token 回调超限，两者都返回转人工话术
- [ ] **Step 4: 运行确认通过** → **Step 5: Commit** — `git commit -m "feat: 主力 agent 裁剪为业务四件，加步数与 token 上限"`

---

### Task 7: 模板节点

**Files:**
- Create: `app/templates.py`
- Modify: `app/repo/commerce.py`（投诉自动建工单需要 source='template' 的建单函数，现有 create_work_order 固定 source='ai'——加参数或新函数）
- Test: `tests/test_templates.py`

**Interfaces:**
- Produces: `chitchat_reply() -> str` 与 `async complaint_reply(visitor_id: str) -> str`（含自动建「投诉建议」工单，无订单号；工单面板可见）

- [ ] **Step 1: 写失败测试**

```python
def test_chitchat_mentions_capabilities():
    text = chitchat_reply()
    assert "订单" in text and "售后" in text
    assert "如需查询" in text          # 给误判留台阶的那句话（Review Focus 钉住）

async def test_complaint_creates_ticket():
    text = await complaint_reply("tmpl-" + uuid.uuid4().hex[:8])
    assert "人工" in text
    # 该访客名下应新增一张投诉建议工单
```

- [ ] **Step 2: 运行确认失败** → **Step 3: 实现**——chitchat 零副作用；complaint 调 commerce 新函数 `create_complaint_ticket(visitor_id)` 建无订单号的「投诉建议」工单（status=待处理，source=template），话术含工单号
- [ ] **Step 4: 运行确认通过** → **Step 5: Commit** — `git commit -m "feat: 投诉与闲聊模板节点，投诉自动建工单"`

---

### Task 8: 图组装与 main.py 切换

**Files:**
- Create: `app/graph.py`
- Modify: `app/main.py`（/api/chat 的 stream() 换成跑图；SSE 事件映射保持）
- Modify: `app/agent.py`（build_agent 移除或改为薄封装）
- Test: `tests/test_graph.py`

**Interfaces:**
- Consumes: Task 1–7 的全部产出
- Produces: `build_graph() -> CompiledStateGraph`；`ConversationState`（TypedDict：resolved_query / intent / intent_confidence / collected_slots / pending_topic / turn_count / final_text / node_path）；图输入 `{"message": str}`，历史经 `AgentContext(visitor_id, history)` 传入（context 不进 checkpoint）

- [ ] **Step 1: 写图的失败测试**（mock LLM 与向量层，验证分流与 state 流转）

```python
async def test_chitchat_route_no_llm(...):     # 「你好」→ final_text 含「订单」「售后」，node_path 含 chitchat
async def test_complaint_route_creates_ticket(...)  # node_path 含 complaint
async def test_knowledge_route_streams(...)     # mock 向量层命中 → final_text 含 [P- 引用格式
async def test_business_route_runs_agent(...)   # mock 工具调用 → node_path 含 business
async def test_state_persisted_via_checkpoint(...)  # 跑两轮，第二轮 turn_count == 2
```

（测试里图用 `MemorySaver` 构造，不连真 checkpoint；llm 与 vector_store 用 monkeypatch 替换）

- [ ] **Step 2: 运行确认失败** → **Step 3: 实现 `app/graph.py`**

节点函数从 Task 3/4/5/6/7 的模块导入组装；分流用 `add_conditional_edges`；日志节点在四路汇合的 END 前执行（Task 2 的 record_trace，写 node_path 与耗时）。config 传 `thread_id=conversation_id` 与 `checkpointer=get_checkpointer()`。

- [ ] **Step 4: 运行确认通过** → **Step 5: 改 `app/main.py`**

`/api/chat` 的 stream()：历史与 AgentContext 照旧准备；`graph.astream_events({"message": req.message}, config, version="v2")`；`on_chat_model_stream` → delta（business agent 与检索节点生成均为流式）；模板路径无 LLM，从图结束后的 state 取 `final_text` 一次性 yield 为 delta；tool 事件照旧映射。跑完后 yield `[DONE]`，异常处理与 finally 落库逻辑保持 r14 现状。

- [ ] **Step 6: 手动冒烟** — `uv run uvicorn app.main:app --port 8766`，curl 打「你好」「我要投诉」「退货运费谁出」三条，SSE 输出与前端协议一致
- [ ] **Step 7: 全量回归** — `uv run pytest -q`（预期：现有 test 里引用 build_agent 或 search_knowledge 的按报错更新；SSE 协议测试不变）
- [ ] **Step 8: Commit** — `git commit -m "feat: StateGraph 分流上线，主力 agent 收归业务节点"`

---

### Task 9: 跨进程 checkpoint 验证与浏览器验收

**Files:**
- Modify: 无新代码，验证任务
- Test: `tests/test_checkpoint_recovery.py`

**Interfaces:**
- Consumes: Task 1 的 get_checkpointer、Task 8 的 build_graph

- [ ] **Step 1: 写跨进程恢复测试**（两次独立的 build_graph + get_checkpointer，模拟服务重启）

```python
async def test_slots_survive_restart():
    # 第一轮：意图 business，collected_slots={"pending_topic": "退货"}
    # 第二轮（新图实例、同 thread_id）：state 里 pending_topic 仍是 "退货"
```

- [ ] **Step 2: 运行确认通过**（Task 1 已保证底层 roundtrip；这里验证图的接线）
- [ ] **Step 3: 浏览器验收**（对照设计文档验收十条）：

1. 「你好」→ 闲聊模板，agent_traces 里 intent_method=rule
2. 「我要投诉你们」→ 模板话术 + 工单面板出现投诉建议工单
3. 「退货运费谁出」→ 带 [P- 引用编号，悬停有正文
4. 「我的订单到哪了」→ agent 调 list_my_orders 反问
5. 「它怎么样了」（上轮聊过订单）→ 消解后进业务节点
6. 把 message 故意写成会死循环的内容 → 输出转人工话术
7. agent_traces 有完整记录
8. 重启 uvicorn 后继续同会话，发「嗯」→ 消解触发并延续上轮话题
9. 前端 SSE 渲染与 r14 无差异
10. 全量测试通过

- [ ] **Step 4: Commit** — `git commit -m "test: 跨进程 checkpoint 恢复与 r15 验收"`

---

### Task 10: 文档与 README

**Files:**
- Modify: `README.md`（结构表加 graph.py / intent.py / resolve.py / checkpointer.py / templates.py / knowledge_node.py；接口说明不变）
- Modify: `docs/dev-notes/r15.md`（过程留痕：关键原话、产出、纠偏、翻车）

- [ ] **Step 1: 更新 README 结构与一句话说明**
- [ ] **Step 2: 补 r15 开发留痕**（brainstorm 定稿的决策：规则+LLM 兜底、MySQL checkpoint、方案 A 历史分工；builder check 若有返工也记）
- [ ] **Step 3: Commit** — `git commit -m "docs: r15 README 与开发留痕"`
