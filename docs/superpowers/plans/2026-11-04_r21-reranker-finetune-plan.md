# r21 实施计划：精排模型微调

设计依据：[2026-11-04_r21-reranker-finetune-design.md](../specs/2026-11-04_r21-reranker-finetune-design.md)

执行纪律：每个 Task 先写失败测试，再写实现，再跑全量；每个 Task 一个提交，
提交信息说「为什么」。测试一律不调真模型、不调远端训练服务。

数据事实（开工前实测）：低置信度 265 条（全 `low_score`）、轨迹 327 条、
反馈 4 条、`eval_cases` 24 条（仅 1 条 accepted）、FAQ 11 篇 / 47 块、
r13 标注集 473 条。远端 192.168.6.23 上 `19530`（Milvus）与 `22`（SSH）
开放，`3307` 与 `3005` 未开放。

---

- [x] **Task 0：远端能力探测（先做，结论决定后面怎么走）**
  - `ssh 192.168.6.23` 上执行 `nvidia-smi`、`docker info`、`docker images`
  - 探测 `8010` 端口是否可开；若被防火墙挡住，退路是 SSH 隧道
    （`ssh -L 8010:127.0.0.1:8010`），本机仍然只当 HTTP 客户端
  - 记录：GPU 型号/显存、CUDA 版本、可用内存、磁盘剩余、HF 镜像站可达性
  - 产出：把结论写进 `docs/dev-notes/r21.md` 开头
  - 提交：`docs: 记录远端算力现状，决定训练落在哪台机器上`
  - 实测补丁：SSH 只支持密码、无免密通道，**拿不到 GPU 型号**。改为
    「容器运行时自适应」——`server.py` 探 CUDA，compose 默认 CPU 配置 +
    `trainer-gpu.yml` 覆盖文件；端口做成 `TRAINER_PORT` 可配并保留 SSH
    隧道退路。`3307` 未开放是因为远端 MySQL 走 `3306`，不是故障

- [x] **Task 1：四张新表 + 仓储**
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
  - 实测补丁：`normalize_query` 必须连空白一起去掉，否则「能 退 吗」与
    「能退吗」算出两个 hash，同一句话会被标两次

- [x] **Task 2：数据导出与清洗（纯函数）**
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

- [x] **Task 3：诊断闸 + 候选生成（改为纯人工标注）**
  - 裁定变更：用户要求**手动标注**，砍掉 LLM 预标注，`diagnose()` 不做
    LLM 判定
  - `scripts/finetune_dataset.py` 加 `build_candidates(rows, top_k=5)`：
    用 `hybrid`（向量 + BM25 经 RRF）算候选并落 `data/finetune/candidates.jsonl`
  - 候选离线算一次落盘，标注页只读缓存——翻页不触发嵌入调用，否则 265 条
    × 每次翻页都是外部 API 账单
  - 诊断结论改由标注结果自然产出：`label_stats` 里 `doc` 数 < 80 即闸门
    blocked（Task 10 的门禁里判）
  - 提交：`feat: 标注页可逐条确认，候选离线算好不按翻页计费`

- [x] **Task 4：标注接口与标注页**
  - `app/api/finetune.py`：
    `GET /admin/finetune`（页面）
    `GET /api/admin/finetune/labels/pending?limit=&offset=`（待标 + 候选）
    `POST /api/admin/finetune/labels`（提交）
    `GET /api/admin/finetune/labels/stats`（进度）
    `GET /api/admin/finetune/labels`（已标清单）
  - pending 由「候选缓存 − 已标 hash」算，不额外存 status 列
  - 候选缓存缺失返回 409 并附可执行指令，不用空列表装正常
  - `doc` 标注校验 `faq_id` 真实存在，避免把错 id 练进模型
  - 前端 `admin_finetune.html` / `.js`：进度条、候选卡、键盘
    `1-5` / `0` / `x` / `↑` `↓`，标完立刻 POST，失败留在原地不静默丢
  - 测试：`tests/test_finetune_api.py` 8 项，含 409 与 doc 校验
  - 提交：`feat: 标注页可逐条确认，候选离线算好不按翻页计费`

- [x] **Task 5：分层抽样、增强与数据集导出**
  - `split(rows, seed)`：按 `category × label` 分层，70/15/15，层内 <3 全进
    train；切分单位是 **query 不是 pair**，否则同一句的 hard negative 会
    漏到 dev，指标虚高
  - `augment(train_rows, traces)` 四路来源：`human` 正例、`trace_rewrite`
    （读 `agent_traces.message → resolved_query`）、`synonym`（规则表，
    只取第一个命中词防组合爆炸）、`hard_negative`（候选里分数最高但非金标，
    取前 2 条）
  - `noise` 只产负例（配 top-1 候选，label 0）；`missing` **不产任何对**——
    知识缺失不是相关性信号，混进训练会把「没见过」学成「不相关」
  - `dev/test` 只保留人工正例，不掺任何增强
  - `_doc_text` = 标题 + 正文：标题里的类别词是正文常缺的区分线索
  - `build_dataset()` 落 `train/dev/test.jsonl` + `split.json` +
    `dataset_stats.json`，`gate` 由正样本数 ≥ 80 判定
  - 端点 `POST /api/admin/finetune/datasets/build`（`asyncio.to_thread`，
    同步 DB 读不能卡事件循环）、`GET /api/admin/finetune/datasets`
  - 页面加「生成 train/dev/test」按钮 + 门禁徽标 + 分集/来源/分层三张表
  - 测试：`tests/test_finetune_split.py` 12 项；`test_finetune_api.py`
    加构建端点 2 项（转发远端 + 不可达 503）。构建本身已移到容器内，
    本地测试只验编排，不再打补丁 `OUT_DIR` 去跑真构建
  - 提交：`feat: 分层切分与增强，dev/test 不掺增强避免指标虚高`

- [x] **Task 6：训练容器**
  - `deploy/trainer/Dockerfile`：`python:3.11-slim` + torch + transformers +
    onnxruntime + fastapi/uvicorn
  - `deploy/trainer-compose.yml`：端口 `8010`、三个命名卷、CPU/内存上限、
    `HF_ENDPOINT=https://hf-mirror.com`、GPU 声明（按 Task 0 结论启用或注释）
  - `deploy/trainer.env.example`：只放占位符
  - `deploy/trainer/train.py`：全参微调 + early stopping（dev MRR）+
    `torch.onnx.export`（`input_ids` / `attention_mask`）+ 固化
    `tokenizer.json` / `config.json` 到版本目录
  - **实测补丁：构建上下文改为仓库根**。原方案是 `context: ./trainer`，数据集
    由本地构建后 `docker cp` 进去。改成容器内直连远端 MySQL 现取标注构建，
    因为标注、FAQ、轨迹本来就在 192.168.6.23 的库里；本地构建只能先拉再传，
    多一次往返，还多一个「忘了同步」导致训练跑在旧标签上的失败模式。
    代价是镜像需要 `app/` 取数层与 `scripts/finetune_dataset.py`，配
    `.dockerignore` 把数据文件与虚拟环境排掉。
  - **实测补丁：`candidates.jsonl` 仍走本地**。它是唯一真正需要本地算力的
    产物（embedding + Milvus 召回），且是静态快照，用命名卷
    `trainer-candidates` 一次性放进去即可，缺失时只少 hard negative 不报错。
  - 提交：`feat: 训练容器就位，远端可独立完成微调与导出`

- [x] **Task 7：训练服务与任务编排**
  - `deploy/trainer/server.py`：`/health`（探 CUDA）、`/train`、
    `/train/{id}`（含 `log_tail`）、`/models`、`/models/{v}/activate`，
    另加 `/datasets/build`、`/datasets`
  - 训练在后台线程跑，日志写 `/app/logs/{job_id}.log`
  - 本机客户端 `app/finetune/trainer.py`（而非原计划的
    `scripts/finetune_train.py`）：提交 → 轮询 → 落 `finetune_jobs` /
    `finetune_models`。做成库而不是脚本，是为了让 HTTP 接口直接复用，
    不必再包一层子进程
  - `POST /api/admin/finetune/jobs`、`GET /api/admin/finetune/jobs/{id}`、
    `GET /api/admin/finetune/models`、`POST /api/admin/finetune/models/{v}/activate`、
    `GET /api/admin/finetune/trainer`
  - **实测补丁：构建端点改为转发远端**。本地不再跑第二遍
    `build_dataset`，只负责编排与登记；两份实现迟早会分叉
  - 测试：`tests/test_finetune_jobs.py` 12 项 —— 提交落库、无数据集拒绝、
    远端不可达 503、轮询状态映射、成功后登记模型、轮询幂等不重复插行、
    失败记错误、远端掉线保留本地状态、切活跃唯一性
  - 提交：`feat: 训练任务可提交可轮询，远端故障不冒充成功`

- [x] **Task 8：精排推理服务与客户端改造**
  - `deploy/trainer/rerank_service.py`：`onnxruntime` 加载活跃版本，
    `POST /v1/rerank` 协议与硅基流动一致，`model` 不存在返回 404
  - `app/retrieval/rerank.py`：`RERANK_BASE_URL` 可配（默认硅基流动），
    `RERANK_MODEL` 透传；**不加分支**
  - **实测补丁：无 key 不再一律降级**。原逻辑是「没有
    `SILICONFLOW_API_KEY` 就直接返回原顺序」，指向自建服务时会被这条挡住，
    部署完还得配一个用不上的密钥。改成只在走硅基流动时才因缺 key 降级。
  - **实测补丁：`RERANK_BASE_URL` 自动补 `/v1`**，与 `EMBED_BASE_URL` 同一
    写法；填 `http://192.168.6.23:8010` 不用记版本段
  - 测试：`tests/test_rerank_client.py` 9 项 —— 默认地址、补 `/v1`、不重复
    补、自建服务免密钥、托管缺 key 不发请求、服务失败降级为原顺序、
    空 docs 与全未知 id 不发请求、`RERANK_MODEL` 透传
  - 提交：`feat: 精排客户端可指向自建服务，失败仍降级不阻断`

- [x] **Task 9：knowledge_node 接线与阈值标定**
  - `run_knowledge_retrieval` 加 `RERANK_ENABLED` 分支：召回 10 → 精排 →
    取 3 → 用精排分过 `RERANK_MIN_SCORE` 闸；拒答路径不变（仍落
    低置信度）
  - `scripts/finetune_eval.py`：dev 集扫阈值 0.1–0.9 步长 0.02，输出
    Youden J 最大点与标定曲线
  - `.env.example` 增 `RERANK_ENABLED` / `RERANK_BASE_URL` /
    `RERANK_MODEL` / `RERANK_MIN_SCORE`
  - 测试：`tests/test_knowledge_rerank.py` —— 开关关时行为与 r20 完全一致、
    开时低分拒答、高分作答、精排失败降级
  - 提交：`feat: 知识路径接入精排，开关默认关闭可随时回滚`

- [x] **Task 10：离线评估与发布门禁**
  - `scripts/finetune_eval.py` 加三把尺：test 集 Hit@1 / MRR@3 / AUC、
    低置信度回放修复率、调 r20 `run_eval` 取 `doc_hit_rate`
  - 门槛常量 `HIT1_MIN=0.75` / `MRR3_MIN=0.80` / `REPLAY_MIN=0.25` /
    `DOC_HIT_TOLERANCE=0.02`
  - 未达标时 `POST /models/{v}/activate` 返回 409 并附未过项
  - 测试：`tests/test_finetune_gate.py` —— 达标放行、未达标拒绝、
    指标缺失按未达标处理
  - 提交：`feat: 三把尺发布门禁，不达标不许上线`

- [ ] **Task 11：前端训练与发布页 + 全量回归 + 浏览器验收**
  - 状态：前端与接口已接通，微调相关 108 条用例通过（clean 10 / split 23 /
    gate 17 / eval 4 / repo 9 / api 15 / jobs 12 / knowledge_rerank 7 /
    rerank_client 11）；**浏览器实跑与远端激活尚未执行**，故不勾选
  - `admin_finetune.html` 补数据集页与训练发布页：清洗漏斗表、分层分布、
    split 条数、发起训练、日志尾部轮询、指标表、版本列表、激活/回滚
  - `docs/dev-notes/r21.md` 记录实测结论
  - 全量 `uv run pytest -q`；浏览器实跑：
    标注页标 5 条 → 建数据集 → 发训练 → 看指标 → 激活 → 对话页实测
    `RERANK_ENABLED=0/1` 两种行为
  - 提交：`feat: 微调看板与发布页，飞轮闭环到线上可验证`