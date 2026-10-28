# ShopPilot 阶段六实施计划：向量检索与 RAG

日期：2026-10-28
类型：实施计划
状态：已完成

对应设计：`docs/superpowers/specs/2026-10-28-vector-rag-design.md`

## 步骤 1：环境探查与选型验证

- [x] 确认硬件：15.4GB RAM，无 NVIDIA GPU（AMD 集显）
- [x] 连通性：PyPI 可达，Docker Hub 不可达，HuggingFace 不可达，ModelScope 可达
- [x] Milvus 镜像拉取失败 → 改用 Milvus Lite
- [x] 验证硅基流动 BGE-M3 API 可用，返回 1024 维
- [x] 验证 Milvus Lite 字符串主键、标量过滤、向量检索均正常
- [x] 装依赖：`pymilvus`、`milvus-lite`
- [x] 提交

## 步骤 2：嵌入模块

- [x] `app/embedding.py`：`embed_texts()` 批量嵌入，分批 32 条
- [x] 校验返回数量与向量维度
- [x] 缺 key 时抛 `EmbeddingError`
- [x] 验证：真实调用返回 1024 维
- [x] 提交

## 步骤 3：向量库层

- [x] `app/vector_store.py`：Milvus Lite 客户端，延迟初始化
- [x] 集合 schema：id/source/source_id/visitor_id/text/vector
- [x] FLAT 索引，metric=IP
- [x] `product_document()` / `work_order_document()` 文本模板
- [x] `upsert_documents()` 捕获所有异常，失败返回 0 不抛出
- [x] `search()` 内置访问隔离过滤
- [x] `rebuild_from_store()` 供脚本与接口共用
- [x] 排查并修正：`MILVUS_URI` 变量名与 pymilvus 冲突，改名 `VECTOR_DB_PATH`
- [x] 验证：重建 143 条全部成功
- [x] 提交

## 步骤 4：双写接入

- [x] `store.py` 新增 `get_work_order_row()` 直接查询（原 `get_work_order` 靠遍历列表）
- [x] `store.py` 新增 `all_products_for_index()` / `all_work_orders_for_index()`
- [x] `main.py` 确认工单接口成功后调 `_index_work_order()`
- [x] 验证：建单后向量可检索
- [x] 提交

## 步骤 5：检索工具与语义回退

- [x] `search_knowledge` 工具，参数 `query` + `top_k`
- [x] 阈值过滤 `RELEVANCE_THRESHOLD = 0.5`
- [x] `lookup_product` 字面查不到时回退语义检索
- [x] 回退结果标记 `match: exact|semantic`
- [x] 提交

## 步骤 6：检索质量迭代

- [x] 实测检索：「杯子裂了」→ 运输破损工单 0.767
- [x] 实测检索：「有没有蓝色的保温杯」→ 蓝色保温杯 0.677
- [x] 发现「杯子」字面查不到商品（库里是「马克杯」「保温杯」）
- [x] 尝试大绿豆退化方案，均导致「手机」误命中「手工青花」，全部撤回
- [x] 最终方案：不做字面模糊，由语义检索兜底
- [x] 验证：「杯子」→ semantic，「手机」→ 空
- [x] 提交

## 步骤 7：重建能力与文件锁

- [x] `scripts/reindex.py` 从 MySQL 全量重建
- [x] 发现 Milvus Lite 文件锁：服务运行时脚本无法打开同一文件
- [x] 新增 `POST /api/admin/reindex`，在服务进程内重建
- [x] 验证：接口重建 173 条全部成功
- [x] 提交

## 步骤 8：测试与验收

- [x] `tests/test_vector_store.py`：嵌入维度、顺序、隔离、删除、降级
- [x] 无 key 时相关测试自动 skip
- [x] 修正顺序测试：API 批量与单条有浮点差异，改比对余弦相似度
- [x] 全量 `uv run pytest` 通过（74 项）
- [x] 浏览器 / curl 实测：商品推荐、工单查询均正常
- [x] 提交

## 完成定义

设计文档「验收标准」八条全部满足，测试全绿。

## 执行中偏离计划的地方

- 原计划用 Milvus Docker，Docker Hub 不可达，改 Milvus Lite。随之引入文件锁
  问题，补了服务内重建接口。
- 原计划 BGE-M3 本地部署，本机无 GPU 且 HuggingFace 不可达，改用硅基流动 API。
- 商品字面检索的模糊退化尝试三次（单字 OR、bigram、去尾字）均引入误命中，
  最终放弃字面模糊，交给语义检索兜底。
