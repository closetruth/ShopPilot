# ShopPilot 阶段十二实施计划：结构化文档解析与批量导入

对应设计：`docs/superpowers/specs/2026-10-28_r12-markdown-split-design.md`

## 步骤 1：切分核心模块

- [ ] 新建 `app/markdown_split.py`：`SplitResult` / `Chunk` 数据类
- [ ] `parse_frontmatter()` 从 `scripts/import_faq.py` 移入
- [ ] 逐行状态机：围栏（``` 与 ~~~）、表格、标题识别
- [ ] 标题栈建树，叶子成条，父节点直属正文按长度决定独立或合并
- [ ] 超长按段落装箱，单段回退句末或硬切，表格按行切并重复表头
- [ ] 碎块合并、section 后缀 `（i/n）`、64 字截断
- [ ] `legacy=True` 分支：只认 `##` 且平铺
- [ ] `tests/test_markdown_split.py`：多级标题、围栏、表格、
      超长切块、深路径截断、CRLF、未闭合围栏、空文档
- [ ] 验证：`uv run pytest tests/test_markdown_split.py -q`
- [ ] 提交

## 步骤 2：脚本兼容

- [ ] `scripts/import_faq.py` 改为从 `app.markdown_split` 导入
- [ ] 调用 `split_markdown(..., legacy=True)`
- [ ] golden 测试：每个 `data/faq/*.md` 断言新旧 `(section, content)` 逐字相等
- [ ] 验证：`uv run pytest tests/test_faq_import.py tests/test_scripts.py -q`
- [ ] 提交

## 步骤 3：批量入库仓储

- [ ] `app/repo/knowledge.py` 新增 `create_documents(title, category, items)`
- [ ] 单事务 `executemany`，id 为 `faq-{uuid12}`，`source='web'`
- [ ] `app/store.py` 导出 `create_faq_documents`
- [ ] 验证：直接调用确认多条写入
- [ ] 提交

## 步骤 4：批量导入接口

- [ ] `POST /api/admin/faq/bulk/preview`：纯计算，不入库不嵌入
- [ ] `POST /api/admin/faq/bulk`：校验全部条目 → 批量写入 → 一次批量嵌入
- [ ] 复用 `FaqPayload` 约束；422 带 item 下标
- [ ] 响应回传 `indexed` 数，供前端提示重建索引
- [ ] `tests/test_admin_bulk.py`：preview 不落库、bulk 全成或全败、
      200 条上限、校验失败下标
- [ ] 验证：`uv run pytest tests/test_admin_bulk.py -q`
- [ ] 提交

## 步骤 5：前端批量导入

- [ ] `admin.html`：顶栏按钮 + `<dialog>` 两步结构
- [ ] `admin.js`：预览请求、渲染列表、勾选、确认导入
- [ ] 徽标：过短 / 第 i/n 段 / 节引言 / 标题截断
- [ ] 修 `renderCategories` 不清空 `#catOptions` 导致 datalist 累积重复
- [ ] `admin.css`：对话框与预览列表样式
- [ ] 验证：浏览器实跑粘贴 → 预览 → 导入 → 检索
- [ ] 提交

## 步骤 6：文档

- [ ] README 补批量导入用法与长度规则
- [ ] 提交

## 完成定义

设计文档验收七条全部满足，全量测试通过，浏览器端到端可用。
