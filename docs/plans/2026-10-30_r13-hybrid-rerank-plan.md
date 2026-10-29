# ShopPilot 阶段十三实施计划：混合检索、精排与四策略评估

对应设计：`docs/superpowers/specs/2026-10-30_r13-hybrid-rerank-design.md`

## 步骤 1：评估数据准备

- [ ] 从 `eval_data/` 三个数据集分层抽样 500 条
- [ ] 按措辞特征预分类，补足各桶至 ~125 条
- [ ] 对每条 query 用向量检索初筛候选政策
- [ ] 人工（AI）逐条标注 `expected_doc_id`，标 `none` 的排除
- [ ] 输出 `docs/eval/r13-queries.csv`
- [ ] 验证：CSV 可解析、各桶数量平衡、无一等多的空白
- [ ] 提交

## 步骤 2：BM25 检索

- [ ] `uv add jieba rank-bm25`
- [ ] 新建 `app/bm25_index.py`：分词、建索引、检索
- [ ] 索引来源与向量一致（`store.all_faq_for_index()`）
- [ ] 验证：手工查询确认关键词命中
- [ ] 提交

## 步骤 3：RRF 融合

- [ ] `app/retrieval.py` 新增 `rrf_fuse(vector_hits, bm25_hits, k=60)`
- [ ] 融合后去重、按融合分排序
- [ ] 验证：单元测试覆盖「两路都命中」「只有一路命中」「都未命中」
- [ ] 提交

## 步骤 4：精排

- [ ] `app/rerank.py`：调硅基流动 `/v1/rerank`
- [ ] 输入 query + 候选文档，输出重排结果
- [ ] 失败时降级为原顺序（不抛异常）
- [ ] 验证：真实调用确认分数分布
- [ ] 提交

## 步骤 5：四策略统一接口

- [ ] `app/retrieval.py` 新增 `retrieve(query, strategy, top_k)`
- [ ] 支持 `vector` / `bm25` / `hybrid` / `hybrid_rerank`
- [ ] 四者输出结构统一（含 doc_id、score、来源策略）
- [ ] 验证：单元测试确认四种策略都可调用
- [ ] 提交

## 步骤 6：评估脚本

- [ ] `scripts/eval_retrieval.py`
- [ ] 读 CSV，对四种策略各跑一遍
- [ ] 计算 R@1/3/5/10、MRR@10，分桶统计
- [ ] 记录延迟 P50/P95
- [ ] 输出 JSON 原始数据 + Markdown 报告
- [ ] 验证：能完整跑完 500 条
- [ ] 提交

## 步骤 7：出报告

- [ ] `docs/eval/r13-report.md`：结论摘要、总体对比、分桶对比、延迟、失败案例、建议
- [ ] 根据数据决定精排阈值是否维持 0.58
- [ ] 提交

## 步骤 8：改造 search_knowledge

- [ ] 改用 `retrieve(strategy="hybrid_rerank", top_k=10)`
- [ ] 返回结构化文本：`【高相关资料】` + `【补充资料】` 两段
- [ ] 保留 `hits` 结构化字段供日志
- [ ] 阈值按步骤 7 的结论设定
- [ ] 验证：工具直接调用确认输出格式
- [ ] 提交

## 步骤 9：验收

- [ ] 浏览器实跑：政策问题命中并引用编号
- [ ] 全量测试通过
- [ ] 更新 README
- [ ] 提交

## 完成定义

设计文档验收六条全部满足，评估报告有数据支撑，线上链路已改为混合检索。
