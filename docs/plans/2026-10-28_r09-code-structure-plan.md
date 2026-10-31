# ShopPilot 阶段八实施计划：代码结构整理

对应设计：`docs/superpowers/specs/2026-10-28_r09-code-structure-design.md`

## 步骤 1：脚本冒烟测试（高优先级）

- [x] `tests/test_scripts.py`：四个脚本可导入、可编译
- [x] `test_reindex_reads_fields_returned_by_rebuild`：用 monkeypatch 注入
      假的 `rebuild_from_store` 返回值，断言脚本不抛 `KeyError`
- [x] `test_import_faq_builds_documents`：切分逻辑有输出
- [x] 验证：`uv run pytest tests/test_scripts.py` 10 项通过
- [x] 提交

## 步骤 2：抽出连接层

- [x] `app/db.py`：`conn_params()` / `connect()` / `init_db()` /
      `_ensure_columns()` / `now()`
- [x] 建表语句整体搬迁，不改动
- [x] 验证：`init_db()` 对已有库幂等
- [x] 提交

## 步骤 3：拆会话仓储

- [x] `app/repo/chats.py`：五个会话函数 + `_title_of`
- [x] 验证：会话相关测试通过
- [x] 提交

## 步骤 4：拆业务仓储

- [x] `app/repo/commerce.py`：商品、订单、工单、草稿
- [x] 商品搜索三函数（`search_products` / `_search_terms` / `_search_terms_any`）
- [x] 抽出 `_resolve_product_id()`，去掉 `create_work_order` 里的嵌套分支
- [x] `_product_for_order` 改名 `product_name_for_order` 并公开
      （草稿模块要跨领域调用）
- [x] 草稿函数去掉 `work_order_` 前缀（模块内已无歧义）
- [x] **`now = _now()` 全部改名 `ts`**：`now` 现在是 `db.py` 的函数名，
      局部变量会遮蔽它
- [x] 验证：订单、工单、草稿测试通过
- [x] 提交

## 步骤 5：拆知识仓储

- [x] `app/repo/knowledge.py`：FAQ 四个函数
- [x] 验证：FAQ 导入与检索测试通过
- [x] 提交

## 步骤 6：门面改写

- [x] `app/store.py` 改为纯转发，保留原有函数名
- [x] `__all__` 显式声明对外接口
- [x] 验证：`import app.main` 通过，所有调用点零改动
- [x] 提交

## 步骤 7：全量验证

- [x] `uv run pytest` 91 项通过（原 81 + 脚本 10）
- [x] 重启服务，`/health`、`/api/orders`、`/api/conversations` 正常
- [x] 对话端到端：政策问题引用 P-RET-001、P-LOG-002
- [x] 提交

## 完成定义

设计文档验收五条全部满足，测试全绿。

## 未执行项

- **前端拆分**：评估后放弃，理由见设计文档「没做的：前端拆分」。
  若要做，前置条件是先加 ES module 冒烟测试。

## 执行中偏离计划的地方

- 原计划同时拆前端 `app.js`，实施时发现 90 多个函数互相调用、共享 20 个
  DOM 引用，且无构建工具，拆分风险高于收益。主动缩减范围并记录理由，
  没有硬做。
- `commerce.py` 原计划再拆成「商品/订单」与「工单」两个文件，实际发现
  工单要从订单取 `product_id`、草稿要查订单商品名，拆开会切断内聚关系，
  保持单文件。
