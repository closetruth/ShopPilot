# ShopPilot 阶段七实施计划：政策知识库

对应设计：`docs/superpowers/specs/2026-10-28-faq-knowledge-design.md`

## 步骤 1：数据获取

- [x] 探查可达数据源：GitHub 可达，HuggingFace / ModelScope 均不可达
- [x] 评估 `akakooluo-ai/customer-service-scripts`：1480 条但以英文为主、
      充斥 `[Days]` 占位符与 emoji，不适合作中文客服知识
- [x] 选定 `hong356/carepilot` 的 `data/policies/`：11 篇中文政策，结构化程度高
- [x] 下载至 `data/faq/`，11 个文件全部成功
- [x] 提交

## 步骤 2：数据模型

- [x] `faq_documents` 表：id / doc_id / title / category / version /
      section / content / source / updated_at
- [x] `upsert_faq_document()` 幂等写入
- [x] `all_faq_for_index()` / `faq_by_id()` / `list_faq()`
- [x] 验证：建表成功
- [x] 提交

## 步骤 3：导入脚本

- [x] `scripts/import_faq.py`：解析 frontmatter + 按 `## ` 切分
- [x] 小节切分跳过空内容
- [x] 无小节时整篇作一节
- [x] 处理 BOM（`utf-8-sig`）——源文件带 BOM 会导致 frontmatter 解析失败
- [x] 验证：导入 11 个文件共 47 条
- [x] 提交

## 步骤 4：向量化

- [x] `faq_document()`：嵌入「标题 + 小节名 + 正文」
- [x] `rebuild_from_store()` 纳入 FAQ
- [x] 验证：重建 220 条（5 商品 + 168 工单 + 47 FAQ）
- [x] 提交

## 步骤 5：检索增强

- [x] `search_knowledge` 命中 FAQ 时回表取完整字段
- [x] 返回 doc_id / title / category / section / content
- [x] 商品与工单命中保持原有结构
- [x] 提交

## 步骤 6：Prompt 约束

- [x] 政策类问题（退货/退款/运费/发票/物流/价保/会员/投诉）必须先查知识库
- [x] 明确「同时要查订单时两者并行，不是二选一」
- [x] 要求引用政策编号
- [x] 实测：「退运费谁出」同时调 search_knowledge + list_my_orders，
      回答引用 P-RET-001、P-REF-002
- [x] 提交

## 步骤 7：测试与验收

- [x] `tests/test_faq_import.py`：frontmatter 解析、小节切分、空节跳过、
      ID 唯一性、核心政策域覆盖
- [x] 修测试断言：正则已吃掉前导换行，改 `lstrip()`
- [x] 全量 `uv run pytest` 通过（82 项）
- [x] 检索质量实测：五类政策问题全部命中正确文档
- [x] 对话实测：回答引用政策编号与具体数值
- [x] 提交

## 完成定义

设计文档「验收标准」六条全部满足，测试全绿。

## 执行中偏离计划的地方

- 原以为要找通用电商 FAQ，实际发现结构化政策文档质量更高。通用客服话术库
  以英文为主且有大量占位符，直接导入会严重污染检索质量。
- 清理了 9 个残留 Python 进程——之前调试遗留，各自持有 Milvus Lite 文件锁，
  导致重建失败。这是 Milvus Lite 单进程模型的副作用，已记入已知限制。
