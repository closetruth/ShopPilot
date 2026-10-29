# ShopPilot 阶段十四实施计划：引用、拒答与低置信度问题池

对应设计：`docs/superpowers/specs/2026-10-30_r14-citation-refusal-design.md`

## 步骤 1：低置信度表

- [x] `app/db.py` 建 `low_confidence_queries` 表
- [x] `app/repo/knowledge.py`（或新 `app/repo/insights.py`）加 `record_low_confidence()`
- [x] 加 `list_low_confidence(limit)` 查询
- [x] 验证：直接调用确认写入与查询
- [x] 提交

## 步骤 2：工具返回结构调整

- [ ] `search_knowledge` 返回增加 `citable_ids`、`insufficient`、`max_score`
- [ ] `INSUFFICIENT_THRESHOLD = 0.65`
- [ ] 证据不足时清空 `results`，只给标记与提示
- [ ] 低置信度写入（失败不影响返回）
- [ ] 验证：直接调用确认三种分支（充分 / 低分 / 空）
- [ ] 提交

## 步骤 3：prompt 约束

- [x] 引用格式要求：`[P-XXX-000-0]`，只能从 `citable_ids` 选
- [x] `insufficient=true` 时的行为：明确拒答，禁止「一般来说」
- [x] 验证：对话实测「买一送一」不再补编
- [x] 提交

## 步骤 4：引用悬停

- [ ] `app/main.py` 加 `GET /api/faq/{faq_id}` 返回单条政策
- [ ] `app/static/app.js` 渲染时正则识别编号，生成 `.cite` 元素
- [ ] 悬停时按需查正文，`Map` 缓存
- [ ] `app/static/style.css` 加虚线下划线样式
- [ ] 验证：浏览器实测悬停显示正文
- [ ] 提交

## 步骤 5：低置信度查看接口

- [ ] `GET /api/admin/low-confidence?limit=100`
- [ ] 验证：curl 能取到步骤 2 写入的记录
- [ ] 提交

## 步骤 6：验收

- [ ] 「退货运费谁出」→ 含 `[P-RET-001-3]`，悬停有正文
- [ ] 「买一送一能退一件吗」→ 纯拒答，无补编
- [ ] 上述两条在低置信度表有记录
- [ ] 「快递丢件怎么办」正常回答不受影响
- [ ] 全量测试通过
- [ ] 提交

## 完成定义

设计文档验收六条全部满足，浏览器实跑通过。
