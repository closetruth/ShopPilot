# ShopPilot 阶段三实施计划：创建工单

日期：2026-10-28
类型：实施计划
状态：已完成

对应设计：`docs/superpowers/specs/2026-10-28-create-work-order-design.md`

## 步骤 1：数据模型

- [x] `store.py` 的 `work_orders` 建表语句增加 `priority`、`source`
- [x] 对已存在的库执行 `ALTER TABLE` 补列（幂等：先查 `information_schema`）
- [x] `scripts/seed.py` 补 `priority`/`source` 字段
- [x] 验证：`DESCRIBE work_orders` 出现两列
- [x] 提交

## 步骤 2：校验模型

- [x] `schemas.py` 新增 `WorkOrderCreate`：`order_no`、`issue_type`、`description`
- [x] `issue_type` 用 `Literal` 限定五个枚举值
- [x] `description` 长度 10–500
- [x] 验证：`uv run pytest tests/test_schemas.py`
- [x] 提交

## 步骤 3：存储层

- [x] `store.py` 新增 `find_open_work_order(visitor_id, order_no, issue_type)`
- [x] `store.py` 新增 `create_work_order(visitor_id, order_no, issue_type, description)`
  - 生成 `WO` + 8 位十六进制编号
  - `product_id` 由该顾客同订单的既有工单反查，查不到留空
  - 固定 `status='待处理'`、`source='ai'`、`priority='普通'`
- [x] 验证：直接调 store，确认写入与反查正确
- [x] 提交

## 步骤 4：工具实现

- [x] `tools.py` 新增 `create_work_order`，参数经 `WorkOrderCreate` 校验
- [x] 写入前先 `find_open_work_order`，命中返回 `duplicate`
- [x] 工具描述写明「仅在顾客明确同意后调用」
- [x] 加入 `TOOLS` 列表
- [x] 验证：脚本调用确认建单、重复判定、跨顾客拒绝
- [x] 提交

## 步骤 5：Prompt 调整

- [x] 补充创建工单的交互规则：先复述请确认、未确认不得调用、不承诺时限
- [x] 验证：跑三轮对话验证确认环节
- [x] 提交

## 步骤 6：测试与浏览器验收

- [x] `tests/test_schemas.py` 覆盖 `WorkOrderCreate` 合法与非法输入
- [x] `tests/test_store.py` 覆盖建单、重复判定、跨顾客隔离
- [x] 浏览器实跑四项验收：正常建单、顾客拒绝、重复提交、跨顾客
- [x] 提交

## 完成定义

设计文档「验收标准」八条全部满足，测试全绿。
