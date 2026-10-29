"""构建 r13 评估标注集。

从 ModelScope 三个客服语料中筛出政策类 query，抽样 500 条，
用向量检索初筛候选政策，供人工逐条标注。

用法：uv run python -m scripts.build_eval_set
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
from collections import defaultdict
from pathlib import Path

from app import store, vector_store

EVAL_DIR = Path("eval_data")
OUT_CSV = Path("docs/eval/r13-queries.csv")

POLICY_SCENES = {
    "退换货", "质量问题", "维权投诉", "补偿方案", "退款", "退货", "质量",
    "质量投诉", "退换货,质量问题",
    "物流停滞", "丢件破损", "快递时效", "催发", "丢件", "查件", "破损",
    "快递实效", "查单号", "停滞", "修改地址", "丢件催发",
    "售后", "退换", "物流",
}

# 场景归并：原始 scene 太碎（24 个），且大量是同一业务的不同写法。
# 归并成 10 个业务大类后按类均匀抽样，避免「退换货 550 条 / 退款 20 条」
# 这种原始分布把结果带偏。
SCENE_GROUPS: dict[str, list[str]] = {
    "退换货": ["退换货", "退货", "退换"],
    "质量问题": ["质量问题", "质量", "质量投诉", "退换货,质量问题", "破损"],
    "物流停滞/延迟": ["物流停滞", "快递时效", "停滞", "快递实效"],
    "丢件破损": ["丢件破损", "丢件", "丢件催发", "查件", "查单号"],
    "催发": ["催发"],
    "售后": ["售后"],
    "投诉": ["维权投诉"],
    "补偿": ["补偿方案"],
    "退款": ["退款"],
    "其他物流": ["物流", "修改地址"],
}

SCENE_TO_GROUP = {s: g for g, scenes in SCENE_GROUPS.items() for s in scenes}

# 业务大类：物流有 4 个 scene 组，若不合并会天然占据 4 份权重。
# 按大类均匀才能消除「物流占比高」的结构性偏置。
BUSINESS_FAMILY: dict[str, str] = {
    "退换货": "退换货",
    "质量问题": "质量问题",
    "物流停滞/延迟": "物流",
    "丢件破损": "物流",
    "催发": "物流",
    "其他物流": "物流",
    "售后": "售后",
    "投诉": "投诉",
    "补偿": "补偿",
    "退款": "退款",
}

# 措辞特征分桶用的标记词
VERBATIM_MARKERS = (
    "无理由", "运费险", "价保", "发票", "七天", "7天", "48小时", "仅退款",
    "丢件", "破损", "停滞", "催发", "退换货", "补偿", "投诉", "维权",
)
COLLOQUIAL_MARKERS = (
    "咋整", "咋办", "啥", "咋", "呗", "嘛", "呢", "啊", "了", "的亲", "客服",
)


def load_policy_queries() -> list[dict]:
    rows: list[dict] = []
    for path in sorted(EVAL_DIR.glob("*.jsonl")):
        source = path.stem
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            scene = row.get("scene")
            if scene in SCENE_TO_GROUP and row.get("user"):
                rows.append(
                    {
                        "query": row["user"].strip(),
                        "scene": SCENE_TO_GROUP[scene],
                        "source": source,
                    }
                )
    return rows


def classify_bucket(query: str) -> str:
    """按措辞特征分桶。这是粗分类，标注时会人工复核调整。"""
    if re.search(r"\d", query):
        return "numeric"
    if any(m in query for m in VERBATIM_MARKERS):
        return "verbatim"
    if any(m in query for m in COLLOQUIAL_MARKERS) or len(query) <= 10:
        return "colloquial"
    return "paraphrase"


def sample_balanced(rows: list[dict], total: int, seed: int = 42) -> list[dict]:
    """按业务大类均匀抽样，大类内再按措辞特征铺开。

    物流有 4 个 scene 组（停滞/丢件/催发/其他），若按 scene 均匀会天然
    占 4 份权重。按大类均匀才能真正消除物流偏置。
    """
    random.seed(seed)

    by_family: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        row["bucket"] = classify_bucket(row["query"])
        family = BUSINESS_FAMILY.get(row["scene"], row["scene"])
        row["family"] = family
        by_family[family].append(row)

    for group in by_family.values():
        random.shuffle(group)

    families = sorted(by_family, key=lambda f: -len(by_family[f]))
    per_family = total // len(families)

    picked: list[dict] = []
    for family in families:
        group = by_family[family]

        # 大类内按措辞分桶均衡
        by_bucket: dict[str, list[dict]] = defaultdict(list)
        for row in group:
            by_bucket[row["bucket"]].append(row)

        quota = min(per_family, len(group))
        inner = max(1, quota // max(1, len(by_bucket)))
        taken: list[dict] = []
        for bucket_rows in by_bucket.values():
            taken.extend(bucket_rows[:inner])

        if len(taken) < quota:
            seen = {id(r) for r in taken}
            for row in group:
                if id(row) not in seen:
                    taken.append(row)
                if len(taken) >= quota:
                    break

        picked.extend(taken[:quota])

    if len(picked) < total:
        seen = {id(r) for r in picked}
        rest = [r for r in rows if id(r) not in seen]
        random.shuffle(rest)
        picked.extend(rest[: total - len(picked)])

    picked.sort(key=lambda r: (r["family"], r["bucket"], r["query"]))
    return picked[:total]


def screen_candidates(rows: list[dict], top_k: int = 3) -> list[dict]:
    """用向量检索给出候选政策，供人工标注参考。"""
    store.init_db()
    for row in rows:
        try:
            hits = vector_store.search(row["query"], "", top_k=top_k)
        except Exception:  # noqa: BLE001
            hits = []
        row["candidates"] = [
            f"{h['source_id']}:{h['score']:.2f}" for h in hits
        ]
    return rows


def write_csv(rows: list[dict]) -> None:
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "query", "bucket", "family", "scene", "source",
                "candidates", "expected_doc_id", "confidence", "note",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "query": row["query"],
                    "bucket": row["bucket"],
                    "family": row.get("family", ""),
                    "scene": row["scene"],
                    "source": row["source"],
                    "candidates": " | ".join(row.get("candidates", [])),
                    "expected_doc_id": "",
                    "confidence": "",
                    "note": "",
                }
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--total", type=int, default=500)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    rows = load_policy_queries()
    print(f"政策类 query: {len(rows)}")

    sampled = sample_balanced(rows, args.total, args.seed)
    counts: dict[str, int] = defaultdict(int)
    for row in sampled:
        counts[row["bucket"]] += 1
    print(f"抽样 {len(sampled)} 条，分桶：{dict(counts)}")

    print("向量初筛候选…")
    sampled = screen_candidates(sampled)

    write_csv(sampled)
    print(f"已写入 {OUT_CSV}")


if __name__ == "__main__":
    main()
