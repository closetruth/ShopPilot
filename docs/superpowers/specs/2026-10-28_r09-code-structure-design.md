# ShopPilot 阶段八设计：代码结构整理

日期：2026-10-28
状态：已确认

## 意图

项目累计 4470 行后，`app/store.py` 涨到 799 行，混着四类不相干的东西：
数据库连接、会话消息、商品订单工单、FAQ。找代码要在长文件里翻，改一处
容易碰到无关部分。

同时发现一个更严重的问题：**脚本没有任何测试**。上一轮删除
`rebuild_from_store` 返回字段后，`scripts/reindex.py` 立即 `KeyError`
崩溃，而当时 81 个测试全绿。

## 目标

1. 按领域拆分存储层，每个文件职责单一
2. 脚本纳入测试覆盖，防止「代码改了脚本没跟上」这类回归
3. 调用方零改动——拆分不应波及上层

## 非目标

- 引入 service 层或依赖注入
- 拆分 `app/main.py`
- 拆分 `app/static/app.js`
- 引入 ORM

## 判断依据：为什么不做完整分层

典型分层（router / service / repository / model）的价值在**多人协作**与
**替换实现**。这个项目两者都不需要：

- 总共 4470 行，改一个需求只碰 1–2 个文件
- 存储从 SQLite 换到 MySQL 时只改了 `store.py`，说明边界本来就对
- 分层会让每个接口多一层 `return service.get_x(...)` 的转手代码

**分层不是免费的。** 每多一层抽象，读代码的人就多一次跳转。

## 拆分方案

| 文件 | 职责 | 行数 |
|---|---|---|
| `app/db.py` | 连接参数、连接上下文、建表、旧表补列、`now()` | 184 |
| `app/repo/chats.py` | 会话与消息 | 90 |
| `app/repo/commerce.py` | 商品、订单、工单、工单草稿 | 478 |
| `app/repo/knowledge.py` | FAQ 政策知识 | 68 |
| `app/store.py` | 转发门面 | 92 |

### 门面模式

`store.py` 不再含实现，只做转发：

```python
search_products = commerce.search_products
create_work_order_draft = commerce.create_draft
```

好处：
- 所有 `store.xxx()` 调用点一行不改
- 需要时可在门面层加日志、缓存、参数归一，不用改各 repo
- 将来若要按领域拆 API 模块，直接从 `app.repo` 引即可

代价：多一层间接。但这是纯赋值，不是函数包装，没有运行时开销。

### commerce.py 仍然较大（478 行）

不继续拆的理由：商品 → 订单 → 工单 → 草稿是一条完整业务线，工单要从
订单取 `product_id`，草稿要查订单商品名。拆开会切断这些内聚关系，
产生跨文件往返。

## 脚本测试

`tests/test_scripts.py`：

| 用例 | 防的是什么 |
|---|---|
| 四个脚本可导入 | 拼错的常量、缺 import |
| 四个脚本可编译 | 语法错误 |
| `reindex` 读的字段 ⊆ `rebuild_from_store` 返回的键 | **上一轮的真实回归** |
| `import_faq.build_documents()` 有输出 | 切分逻辑静默失效 |

第三条用 monkeypatch 注入假的 `rebuild_from_store` 返回值，断言脚本
不抛 `KeyError`。它锁死的是「函数返回值与脚本消费方之间的契约」。

## 命名陷阱

`db.py` 导出 `now()` 函数。原 `store.py` 里有大量 `now = _now()` 的
局部变量，搬进 `commerce.py` 后会遮蔽模块级函数，导致同一函数内既用
局部 `now` 又调用 `now()` 时报 `TypeError`。已全部改名为 `ts`。

## 验收

1. `store.py` 降到 100 行以内
2. 四个 repo 文件职责单一
3. 所有调用点零改动
4. 91 个测试全绿（原 81 + 脚本 10）
5. 服务重启后接口与对话链路正常

## 没做的：前端拆分

`app.js` 816 行，计划拆为 6 个 ES module，**评估后放弃**：

- 90 多个函数互相调用，共享 20 个 DOM 引用
- 无构建工具，"拆得开" 但 "依赖关系要手工维护"
- 上一轮 `ISSUE_TYPE_OPTIONS` 重复声明直接让整页 JS 静默失效；
  同样错误分散在 6 个文件里更难定位

前端拆分的收益是「文件变小」，代价是「引入模块依赖管理」。项目未到
需要那一步的规模。

若将来要做，前置条件是先加 ES module 冒烟测试（加载页面、断言关键
函数可用），否则拆完白屏无法及时发现。
