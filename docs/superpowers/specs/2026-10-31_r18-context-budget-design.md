# ShopPilot 阶段十八设计：会话上下文预算与历史梗概

## 意图

历史窗口现在硬编码「最后 20 条」，不看 token：长消息几轮就把上下文
撑到离谱，短消息又浪费预算。且没有可观测性——上下文构成是黑盒，
爆没爆、超了多少没人知道。

本轮把上下文从「条数」改成「token 预算」：从模型窗口倒推，减去
system/工具/输出余量后分给「梗概 + 原文」两段；原文超预算时把水位线
之前的部分压成事实型梗概（后台异步），下一轮起生效。

## 目标

1. 上下文组装 = system 提示词 + 工具定义 + 梗概（若有）+ 预算内原文
2. **接进主力 agent**（builder check 发现业务 agent 现在不消费历史，
   用户裁定接线）：梗概与预算内原文作为对话历史随 resolved_query
   传入主力 agent 的 messages
3. 原文超预算触发后台异步压缩，梗概单开一张表，不写原始消息
4. 梗概提示词只提炼事实与诉求：问过的商品、报过的订单号/手机号、
   明确诉求、未解决的问题；对话里没出现的内容一个字不许编；寒暄不留
5. token 预算从模型窗口倒推（默认 8192 紧凑窗口，env 可配真窗口）
6. token 计数用 tiktoken（cl100k_base）本地近似，中文偏保守
7. 上下文可观测：每轮 trace 记录上下文构成账目

## 非目标

- 不做多级梗概（梗概的梗概）——单级，超长会话靠原文窗口缩小
- 不改检索/意图/消解（它们不消费历史窗口，消解用的历史另行传入）
- 不做前端展示上下文账目（admin API 够用）
- 不改业务 agent 内部的工具轮次预算（那是 r15 的步数/token 上限）

## 反目标

- 不允许摘要编造对话里没出现的内容（提示词硬约束 + 测试钉）
- 不允许梗概写进 messages 表（原文表保持逐字事实）
- 不允许摘要生成阻塞对话响应（后台异步）
- 不允许在请求路径上调 API 数 token

## 架构

```
本轮请求
   ↓
[上下文组装]  预算账目：
              total = CONTEXT_WINDOW × 0.25
              - system prompt 实测
              - 工具定义实测
              - 本轮输入 + 输出余量
              = 剩余分两段：梗概 ≤30%，原文 ≥70%
   ↓
[读库]  summaries 表取梗概（covered_until 水位线）
        messages 表取水位线之后的原文
   ↓  原文 token 超预算？
[截断]  从最旧开始丢，保住最近原文（梗概已覆盖的部分不怕丢）
   ↓
给图（resolved_query 消解用的历史同样组装）
   ↓
响应完成后，后台任务检查：原文总量 > 预算线？
   → 是：异步调 LLM 把「梗概尾部 + 水位线后原文」压成新梗概，
        更新 summaries 表（水位线前移）
```

## 关键决定

| 决定 | 选择 | 理由 |
|---|---|---|
| 预算口径 | CONTEXT_WINDOW=8192（env 可配）× 0.25 | 紧凑窗口让压缩机制真能触发、可验收；生产改大即用真实 128K |
| 预算下限 | history_budget 保底 400 token（约 10 条中文短消息） | 紧凑窗口下 system+工具+输出余量可能吃满预算，无下限会每轮全截光 |
| 接线位置 | 梗概+预算内原文接进主力 agent 的 messages | builder check 发现业务 agent 不消费历史；用户裁定接线（否则预算机制无人消费） |
| 梗概形态 | 一条 `{"role": "user", "content": "【此前对话梗概】..."}` 进 messages 首位 | OpenAI 兼容接口无更合适槽位；resolve 的 `f"{role}: {content}"` 拼接自然 |
| 梗概超 30% 上限 | 触发重写收紧而非截断 | 截断可能正好截掉订单号；重写带「压缩到 N 字内」约束 |
| 水位线取值 | 本次读到的消息 max(id) 快照，同一事务内定死 | 防止摘要进行中用户新发消息被错误标记已覆盖（内容永久丢失） |
| build_context 签名 | `build_context(conversation_id, current_message)`；先组装后 append | 本轮输入作为扣减项参与预算，不与原文窗口重复计费 |
| /api/history | 豁免水位线，展示全量原文 | 前端渲染不能少消息；水位线过滤只发生在 build_context 内部 |
| token 计数 | tiktoken cl100k_base 本地近似，加载失败降级 chars/3 | 零延迟零成本；中文略多计（偏保守）；词表 CDN 不可达时对话不断 |
| 摘要触发 | 响应完成后 asyncio.create_task，水位线后原文 > history_budget×0.8 | 不阻塞对话；失败下轮重试 |
| 梗概占比 | ≤30% 预算 | 原文是主要信号源，梗概只是补充 |
| 摘要模型 | 复用 get_llm_quiet（非流式） | 内部调用不冒泡 SSE（r15 Critical 教训） |

## 摘要提示词（核心约束）

```
只提炼对话中的事实与顾客诉求，四类：
1. 问过的商品
2. 报过的订单号、手机号
3. 明确诉求（想退/想换/要发票/要补偿…）
4. 尚未解决的问题

硬约束：
- 对话里没出现的内容一个字不许编
- 寒暄、闲聊、客套不留
- 不知道就说没有，禁止推测
```

## 数据模型

```sql
CREATE TABLE conversation_summaries (
    conversation_id VARCHAR(32) PRIMARY KEY,
    summary         TEXT         NOT NULL,   -- 事实型梗概文本
    covered_until   BIGINT       NOT NULL,   -- messages.id 水位线
    token_count     INT          NOT NULL,   -- 梗概自身 token 数
    created_at      DATETIME(6)  NOT NULL,
    updated_at      DATETIME(6)  NOT NULL,
    KEY idx_summaries_conv (conversation_id)
)
```

消息表零改动。水位线是 messages.id，读历史时 `WHERE id > covered_until`。

## token 计数（app/tokens.py，新增）

```python
def count_tokens(text: str) -> int
def count_messages_tokens(messages: list[dict]) -> int   # 每条 +4 消息封装开销
```

tiktoken `encoding_for_model("gpt-4o")`（cl100k_base）。DeepSeek 自家
tokenizer 与之有偏差但方向稳定（中文略多计），预算因此偏保守——宁可
提前压缩，不可撑爆真实窗口。

## 上下文组装（app/context_builder.py，新增）

```python
@dataclass
class ContextBundle:
    summary_text: str | None     # 梗概（水位线前）
    recent_messages: list[dict]  # 预算内原文（水位线后）
    budget: dict                 # 可观测账目

def build_context(conversation_id: str) -> ContextBundle
```

预算账目（budget dict）：

```python
{
    "window": 8192,            # 配置窗口
    "total": 2048,             # 窗口 × 0.25
    "system": N,               # BUSINESS_PROMPT 实测
    "tools": N,                # 工具定义实测（四件 tool schema 拼接）
    "output_reserve": N,       # 1024 固定
    "turn_input": N,           # 本轮输入实测
    "history_budget": N,       # total - system - tools - output - turn
    "summary_tokens": N,       # 实际梗概占用
    "recent_tokens": N,        # 实际原文占用
    "dropped": N,              # 原文截断丢掉的条数
    "summary_covered_until": N # 水位线
}
```

## 摘要服务（app/summarizer.py，新增）

```python
async def maybe_summarize(conversation_id: str) -> dict | None
    # 水位线后原文 token > history_budget × 0.8 时触发
    # 输入 = 旧梗概（若有）+ 水位线后原文
    # 输出 = 新梗概，upsert 表，水位线前移到最新消息 id
    # 失败只记日志，下一轮重新评估
```

调用点：`/api/chat` 响应完成后的 finally 里 `asyncio.create_task`。

## 与现有代码的关系

| 位置 | 变化 |
|---|---|
| app/db.py | SCHEMA 加 conversation_summaries 表 |
| app/repo/chats.py | 水位线读历史（id > covered_until，仅供 build_context；/api/history 全量不变）；梗概 get/upsert |
| app/store.py | 导出新函数 |
| app/api/customer.py | 历史组装换 build_context；finally 里 create_task(maybe_summarize) |
| app/graph.py | business_node 改为把梗概+预算内原文传给 run_business_agent_with_limit（builder check 裁定：接线） |
| app/agent.py | run_business_agent_with_limit 接受完整 messages（含历史），不再只发本轮消息 |
| app/tokens.py | 新增 |
| app/context_builder.py | 新增 |
| app/summarizer.py | 新增 |
| app/api/admin.py | GET /api/admin/context-usage?conversation_id= 查账目 |

HISTORY_LIMIT 退役（预算制取代条数制）。

## 风险

- **tiktoken 对 DeepSeek 的偏差**：略多计 → 提前压缩 → 保守方向，
  安全；验收时用真实 usage 对账校准偏差幅度
- **摘要丢细节**：提示词只允许四类事实，顾客闲聊里提到的偏好会丢——
  可接受，客服场景事实与诉求才是要延续的
- **摘要任务与下一轮并发**：水位线取读快照的 max(id)（同一事务），
  下一轮用旧梗概正确性无损，新消息下轮正常计入；upsert 原子
- **梗概覆盖后指代消解的历史变短**：消解用的历史也走 ContextBundle，
  梗概文本里有订单号等实体，够用
- **业务 agent 历史变长后的行为**：prompt 已有「工具结果后基于结果
  回答」约束，历史消息里含此前工具结论不会诱导编造；步数/token
  上限不变，历史变长只影响输入 token（预算内）

## 验收

1. 聊 25+ 轮（含长消息），上下文总量全程 ≤ 预算线，不爆
2. 压缩真实发生：水位线前移，summaries 表出现事实型梗概
   （含订单号/诉求，无寒暄）
3. 梗概规则钉住：构造含订单号 + 寒暄的对话，梗概里有订单号、无寒暄
4. 每轮 trace 的 budget 账目完整（system/tools/梗概/原文/丢弃数）
5. admin 端点可查最近轮次账目
6. 全量测试通过；浏览器实聊 25 轮验收
