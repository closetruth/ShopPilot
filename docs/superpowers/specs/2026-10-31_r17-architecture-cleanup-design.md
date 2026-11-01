# ShopPilot 阶段十七设计：架构清理（三处技术债）

## 意图

r15/r16 连续两轮功能叠加后，代码里沉淀了三处债（r16 收尾时确认）：

1. `main.py` 偏胖（~600 行）：SSE、轨迹、草稿确认、admin CRUD、图缓存
   混在一个文件
2. 口语词表两处维护：`intent.py` 的 `_AFTERSALE_RE` 与
   `query_rewrite.py` 的 `REWRITE_RULES` 是同一组口语词的两个投影，
   加词条容易漂移
3. `ConversationState` 17 个字段平铺：输入、跨轮槽位、本轮产出、
   观测数据混在一起

本轮全部处理。**纯重构，行为不变**——246 项测试是安全网。

## 非目标

- 不改任何对外行为（接口、SSE 协议、意图结果、检索结果）
- 不动检索与改写逻辑本身
- 不引入新依赖

## 反目标

- 不允许拆分后出现循环导入（api → graph → api 之类）
- 不允许为了分组改变 LangGraph state 的 flat channel 语义

## 1. main.py 拆分

```
app/main.py          FastAPI 实例、lifespan、路由挂载、静态托管（薄壳）
app/api/chat.py      POST /api/chat、SSE 流、图单例、_record_turn_trace、
                     TOOL_LABELS、草稿事件提取
app/api/admin.py     /api/admin/faq CRUD + bulk + categories + search +
                     reindex + toggle、/api/admin/low-confidence
app/api/customer.py  /api/history、/api/conversations、/api/orders、
                     /api/work-orders（列表/确认/草稿）、/api/faq/{id}
```

依赖方向：api/* → graph/llm/store/tools（单向）。`app.main:app` 入口
不变（uvicorn 与 launch.json 无感）。

test_chat.py / test_chat_stream.py 的导入路径随实现更新（不留兼容
re-export——按惯例删干净）。

## 2. 词表单一来源

`query_rewrite.py` 的规则升级为带类别的结构：

```python
@dataclass(frozen=True)
class RewriteRule:
    name: str            # 类别名：质量问题 / 破损 / 物流延迟 / 丢件 / ...
    pattern: re.Pattern  # 触发词
    terms: str           # 追加术语
```

「情况陈述类」（质量问题、破损、物流延迟、丢件——顾客在客观描述
出了什么状况，不是在情绪表达）的触发词合并派生出 `AFTERSALE_RE`，
`intent.py` 直接 import 它替换手写的 `_AFTERSALE_RE`。

顺带补齐两表差异：`漏了/开裂/掉漆/失灵/用不了` 进质量问题触发词
（改写层也受益）；`有问题/有毛病/瑕疵/故障` 自然进入意图投影。

## 3. ConversationState 分组

TypedDict 多继承，channel 保持 flat：

```python
class _TurnInput(TypedDict, total=False):      # 每轮输入，不跨轮
    message / history / visitor_id
class _CrossTurnSlots(TypedDict, total=False): # 跨轮槽位，checkpoint 持久化
    pending_topic / collected_slots / turn_count
class _TurnOutput(TypedDict, total=False):     # 本轮产出
    resolved_query / intent / final_text / node_path / ...
class _Observability(TypedDict, total=False):  # 观测，喂 trace
    top_score / tool_calls / steps / total_tokens / stopped_by /
    rewritten_terms / rewritten_query / fallback_used

class ConversationState(_TurnInput, _CrossTurnSlots, _TurnOutput, _Observability, total=False): ...
```

节点读写方式不变（仍是 `state["message"]` / `return {"final_text": ...}`）。

## 风险

- **拆分时丢 helper**：`_text_of`、`_sse` 被测试直接 import，搬走后
  测试导入路径要同步改——漏改会收集期报错，安全网会叫
- **TypedDict 继承不被 LangGraph 识别**：schema 解析走
  `__annotations__`，多继承会合并注解，测试（图四路路由）直接验证
- **意图投影变化引入误判**：新增触发词让原本 None 的 query 变
  knowledge——参数化测试盯边界（「这个商品我能用吗」仍 None）

## 验收

1. 全量测试通过（246 项，含协议级）
2. 浏览器冒烟四条路径（闲聊/投诉/知识口语/业务）行为不变
3. `app/main.py` 只剩装配代码；grep 无循环导入
4. 词表加一条词条只需改 `query_rewrite.py` 一处
