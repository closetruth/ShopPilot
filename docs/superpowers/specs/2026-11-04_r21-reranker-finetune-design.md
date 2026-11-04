# r21 精排模型微调设计

## 1. 一句话

把 r20 飞轮攒下的低置信度问题，变成一次可复算的精排模型微调：标注诊断先
判定「缺知识还是缺措辞」，确认是措辞才训练，训练产物用 ONNX 服务化，
线上靠一个开关切换，不达标就不切。

```
低置信度 265 条
   │ 清洗（去重/噪声/PII/跨源合并）
   ▼
诊断闸：可回答 / 知识库缺失 / 噪声 —— 可回答不足 80 条就停，改出缺口报告
   │
   ▼ 分层抽样 + 增强（轨迹改写 / LLM 改写 / 规则同义 / hard negative）
标注（人工逐条确认，LLM 预标注给建议）
   │
   ▼
远端容器全参微调 chinese-roberta-wwm-ext → 导出 ONNX
   │
   ▼
三把尺评估（离线 / 低置信度回放 / r20 端到端回归）
   │  达标才继续
   ▼
线上 RERANK_ENABLED 开关切换，随时回滚
```

## 2. 现状与断点

| 已有 | 位置 | 本轮怎么用 |
|---|---|---|
| 低置信度池 265 条（全 `low_score`） | `low_confidence_queries` | 主标注对象 |
| 轨迹 327 条（含 `rewritten_query`） | `agent_traces` | 免费改写增强 + 归因 |
| 用户反馈 4 条（2 踩） | `message_feedback` | 只进重点观察清单，不进训练 |
| r13 标注集 473 条 | `docs/eval/r13-queries.csv` | 训练正样本主力，不重标 |
| FAQ 11 篇 / 47 块 | `data/faq/`、`faq` 表 | 文档正文与 `doc_id` 金标 |
| 精排客户端 | `app/retrieval/rerank.py` | 已走硅基流动 `bge-reranker-v2-m3` |
| r20 评估流水线 | `app/evals/runner.py` | 端到端回归尺 |

三个断点，每个都决定这轮怎么做：

1. **线上根本没走精排**。`run_knowledge_retrieval` 是
   `vector_store.search → 直接过 0.65 闸`，`hybrid_rerank` 只在
   `scripts/eval_retrieval.py` 里跑过，没进产品。所以「精排微调」不是锦上
   添花，是**补一条断掉的路径**；也因此必须带开关，不能默认开。
2. **265 条不全是措辞问题**。全部 `reason=low_score` 说明「检索到了但分不
   够」，可其中相当比例是知识库压根没有对应条款（问价格、问几点开会）。
   这类样本进训练集只会教模型「硬凑一个文档」。必须先诊断。
3. **精排分与余弦不可比**。现有 `RELEVANCE_THRESHOLD=0.58` /
   `INSUFFICIENT_THRESHOLD=0.65` 是 BGE-M3 余弦尺度上的标定值，交叉编码器
   输出 sigmoid 分，量纲完全不同。直接复用这两个常量，等于换了个尺子还用
   旧刻度。

## 3. 目标与非目标

**目标**

- 一条可复跑的数据流水线：MySQL 导出 → 清洗 → 诊断 → 分层 → 增强 → jsonl
- 一个标注页，265 条可在 20 分钟内标完，带 LLM 建议与键盘操作
- 远端容器完成全参微调并导出 ONNX，训练日志可轮询
- 三把尺评估，未达标自动拒绝发布
- 线上开关 `RERANK_ENABLED`，默认关；开了走「召回 10 → 精排 → 取 3 →
  精排分过闸」
- 阈值在 dev 集上标定后写 `.env`，不硬编码

**非目标**

- 不微调生成模型（LLM 仍是 DeepSeek）
- 不替换 BGE-M3 嵌入（只在其上加一层精排）
- 不做量化、蒸馏、AB 分流、自动调度
- 不改 FAQ 正文（缺知识只出报告，不动内容）
- 不把训练依赖（torch/transformers/peft）写进 `pyproject.toml`
- 不在 `eval_cases` 里塞标注数据（避免污染 r20 基线）

## 4. 结构与接线

```
本机（无 NVIDIA GPU，HuggingFace 不可达）        远端 192.168.6.23
  app/static/admin_finetune.html/.js
  scripts/finetune_dataset.py  ──── HTTP ────▶  deploy/trainer 容器 :8010
  scripts/finetune_train.py    ──── HTTP ────▶  ├─ POST /train
  app/retrieval/rerank.py      ──── HTTP ────▶  ├─ POST /v1/rerank
                                                ├─ GET  /models
                                                └─ GET  /health
```

新增文件：

```
app/
  repo/finetune.py          # 标注、数据集、任务、模型版本
  api/finetune.py           # 标注/数据集/训练/发布接口
  static/admin_finetune.html / admin_finetune.js
scripts/
  finetune_dataset.py       # 导出 → 清洗 → 诊断 → 分层 → 增强 → jsonl
  finetune_train.py         # 提交训练任务、轮询、拉指标
  finetune_eval.py          # 离线评估：Hit@1 / MRR@3 / AUC
deploy/
  trainer-compose.yml
  trainer.env.example
  trainer/Dockerfile
  trainer/server.py         # FastAPI：/train /v1/rerank /models /health
  trainer/train.py          # 全参微调 + ONNX 导出
  trainer/rerank_service.py # onnxruntime 推理
```

接线只改两处：

- `app/retrieval/rerank.py`：`RERANK_BASE_URL` / `RERANK_MODEL` 可配，
  默认硅基流动；指向自建服务时**协议一致，代码零分支**
- `app/retrieval/knowledge_node.py`：`RERANK_ENABLED` 分支与精排分过闸

## 5. 数据模型

```sql
CREATE TABLE IF NOT EXISTS finetune_labels (
    id           BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
    query        VARCHAR(512) NOT NULL,
    query_hash   CHAR(32)     NOT NULL,
    low_conf_id  BIGINT       NULL,
    faq_id       VARCHAR(64)  NULL,
    label        VARCHAR(16)  NOT NULL,
    note         VARCHAR(256) NULL,
    annotator    VARCHAR(32)  NOT NULL DEFAULT 'human',
    created_at   DATETIME(6)  NOT NULL,
    updated_at   DATETIME(6)  NOT NULL,
    UNIQUE KEY uk_labels_hash (query_hash),
    KEY idx_labels_label (label, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS finetune_datasets (
    id         BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
    name       VARCHAR(64)  NOT NULL,
    path       VARCHAR(256) NOT NULL,
    split_json JSON         NULL,
    stats_json JSON         NULL,
    created_at DATETIME(6)  NOT NULL,
    KEY idx_datasets_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS finetune_jobs (
    id           VARCHAR(32)  NOT NULL PRIMARY KEY,
    dataset_id   BIGINT       NOT NULL,
    status       VARCHAR(16)  NOT NULL,
    params_json  JSON         NULL,
    metrics_json JSON         NULL,
    log_path     VARCHAR(256) NULL,
    error        TEXT         NULL,
    started_at   DATETIME(6)  NOT NULL,
    finished_at  DATETIME(6)  NULL,
    KEY idx_jobs_status (status, started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS finetune_models (
    id           BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
    version      VARCHAR(32)  NOT NULL,
    base_model   VARCHAR(64)  NOT NULL,
    model_dir    VARCHAR(256) NOT NULL,
    onnx_path    VARCHAR(256) NOT NULL,
    metrics_json JSON         NULL,
    active       TINYINT(1)   NOT NULL DEFAULT 0,
    created_at   DATETIME(6)  NOT NULL,
    UNIQUE KEY uk_models_version (version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

- `finetune_labels` 用 `query_hash` 做唯一键：同一句话无论来自低置信度、反馈
  还是 r13，只标一次，重复提交覆盖。
- `label` 三值：`doc`（应命中某条 FAQ）/ `missing`（知识库缺失）/ `noise`
  （非政策问题）。只有 `doc` 能进训练正样本。
- `faq_id` 存**检索单元** id（`faq_documents.id`，如 `P-RET-001-3`），不是
  文档 id：向量库里索引的是小节，精排打分的也是小节，标注必须同口径。
  r13 标注集的 `expected_doc_id` 同样是这个粒度，可直接合并。
- `finetune_models.active` 用「先全置 0 再置 1」保证唯一活跃版本，回滚就是
  改这一列 + 通知推理服务重载。

## 6. 数据流水线

### 6.1 清洗（每步出统计，写进 `stats_json`）

| 步骤 | 规则 | 计数键 |
|---|---|---|
| 归一化 | 去空白、去标点、全角转半角、小写 | `normalized` |
| 精确去重 | 归一化后完全相同只留一条 | `dup_exact` |
| 近似去重 | `difflib` 相似度 ≥ 0.9 归并到先出现的一条 | `dup_near` |
| 长度 | 有效字符 2–200，超长截断而非丢弃 | `drop_len` |
| 噪声 | 复用 `intent.py` 的寒暄/投诉正则；无政策话题词且无订单引用 → `noise` | `noise` |
| PII | `ORDER[A-Z0-9]{4,}` → `<ORDER>`，11 位手机号 → `<PHONE>` | `masked` |
| 跨源合并 | 与 r13 同 query 合并，`source=both` | `merged` |

清洗是纯函数，不碰数据库也不调模型，测试可以直接喂列表断言计数。

### 6.2 诊断闸

对每条清洗后的 query 生成**候选文档**（向量召回 top-5 + BM25 top-5 合并去
重），再由 LLM 预标注给建议：

- 有明确对应条款 → 建议 `doc_id`，置信度
- 无对应条款但属政策范畴 → 建议 `missing`
- 不属政策范畴 → 建议 `noise`

**闸门条件**：`可回答（label=doc）≥ 80 条` 才继续训练。不足时流水线以
`gate_blocked` 结束，输出 `docs/eval/r21-gap-report.md`（哪些 category 缺
条款、缺多少条、典型问法），本轮转做扩知识库。

80 这个数字的依据：47 块 FAQ 语料下，每条正例平均能贡献约 1 个有效训练
信号；低于 80 条正例时 dev 集每层不足 3 条，指标完全是抖动，无法作为发布
依据。

### 6.3 分层抽样

- 分层键：`category` × `label`。
- 比例 train 70 / dev 15 / test 15，层内按比例抽。
- 层内总数 < 3 的全部进 train——避免 dev 出现 1 条样本。
- 固定随机种子（`SEED=20261104`），结果落 `data/finetune/split.json`。
- **dev / test 只收人工标注样本，绝不掺增强数据**，否则指标虚高。

### 6.4 数据增强（只作用于 train，只对 `label=doc`）

| 手段 | 说明 | 量级 |
|---|---|---|
| a 轨迹改写配对 | `agent_traces` 里同轮的原话与 `rewritten_query` 组成同一标签的样本对 | 免费，约 +80–150 |
| b LLM 口语改写 | 每条生成 2 个变体，prompt 锁死「不得改变诉求与对象」，带 `origin_id` | 约 530 次调用 |
| c 规则同义词 | 退/退货、寄/发、几天/多久、能退吗/可以退吗 | 免费 |
| d hard negative | 每条 query 配 1–2 个同 category 内**检索分最高但非金标**的 doc（来自 `low_confidence_queries.retrieved`） | 免费 |

hard negative 是本轮最关键的增强：随机负例太容易，模型学不到「为什么这条
不是答案」；同 category 高分负例才是线上真正会误召的东西。

### 6.5 标注

`admin_finetune.html` 的标注标签页：

- 左侧：query 原文 + 来源（低置信度 id / r13 / 反馈）
- 右侧：候选文档 top-5，显示标题、`doc_id`、检索分、正文预览
- 操作：`1-5` 选中候选、`0` 标知识库缺失、`x` 标噪声、`Enter` 下一条
- LLM 建议以徽标形式标在候选旁，人工**点确认或否决**，不是抽检
- 顶部进度条 + 剩余条数

标注写入 `finetune_labels`，与 `eval_cases` 完全隔离。

## 7. 训练

### 7.1 任务定义

- 任务：cross-encoder 相关性打分，`[CLS] query [SEP] doc [SEP]` → 单 logit
- 基座：`hfl/chinese-roberta-wwm-ext`（102M）
- 全参微调，lr 2e-5，batch 16（GPU）/ 8（CPU），3–5 epoch，early stopping
  按 dev MRR
- 损失：pairwise 软标签（`BCEWithLogitsLoss`，正例 1，hard negative 0，
  其余负例 0 但权重 0.5）

为什么不用嵌入模型方案：`chinese-roberta-wwm-ext` 不是句向量模型，要做对比
学习得从零构造，几百条正例会严重过拟合；且输出 768 维与 Milvus 现有 1024
维不兼容，等于重造索引。交叉编码器是它的原生用法，数据量也友好。

### 7.2 训练容器

`deploy/trainer/Dockerfile`：`python:3.11-slim` + `torch`（CPU 或 cu121）+
`transformers` + `onnxruntime` + `fastapi`。`HF_ENDPOINT=https://hf-mirror.com`
写进 compose 环境变量（本机 HF 不通，容器里必须走镜像站）。

`deploy/trainer-compose.yml`：

- 端口 `8010:8010`
- 卷：`trainer-models`（模型版本目录）、`trainer-cache`（HF 权重缓存）、
  `trainer-data`（数据集与日志）
- `deploy.resources.limits`：`cpus: "6"`、内存上限，避免与 Milvus、
  Langfuse 抢资源
- GPU 存在时用 `deploy.resources.reservations.devices` 声明，无则注释掉

`trainer/server.py` 接口：

| 端点 | 语义 |
|---|---|
| `GET /health` | `{cuda: bool, device_name, versions}` |
| `POST /train` | `{dataset_path, epochs, ...}` → `{job_id}`，后台跑 |
| `GET /train/{job_id}` | `{status, metrics, log_tail}` |
| `GET /models` | 版本列表 + active |
| `POST /models/{version}/activate` | 切活跃版本，推理服务重载 |
| `POST /v1/rerank` | 与硅基流动同协议：`{model, query, documents, top_n}` → `{results}` |

`/v1/rerank` 与硅基流动协议一致，是为了让 `app/retrieval/rerank.py` 只改
`RERANK_BASE_URL` 就能切，不用加分支。

## 8. 线上接线

### 8.1 knowledge_node 分支

```
RERANK_ENABLED=0（默认）
  召回 top-3 → 余弦 0.58/0.65 闸（现状，一行不改）

RERANK_ENABLED=1
  召回 top-10 → 精排（微调模型）→ 取 top-3
  → 精排分 ≥ RERANK_ANSWER_THRESHOLD 才作答，否则拒答
```

拒答仍走 `REFUSAL_TEMPLATE` 并落 `low_confidence_queries`——**开了精排之后
的低置信度数据仍然是飞轮的原料**，闭环不断。

### 8.2 阈值标定

交叉编码器 sigmoid 分与余弦不可比，`RELEVANCE_THRESHOLD` / 
`INSUFFICIENT_THRESHOLD` 保留给旧路径。新阈值：

- 在 dev split 上扫 0.1–0.9，步长 0.02，取 Youden J 最大点
- 写 `.env` 的 `RERANK_ANSWER_THRESHOLD`，不硬编码
- `scripts/finetune_eval.py` 输出标定曲线，供人工确认

### 8.3 回滚

- 代码级：`RERANK_ENABLED=0`
- 模型级：`POST /models/{旧版本}/activate`
- 两级都试过还不行的最坏情况：`RERANK_BASE_URL` 改回硅基流动原模型

## 9. 评估

| 尺 | 数据 | 指标 | 门槛 |
|---|---|---|---|
| 精排离线 | test split | Hit@1、MRR@3、AUC | Hit@1 ≥ 0.75 且 MRR@3 ≥ 0.80 |
| 低置信度回放 | 265 条清洗后 | 越过闸且引用正确比例 | ≥ 25% |
| 端到端回归 | r20 `run_eval`（accepted 样本） | `doc_hit_rate`、`citation_valid_rate` | 不劣化超过 2 个点 |

三把尺缺一不可：只看离线指标会得到「在自己的测试集上很好」的结论；只看端
到端会淹没在样本量里；回放是唯一直接回答「飞轮攒的东西有没有变成能力」的
数字。

未达标时 `POST /models/{version}/activate` 拒绝执行，模型留在版本列表里可查
但不可用。

## 10. 前端配套

`admin_finetune.html` 三个标签，复用 `admin.css`，不引图表库：

1. **标注**：进度条、候选列表、AI 建议徽标、键盘快捷键、上/下一条
2. **数据集**：清洗漏斗（每步剩余条数）、分层分布表、split 条数、增强计数、
   `gate_blocked` 时的缺口报告链接、导出按钮
3. **训练与发布**：发起训练、日志尾部轮询、dev/test 指标表、版本列表、
   激活/回滚按钮

管理接口沿用 `/api/admin/*` 无鉴权约定（仅本地开发）。

## 11. 风险与坑

1. 本机无 GPU、HF 不可达 → 训练只能在远端容器，镜像站必须配
2. 265 条可能大半是知识库缺失 → 诊断闸先判，不足 80 条直接停
3. 全参 + 小数据易过拟合 → 三分集 + early stopping，dev 与 test 必须同向
4. 精排分与余弦不可比 → 阈值重标，旧常量只服务旧路径
5. 线上本来就没走精排 → 这是行为变更，必须 flag + 端到端回归
6. ONNX 与 tokenizer 版本漂移 → 导出时把 `tokenizer.json`、`config.json`
   一起固化进版本目录
7. 训练容器与 Milvus/Langfuse 抢资源 → compose 限 CPU 与内存
8. 远端 `8010` 端口可能未开放 → Task 0 先探测，不通则改端口或走 SSH 隧道

## 12. 验收

- 标注页能标完 265 条，进度条归零，`finetune_labels` 三类分布可见
- 数据集页显示清洗漏斗与分层分布，jsonl 可导出且行数对得上
- 训练页能发起一次训练，日志可见，指标落库
- 三把尺结果都在页面上，未达标时激活按钮置灰
- `RERANK_ENABLED=1` 后在对话页实测：低置信度里原本拒答的问题能答对，
  且引用编号正确可点
- `RERANK_ENABLED=0` 一键回到 r20 行为，对话无回归