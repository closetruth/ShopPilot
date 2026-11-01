# r18 实施计划：会话上下文预算与历史梗概

设计：`docs/superpowers/specs/2026-10-31_r18-context-budget-design.md`

裁定回顾：梗概+预算内原文**接进主力 agent**（graph.py/agent.py 要动）；
紧凑窗口 8192 默认；水位线取读快照 max(id)；梗概超限重写不截断；
/api/history 豁免水位线。

- [ ] **Task 1：token 计数（app/tokens.py）**
  - `count_tokens(text)`：tiktoken cl100k_base，加载失败降级 chars/3 并 warning
  - `count_messages_tokens(messages)`：每条 +4 封装开销
  - 测试：中英文计数、降级路径、messages 封装
  - 提交：`feat: 本地 token 计数，tiktoken 不可达时降级估算`

- [ ] **Task 2：梗概表与水位线读（repo/chats.py + db.py）**
  - SCHEMA 加 conversation_summaries（conversation_id 主键、summary、
    covered_until、token_count、created_at、updated_at）
  - `get_summary(conversation_id)`、`upsert_summary(...)`、
    `load_history_after(conversation_id, after_id)`（水位线后原文）
  - `load_history` 不动（/api/history 全量展示）
  - 测试：upsert 幂等、水位线过滤边界、原表不受影响
  - 提交：`feat: 会话梗概表与水位线读历史`

- [ ] **Task 3：上下文组装（app/context_builder.py）**
  - `build_context(conversation_id, current_message) -> ContextBundle`
  - 账目：window/total/system/tools/output_reserve/turn_input/
    history_budget（保底 400）/summary_tokens/recent_tokens/dropped/
    summary_covered_until
  - 原文超预算从最旧截断（梗概已覆盖部分不怕丢）
  - 测试：预算账目闭合、截断从旧到新、保底 400、无梗概/有梗概两态
  - 提交：`feat: token 预算的上下文组装，账目可观测`

- [ ] **Task 4：摘要服务（app/summarizer.py）**
  - SUMMARY_PROMPT：只提炼四类事实，编造禁令，寒暄不留
  - `maybe_summarize(conversation_id)`：原文 > budget×0.8 触发；
    水位线 = 本次读到消息的 max(id)（同事务快照）；梗概超预算 ×1.3
    时带「压缩到 N 字内」重写
  - 用 get_llm_quiet；失败只记日志
  - 测试：编造禁令（FakeLLM 返回对话外内容→测试钉提示词存在）、
    水位线快照语义、阈值触发、不触发
  - 提交：`feat: 后台异步摘要，事实型梗概单表存储`

- [ ] **Task 5：接线（customer.py + graph.py + agent.py）**
  - customer.py：build_context 替换 load_history；响应完成 finally 里
    create_task(maybe_summarize)；ContextBundle.summary_text +
    recent_messages 组装成 history 进图输入
  - graph.py：business_node 把完整 messages（system 后跟梗概+原文+
    本轮 resolved_query）传 run_business_agent_with_limit
  - agent.py：签名照收 messages，不再只发单条
  - trace detail 记 budget 账目
  - 测试：图测试 business 路径收到的 messages 含梗概条目
  - 提交：`feat: 梗概+预算内原文接进主力 agent`

- [ ] **Task 6：admin 账目端点 + HISTORY_LIMIT 退役**
  - GET /api/admin/context-usage?conversation_id= 返回最近轮次 budget
  - .env.example 清 HISTORY_LIMIT；README 环境变量表更新
  - 提交：`chore: 上下文账目端点，HISTORY_LIMIT 退役`

- [ ] **Task 7：全量回归 + 浏览器实聊 25 轮验收**
  - 全量 pytest
  - 浏览器实聊 25 轮（含长消息、订单号、寒暄），观察：压缩触发、
    水位线前移、trace 账目、agent 能引用早期订单号（梗概生效证据）
  - admin 端点查账
  - 提交：`chore: r18 验收记录`

- [ ] **Task 8：README + 开发日志 + 本轮提示词留痕**
  - README 对话流程与环境变量；docs/dev-notes/r18.md；docs/prompts/ 本轮提示词
  - 提交：`docs: r18 文档与日志`
