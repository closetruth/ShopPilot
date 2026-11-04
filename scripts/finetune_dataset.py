"""把飞轮攒下的低置信度数据清洗成可标注、可训练的样本。

分两层，方便测试：

- 纯函数层（`normalize` / `mask_pii` / `detect_noise` / `clean`）不碰数据库、
  不调模型，喂列表就能断言
- 取数层（`fetch_low_conf` / `fetch_r13` / `fetch_traces`）只读，不改任何表

清洗顺序是有讲究的：先归一化再判长度，先合并同义再去近似，噪声判定放在
PII 掩码之前——掩码会吃掉订单号，「查订单到哪了」这类业务问题就判不出来
了。
"""

from __future__ import annotations

import csv
import difflib
import json
import random
import re
import sys
import unicodedata
from pathlib import Path

from app.chat.intent import classify_by_rules
from app.core.db import connect
from app.repo.finetune import normalize_query

ROOT = Path(__file__).resolve().parent.parent
R13_CSV = ROOT / "docs" / "eval" / "r13-queries.csv"
OUT_DIR = ROOT / "data" / "finetune"

NEAR_DUP_RATIO = 0.9
MIN_CHARS = 2
MAX_CHARS = 200

# 正样本少于这个数就发布门禁 blocked：80 条是「每个类别至少能摊到几条」的
# 经验下限，再少模型只会学到句式而不是条款区分。
GATE_MIN_POSITIVE = 80

# 规则同义词表。只覆盖客服话术里高频的换说法，不做通用近义词库——
# 规则改写的目的是让模型见到同一句话的几种口语形态，不是造新语料。
_SYNONYMS = {
    "退货": ("退掉", "退了"),
    "换货": ("换个", "换一个"),
    "运费": ("邮费", "快递费"),
    "几天": ("多久", "多长时间"),
    "怎么": ("如何", "咋"),
    "可以": ("能", "能不能"),
    "需要": ("要", "需不需要"),
    "能不能": ("可不可以",),
}

_ORDER_RE = re.compile(r"ORDER[A-Z0-9]{4,}")
_PHONE_RE = re.compile(r"1[3-9]\d{9}")
_LONG_DIGITS_RE = re.compile(r"(?<!\d)\d{8,}(?!\d)")

STAT_KEYS = (
    "normalized",
    "drop_len",
    "truncated",
    "dup_exact",
    "dup_near",
    "noise",
    "masked",
    "merged",
    "kept",
)


def normalize(text: str) -> str:
    """显示与训练用的归一化：全角转半角、去首尾空白、压平连续空白。

    与 `normalize_query` 的分工：这里保留空格与标点，因为送进模型的是这句
    话本身；`normalize_query` 只用于算去重 hash。
    """
    text = unicodedata.normalize("NFKC", text or "")
    return " ".join(text.split())


def mask_pii(text: str) -> tuple[str, bool]:
    """把订单号、手机号、长数字串换成占位符。返回 (文本, 是否改动)。"""
    masked = _ORDER_RE.sub("<ORDER>", text)
    masked = _PHONE_RE.sub("<PHONE>", masked)
    masked = _LONG_DIGITS_RE.sub("<NUM>", masked)
    return masked, masked != text


def detect_noise(query: str) -> bool:
    """是否不属政策检索范畴。

    复用 `intent.classify_by_rules`：规则判 knowledge 的留下；判 chitchat /
    complaint / business 的剔除（业务问题问的是订单数据，不是政策条款）；
    规则分不出类的（如「能便宜点吗」）保留，交人工判定——宁可多标一条，
    不可把真问题当噪声扔掉。
    """
    if not normalize_query(query):
        return True
    intent, confident = classify_by_rules(query)
    if confident and intent in ("chitchat", "complaint", "business"):
        return True
    return False


def _merge_sources(rows: list[dict], stats: dict) -> list[dict]:
    """同一句话来自多个源时合成一条，源标记为 both。"""
    groups: dict[str, dict] = {}
    for row in rows:
        key = normalize_query(row["query"])
        hit = groups.get(key)
        if hit is None:
            groups[key] = {
                **row,
                "sources": {row["source"]},
                "retrieved": list(row.get("retrieved") or []),
            }
            continue
        stats["dup_exact"] += 1
        hit["sources"].add(row["source"])
        if row.get("low_conf_id") and not hit.get("low_conf_id"):
            hit["low_conf_id"] = row["low_conf_id"]
        if row.get("expected_faq_id") and not hit.get("expected_faq_id"):
            hit["expected_faq_id"] = row["expected_faq_id"]
        for item in row.get("retrieved") or []:
            if item not in hit["retrieved"]:
                hit["retrieved"].append(item)

    out = []
    for hit in groups.values():
        sources = hit.pop("sources")
        if len(sources) > 1:
            stats["merged"] += 1
            hit["source"] = "both"
        else:
            hit["source"] = sources.pop()
        out.append(hit)
    return out


def _drop_near_duplicates(rows: list[dict], stats: dict) -> list[dict]:
    kept: list[dict] = []
    keys: list[str] = []
    for row in rows:
        key = normalize_query(row["query"])
        duplicate = False
        for other in keys:
            if difflib.SequenceMatcher(None, key, other).ratio() >= NEAR_DUP_RATIO:
                duplicate = True
                break
        if duplicate:
            stats["dup_near"] += 1
            continue
        keys.append(key)
        kept.append(row)
    return kept


def clean(rows: list[dict]) -> tuple[list[dict], dict]:
    """清洗主流程。rows 每项至少含 query / source。"""
    stats = {key: 0 for key in STAT_KEYS}

    prepared: list[dict] = []
    for row in rows:
        query = normalize(row["query"])
        stats["normalized"] += 1
        if len(query) < MIN_CHARS:
            stats["drop_len"] += 1
            continue
        if len(query) > MAX_CHARS:
            query = query[:MAX_CHARS]
            stats["truncated"] += 1
        prepared.append({**row, "query": query})

    merged = _merge_sources(prepared, stats)
    deduped = _drop_near_duplicates(merged, stats)

    kept: list[dict] = []
    for row in deduped:
        if detect_noise(row["query"]):
            stats["noise"] += 1
            continue
        masked, changed = mask_pii(row["query"])
        if changed:
            stats["masked"] += 1
            row = {**row, "query": masked}
        kept.append(row)

    stats["kept"] = len(kept)
    return kept, stats


def fetch_low_conf(limit: int | None = None) -> list[dict]:
    """低置信度问题，`retrieved` 是检索到的 faq_id 列表，用于配 hard negative。"""
    sql = (
        "SELECT id, query, retrieved FROM low_confidence_queries"
        " ORDER BY id DESC"
    )
    params: tuple = ()
    if limit:
        sql += " LIMIT %s"
        params = (limit,)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    out = []
    for row in rows:
        try:
            retrieved = json.loads(row["retrieved"] or "[]")
        except (TypeError, ValueError):
            retrieved = []
        out.append(
            {
                "query": row["query"],
                "source": "low_conf",
                "low_conf_id": int(row["id"]),
                "retrieved": retrieved,
                "expected_faq_id": None,
            }
        )
    return out


def fetch_r13(path: Path | None = None) -> list[dict]:
    """r13 已标注检索集，直接复用，不重标。"""
    path = path or R13_CSV
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            query = (row.get("query") or "").strip()
            if not query:
                continue
            expected = (row.get("expected_doc_id") or "").strip() or None
            out.append(
                {
                    "query": query,
                    "source": "r13",
                    "low_conf_id": None,
                    "retrieved": [],
                    "expected_faq_id": expected,
                }
            )
    return out


def fetch_traces(limit: int | None = None) -> list[dict]:
    """轨迹里的原话与改写对，增强阶段的免费样本源。"""
    sql = (
        "SELECT query, rewritten_query FROM agent_traces"
        " WHERE rewritten_query IS NOT NULL AND rewritten_query <> ''"
        " ORDER BY id DESC"
    )
    params: tuple = ()
    if limit:
        sql += " LIMIT %s"
        params = (limit,)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [
        {"query": row["query"], "rewritten_query": row["rewritten_query"]}
        for row in rows
    ]


def build_candidates(rows: list[dict], top_k: int = 5) -> list[dict]:
    """给每条待标问题算候选条款，供标注页直接展示。

    用 hybrid（向量 + BM25 经 RRF 融合），不用 hybrid_rerank：候选只是给
    人看的参考，多调一次精排 API 换不来更好的参考质量，却要按条计费。
    这一步是离线的，算完落盘，标注页只读缓存——避免每次翻页都重新嵌入。
    """
    from app.retrieval.retrieval import retrieve

    out: list[dict] = []
    for row in rows:
        try:
            hits = retrieve(row["query"], strategy="hybrid", top_k=top_k)
        except Exception as exc:  # noqa: BLE001 - 检索不可用不该中断整批
            print(f"候选生成失败（{row['query'][:20]}）：{exc}", file=sys.stderr)
            hits = []
        out.append(
            {
                **row,
                "candidates": [
                    {
                        "faq_id": hit["id"],
                        "title": hit["title"],
                        "category": hit["category"],
                        "section": hit["section"],
                        "content": hit["content"],
                        "score": hit["score"],
                    }
                    for hit in hits
                ],
            }
        )
    return out


def _read_candidates(path: Path | None = None) -> list[dict]:
    path = path or (OUT_DIR / "candidates.jsonl")
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_labeled(candidates_path: Path | None = None) -> list[dict]:
    """把人工标注结论拼成可切分的行。

    `missing` 与 `noise` 没有金标条款，category 记 `unknown`：它们不参与
    正样本，但仍要参与分层统计——否则缺口报告会漏掉「哪类问题压根没条款」。
    """
    from app.core import store
    from app.repo import finetune as finetune_repo

    labels = finetune_repo.list_labels(limit=100000)
    faq = {row["id"]: row for row in store.list_faq(None)}
    cand_map = {
        finetune_repo.query_hash(row["query"]): row.get("candidates") or []
        for row in _read_candidates(candidates_path)
    }

    out: list[dict] = []
    for row in labels:
        gold = faq.get(row["faq_id"]) if row["faq_id"] else None
        out.append(
            {
                "query": row["query"],
                "label": row["label"],
                "faq_id": row["faq_id"],
                "gold": gold,
                "category": gold["category"] if gold else "unknown",
                "candidates": cand_map.get(finetune_repo.query_hash(row["query"])) or [],
                "low_conf_id": row["low_conf_id"],
            }
        )
    return out


def split(
    rows: list[dict],
    *,
    seed: int = 20261104,
    dev_ratio: float = 0.15,
    test_ratio: float = 0.15,
) -> dict[str, list[dict]]:
    """按 category × label 分层切分。

    以 query 为单位切，不按 pair 切：同一句话的正例与它的 hard negative
    必须落在同一侧，否则 dev 里的负例其实是 train 见过的文档，指标虚高。
    层内样本少于 3 条时全部进 train——1 条样本的 dev 只能产出抖动的指标。
    """
    rng = random.Random(seed)
    strata: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        strata.setdefault((row["category"], row["label"]), []).append(row)

    out = {"train": [], "dev": [], "test": []}
    for key in sorted(strata):
        bucket = list(strata[key])
        rng.shuffle(bucket)
        if len(bucket) < 3:
            out["train"].extend(bucket)
            continue
        n_dev = max(1, int(round(len(bucket) * dev_ratio)))
        n_test = max(1, int(round(len(bucket) * test_ratio)))
        if len(bucket) - n_dev - n_test < 1:
            out["train"].extend(bucket)
            continue
        out["dev"].extend(bucket[:n_dev])
        out["test"].extend(bucket[n_dev : n_dev + n_test])
        out["train"].extend(bucket[n_dev + n_test :])
    return out


def _doc_text(faq: dict) -> str:
    """交叉编码器的文档侧输入：标题 + 正文。

    带标题是有意的：条款标题往往含「退货条件」这类类别词，模型能把
    「问的是流程还是条件」区分开，正文本身经常没有这类提示。
    """
    return f"{faq['title']}：{faq['content']}"


def _synonym_variants(query: str) -> list[str]:
    """规则同义词改写，只取第一个命中的词，避免组合爆炸。"""
    for word, alts in _SYNONYMS.items():
        if word in query:
            return [query.replace(word, alt, 1) for alt in alts]
    return []


def augment(
    train_rows: list[dict],
    traces: list[dict] | None = None,
) -> tuple[list[dict], dict]:
    """训练集增强。dev/test 永不经过这里。

    四路来源各有分工：轨迹改写是免费的真实口语样本；规则同义词兜住常见
    换说法；hard negative 来自同批候选里分数最高但不是金标的条款——随机
    负例太容易，模型学不到「为什么这条不是答案」。
    """
    from app.repo.finetune import normalize_query

    trace_map: dict[str, str] = {}
    for trace in traces or []:
        key = normalize_query(trace.get("message") or trace.get("query") or "")
        rewritten = (
            trace.get("resolved_query") or trace.get("rewritten_query") or ""
        ).strip()
        if key and rewritten and normalize_query(rewritten) != key:
            trace_map.setdefault(key, rewritten)

    pairs: list[dict] = []
    stats = {"positive": 0, "trace": 0, "synonym": 0, "hard_neg": 0, "noise_neg": 0}

    for row in train_rows:
        gold = row["gold"]
        label = row["label"]

        if label == "doc" and gold is not None:
            origin = normalize_query(row["query"])
            pairs.append(
                {
                    "query": row["query"],
                    "doc": _doc_text(gold),
                    "label": 1,
                    "origin_id": origin,
                    "faq_id": row["faq_id"],
                    "source": "human",
                }
            )
            stats["positive"] += 1

            rewritten = trace_map.get(normalize_query(row["query"]))
            if rewritten:
                pairs.append(
                    {
                        "query": rewritten,
                        "doc": _doc_text(gold),
                        "label": 1,
                        "origin_id": origin,
                        "faq_id": row["faq_id"],
                        "source": "trace_rewrite",
                    }
                )
                stats["trace"] += 1

            for variant in _synonym_variants(row["query"]):
                if normalize_query(variant) == normalize_query(row["query"]):
                    continue
                pairs.append(
                    {
                        "query": variant,
                        "doc": _doc_text(gold),
                        "label": 1,
                        "origin_id": origin,
                        "faq_id": row["faq_id"],
                        "source": "synonym",
                    }
                )
                stats["synonym"] += 1

            negatives = [
                cand
                for cand in row["candidates"]
                if cand["faq_id"] != row["faq_id"]
            ][:2]
            for cand in negatives:
                pairs.append(
                    {
                        "query": row["query"],
                        "doc": f"{cand['title']}：{cand['content']}",
                        "label": 0,
                        "origin_id": origin,
                        "faq_id": cand["faq_id"],
                        "source": "hard_negative",
                    }
                )
                stats["hard_neg"] += 1

        elif label == "noise":
            # 噪声问题配上政策条款做负例，教模型认「这句问的根本不是政策」
            for cand in row["candidates"][:1]:
                pairs.append(
                    {
                        "query": row["query"],
                        "doc": f"{cand['title']}：{cand['content']}",
                        "label": 0,
                        "origin_id": normalize_query(row["query"]),
                        "faq_id": cand["faq_id"],
                        "source": "noise_negative",
                    }
                )
                stats["noise_neg"] += 1

    return pairs, stats


def build_pairs(splits: dict[str, list[dict]], traces: list[dict] | None = None) -> dict:
    """把三分集行展开成训练对。dev/test 只保留人工正例，不掺任何增强。"""
    from app.repo.finetune import normalize_query

    train_pairs, aug_stats = augment(splits["train"], traces)

    plain: dict[str, list[dict]] = {}
    for name in ("dev", "test"):
        rows = []
        for row in splits[name]:
            if row["label"] == "doc" and row["gold"] is not None:
                rows.append(
                    {
                        "query": row["query"],
                        "doc": _doc_text(row["gold"]),
                        "label": 1,
                        "origin_id": normalize_query(row["query"]),
                        "faq_id": row["faq_id"],
                        "source": "human",
                    }
                )
        plain[name] = rows

    return {
        "train": train_pairs,
        "dev": plain["dev"],
        "test": plain["test"],
        "augment": aug_stats,
    }


def export_jsonl(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def build_dataset(
    *,
    seed: int = 20261104,
    candidates_path: Path | None = None,
    out_dir: Path | None = None,
) -> dict:
    """一次完整构建：读标注 → 分层 → 增强 → 落三份 jsonl + 统计。

    返回的 stats 直接给页面渲染漏斗与分层分布，不在前端二次统计。
    """
    from app.core import store
    from app.repo import finetune as finetune_repo

    out_dir = out_dir or OUT_DIR
    labeled = load_labeled(candidates_path)
    if not labeled:
        return {
            "ok": False,
            "reason": "no_labels",
            "message": "还没有任何人工标注，先在标注页标几条",
        }

    splits = split(labeled, seed=seed)
    traces = store.list_traces(limit=1000)
    built = build_pairs(splits, traces)

    names = ("train", "dev", "test")
    for name in names:
        export_jsonl(built[name], out_dir / f"{name}.jsonl")
    (out_dir / "split.json").write_text(
        json.dumps(
            {
                "seed": seed,
                "sizes": {name: len(splits[name]) for name in names},
                "pairs": {name: len(built[name]) for name in names},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    by_category: dict[str, dict[str, int]] = {}
    for row in labeled:
        bucket = by_category.setdefault(row["category"], {})
        bucket[row["label"]] = bucket.get(row["label"], 0) + 1

    label_counts = {name: 0 for name in finetune_repo.LABELS}
    for row in labeled:
        label_counts[row["label"]] += 1

    stats = {
        "labeled": len(labeled),
        "labels": label_counts,
        "positive": label_counts.get("doc", 0),
        "splits": {name: len(splits[name]) for name in names},
        "pairs": {name: len(built[name]) for name in names},
        "augment": built["augment"],
        "by_category": by_category,
        "gate": "pass" if label_counts.get("doc", 0) >= GATE_MIN_POSITIVE else "blocked",
        "gate_min_positive": GATE_MIN_POSITIVE,
    }
    (out_dir / "dataset_stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"ok": True, "stats": stats}


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()

    rows = fetch_low_conf() + fetch_r13()
    cleaned, stats = clean(rows)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    clean_path = OUT_DIR / "clean.jsonl"
    with clean_path.open("w", encoding="utf-8") as handle:
        for row in cleaned:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    (OUT_DIR / "stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"读入 {len(rows)} 条，保留 {len(cleaned)} 条 → {clean_path}")
    for key in STAT_KEYS:
        print(f"  {key}: {stats[key]}")

    with_candidates = build_candidates(cleaned, top_k=5)
    cand_path = OUT_DIR / "candidates.jsonl"
    with cand_path.open("w", encoding="utf-8") as handle:
        for row in with_candidates:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    hit = sum(1 for row in with_candidates if row["candidates"])
    print(f"候选生成完成：{hit}/{len(with_candidates)} 条有候选 → {cand_path}")

    result = build_dataset()
    if not result["ok"]:
        print(f"数据集未构建：{result['message']}")
        return
    stats_out = result["stats"]
    print(
        f"数据集：标注 {stats_out['labeled']} 条，正样本 {stats_out['positive']} 条，"
        f"train/dev/test = {stats_out['splits']['train']}/"
        f"{stats_out['splits']['dev']}/{stats_out['splits']['test']}"
    )
    print(f"训练对：{stats_out['pairs']['train']} 条，增强 {stats_out['augment']}")
    print(f"发布门禁：{stats_out['gate']}（需正样本 ≥ {stats_out['gate_min_positive']}）")


if __name__ == "__main__":
    main()