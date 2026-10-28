# ShopPilot 阶段五实施计划：无订单工单

对应设计：`docs/superpowers/specs/2026-10-28-orderless-work-order-design.md`

## 步骤 1：规则与校验

- [x] `schemas.py` 新增「咨询」「投诉建议」两个类型，共七类
- [x] `ORDERLESS_ISSUE_TYPES` 常量：咨询 / 投诉建议 / 其他
- [x] `WorkOrderCreate.order_no` 改为可选，空白串归一为 `None`
- [x] `model_validator` 强制四类售后必须有订单号
- [x] 验证：`pytest tests/test_schemas.py`
- [x] 提交

## 步骤 2：数据模型

- [x] `work_orders.order_no` 改 `NULL`
- [x] `work_order_drafts.order_no` 改 `NULL`
- [x] `init_db()` 用 `information_schema` 检测并 `MODIFY COLUMN`，幂等
- [x] `orders.order_no` 保持 `NOT NULL`
- [x] 验证：两表 `order_no` 均 `IS_NULLABLE = YES`
- [x] 提交

## 步骤 3：去重与创建

- [x] `find_open_work_order()` 显式处理 `order_no IS NULL` 分支
- [x] `create_work_order()` 支持 `order_no=None`，商品留空
- [x] `create_work_order_draft()` 支持 `order_no=None`
- [x] `_product_for_order()` 对无订单返回 `None`
- [x] 验证：无订单建单、按访客+类型判重、跨类型不冲突
- [x] 提交

## 步骤 4：工具与接口

- [x] `draft_work_order` 的 `order_no` 参数改为可选并说明规则
- [x] 归属校验仅在订单号非空时执行
- [x] `POST /api/work-orders/drafts` 的 `order_no` 改可选
- [x] 修 `_draft_from_tool_event` 对 `order_no=None` 的误判（原先会直接跳过）
- [x] 验证：`draft_work_order.func` 直接调用四种组合
- [x] 提交

## 步骤 5：前端

- [x] 面板加「提交无订单工单」按钮，仅「我的订单」页签显示
- [x] 抽出 `buildDraftForm()`，订单提单与独立提单共用
- [x] 类型下拉标注「（无需订单）」，无订单时默认选「咨询」
- [x] 选售后类但无订单号时前端拦截并提示
- [x] 卡片与列表显示「未关联订单」，不出现 `null`
- [x] 修重复声明 `ISSUE_TYPE_OPTIONS` 导致的 `SyntaxError`
- [x] 提交

## 步骤 6：prompt 与验收

- [x] prompt 增补订单号规则与「不要机械要求订单号」
- [x] 补测试：无订单建单、去重、三类无需订单、四类必须订单
- [x] 修测试订单主键冲突（改为 `ON DUPLICATE KEY UPDATE`）
- [x] 全量 `uv run pytest` 通过（64 项）
- [x] 浏览器验收：独立提单、重复拦截、AI 起草、标签切换
- [x] 提交

## 完成定义

设计文档「验收标准」七条全部满足，测试全绿。

## 执行中偏离计划的地方

- 原以为只改 `work_orders.order_no`，实际 `work_order_drafts.order_no` 同样是
  `NOT NULL`，提交时抛 `IntegrityError` 才发现，两张表都要迁移。
- `_draft_from_tool_event` 里 `if not (order_no and issue_type)` 会把无订单草稿
  直接丢弃，改为只校验 `issue_type`。
- 前端 `ISSUE_TYPE_OPTIONS` 被重复声明，浏览器报 `SyntaxError` 导致整页脚本失效。
