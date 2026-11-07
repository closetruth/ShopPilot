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
    """r13 已标注检索集，直接复用，不重标。

    `candidates_raw` 是 CSV 里的原始候选串（`P-XXX:0.64 | P-YYY:0.61`），
    带出来是为了在训练集里配 hard negative：r13 的 473 条金标是正样本
    主力，没有候选就只剩随机负例，模型学不到「相近但不正确」的边界。
    """
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
                    "candidates_raw": (row.get("candidates") or "").strip(),
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


def _candidates_from_retrieved() -> dict[str, list[dict]]:
    """把库里已有的 `retrieved` 还原成候选条款，按 query hash 索引。

    `low_confidence_queries.retrieved` 是线上检索当时真实返回的那几条。
    用它当 hard negative 比重新跑一次检索更贴近线上：精排要面对的正是
    「同主题、但确实答不上」的干扰项，而随机抽的条款往往主题都不同，
    模型只要学会「主题不匹配就判 0」就能把训练集做满分，一到线上遇到
    同主题干扰就全部给高分——这一轮实测里 abstain 全部不拒就是这个现象。

    走这条路还有个工程上的好处：它已经在 MySQL 里，不需要 embedding、
    不需要 `docker cp candidates.jsonl`。原先候选是本地算好再拷进容器的
    一次性产物，忘了同步就让 hard_neg 归零，而归零不会报错，只会让指标
    看起来漂亮、线上分不开闸——这正是要消掉的那类失败模式。
    """
    from app.core import store
    from app.repo import finetune as finetune_repo

    faq = {row["id"]: row for row in store.list_faq(None)}
    out: dict[str, list[dict]] = {}
    for row in fetch_low_conf():
        cands = []
        for faq_id in row["retrieved"]:
            item = faq.get(faq_id)
            # 停用条款线上检索拿不到，拿它当负例等于教模型区分一个线上
            # 永远不会出现的输入
            if not item or not item.get("enabled"):
                continue
            cands.append(
                {
                    "faq_id": item["id"],
                    "title": item.get("title") or "",
                    "category": item.get("category") or "",
                    "section": item.get("section") or "",
                    "content": item.get("content") or "",
                    # 检索分保留为空：它是旧一次检索的打分，与现在这次训练
                    # 无关，填进去只会让人误以为能拿来排序
                    "score": None,
                }
            )
        if cands:
            out[finetune_repo.query_hash(row["query"])] = cands
    return out


def _r13_gold(raw: str | None, faq: dict) -> str | None:
    """r13 的 `expected_doc_id` 可能是 `A|B`，表示两个答案都算对。

    训练只能给一个金标，取第一个仍启用的条款；被丢掉的那些在候选里仍
    存在，可以作为负例来源。
    """
    for part in (raw or "").split("|"):
        faq_id = part.strip()
        item = faq.get(faq_id)
        if item and item.get("enabled"):
            return faq_id
    return None


def _r13_candidates(raw: str | None, faq: dict) -> list[dict]:
    """解析 r13 CSV 的 `P-XXX:0.64 | P-YYY:0.61` 候选串。

    分数留空，理由同 `_candidates_from_retrieved`：那是旧一次检索的打分，
    与本次训练无关，填进去会让人误以为能拿来排序。
    """
    out: list[dict] = []
    for part in (raw or "").split("|"):
        faq_id = part.split(":")[0].strip()
        item = faq.get(faq_id)
        if not item or not item.get("enabled"):
            continue
        out.append(
            {
                "faq_id": item["id"],
                "title": item.get("title") or "",
                "category": item.get("category") or "",
                "section": item.get("section") or "",
                "content": item.get("content") or "",
                "score": None,
            }
        )
    return out


def load_labeled(candidates_path: Path | None = None) -> list[dict]:
    """把人工标注结论拼成可切分的行。

    `missing` 与 `noise` 没有金标条款，category 记 `unknown`：它们不参与
    正样本，但仍要参与分层统计——否则缺口报告会漏掉「哪类问题压根没条款」。

    人工标注之后追加 r13 的 473 条金标。两份数据是互补的：r13 是离线抽样
    的书面问法，人工标注是线上真实低置信度问题，后者更贴近线上分布，所以
    同一句话两边都有时以人工结论为准。
    """
    from app.core import store
    from app.repo import finetune as finetune_repo

    labels = finetune_repo.list_labels(limit=100000)
    faq = {row["id"]: row for row in store.list_faq(None)}
    cand_map = {
        finetune_repo.query_hash(row["query"]): row.get("candidates") or []
        for row in _read_candidates(candidates_path)
    }
    # 快照文件优先，缺了就从库里 `retrieved` 还原。两者都不存在时
    # candidates 为空，hard_neg 会归零——所以宁可多查一次 MySQL。
    retrieved_map = _candidates_from_retrieved()

    out: list[dict] = []
    seen: set[str] = set()
    for row in labels:
        gold = faq.get(row["faq_id"]) if row["faq_id"] else None
        key = finetune_repo.query_hash(row["query"])
        seen.add(key)
        out.append(
            {
                "query": row["query"],
                "label": row["label"],
                "faq_id": row["faq_id"],
                "gold": gold,
                "category": gold["category"] if gold else "unknown",
                "candidates": cand_map.get(key) or retrieved_map.get(key) or [],
                "low_conf_id": row["low_conf_id"],
                "origin": "human",
            }
        )

    for row in fetch_r13():
        key = finetune_repo.query_hash(row["query"])
        if key in seen:
            continue
        gold_id = _r13_gold(row["expected_faq_id"], faq)
        if not gold_id:
            continue
        gold = faq[gold_id]
        seen.add(key)
        out.append(
            {
                "query": row["query"],
                "label": "doc",
                "faq_id": gold_id,
                "gold": gold,
                "category": gold["category"],
                "candidates": _r13_candidates(row["candidates_raw"], faq)
                or retrieved_map.get(key)
                or [],
                "low_conf_id": None,
                "origin": "r13",
            }
        )
    return out


def load_faq_pool() -> list[dict]:
    """全部条款的文档侧文本，供负例抽样与全池评估。

    key 用 `faq_documents.id`，与 `finetune_labels.faq_id` 是同一空间。
    两处标识不一致会让金标被当成负例喂进训练，那是最难发现的一类错误：
    指标不会崩，只是学不出来。

    只取 `enabled = 1`：线上检索根本拿不到停用条款，把它放进评估池会让
    排名变好看但线上不成立。`doc_id` 一并带出，抽样负例时要排除金标所在
    的整篇文档——同篇的其他小节往往部分相关，标成 0 会教模型反向学习。
    """
    from app.core import store

    return [
        {
            "faq_id": row["id"],
            "doc_id": row["doc_id"],
            "doc": _doc_text(row),
            "category": row["category"],
        }
        for row in store.list_faq(None)
        if row.get("enabled")
    ]


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
    *,
    pool: list[dict] | None = None,
    seed: int = 20261104,
    negatives_per_positive: int = 2,
) -> tuple[list[dict], dict]:
    """训练集增强。dev/test 永不经过这里。

    四路来源各有分工：轨迹改写是免费的真实口语样本；规则同义词兜住常见
    换说法；hard negative 来自同批候选里分数最高但不是金标的条款——随机
    负例太容易，模型学不到「为什么这条不是答案」；候选快照缺失时用全池
    随机负例兜底。

    兜底这一路不是可选项：没有负例的训练集只有正样本，BCE 的最优解是
    给所有输入打高分——模型学不到任何区分信号，而 loss 会漂亮地下降。
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

    rng = random.Random(seed)
    doc_by_id = {row["faq_id"]: row["doc"] for row in (pool or [])}
    doc_owner = {row["faq_id"]: row.get("doc_id") for row in (pool or [])}

    def sample_negatives(exclude: set, k: int, exclude_docs: set | None = None) -> list:
        """从全池抽 k 条不在 exclude 里的条款，确定性（seed 固定）。

        `exclude_docs` 排除金标所在的整篇文档：同一篇的其他小节往往部分
        相关，把它标成 0 等于教模型「相关的也要判不相关」。
        """
        if k <= 0:
            return []
        exclude_docs = exclude_docs or set()
        others = [
            faq_id
            for faq_id in doc_by_id
            if faq_id not in exclude
            and doc_owner.get(faq_id) not in exclude_docs
        ]
        if not others:
            return []
        return rng.sample(others, min(k, len(others)))

    pairs: list[dict] = []
    stats = {
        "positive": 0,
        "trace": 0,
        "synonym": 0,
        "hard_neg": 0,
        "random_neg": 0,
        "noise_neg": 0,
        "missing_neg": 0,
    }

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
                    "source": row.get("origin", "human"),
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

            # 候选里同篇文档的其他小节直接丢掉：它们是同一份政策的另一段，
            # 标成 0 会教模型压掉「近义但正确」的条款，线上表现是该答的答
            # 不上——比召回不到更难查。空出来的槽位由后面的随机负例补，
            # 那条路径本来就排除了金标所在的整篇。
            gold_doc = (gold or {}).get("doc_id")
            negatives = [
                cand
                for cand in row["candidates"]
                if cand["faq_id"] != row["faq_id"]
                and doc_owner.get(cand["faq_id"]) != gold_doc
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

            # 候选快照缺失时 hard_neg 为 0，这里补随机负例，保证每条正例
            # 至少有一个负例可学。金标与已用过的候选都排除，避免把答案
            # 标成不相关。
            taken = {row["faq_id"]} | {cand["faq_id"] for cand in negatives}
            gold_docs = {(gold or {}).get("doc_id")}
            for neg_id in sample_negatives(
                taken, negatives_per_positive - len(negatives), gold_docs
            ):
                pairs.append(
                    {
                        "query": row["query"],
                        "doc": doc_by_id[neg_id],
                        "label": 0,
                        "origin_id": origin,
                        "faq_id": neg_id,
                        "source": "random_negative",
                    }
                )
                stats["random_neg"] += 1

        elif label in ("noise", "missing"):
            # noise：这句话问的根本不是政策；missing：看着像政策问题，但库里
            # 确实没有条款能答。对精排而言两者是同一种输入——「给定任何条款
            # 都该判 0」。
            #
            # missing 原先完全没进训练，后果在 dev 尺上一眼可见：模型对没有
            # 答案的问题照样打 0.9 分，和正例的金标分几乎重合，阈值无论取
            # 多少都分不开「该答」和「该拒」。闸的负例不能只靠 noise 撑，
            # 它俩的措辞分布差太远。
            is_missing = label == "missing"
            k = negatives_per_positive if is_missing else 1
            picks = row["candidates"][:k]
            if not picks:
                # 同样兜底：没有候选快照时这类标注一条都进不了训练
                picks = [
                    {"faq_id": neg_id, "title": "", "content": doc_by_id[neg_id]}
                    for neg_id in sample_negatives(set(), k)
                ]
            for cand in picks:
                doc = (
                    f"{cand['title']}：{cand['content']}"
                    if cand["title"]
                    else cand["content"]
                )
                pairs.append(
                    {
                        "query": row["query"],
                        "doc": doc,
                        "label": 0,
                        "origin_id": normalize_query(row["query"]),
                        "faq_id": cand["faq_id"],
                        "source": (
                            "missing_negative" if is_missing else "noise_negative"
                        ),
                    }
                )
                stats["missing_neg" if is_missing else "noise_neg"] += 1

    return pairs, stats


def build_pairs(
    splits: dict[str, list[dict]],
    traces: list[dict] | None = None,
    *,
    pool: list[dict] | None = None,
    seed: int = 20261104,
) -> dict:
    """把三分集行展开成训练对。dev/test 只保留人工正例，不掺任何增强。"""
    from app.repo.finetune import normalize_query

    train_pairs, aug_stats = augment(
        splits["train"], traces, pool=pool, seed=seed
    )

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
            elif row["label"] in ("missing", "noise"):
                # 这两类是「本来就不该答」的人工标注，必须留在 dev/test。
                # 原先这里直接丢掉，导致 dev/test 只剩 4 条正例：Hit@1 的
                # 最小刻度是 0.25，MRR 早停在三轮里三次一模一样，等于用
                # 噪声挑轮次。它们同时也是阈值标定唯一真实的负样本——
                # 正例只能给出「最强的错误对手」，给不出「该不该开口」。
                rows.append(
                    {
                        "query": row["query"],
                        "doc": "",
                        "label": 0,
                        "origin_id": normalize_query(row["query"]),
                        "faq_id": None,
                        "source": "human",
                        "expect": "abstain",
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
        # 两个来源都可能是空的，报错必须说清是哪个，否则会把人引到标注页
        # 上去——而缺的其实是 r13 那份 CSV（容器里没拷进镜像时就是这样）
        if not R13_CSV.exists():
            return {
                "ok": False,
                "reason": "no_r13",
                "message": f"找不到 r13 标注集：{R13_CSV}；它提供全部正样本",
            }
        return {
            "ok": False,
            "reason": "no_labels",
            "message": "还没有任何人工标注，先在标注页标几条",
        }

    splits = split(labeled, seed=seed)
    traces = store.list_traces(limit=1000)
    pool = load_faq_pool()
    built = build_pairs(splits, traces, pool=pool, seed=seed)

    names = ("train", "dev", "test")
    for name in names:
        export_jsonl(built[name], out_dir / f"{name}.jsonl")

    # 全池快照：评估时金标要跟全部条款竞争第一名。没有它，评估只能在
    # dev 自己的 3 条正例里排序，那算出来的 MRR 与随机排序无法区分。
    export_jsonl(pool, out_dir / "faq_pool.jsonl")
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
        "pool": len(pool),
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