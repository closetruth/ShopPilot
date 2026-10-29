# ShopPilot 阶段十四设计：引用、拒答与低置信度问题池

## 意图

r13 的实测暴露了一个结构性漏洞。问「买一送一的商品能退一件吗」时模型的回答是：

> 知识库里没有查到买一送一活动商品单独退货的条款，建议以商品详情页的活动说明为准。
> **一般来说，赠品或赠送件需与主商品一并退回，才能按实付金额退款。**

前半句正确拒答，后半句是编的——47 条政策里没有任何一条提到赠品退货。
模型用「一般来说」把编造内容包装成了常识。

现有的防线只有 prompt 里一句「不要编造」，它是建议不是约束。这一轮把
可靠性做成结构：

1. 回答必须带引用编号，且编号能追溯回原文
2. 证据不足时结构性拒答，不依赖模型自觉
3. 拒答的问题落库，形成可分析的缺口清单

## 目标

1. 回答中的政策依据以 `[P-LOG-001-3]` 形式标注，前端悬停显示该条正文
2. 检索最高分低于阈值时，工具返回 `insufficient` 标记，模型必须拒答
3. 检索为空或低分的问题写入 `low_confidence_queries` 表
4. 有一份可直接拿来补知识的缺口清单

## 非目标

- 修改 r13 的混合检索实现（暂停中，另行推进）
- 前端跳转到知识库详情页（本轮只做悬停提示）
- 模型输出格式校验（比如强制 JSON 输出）
- 自动补知识（人工决定补什么）

## 反目标

- 不允许模型引用检索结果之外的编号
- 不允许在 `insufficient` 为真时仍给出具体条款
- 低置信度记录不能拖慢正常对话

## 关键决定

| 决定 | 选择 | 理由 |
|---|---|---|
| 引用呈现 | 悬停显示条例正文 | 不打断对话流；点击跳转会离开当前页 |
| 低置信度判定 | 仅用分数，不用措辞匹配 | 措辞匹配会误判「没查到」这类正常表述 |
| 本轮范围 | 只做这三条 | r13 混检未经评估验证，先不叠加 |

## 一、引用编号

### 工具返回值

`search_knowledge` 现有的 `results` 已含 `doc_id`、`title`、`section`、
`content`。增加一个显式的可用编号清单：

```json
{
  "results": [{"doc_id": "P-LOG-001-3", "title": "...", "section": "...", "content": "..."}],
  "citable_ids": ["P-LOG-001-3", "P-LOG-001-2"],
  "insufficient": false,
  "max_score": 0.73
}
```

`citable_ids` 是给模型的明确边界：**只能从这里面选**。

### prompt 约束

```
回答政策问题时，每个结论后面必须标注来源编号，格式 [P-XXX-000-0]。
只能引用工具返回的 citable_ids 里的编号，不得自行编造或推测。
示例：
  根据[P-RET-001-3]，七天无理由退货的退回运费由消费者承担。
无法在检索结果中找到依据的内容，不要写入回答。
```

### 前端渲染

回答气泡里的 `[P-XXX-000-0]` 用正则识别，渲染成带虚线下划线的 `<span>`，
`data-id` 存编号，`title` 属性放该条正文（悬停即显示原生 tooltip）。

**正文从哪来**：对话流式返回时不带政策正文。两种做法：

| 做法 | 说明 |
|---|---|
| A. 前端按编号查 | 渲染时对每个新编号发一次 `GET /api/faq/{id}`，缓存结果 |
| B. SSE 带正文 | 工具调用完成时，把命中的政策正文一起推给前端 |

**选 A**：实现简单，且编号数量有限（top10 内），加个内存缓存即可。
B 会让 SSE 协议变复杂，而正文在工具返回时已经有了。

## 二、结构性拒答

### 判定在工具内完成

```python
INSUFFICIENT_THRESHOLD = 0.65   # 证据充分性下限

max_score = max(h["score"] for h in relevant)
insufficient = max_score < INSUFFICIENT_THRESHOLD
```

阈值从现有 0.58 **上调到 0.65**：0.58 只保证「有点相关」，0.65 才接近
「足以支撑回答」。这个数字**未经数据验证**，属我的判断，实践后需复核。

### 返回值形态

证据不足时不再返回半截结果让模型自由发挥：

```json
{
  "results": [],
  "insufficient": true,
  "max_score": 0.52,
  "note": "检索到的内容相关度不足（最高 0.52，门槛 0.65），无法据此回答。"
          "必须告知顾客没有查到相关规定，不得给出具体条款，"
          "不得使用「一般来说」「通常」等措辞补充。"
}
```

**关键：`results` 清空**。只给标记不给内容，模型没有素材可编。

### prompt 配套

```
当 search_knowledge 返回 insufficient 为 true 时：
- 必须明确告知顾客「这个问题我这边没有查到相关规定」
- 可以建议以商品详情页为准，或转人工
- 禁止使用「一般来说」「通常情况下」「据我所知」等措辞补充任何内容
- 禁止引用任何编号
```

## 三、低置信度问题池

### 数据模型

```sql
CREATE TABLE low_confidence_queries (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    visitor_id  VARCHAR(64)  NOT NULL,
    query       TEXT         NOT NULL,
    max_score   FLOAT        NULL,      -- 检索为空时为 NULL
    result_count INT         NOT NULL,  -- 检索到几条
    reason      VARCHAR(32)  NOT NULL,  -- empty / low_score
    retrieved   VARCHAR(512) NULL,      -- 命中的 doc_id，逗号分隔
    created_at  DATETIME(6)  NOT NULL,
    KEY idx_low_conf_created (created_at),
    KEY idx_low_conf_reason (reason)
)
```

`reason` 两种取值：
- `empty`：检索完全没有结果
- `low_score`：有结果但最高分低于阈值

### 写入时机

在 `search_knowledge` 工具内部写，**不阻塞返回**：

```python
try:
    store.record_low_confidence(...)
except Exception:
    logger.warning("低置信度记录失败，不影响对话", exc_info=True)
```

记录失败不能影响顾客拿到回答。

### 查看

`GET /api/admin/low-confidence?limit=100` 返回最近记录，按时间倒序。
暂不做前端页面——先看数据决定要不要做。

## 前端

### 引用悬停

回答渲染时正则匹配 `\[(P-[A-Z]+-\d{3}-\d+)\]`，替换成：

```html
<span class="cite" data-cite-id="P-LOG-001-3">P-LOG-001-3</span>
```

CSS 加虚线下划线，鼠标悬停时由 JS 查一次正文并设置 `title`。
加 `Map` 缓存，同一编号只查一次。

### 无其他界面改动

拒答是模型行为，前端不需要特殊处理。

## 验收

1. 问「退货运费谁出」，回答含 `[P-RET-001-3]`，悬停显示该条正文
2. 问「买一送一能退一件吗」，回答只有拒答，**没有「一般来说」式补编**
3. 上述问题在 `low_confidence_queries` 表里有一条记录，`reason=low_score`
4. 问「花呗分期」，同样拒答并落库 `reason=empty`
5. 正常问题（如「快递丢件怎么办」）回答不受影响
6. 全量测试通过

## 已知限制

- **阈值 0.65 未经验证**：是在 0.58 基础上拍的上调，需要用 r13 的评估数据校准
- **引用编号靠 prompt 约束**：没有代码层校验模型是否真的只用了 `citable_ids`
- **悬停查正文多一次请求**：编号多时会有多次请求，靠缓存缓解
- **低置信度池只看分数**：不分析模型是否真的拒答了，可能记录一堆其实回答得很好的问题
