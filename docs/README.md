# 文档索引

按轮次排列。每轮对应一份设计（specs）与一份实施计划（plans），
日期统一为 `2026-10-28`。

## 轮次

| 轮次 | 主题 | 设计 | 计划 |
|---|---|---|---|
| 一 | 纯对话 MVP | [design](superpowers/specs/2026-10-28-chat-mvp-design.md) | [plan](../../plans/2026-10-28-chat-mvp-plan.md) |
| 二 | 工具调用 + MySQL | [design](superpowers/specs/2026-10-28-tools-and-mysql-design.md) | [plan](../../plans/2026-10-28-tools-and-mysql-plan.md) |
| 三 | 创建工单 | [design](superpowers/specs/2026-10-28-create-work-order-design.md) | [plan](../../plans/2026-10-28-create-work-order-plan.md) |
| 四 | 工单可见化与前端确认 | [design](superpowers/specs/2026-10-28-work-order-visibility-design.md) | [plan](../../plans/2026-10-28-work-order-visibility-plan.md) |
| 五 | 无订单工单 | [design](superpowers/specs/2026-10-28-orderless-work-order-design.md) | [plan](../../plans/2026-10-28-orderless-work-order-plan.md) |
| 六 | 向量检索与 RAG | [design](superpowers/specs/2026-10-28-vector-rag-design.md) | [plan](../../plans/2026-10-28-vector-rag-plan.md) |
| 七 | 政策知识库 | [design](superpowers/specs/2026-10-28-faq-knowledge-design.md) | [plan](../../plans/2026-10-28-faq-knowledge-plan.md) |
| 八 | 代码结构整理 | [design](superpowers/specs/2026-10-28-code-structure-design.md) | [plan](../../plans/2026-10-28-code-structure-plan.md) |
| 九 | 知识库录入界面 | [design](superpowers/specs/2026-10-28-knowledge-admin-design.md) | [plan](../../plans/2026-10-28-knowledge-admin-plan.md) |
| — | 向量检索范围收敛 | [design](superpowers/specs/2026-10-28-vector-scope-reduction-design.md) | [plan](../../plans/2026-10-28-vector-scope-reduction-plan.md) |
| — | 检索链路测试与阈值标定 | [design](superpowers/specs/2026-10-28-retrieval-testing-design.md) | [plan](../../plans/2026-10-28-retrieval-testing-plan.md) |

标注「—」的两轮是阶段内的修正，未单独编号。

## 推荐阅读顺序

想快速理解项目现状，读这三份：

1. **[知识库录入界面](superpowers/specs/2026-10-28-knowledge-admin-design.md)** —
   最新状态，含当前架构、已知限制
2. **[代码结构整理](superpowers/specs/2026-10-28-code-structure-design.md)** —
   目录为什么这样分，哪些层故意不做
3. **[向量检索范围收敛](superpowers/specs/2026-10-28-vector-scope-reduction-design.md)** —
   为什么只有 FAQ 进向量库

## 提示词

见 [prompts/README.md](../prompts/README.md)。

## 文档里的元信息约定

所有设计与计划文件头部统一三行：

```
日期：2026-10-28
类型：设计文档 | 实施计划 | 提示词
状态：已确认 | 已完成 | 已过时
```

状态值含义：

| 状态 | 含义 |
|---|---|
| 已确认 | 设计已拍板，尚未或正在实施 |
| 已完成 | 计划全部执行，验收通过 |
| 已归档 | 历史记录，不再维护 |
| 已过时 | 内容与实际不符，不要参照 |
