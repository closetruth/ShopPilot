# r21 实施计划：精排模型微调

设计依据：[2026-11-04_r21-reranker-finetune-design.md](../specs/2026-11-04_r21-reranker-finetune-design.md)

执行纪律：每个 Task 先写失败测试，再写实现，再跑全量；每个 Task 一个提交，
提交信息说「为什么」。测试一律不调真模型、不调远端训练服务。

数据事实（开工前实测）：低置信度 265 条（全 `low_score`）、轨迹 327 条、
反馈 4 条、`eval_cases` 24 条（仅 1 条 accepted）、FAQ 11 篇 / 47 块、
r13 标注集 473 条。远端 192.168.6.23 上 `19530`（Milvus）与 `22`（SSH）
开放，`3307` 与 `3005` 未开放。

---

- [ ] **Task 0：远端能力探测（先做，结论决定后面怎么走）**
  - `ssh 192.168.6.23` 上执行 `nvidia-smi`、`docker info`、`docker images`
  - 探测 `8010` 端口是否可开；若被防火墙挡住，退路是 SSH 隧道
    （`ssh -L 8010:127.0.0.1:8010`），本机仍然只当 HTTP 客户端
  - 记录：GPU 型号/显存、CUDA 版本、可用内存、磁盘剩余、HF 镜像站可达性
  - 产出：把结论写进 `docs/dev-notes/r21.md` 开头
  - 提交：`docs: 记录远端算力现状，决定训练落在哪台机器上`

- [ ] **Task 1：四张新表 + 仓储**
  - `app/core/db.py` 的 `SCHEMA` 追加 `finetune_labels` /
    `finetune_datasets` / `finetune_jobs` / `finetune_models`，字段照设计 §5
  - 新增 `app/repo/finetune.py`：
    `upsert_label` / `list_labels` / `label_stats` / `get_label`
    `create_dataset` / `get_dataset` / `list_datasets`
    `create_job` / `update_job` / `get_job` / `list_jobs` / `running_job`
    `add_model` / `list_models` / `activate_model` / `active_model`
  - `activate_model` 在同一事务里把旧 active 置 0
  - `app/core/store.py` 加转发
  - 测试：`tests/test_finetune_repo.py` —— 建表可查、label 唯一键覆盖、
    激活唯一性、任务状态流转
  - 提交：`feat: 微调四张表落地，标注与训练版本可追溯`

- [ ] **Task 2：数据导出与清洗（纯函数）**
  - 新增 `scripts/finetune_dataset.py`，拆两层：
    纯函数 `normalize` / `dedupe` / `detect_noise` / `mask_pii` / `clean()`
    取数函数 `fetch_low_conf()` / `fetch_r13()` / `fetch_traces()`
  - `clean()` 返回 `(rows, stats)`，`stats` 含设计 §6.1 的七个计数键
  - 噪声判定复用 `app.chat.intent` 的 `classify_by_rules`：规则判
    `knowledge` 的保留，判 `chitchat`/`complaint` 的标 `noise`，
    判 `business` 的标 `noise`（业务数据问题不属政策检索范畴）
  - 测试：`tests/test_finetune_clean.py` —— 精确/近似去重、超长截断、
    PII 掩码、噪声分类、跨源合并，全部断言 `stats` 计数
  - 提交：`feat: 低置信度数据清洗，噪声与 PII 先过一遍`

- [ ] **Task 3：诊断闸 + LLM 预标注**
  - `scripts/finetune_dataset.py` 加 `diagnose(rows, llm, top_k=5)`：
    向量召回 + BM25 合并去重 → LLM 给 `{label, doc_id, confidence}`
  - LLM 失败按 `missing` 兜底并记 `annotator=llm_failed`
  - `gate_check(labels)`：`doc` 数 < 80 → 返回 `blocked` 并生成
    `docs/eval/r21-gap-report.md`（按 category 汇总缺失条数与典型问法）
  - 测试：mock LLM 返回三种 label；`doc` 数 79 → blocked、80 → pass
  - 提交：`feat: 诊断闸先判缺知识还是缺措辞，避免在错的数据上训练`

- [ ] **Task 4：标注接口与标注页**
  - 新增 `app/api/finetune.py`：
    `GET /api/admin/finetune/labels?status=pending`（带候选文档）
    `POST /api/admin/finetune/labels`（`query, label, doc_id`）
    `GET /api/admin/finetune/labels/stats`
    `GET /admin/finetune`（页面）
  - 候选文档由服务端算（向量 + BM25），不在前端拼
  - 新增 `app/static/admin_finetune.html` / `.js` 的标注标签页：
    进度条、候选卡、AI 建议徽标、键盘 `1-5` / `0` / `x` / `Enter`
  - 测试：`tests/test_finetune_api.py` —— 待标列表、提交覆盖、统计、
    `doc_id` 不存在时 400
  - 提交：`feat: 标注页可逐条确认，LLM 建议只做参考`

- [ ] **Task 5：分层抽样、增强与数据集导出**
  - `split(rows, seed)`：按 `category × label` 分层，70/15/15，层内 <3 全进
    train；结果落 `data/finetune/split.json`
  - `augment(train_rows)`：轨迹改写配对 + 规则同义词 + hard negative；
    LLM 改写走 `--augment-llm` 开关，默认关（先跑免费的那部分）
  - `export_jsonl(rows, path)`：`{"query","doc","label","origin_id"}`
  - `POST /api/admin/finetune/datasets/build`：跑完落
    `finetune_datasets` 一行，返回 `stats`
  - 测试：`tests/test_finetune_split.py` —— 分层比例、小层全进 train、
    种子可复现、增强不污染 dev/test、hard negative 不与正例同 doc
  - 提交：`feat: 分层抽样与增强，dev/test 保持人工标注纯净`

- [ ] **Task 6：训练容器**
  - `deploy/trainer/Dockerfile`：`python:3.11-slim` + torch + transformers +
    onnxruntime + fastapi/uvicorn
  - `deploy/trainer-compose.yml`：端口 `8010`、三个命名卷、CPU/内存上限、
    `HF_ENDPOINT=https://hf-mirror.com`、GPU 声明（按 Task 0 结论启用或注释）
  - `deploy/trainer.env.example`：只放占位符
  - `deploy/trainer/train.py`：全参微调 + early stopping（dev MRR）+
    `torch.onnx.export`（`input_ids` / `attention_mask`）+ 固化
    `tokenizer.json` / `config.json` 到版本目录
  - 提交：`feat: 训练容器就位，远端可独立完成微调与导出`

- [ ] **Task 7：训练服务与任务编排**
  - `deploy/trainer/server.py`：`/health`（探 CUDA）、`/train`、
    `/train/{id}`（含 `log_tail`）、`/models`、`/models/{v}/activate`
  - 训练在后台线程跑，日志写 `trainer-data/logs/{job_id}.log`
  - 本机 `scripts/finetune_train.py`：提交任务 → 轮询 → 落
    `finetune_jobs` / `finetune_models`
  - `POST /api/admin/finetune/jobs`、`GET /api/admin/finetune/jobs/{id}`
  - 测试：`tests/test_finetune_jobs.py` —— mock HTTP，提交成功/失败/轮询
    落库；远端不可达时返回 503 而不是 500
  - 提交：`feat: 训练任务可提交可轮询，远端故障不冒充成功`

- [ ] **Task 8：精排推理服务与客户端改造**
  - `deploy/trainer/rerank_service.py`：`onnxruntime` 加载活跃版本，
    `POST /v1/rerank` 协议与硅基流动一致，`model` 不存在返回 404
  - `app/retrieval/rerank.py`：`RERANK_BASE_URL` 可配（默认硅基流动），
    `RERANK_MODEL` 透传；**不加分支**
  - 测试：`tests/test_rerank_client.py` —— 自定义 base_url 生效、
    服务失败降级为原顺序、空 docs 直接返回
  - 提交：`feat: 精排客户端可指向自建服务，失败仍降级不阻断`

- [ ] **Task 9：knowledge_node 接线与阈值标定**
  - `run_knowledge_retrieval` 加 `RERANK_ENABLED` 分支：召回 10 → 精排 →
    取 3 → 用精排分过 `RERANK_ANSWER_THRESHOLD` 闸；拒答路径不变（仍落
    低置信度）
  - `scripts/finetune_eval.py`：dev 集扫阈值 0.1–0.9 步长 0.02，输出
    Youden J 最大点与标定曲线
  - `.env.example` 增 `RERANK_ENABLED` / `RERANK_BASE_URL` /
    `RERANK_MODEL` / `RERANK_ANSWER_THRESHOLD`
  - 测试：`tests/test_knowledge_rerank.py` —— 开关关时行为与 r20 完全一致、
    开时低分拒答、高分作答、精排失败降级
  - 提交：`feat: 知识路径接入精排，开关默认关闭可随时回滚`

- [ ] **Task 10：离线评估与发布门禁**
  - `scripts/finetune_eval.py` 加三把尺：test 集 Hit@1 / MRR@3 / AUC、
    低置信度回放修复率、调 r20 `run_eval` 取 `doc_hit_rate`
  - 门槛常量 `HIT1_MIN=0.75` / `MRR3_MIN=0.80` / `REPLAY_MIN=0.25` /
    `DOC_HIT_TOLERANCE=0.02`
  - 未达标时 `POST /models/{v}/activate` 返回 409 并附未过项
  - 测试：`tests/test_finetune_gate.py` —— 达标放行、未达标拒绝、
    指标缺失按未达标处理
  - 提交：`feat: 三把尺发布门禁，不达标不许上线`

- [ ] **Task 11：前端训练与发布页 + 全量回归 + 浏览器验收**
  - `admin_finetune.html` 补数据集页与训练发布页：清洗漏斗表、分层分布、
    split 条数、发起训练、日志尾部轮询、指标表、版本列表、激活/回滚
  - `docs/dev-notes/r21.md` 记录实测结论
  - 全量 `uv run pytest -q`；浏览器实跑：
    标注页标 5 条 → 建数据集 → 发训练 → 看指标 → 激活 → 对话页实测
    `RERANK_ENABLED=0/1` 两种行为
  - 提交：`feat: 微调看板与发布页，飞轮闭环到线上可验证`