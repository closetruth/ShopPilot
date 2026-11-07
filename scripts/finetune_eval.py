"""离线评估与阈值标定：在远端容器里跑，产出 `eval.json`。

只算两把能用算力算出来的尺（test 排序、低置信度回放）和一条标定曲线；
第三把「端到端回归」要跑整个对话图，只能在本地 app 侧跑完把数字带进来，
所以这里只接受、不计算——传进来就记进 `eval.json`，不传就留空，由门禁
按「缺失即未达标」处理。

打分统一走 `rerank_service`（onnxruntime 读导出的 model.onnx），而不是
torch 权重：线上跑的就是这份 ONNX，用 torch 评估等于量了另一份东西。
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from app.finetune.gate import (
    auc,
    hit1_rate,
    mrr_at_k,
    sweep_threshold,
)

DATA_DIR = Path("/app/data")
MODELS_DIR = Path("/app/models")
TOP_K = 3


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_split(data_dir: Path, name: str) -> list[dict]:
    rows = _read_jsonl(data_dir / f"{name}.jsonl")
    if not rows:
        raise FileNotFoundError(f"{data_dir / f'{name}.jsonl'} 为空或不存在")
    return rows


def load_pool(data_dir: Path) -> list[dict]:
    """全部启用条款。缺它评估就退化成「几条正例里排序」，指标无意义。"""
    pool = _read_jsonl(data_dir / "faq_pool.jsonl")
    if not pool:
        raise FileNotFoundError(
            "缺少 faq_pool.jsonl，先重建数据集；没有全池就无法做全池评估"
        )
    return pool


def make_scorer(version: str, models_dir: Path):
    """返回 `(query, docs) -> scores`。

    优先用推理服务：它加载的是线上同一份 ONNX 与 tokenizer.json，评估结果
    与线上一致。容器外（比如本机调试）没有 rerank_service 时退回 torch 权重。
    """
    try:
        import rerank_service

        rerank_service.MODELS_DIR = models_dir
        return lambda query, docs: rerank_service.score(version, query, docs)
    except ImportError:
        pass

    import torch
    from transformers import AutoTokenizer, AutoModelForSequenceClassification

    model_dir = models_dir / version
    tokenizer = AutoTokenizer.from_pretrained(str(model_dir))
    model = AutoModelForSequenceClassification.from_pretrained(str(model_dir))
    model.eval()

    def score(query: str, docs: list[str]) -> list[float]:
        encoded = tokenizer(
            [query] * len(docs), docs, truncation=True, max_length=256,
            padding=True, return_tensors="pt",
        )
        with torch.no_grad():
            logits = model(**encoded).logits.squeeze(-1)
        return torch.sigmoid(logits).tolist()

    return score


def rank_gold(
    scorer, pool: list[dict], query: str, gold_id: str
) -> tuple[int, float, float]:
    """金标在整池里的名次、金标分、最高的非金标分。

    第三个值给标定用：判断「该不该答」时，线上看到的是最高分那条，所以
    负例取最强的错误对手，而不是随便一条。
    """
    doc_ids = [row["faq_id"] for row in pool]
    scores = scorer(query, [row["doc"] for row in pool])
    order = sorted(zip(doc_ids, scores), key=lambda item: -item[1])
    ranked = [doc_id for doc_id, _ in order]
    rank = ranked.index(gold_id) + 1 if gold_id in ranked else len(ranked) + 1
    gold_score = dict(zip(doc_ids, scores)).get(gold_id, 0.0)
    rival = max(
        (score for doc_id, score in zip(doc_ids, scores) if doc_id != gold_id),
        default=0.0,
    )
    return rank, float(gold_score), float(rival)


def eval_offline(
    scorer, split: list[dict], pool: list[dict], threshold: float | None = None
) -> dict:
    """test 尺：Hit@1 / MRR@3 / AUC，金标与全部条款竞争。

    额外报一个 `abstain_rate`：人工标成 missing / noise 的问题里，有多少
    的最高池内分数低于阈值——也就是该拒答的真的拒了。它不是门禁指标，
    因为阈值本身还没在线上验证过，先让人看见这个数。
    """
    pool_ids = {row["faq_id"] for row in pool}
    targets = [
        row for row in split if row.get("label") == 1 and row.get("faq_id") in pool_ids
    ]
    abstain = [row for row in split if row.get("label") == 0]
    if not targets:
        return {
            "hit1": 0.0, "mrr3": 0.0, "auc": 0.0,
            "queries": 0, "pool": len(pool), "note": "test 集没有可评估的正例",
        }

    ranks: list[int] = []
    positives: list[float] = []
    negatives: list[float] = []
    for row in targets:
        rank, gold_score, rival = rank_gold(scorer, pool, row["query"], row["faq_id"])
        ranks.append(rank)
        positives.append(gold_score)
        negatives.append(rival)

    result = {
        "hit1": round(hit1_rate(ranks), 4),
        "mrr3": round(mrr_at_k(ranks, TOP_K), 4),
        "auc": round(auc(positives, negatives), 4),
        "queries": len(targets),
        "pool": len(pool),
        "abstain_queries": len(abstain),
    }

    if abstain and threshold is not None:
        refused = 0
        for row in abstain:
            scores = scorer(row["query"], [item["doc"] for item in pool])
            if max(scores) < threshold:
                refused += 1
        result["abstain_rate"] = round(refused / len(abstain), 4)
    elif abstain:
        result["abstain_rate"] = None

    return result


def calibrate(scorer, split: list[dict], pool: list[dict]) -> dict:
    """dev 尺：扫阈值取 Youden J 最大点，同时给出整条曲线。

    负例有两个来源，缺一不可：

    - 正例问题里「最强的错误对手」：金标之外分最高的条款。它衡量的是
      「排错了但仍有分」，属于排序层面的负例；
    - 人工标成 missing / noise 的问题在全池上的最高分：这类问题压根没有
      答案，任何一条越过阈值都是误答。它衡量的是「该不该开口」，属于
      闸层面的负例。

    只用前者会把阈值标得偏低——那些问题里总有一个「最像答案的错误答案」，
    模型给它打高分是正常的，于是阈值被迫下压；只有后者才能约束住误答。
    """
    pool_ids = {row["faq_id"] for row in pool}
    targets = [
        row for row in split if row.get("label") == 1 and row.get("faq_id") in pool_ids
    ]
    if not targets:
        return {"threshold": None, "youden_j": None, "curve": [], "note": "dev 无可标定正例"}

    positives: list[float] = []
    negatives: list[float] = []
    for row in targets:
        _, gold_score, rival = rank_gold(scorer, pool, row["query"], row["faq_id"])
        positives.append(gold_score)
        negatives.append(rival)

    abstain = [row for row in split if row.get("label") == 0]
    for row in abstain:
        scores = scorer(row["query"], [item["doc"] for item in pool])
        negatives.append(float(max(scores)))

    result = sweep_threshold(positives, negatives)
    result["abstain_queries"] = len(abstain)
    return result


def eval_replay(scorer, pool: list[dict], threshold: float) -> dict:
    """回放尺：低置信度池里原本没答好的问题，有多少现在能越过闸且引用正确。

    只认 `low_conf_id` 不为空的标注。这一列就是「这条问题来自低置信度池」的
    唯一凭据：r13 复用集与轨迹增强样本都没有它。若把全部标注都算进来，这把
    尺就退化成 test 尺的第二次抽样，量不出飞轮攒的东西有没有变成能力——
    而那正是发布门禁里唯一无法用离线指标替代的那把。

    标注里 `missing` / `noise` 没有金标，不算修复也不算失败，直接排除：
    把它们算进分母会把「知识库压根没这条」的账记到模型头上。
    """
    from scripts.finetune_dataset import load_labeled

    pool_ids = {row["faq_id"] for row in pool}
    labeled = [
        row
        for row in load_labeled()
        if row.get("low_conf_id") is not None
        and row.get("faq_id") in pool_ids
        and row.get("label") == 1
    ]
    if not labeled:
        return {
            "fix_rate": 0.0,
            "total": 0,
            "answered": 0,
            "threshold": threshold,
            "note": "没有低置信度池的带金标标注，回放尺无从计算",
        }

    answered = 0
    for row in labeled:
        rank, gold_score, rival = rank_gold(scorer, pool, row["query"], row["faq_id"])
        top_score = max(gold_score, rival)
        if rank == 1 and top_score >= threshold:
            answered += 1

    return {
        "fix_rate": round(answered / len(labeled), 4),
        "total": len(labeled),
        "answered": answered,
        "threshold": round(threshold, 4),
    }


def run(
    *,
    version: str,
    data_dir: Path = DATA_DIR,
    models_dir: Path = MODELS_DIR,
    e2e: dict | None = None,
    log=print,
) -> dict:
    """跑完两把离线尺 + 标定，落 `eval.json` 并返回全文。"""
    pool = load_pool(data_dir)
    test_rows = load_split(data_dir, "test")
    dev_rows = load_split(data_dir, "dev")
    scorer = make_scorer(version, models_dir)

    log(f"评估 {version}：test {len(test_rows)} 行，dev {len(dev_rows)} 行，池 {len(pool)} 条")

    calibration = calibrate(scorer, dev_rows, pool)
    threshold = calibration.get("threshold")
    if threshold is None:
        # 没标定出来就别瞎猜：退回 0.5，并让门禁看到 recall 之类的缺失
        threshold = 0.5
        log("dev 未产出阈值，回放暂用 0.5，请在页面上人工确认")
    else:
        log(
            f"标定阈值 {threshold}（Youden J {calibration['youden_j']}，"
            f"召回 {calibration['recall']} / 特异度 {calibration['specificity']}，"
            f"负例含 {calibration.get('abstain_queries', 0)} 条「本就不该答」）"
        )

    test = eval_offline(scorer, test_rows, pool, threshold=threshold)
    log(
        f"test Hit@1 {test['hit1']} MRR@3 {test['mrr3']} AUC {test['auc']}"
        f"（{test['queries']} 条正例 / {test.get('abstain_queries', 0)} 条负例"
        f" / 池 {test['pool']}）"
    )
    if test.get("abstain_rate") is not None:
        log(f"test 该拒未拒率 {1 - test['abstain_rate']:.4f}（阈值 {threshold}）")

    replay = eval_replay(scorer, pool, threshold)
    log(
        f"回放 {replay['answered']}/{replay['total']} 修复"
        f"（{replay['fix_rate']}，阈值 {replay['threshold']}）"
    )

    result = {
        "version": version,
        "test": test,
        "calibration": calibration,
        "replay": replay,
        "e2e": e2e,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    out_path = models_dir / version / "eval.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"评估结果已写入 {out_path}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="精排离线评估与阈值标定")
    parser.add_argument("--version", required=True)
    parser.add_argument("--data-dir", default=str(DATA_DIR), type=Path)
    parser.add_argument("--models-dir", default=str(MODELS_DIR), type=Path)
    parser.add_argument("--e2e-doc-hit-rate", type=float, default=None)
    parser.add_argument("--e2e-baseline", type=float, default=None)
    args = parser.parse_args()

    e2e = None
    if args.e2e_doc_hit_rate is not None and args.e2e_baseline is not None:
        e2e = {"doc_hit_rate": args.e2e_doc_hit_rate, "baseline": args.e2e_baseline}

    result = run(
        version=args.version,
        data_dir=args.data_dir,
        models_dir=args.models_dir,
        e2e=e2e,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()