# ShopPilot 阶段九实施计划：知识库录入界面

对应设计：`docs/superpowers/specs/2026-10-28-knowledge-admin-design.md`

## 步骤 1：数据模型

- [x] `faq_documents` 增加 `enabled TINYINT(1) NOT NULL DEFAULT 1`
- [x] `_ensure_columns()` 补列，对已有库幂等
- [x] 验证：`DESCRIBE faq_documents` 出现 `enabled`
- [x] 提交

## 步骤 2：仓储扩展

- [x] `create_document()`：Web 录入用，生成 `faq-` + 12 位 id
- [x] `update_document()` / `set_enabled()` / `delete_document()`
- [x] `categories()`：分类及条目数
- [x] `delete_missing()`：按来源清理文件里已删除的小节
- [x] `all_for_index()` 只取 `enabled=1`
- [x] `list_by_category()` 返回含停用项
- [x] 验证：直接调用确认增删改查
- [x] 提交

## 步骤 3：import_faq 语义修正

- [x] 导入后删除该来源下文件中已不存在的小节
- [x] 同步删除对应向量
- [x] 输出提示「需重建索引才生效」
- [x] 验证：脚本可运行，删除逻辑生效
- [x] 提交

## 步骤 4：管理接口

- [x] 七个端点：列表、分类、新建、编辑、删除、启停、调试检索
- [x] 写入后立即 `_index_faq()` 同步向量
- [x] 停用时 `delete_by_source("faq", id)`
- [x] `/admin` 返回 `admin.html`
- [x] 验证：curl 跑通增删改查，新建后立即能检索到
- [x] 提交

## 步骤 5：录入界面

- [x] `admin.html`：分类 chips + 列表 + 编辑表单
- [x] `admin.css`：复用聊天页的变量体系
- [x] `admin.js`：ES module，含校验、保存、停用、删除、重建索引
- [x] 停用项显示删除线与「已停用」标记，不隐藏
- [x] 调试检索分数在 UI 中不暴露（仅 API）
- [x] 验证：浏览器实测选中、新建、保存、状态提示
- [x] 提交

## 步骤 6：修复 prompt 路由

- [x] 发现「XX 商品怎么保存」走了 `lookup_product` 而非 `search_knowledge`
- [x] prompt 明确「判断依据是问什么，不是有没有商品名」
- [x] `lookup_product` 查不到时提示可改用 `search_knowledge`
- [x] 验证：重跑同一问题，工具调用正确
- [x] 提交

## 步骤 7：修复测试静默跳过

- [x] 发现 14 项测试因 `.env` 未加载而全部 skip
- [x] 新增 `tests/conftest.py` 统一 `load_dotenv()`
- [x] 修复 `faq_by_id` 缺 `source` 字段导致的断言失败
- [x] 验证：123 项全部实跑通过
- [x] 提交

## 步骤 8：测试与验收

- [x] `tests/test_admin_faq.py`：14 项覆盖增删改查、校验、索引联动
- [x] 全量 `uv run pytest` 通过（123 项）
- [x] 浏览器实测：列表加载、编辑表单加载、新建保存、调试检索 0.797 分
      （实测时列表含测试残留共 49 条，清理后回到 47 条基础数据）
- [x] 清理测试过程中产生的垃圾数据，恢复 47 条
- [x] 提交

## 完成定义

设计文档验收七条全部满足，测试全绿。

## 执行中偏离计划的地方

- 原计划不包含「调试检索端点」。实测「新建后搜不到」时无法判断是没进索引
  还是分数不够，加了 `/api/admin/faq/search` 才定位到索引正常、问题在 prompt
  路由。这个端点保留下来，录不入时能自诊断。
- 原以为问题在索引写入，绕了很久。实际索引一直正常，是模型没调用检索工具。
