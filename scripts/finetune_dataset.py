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


if __name__ == "__main__":
    main()