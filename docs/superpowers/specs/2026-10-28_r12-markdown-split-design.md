# ShopPilot 阶段十二设计：结构化文档解析与批量导入

## 意图

知识库录入目前只有两条路：单条手填，或把 Markdown 放进 `data/faq/` 跑脚本。

前者录入一份完整政策手册要点几十次；后者需要会写文件、敲命令。都不适合
非开发者。

现有的切分规则 `split_sections()` 只认 `##` 二级标题，不处理多级层级、表格、
代码围栏，也没有长度上限——一个 `##` 下写几千字就是一整条，而向量文本
`text[:2048]` 会静默截断，后半段内容即使命中也不可见。

这一轮让管理者在 Web 界面粘贴一份大文档，自动解析成多条结构化知识，
预览确认后一次性入库。

## 目标

1. 粘贴 Markdown，识别多级标题、表格、列表、代码围栏，切成多条知识
2. 单节超 500 字按段落再切
3. 入库前可预览切分结果，确认后才写库
4. 现有 `data/faq/` 导入行为保持不变

## 非目标

- 富文本渲染（正文仍是纯文本）
- 保留「文档」整体概念（每节独立成条，不建文档表）
- 图片、附件
- 拖拽上传文件（只支持粘贴）

## 反目标

- 不允许切分结果未经人确认就入库
- 不允许改变存量 `data/faq/` 的导入结果

## 关键决定

| 决定 | 选择 | 理由 |
|---|---|---|
| 切分产物 | 直接拆成多条知识 | 检索最直接，无需两层结构 |
| 超长阈值 | 500 字 | 与已验证的检索粒度一致；现有最长 115 字，留足余量 |
| 脚本是否复用新解析器 | 复用，但固定 legacy 模式 | 保输出逐字一致，避免存量条目被删重建 |
| section 命名 | 默认末级标题，可选路径模式 | 向量文本已含 title，路径易超 64 字上限 |

## 切分算法

新建 `app/markdown_split.py`，纯函数，无外部依赖：

```python
def split_markdown(
    text: str,
    *,
    title: str | None = None,
    category: str | None = None,
    max_chars: int = 500,
    section_mode: Literal["leaf", "path"] = "leaf",
    legacy: bool = False,
) -> SplitResult
```

`SplitResult` 含 `title / category / frontmatter / chunks / warnings`，
`Chunk` 含 `section / content / path / kind / part / warnings`。

要点：

- 统一 `\r\n → \n`，剥离 frontmatter
- 逐行状态机区分围栏内外：` ``` ` 与 `~~~` 之间不识别标题，整块视为原子；
  连续 `|` 行组成表格也视为原子
- 标题正则 `^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$`，用栈建树
- 叶子节点成条；父节点直属正文 ≥20 字时独立成条（`kind="preamble"`），
  <20 字并入首个子块
- 全文无标题 → 整篇作一条「全文」
- 超长按 `\n\n` 段落贪心装箱；单段超长时在尾部 100 字内回退到 `。！？` 句末，
  找不到硬切；表格按行切且每块重复表头
- 碎块（<20 字）并入相邻块，满足 API 的 `content ≥ 20` 约束
- 切多块时 section 追加全角 `（i/n）`，基础名截断到 `64 - len(后缀)` 保证不超限
- `legacy=True`：只识别 `##` 且平铺，完全复现旧行为

## 接口

```
POST /api/admin/faq/bulk/preview
  请求 { markdown(1..100000), title?, category?, section_mode?, max_chars?(200..2000) }
  响应 { title, category, chunks:[{index,section,content,chars,path,kind,warnings}],
         summary:{chunks, too_short, duplicate_sections} }

POST /api/admin/faq/bulk
  请求 { title, category, items:[{section, content}] }   # 1..200 条
  响应 { created:[{id, section}], indexed }
```

切分是启发式的，必须让人确认后再入库，所以 preview 不可省。
预览与入库用同一套 `FaqPayload` 约束校验，保证预览能过的入库一定能过。

**批量入库用单事务 + 一次批量嵌入**，不前端循环调单条：N 次 HTTP 往返、
N 次 Milvus 写入与 flush，半途失败难回收。

## 前端

顶栏新增「批量导入」，原生 `<dialog>` 两步：

第一步：粘贴区、标题（留空则取 frontmatter/H1）、分类（复用 datalist）、
「使用标题路径」复选框、「解析预览」。

第二步：摘要行（共 N 条、最长 M 字）+ 预览列表，每行含勾选框 / section /
字数 / 前 80 字摘要 / 徽标（过短·第 i/n 段·节引言·标题截断）。
底部「返回修改」「确认导入 N 条」。

确认 → 批量接口 → 关弹窗 → 刷新列表 → 提示「已导入 N 条，索引 M 条」；
若 `indexed < created` 提示可点重建索引。

## 脚本兼容

`scripts/import_faq.py` 改调 `split_markdown(..., legacy=True)`。

现状 11 个文件零个 `###`、零表格、零代码围栏，最长 content 115 字，
legacy 模式下输出与旧 `split_sections` 逐字一致，id 仍为 `{doc_id}-{index+1}`，
`delete_stale_faq` 不会误删。

**不**在脚本里启用多级/超长切分：现状无收益，且将来某节一旦跨 500 触发多块，
序号重排会让该文件后续条目全部被删重建，还得重建索引。Web 走 uuid id 无此问题。

## 验收

1. 多级标题文档能正确切成多条，section 命名合理
2. 超 500 字的节自动切块并带 `（i/n）` 后缀
3. 代码围栏内的 `#` 不被当成标题
4. 表格与列表完整保留在所属正文
5. 预览不写库；确认后一次性入库并可检索
6. `data/faq/` 导入结果与改造前逐字一致
7. 全量测试通过

## 已知限制

- 不识别 Setext 标题（`===` / `---` 下划线式），会在警告中提示
- 切分是启发式的，复杂排版可能切得不理想，靠预览人工确认兜底
- 同文档重复叶子名会产生近似重复向量，预览会暴露该问题
- 管理接口仍无鉴权，批量会放大嵌入开销，故限制 markdown ≤100k 字符、
  单次 ≤200 条
