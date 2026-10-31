# ShopPilot 阶段四实施计划：工单可见化与前端确认

对应设计：`docs/superpowers/specs/2026-10-28_r04-work-order-visibility-design.md`

## 步骤 1：订单表

- [x] `store.py` 建 `orders` 表（订单号、访客、商品、数量、金额、下单时间）
- [x] `list_orders()` / `get_order()`，返回每单已有关联工单
- [x] `visitor_has_order()` 改为查 `orders`，不再从工单反推
- [x] `scripts/seed.py` 灌 4 笔订单，含一笔无工单的新订单
- [x] 验证：`list_orders('demo-visitor')` 返回 3 笔，其中一单无工单
- [x] 提交

## 步骤 2：草稿表与接口

- [x] 建 `work_order_drafts` 表，30 分钟过期
- [x] `create_work_order_draft()` / `get_work_order_draft()` / `delete_work_order_draft()`
- [x] 草稿带出关联商品名，供确认卡核对
- [x] 验证：草稿不产生工单记录
- [x] 提交

## 步骤 3：工具改造

- [x] 新增 `list_my_orders`（无参数）
- [x] `create_work_order` 改为 `draft_work_order`：只建草稿，返回 `drafted: true`
- [x] 工具描述写明「不会真正提交」「不要宣称已提交」
- [x] 归属校验与去重保留在起草前
- [x] 验证：脚本调用确认起草不写库
- [x] 提交

## 步骤 4：后端接口与 SSE

- [x] `GET /api/orders`
- [x] `GET /api/work-orders`
- [x] `POST /api/work-orders/drafts`（顾客主动提单）
- [x] `POST /api/work-orders`（凭 draft_id 确认，只接受 id 不接受内容）
- [x] SSE 在 `draft_work_order` 成功后推 `work_order_draft`
- [x] 修正 `_draft_from_tool_event`：`ToolMessage.content` 是 JSON 字符串需解析
- [x] 验证：curl 跑通四条接口
- [x] 提交

## 步骤 5：前端面板与卡片

- [x] 面板改双标签：我的订单 / 我的工单
- [x] 订单项显示商品、数量、金额、下单时间、关联工单徽标
- [x] 订单项内联提单表单：问题类型下拉 + 描述 + 提交
- [x] 聊天确认卡片：逐行列出待核对字段 + 确认/取消
- [x] 提交后原地变结果卡片，可跳转面板
- [x] 重复提交在前端给出既有工单号
- [x] 验证：浏览器实跑
- [x] 提交

## 步骤 6：prompt 与验收

- [x] prompt 加入 `list_my_orders` 优先规则：不让顾客凭记忆报单号
- [x] prompt 强调 AI 不能宣称「已提交」，工单由顾客点击生成
- [x] 修正 seed 中工单 `status` 误填为问题类型的问题
- [x] 补测试：订单列表、归属隔离、草稿不写库
- [x] 全量 `uv run pytest` 通过（55 项）
- [x] 浏览器验收八条
- [x] 提交

## 完成定义

设计文档「验收标准」八条全部满足，测试全绿。

## 执行中偏离计划的地方

- 原计划只做「工单可见」，实际发现「顾客不知道订单号」与「首单顾客建不了单」
  两个更根本的问题，因此额外引入 `orders` 表，工作量大于预期。
- seed 数据里 `status` 字段原本误填成问题类型（如「运输破损」），导致去重判断
  失效；已改为真实状态值。
