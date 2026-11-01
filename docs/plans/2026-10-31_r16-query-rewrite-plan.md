# r16 实施计划：Query 标准化（售后口语改写）

设计：`docs/superpowers/specs/2026-10-31_r16-query-rewrite-design.md`

裁定回顾：不为「包装破了」单条硬调词条（验收放宽为分数显著提升）；
「要投诉你们的服务」测试更新为 r16 新行为（越闸命中投诉条款）；
轨迹走 state 加字段。

- [ ] **Task 1：rewrite_query 纯函数 + 词表**
  - 新增 `app/query_rewrite.py`：`rewrite_query(query) -> tuple[str, list[str]]`
  - 拼接不替换；无命中返回原话；多词条命中按词表序拼接去重
  - `tests/test_query_rewrite.py`：触发命中、无命中透传、多词条去重、
    增益验证参数化（每条词条：原分 <0.65 时改写后 ≥0.65 或提升 ≥0.05）+ 术语存在于 FAQ 内容
  - 提交：`feat: query 标准化词表，售后口语到政策术语`

- [ ] **Task 2：检索节点接入改写 + 原话回退**
  - `app/knowledge_node.py`：检索前 rewrite；insufficient 且有改写时用
    原话回退重查，取 max_score 高的一组；Outcome 加 `rewritten_terms`/
    `rewritten_query`/`fallback_used`
  - 低置信度落库记最终采用的 query
  - 测试：改写命中提升、回退生效（改写后仍不足→原话兜底）、原话更好时取原话
  - 提交：`feat: 检索节点接入 query 改写，证据不足原话回退取优`

- [ ] **Task 3：轨迹落 detail**
  - `app/graph.py`：ConversationState 加 `rewritten_terms`
  - `app/main.py`：trace detail = {rewritten_terms, rewritten_query, fallback_used}
  - 测试：图跑知识路径后 state 带 rewritten_terms
  - 提交：`chore: 改写词条落 agent_traces detail`

- [ ] **Task 4：检索测试更新 + 口语用例**
  - 「要投诉你们的服务」更新为 r16 新行为（命中投诉条款），docstring 注明裁定
  - 新增口语 query 用例：「快递好久没动」→ P-LOG-001 补偿；
    「东西坏了想退」→ 质量问题；「七天了还能退吗」→ P-RET-001
  - 「包装破了」固化新行为：分数显著提升 + 命中「商品完好的判定」方向
  - 无关 query 组（明天几点开会等）维持拒答
  - 提交：`test: 口语 query 检索用例与 r16 行为固化`

- [ ] **Task 5：浏览器验收**
  - 真服务跑「包装破了」「快递好久没动」「退货的运费谁出」（回归）、
    「明天几点开会」（拒答）
  - 查 agent_traces 确认 detail 落库
  - 提交：`chore: r16 验收记录`

- [ ] **Task 6：README + 开发日志**
  - README 对话流程段补一行 query 标准化
  - 开发日志 `docs/dev-notes/r16.md`：裁定三件（包装破了放宽、
    投诉测试更新、state 通路）+ builder check 四问
  - 提交：`docs: r16 文档与日志`
