# 精排微调训练容器。
#
# 一个镜像承担两件事：离线训练（train.py）与在线推理（rerank_service.py）。
# 合并的理由是两者共用同一个 tokenizer 与 onnxruntime，分成两个镜像会让
# 版本对齐变成两处维护。
#
# 构建上下文是**仓库根**，不是本目录：数据集要在容器内直接从远端 MySQL
# 构建，所以必须把 `app/` 的取数层和 `scripts/finetune_dataset.py` 拷进来。
# 只有 `candidates.jsonl` 这个检索快照仍由本地生成后挂载（见 compose）。
#
# 远端是 CPU 还是 GPU 未知（见 docs/dev-notes/r21.md），所以这里装 CPU 版
# torch；GPU 机器由 deploy/trainer-gpu.yml 覆盖成 CUDA 版索引地址。

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models/hf \
    TRANSFORMERS_CACHE=/models/hf \
    TOKENIZERS_PARALLELISM=false \
    PYTHONPATH=/app

# 远端实测：官方索引从这里拉 195MB 的 CPU 轮子会长时间卡在 "Downloading"
# 不往下走，而阿里云的 pytorch-wheels 镜像 9 秒就下完。所以默认直接下
# 确定的轮子文件，而不是走 pip 的索引解析——索引解析会额外请求元数据，
# 在网络不稳时更容易挂住。
# GPU 场景由 deploy/trainer-gpu.yml 把 TORCH_WHEEL_URL 置空、改走 cu121 索引。
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
ARG TORCH_WHEEL_URL=https://mirrors.aliyun.com/pytorch-wheels/cpu/torch-2.4.1%2Bcpu-cp311-cp311-linux_x86_64.whl
# pip 会从**文件名**里解析版本与平台，存成 torch.whl 会被判成
# "Invalid wheel filename (wrong number of parts)"，所以落盘名必须与
# 轮子本身的名字完全一致（含 +cpu 与 ABI 段）
ARG TORCH_WHEEL_FILE=torch-2.4.1+cpu-cp311-cp311-linux_x86_64.whl

# apt 换阿里云镜像：远端直连 deb.debian.org 时，单是 update 就花了 225 秒。
# 也去掉了 build-essential——torch/onnx/onnxruntime 全是预编译轮子，
# 没有需要现场编译的 C 扩展，留着它只是白白多拉 351MB。
RUN sed -i 's|http://deb.debian.org|https://mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true \
 && apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY deploy/trainer/requirements.txt /tmp/requirements.txt

RUN pip install --upgrade pip \
 && if [ -n "${TORCH_WHEEL_URL}" ]; then \
      curl -fsSL -o "/tmp/${TORCH_WHEEL_FILE}" "${TORCH_WHEEL_URL}" \
      && pip install "/tmp/${TORCH_WHEEL_FILE}" \
      && rm -f "/tmp/${TORCH_WHEEL_FILE}"; \
    else \
      pip install --index-url "${TORCH_INDEX_URL}" torch==2.4.1; \
    fi \
 && pip install -r /tmp/requirements.txt

WORKDIR /app

COPY deploy/trainer/train.py deploy/trainer/server.py deploy/trainer/rerank_service.py ./

# 取数层只用到 pymysql 与纯函数模块（normalize_query / classify_by_rules），
# 不牵涉检索与 LLM，因此整个 app 包可以放心拷进来
COPY app /app/app
COPY scripts/finetune_dataset.py scripts/finetune_eval.py /app/scripts/

# r13 标注集是训练正样本的主力（434 条，全部带候选串），构建数据集时由
# `fetch_r13` 按 `ROOT / docs / eval / r13-queries.csv` 读取。不拷进来时它
# 会静默返回空列表，构建最后报成「还没有任何人工标注」——把排查引到标注页
# 上去，实际缺的是这个文件。
COPY docs/eval/r13-queries.csv /app/docs/eval/r13-queries.csv

RUN mkdir -p /app/data /app/models /app/logs

EXPOSE 8010

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8010/health || exit 1

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8010"]