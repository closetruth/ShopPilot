# 向量检索范围收敛：实施计划

对应设计：`docs/superpowers/specs/2026-10-28_r08-vector-scope-reduction-design.md`

## 步骤 1：移除商品与工单向量

- [x] 删除 `vector_store.product_document`
- [x] 删除 `vector_store.work_order_document`
- [x] `rebuild_from_store` 改为只索引 FAQ，返回结构去掉 products/work_orders
- [x] 删除 `main.confirm_work_order` 里的 `_index_work_order` 双写
- [x] 验证：重建返回 `{"faq": 47, "total": 47, "written": 47}`
- [x] 提交

## 步骤 2：商品改全量返回

- [x] 删除 `tools._semantic_products`
- [x] `lookup_product` 精确匹配失败时返回全部在售商品，附说明
- [x] 验证：「我想买个杯子送人」返回 4 款杯子
- [x] 提交

## 步骤 3：search_knowledge 只查 FAQ

- [x] 删除 `tools._resolve_source`，FAQ 回表逻辑内联
- [x] 检索时 `visitor_id` 传空串——FAQ 是公开数据
- [x] 返回结构去掉 `source` 字段（只剩一种来源）
- [x] 工具描述改为「检索店内政策知识」，并指明商品/订单用别的工具
- [x] 验证：「退货运费谁出」命中 P-RET-001 与 P-LOG-002
- [x] 提交

## 步骤 4：清理无引用函数

- [x] `store.get_work_order_row`
- [x] `store.get_work_order_row_by_no`
- [x] `store._fetch_work_order`
- [x] `store.all_products_for_index`
- [x] `store.all_work_orders_for_index`
- [x] `store.product_by_id`
- [x] 验证：全文搜索无残留引用，`import app.main` 通过
- [x] 提交

## 步骤 5：测试重写

- [x] 删除商品/工单向量相关用例
- [x] 删除跨访客隔离用例（已无私有数据进索引）
- [x] 新增：FAQ 文档构建包含标题与小节名
- [x] 新增：检索结果按分数降序
- [x] 全量 `uv run pytest` 通过（81 项）
- [x] 提交

## 完成定义

设计文档验收五条全部满足，测试全绿。

## 执行中偏离计划的地方

- 最初想给商品加中文词法规则（去末字、bigram），补「杯子」匹配「马克杯」。
  这属于用规则补规则，且每加一条都会引入新的误命中（「手机」→「手工」）。
  改为直接全量返回，模型自己判断，代码更少、行为更可预测。
- 重启服务时 `pkill -f uvicorn` 在 Windows 下不生效，导致重建仍走旧代码，
  返回里还带着 products/work_orders 字段。改用 PowerShell 按命令行匹配杀进程。
