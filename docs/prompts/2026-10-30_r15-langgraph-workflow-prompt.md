# r15 提示词：LangGraph 图架构改造

## 用法

本轮开工时粘贴给助手的上下文。

---

你是 ShopPilot 项目的开发助手，按 Superpowers 流程工作，先 brainstorm。

## 交互约定

- 需求不明确时用 AskUserQuestion 弹窗给选项，不要用纯文字提问
- 一旦拍板的方案直接实施，不要反复确认
- 全程中文交流

## 本轮任务

workflow + agent，用 LangGraph 图按指代消解、意图识别、意图分流、知识检索、
置信度闸、主力 agent、日志记录组织对话。意图归四个类：知识检索、业务数据、
投诉直接模板、闲聊模板。会话状态用 LangGraph checkpoint。主力 agent 是
ReAct 循环，缺信息问用户，停止条件有 token 和步数。置信度弱的兜底话术
落库供数据飞轮，持久化用 LangGraph 的 checkpoint。

## 已定决策

| 项 | 选择 |
|---|---|
| 意图识别 | 规则先行 + LLM 兜底 |
| checkpoint | MySQL（与业务同库），不行退 SQLite |
| 历史分工 | MySQL 存消息，checkpoint 只存工作字段（不含消息） |
| 主力 agent 工具 | 只拿业务四件（查商品/订单/工单/起草），search_knowledge 退役 |
| 指代消解 | 按需触发：含代词或极短且延续话题才跑 |
| 停止条件 | recursion_limit=15 + token 上限 8000 |
| 日志 | agent_traces 表，每轮一条 |
| 模板节点 | 投诉（安抚+转人工+自动建工单）、闲聊（自我介绍）零 LLM 调用 |

## 需要特别注意

- **checkpoint state 不含消息**：消息每轮从 MySQL 读入作为图输入；
  state 只放 resolved_query / intent / slots 等工作字段
- **意图识别错误没有回头路**：闲聊模板话术要留台阶（「如需查询订单或
  售后请告诉我」）；LLM 兜底置信度 < 0.5 按业务数据处理
- **langgraph-checkpoint-mysql 包较新**：先验证与 langgraph 1.2.18 兼容，
  不行退 SQLite（checkpoint 接口是 BaseCheckpointSaver，换存储只改构造）
- **search_knowledge 退役**：检索逻辑移入图的检索节点，意图识别代替
  模型自主决定
- **SSE 协议不变**：astream_events 在 StateGraph 下事件名一致，前端零改动
- **低置信度池复用 r14**：检索节点里弱置信写入 low_confidence_queries

## 验收

「你好」零 LLM 调用进闲聊模板；「我要投诉」出模板话术 + 自动建投诉工单；
「退货运费谁出」带引用编号；「它怎么样了」能消解；agent_traces 每轮有
记录；checkpoint 在 MySQL 可见；SSE 协议零不兼容变化；全量测试通过。
