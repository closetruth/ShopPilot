"""精排交叉编码器全参微调。

基座是 hfl/chinese-roberta-wwm-ext，任务是把 `[CLS] query [SEP] doc [SEP]`
压成一个相关性 logit。手写训练循环而不是用 `Trainer`，原因有三条：

1. 早停看的是 dev MRR 而不是 loss。loss 下降不代表排序变好，用 loss 选
   最优轮次常常选出排序更差的模型。
2. fp16 只在有 CUDA 时才开。远端算力未知（见 docs/dev-notes/r21.md），
   CPU 上开 amp 会直接报错。
3. 不引入 `accelerate` 依赖，镜像小一圈，远端拉取更快。

ONNX 导出同时固化 `tokenizer.json` 与 `config.json`：推理服务只读
onnxruntime，不装 transformers 的模型加载链路。
"""

from __future__ import annotations

import argparse
import json
import os
import random
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F
from torch.optim import AdamW
from torch.utils.data import DataLoader, Dataset
from transformers import (
    AutoModelForSequenceClassification,
    AutoTokenizer,
    DataCollatorWithPadding,
    get_linear_schedule_with_warmup,
)

BASE_MODEL = os.getenv("BASE_MODEL", "hfl/chinese-roberta-wwm-ext")
MAX_LENGTH = int(os.getenv("MAX_LENGTH", "256"))
WEIGHT_DECAY = 0.01
WARMUP_RATIO = 0.1
PATIENCE = 2


@dataclass
class TrainConfig:
    data_dir: Path = Path("/app/data")
    out_dir: Path = Path("/app/models")
    version: str = ""
    base_model: str = BASE_MODEL
    max_length: int = MAX_LENGTH
    epochs: int = int(os.getenv("EPOCHS", "4"))
    lr: float = float(os.getenv("LR", "2e-5"))
    batch_size: int = int(os.getenv("BATCH_SIZE", "0"))
    seed: int = int(os.getenv("SEED", "20261104"))
    fp16: bool = os.getenv("FP16", "0") == "1"
    rows: dict = field(default_factory=dict)


def _set_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def resolve_batch_size(config: TrainConfig, device: torch.device) -> int:
    """BATCH_SIZE 留空时按设备自适应。

    远端有没有卡我们事先不知道，所以不能写死 batch size：CPU 上 16 会直接
    把内存打爆，GPU 上 8 又浪费显存。
    """
    if config.batch_size > 0:
        return config.batch_size
    return 16 if device.type == "cuda" else 8


def _cgroup_cpu_quota() -> float | None:
    """容器被允许用的 CPU 数（docker `--cpus` 的等效值），读不到返回 None。

    `os.cpu_count()` 报的是宿主总核数，不是容器配额。远端宿主 15 核但
    compose 只给了 10，按 15 开线程就是超额订阅：线程互相抢核、上下文
    切换暴增，配合受限内存会把整台机器拖进 swap——实测一次训练后宿主
    连 sshd 都不响应了。所以线程数必须先看配额。
    """
    try:
        v2 = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if v2 and v2[0] != "max":
            return float(v2[0]) / float(v2[1])
    except (OSError, ValueError, IndexError):
        pass
    try:
        quota = float(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text())
        period = float(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text())
        if quota > 0 and period > 0:
            return quota / period
    except (OSError, ValueError):
        pass
    return None


def resolve_threads(device: torch.device) -> None:
    """CPU 训练留一个核给同机的其他服务。

    torch 默认吃满所有核，训练期间 MySQL、Milvus 与对话接口都会明显变慢；
    少用一个核的代价可忽略，换来的是不打扰。GPU 训练不涉及这条。
    """
    if device.type != "cpu":
        return
    cores = os.cpu_count() or 4
    quota = _cgroup_cpu_quota()
    if quota:
        cores = min(cores, int(quota))
    override = int(os.getenv("THREADS", "0"))
    if override > 0:
        cores = override
    torch.set_num_threads(max(1, cores - 1))


def load_split(data_dir: Path) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for name in ("train", "dev", "test"):
        path = data_dir / f"{name}.jsonl"
        if not path.exists():
            raise FileNotFoundError(
                f"缺少 {path}，先调 POST /datasets/build 让容器从 MySQL 现取标注重建数据集"
            )
        rows = []
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        out[name] = rows
    if not out["train"]:
        raise ValueError("训练集为空，检查数据构建是否产出了训练对")
    return out


class PairDataset(Dataset):
    """(query, doc) 对。padding 交给 collator，避免每条都补到 max_length。"""

    def __init__(self, rows: list[dict], tokenizer, max_length: int) -> None:
        self.rows = rows
        self.tokenizer = tokenizer
        self.max_length = max_length

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, index: int) -> dict:
        row = self.rows[index]
        encoded = self.tokenizer(
            row["query"],
            row["doc"],
            truncation=True,
            max_length=self.max_length,
        )
        encoded["labels"] = float(row["label"])
        return encoded


def _autocast(device: torch.device, fp16: bool):
    if fp16 and device.type == "cuda":
        return torch.autocast(device_type="cuda", dtype=torch.float16)
    return torch.autocast(device_type="cpu", enabled=False)


def score_pairs(
    model,
    tokenizer,
    pairs: list[tuple[str, str]],
    *,
    max_length: int,
    batch_size: int,
    device: torch.device,
    fp16: bool,
) -> list[float]:
    """给一批 (query, doc) 打分，返回 sigmoid 后的相关性分。"""
    model.eval()
    scores: list[float] = []
    with torch.no_grad():
        for start in range(0, len(pairs), batch_size):
            chunk = pairs[start : start + batch_size]
            encoded = tokenizer(
                [q for q, _ in chunk],
                [d for _, d in chunk],
                truncation=True,
                max_length=max_length,
                padding=True,
                return_tensors="pt",
            )
            encoded = {key: value.to(device) for key, value in encoded.items()}
            with _autocast(device, fp16):
                logits = model(**encoded).logits.squeeze(-1)
            scores.extend(torch.sigmoid(logits.float()).cpu().tolist())
    return scores


def load_pool(data_dir: Path) -> list[dict]:
    """全部条款快照，评估时作为候选池。

    由数据构建阶段导出（`faq_pool.jsonl`）。缺它时评估会退化成只在 dev
    自己的几条正例里排序，那样的 MRR 与随机排序无法区分。
    """
    path = data_dir / "faq_pool.jsonl"
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def random_mrr(pool_size: int) -> float:
    """随机排序的期望 MRR。

    池子有 N 条时，金标落在第 r 名的概率是 1/N，期望 MRR 是 (1/N)·Σ1/r。
    47 条池子只有 0.09——所以「MRR 0.30」是好是坏必须跟这个基线比，不能
    跟 0 比。
    """
    if pool_size <= 0:
        return 0.0
    return sum(1.0 / rank for rank in range(1, pool_size + 1)) / pool_size


def evaluate(
    model,
    tokenizer,
    rows: list[dict],
    *,
    pool: list[dict],
    max_length: int,
    batch_size: int,
    device: torch.device,
    fp16: bool,
    log=print,
) -> dict:
    """在全池上算 MRR / Top-1 / 成对准确率。

    金标要跟池里全部条款竞争第一名，这才是线上精排的真实处境（线上也是
    从候选里挑一条）。旧写法拿 dev 自己的 faq_id 当候选池，池子只有几条
    且每条对某个 query 都是正例——模型不需要学会区分就能排好，指标量的
    是噪声，而且会拿它做早停。
    """
    if pool:
        doc_ids = [row["faq_id"] for row in pool]
        docs = [row["doc"] for row in pool]
    else:
        merged: dict[str, str] = {}
        for row in rows:
            merged.setdefault(row["faq_id"], row["doc"])
        doc_ids = list(merged)
        docs = [merged[key] for key in doc_ids]
        log("警告：缺 faq_pool.jsonl，评估池只有 dev 正例，指标不可信")

    targets = [
        (row["query"], row["faq_id"]) for row in rows if row.get("label") == 1
    ]
    id_set = set(doc_ids)
    targets = [(query, faq_id) for query, faq_id in targets if faq_id in id_set]
    # 「本来就不该答」的人工负例（missing / noise）。它们没有金标，进不了
    # MRR，但能量出另一件事：模型会不会对没有答案的问题也给高分。
    # 只报数不参与早停——没有标定过的阈值，拿它挑轮次等于换个噪声。
    abstain_queries = [row["query"] for row in rows if row.get("label") == 0]
    baseline = round(random_mrr(len(doc_ids)), 4)
    if not targets or not doc_ids:
        return {
            "mrr": 0.0,
            "top1": 0.0,
            "pair_acc": 0.0,
            "queries": 0,
            "abstain_queries": len(abstain_queries),
            "abstain_top1": 0.0,
            "pool": len(doc_ids),
            "random_mrr": baseline,
        }

    reciprocal: list[float] = []
    top1 = 0
    pair_hits = 0
    pair_total = 0
    total = len(targets)
    for index, (query, faq_id) in enumerate(targets, start=1):
        scores = score_pairs(
            model,
            tokenizer,
            [(query, doc) for doc in docs],
            max_length=max_length,
            batch_size=batch_size,
            device=device,
            fp16=fp16,
        )
        score_by_id = dict(zip(doc_ids, scores))
        ranked = [
            doc_id
            for doc_id, _ in sorted(zip(doc_ids, scores), key=lambda item: -item[1])
        ]
        rank = ranked.index(faq_id) + 1
        reciprocal.append(1.0 / rank)
        if rank == 1:
            top1 += 1

        gold_score = score_by_id[faq_id]
        for doc_id, score in score_by_id.items():
            if doc_id == faq_id:
                continue
            pair_total += 1
            if gold_score > score:
                pair_hits += 1

        if index % 10 == 0 or index == total:
            log(f"  评估 {index}/{total}")

    # 负例问题的最高池内分数：这个数越接近正例的金标分，说明模型越倾向
    # 「无论问什么都给高分」，线上表现就是拒答率塌掉。
    abstain_top1 = 0.0
    if abstain_queries:
        tops: list[float] = []
        for query in abstain_queries:
            scores = score_pairs(
                model,
                tokenizer,
                [(query, doc) for doc in docs],
                max_length=max_length,
                batch_size=batch_size,
                device=device,
                fp16=fp16,
            )
            tops.append(max(scores))
        abstain_top1 = round(sum(tops) / len(tops), 4)

    return {
        "mrr": round(sum(reciprocal) / len(reciprocal), 4),
        "top1": round(top1 / len(targets), 4),
        "pair_acc": round(pair_hits / pair_total, 4) if pair_total else 0.0,
        "queries": len(targets),
        "abstain_queries": len(abstain_queries),
        "abstain_top1": abstain_top1,
        "pool": len(doc_ids),
        "random_mrr": baseline,
    }


def export_onnx(model, tokenizer, out_dir: Path, max_length: int, device: torch.device) -> Path:
    """导出固定输入名 `input_ids` / `attention_mask` 的 ONNX。

    `position_ids` 不显式导出：transformers 在 forward 内部按输入长度生成，
    dynamic_axes 能把 batch 与 seq 两个维度都放开，推理时长短句不用补到
    max_length。
    """
    model.eval()
    dummy = tokenizer(
        ["退货怎么弄"],
        ["退货条件：七天无理由退货。"],
        truncation=True,
        max_length=max_length,
        padding="max_length",
        return_tensors="pt",
    )
    path = out_dir / "model.onnx"
    torch.onnx.export(
        model,
        (dummy["input_ids"].to(device), dummy["attention_mask"].to(device)),
        str(path),
        input_names=["input_ids", "attention_mask"],
        output_names=["logits"],
        dynamic_axes={
            "input_ids": {0: "batch", 1: "sequence"},
            "attention_mask": {0: "batch", 1: "sequence"},
            "logits": {0: "batch"},
        },
        opset_version=14,
        do_constant_folding=True,
    )
    return path


def _default_version() -> str:
    return "r21-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def train(config: TrainConfig, log=print) -> dict:
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    fp16 = config.fp16 and device.type == "cuda"
    batch_size = resolve_batch_size(config, device)
    resolve_threads(device)
    _set_seed(config.seed)

    log(
        f"设备 {device.type}"
        + (f"（{torch.cuda.get_device_name(0)}）" if device.type == "cuda" else "")
        + f"，batch {batch_size}，线程 {torch.get_num_threads()}，"
        f"fp16 {'开' if fp16 else '关'}"
    )

    splits = load_split(config.data_dir)
    pool = load_pool(config.data_dir)
    negatives = sum(1 for row in splits["train"] if row["label"] == 0)
    log(
        f"数据 train {len(splits['train'])} / dev {len(splits['dev'])}"
        f" / test {len(splits['test'])}，评估池 {len(pool)} 条"
    )
    log(f"训练对中负例 {negatives} 条")
    if negatives == 0:
        # 全正例上 BCE 的最优解是给所有输入打高分：loss 会降，但模型没有
        # 任何区分能力。这种情况必须让用户看见，而不是训完再发现。
        log("警告：训练集没有负例，模型只会学到「全都相关」")

    tokenizer = AutoTokenizer.from_pretrained(config.base_model)
    model = AutoModelForSequenceClassification.from_pretrained(
        config.base_model, num_labels=1
    ).to(device)

    collator = DataCollatorWithPadding(tokenizer=tokenizer, return_tensors="pt")
    loader = DataLoader(
        PairDataset(splits["train"], tokenizer, config.max_length),
        batch_size=batch_size,
        shuffle=True,
        collate_fn=collator,
    )

    optimizer = AdamW(model.parameters(), lr=config.lr, weight_decay=WEIGHT_DECAY)
    total_steps = max(1, len(loader) * config.epochs)
    scheduler = get_linear_schedule_with_warmup(
        optimizer,
        num_warmup_steps=int(total_steps * WARMUP_RATIO),
        num_training_steps=total_steps,
    )

    best = {"mrr": -1.0, "epoch": 0}
    best_state: dict | None = None
    stale = 0

    for epoch in range(1, config.epochs + 1):
        model.train()
        running = 0.0
        for step, batch in enumerate(loader, start=1):
            labels = batch.pop("labels").to(device)
            batch = {key: value.to(device) for key, value in batch.items()}
            optimizer.zero_grad(set_to_none=True)
            with _autocast(device, fp16):
                logits = model(**batch).logits.squeeze(-1)
            loss = F.binary_cross_entropy_with_logits(logits.float(), labels.float())
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            scheduler.step()
            running += loss.item()
            if step % 10 == 0 or step == len(loader):
                log(
                    f"epoch {epoch}/{config.epochs} step {step}/{len(loader)}"
                    f" loss {running / step:.4f}"
                )

        metrics = evaluate(
            model,
            tokenizer,
            splits["dev"],
            pool=pool,
            max_length=config.max_length,
            batch_size=batch_size,
            device=device,
            fp16=fp16,
            log=log,
        )
        log(
            f"epoch {epoch} dev MRR {metrics['mrr']:.4f}"
            f" Top-1 {metrics['top1']:.4f}"
            f" 成对准确率 {metrics['pair_acc']:.4f}"
            f" 负例最高分 {metrics['abstain_top1']:.4f}"
            f"（{metrics['queries']} 正例 / {metrics['abstain_queries']} 负例，"
            f"随机基线 MRR {metrics['random_mrr']:.4f}）"
        )

        if metrics["mrr"] > best["mrr"]:
            best = {**metrics, "epoch": epoch}
            best_state = {
                key: value.detach().cpu().clone()
                for key, value in model.state_dict().items()
            }
            stale = 0
        else:
            stale += 1
            log(f"dev MRR 未提升（连续 {stale} 轮），最优仍是 epoch {best['epoch']}")
            if stale >= PATIENCE:
                log("触发早停")
                break

    if best_state is not None:
        model.load_state_dict(best_state)

    version = config.version or _default_version()
    out_dir = config.out_dir / version
    out_dir.mkdir(parents=True, exist_ok=True)

    # 关掉 safetensors：基座 hfl/chinese-roberta-wwm-ext 的权重在磁盘上是
    # 转置存的（(768,768) 的 stride 是 (1,768)），加载后 73 个参数都是非连续
    # 视图。safetensors 只接受连续张量，会直接抛
    # "trying to save a non contiguous tensor"，整轮训练白跑。
    # 存成 pytorch_model.bin 没有这个限制；推理端本来就只用 model.onnx 与
    # tokenizer.json，这份 torch 权重是给调试与回退路径用的。
    model.save_pretrained(out_dir, safe_serialization=False)
    tokenizer.save_pretrained(out_dir)
    onnx_path = export_onnx(model, tokenizer, out_dir, config.max_length, device)
    log(f"ONNX 已导出 {onnx_path}（{onnx_path.stat().st_size / 1e6:.1f} MB）")

    test_metrics = evaluate(
        model,
        tokenizer,
        splits["test"],
        pool=pool,
        max_length=config.max_length,
        batch_size=batch_size,
        device=device,
        fp16=fp16,
        log=log,
    )
    log(
        f"test MRR {test_metrics['mrr']:.4f} Top-1 {test_metrics['top1']:.4f}"
        f" 成对准确率 {test_metrics['pair_acc']:.4f}"
        f" 负例最高分 {test_metrics['abstain_top1']:.4f}"
        f"（{test_metrics['queries']} 正例 / {test_metrics['abstain_queries']} 负例，"
        f"随机基线 MRR {test_metrics['random_mrr']:.4f}）"
    )

    meta = {
        "version": version,
        "base_model": config.base_model,
        "max_length": config.max_length,
        "config": {
            **asdict(config),
            "data_dir": str(config.data_dir),
            "out_dir": str(config.out_dir),
            "batch_size": batch_size,
            "fp16": fp16,
            "device": device.type,
        },
        "rows": {name: len(rows) for name, rows in splits.items()},
        "pool": len(pool),
        "best": best,
        "test": test_metrics,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (out_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"模型目录 {out_dir}")

    return {
        "version": version,
        "path": str(out_dir),
        "onnx": str(onnx_path),
        "best": best,
        "test": test_metrics,
        "rows": meta["rows"],
        "device": device.type,
        "fp16": fp16,
        "batch_size": batch_size,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="精排交叉编码器微调")
    parser.add_argument("--data-dir", default="/app/data", type=Path)
    parser.add_argument("--out-dir", default="/app/models", type=Path)
    parser.add_argument("--version", default="")
    parser.add_argument("--epochs", default=TrainConfig.epochs, type=int)
    parser.add_argument("--lr", default=TrainConfig.lr, type=float)
    parser.add_argument("--batch-size", default=0, type=int)
    parser.add_argument("--max-length", default=MAX_LENGTH, type=int)
    parser.add_argument("--seed", default=20261104, type=int)
    parser.add_argument("--fp16", action="store_true")
    args = parser.parse_args()

    config = TrainConfig(
        data_dir=args.data_dir,
        out_dir=args.out_dir,
        version=args.version,
        epochs=args.epochs,
        lr=args.lr,
        batch_size=args.batch_size,
        max_length=args.max_length,
        seed=args.seed,
        fp16=args.fp16,
    )
    result = train(config)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()